#!/usr/bin/env python3
from __future__ import annotations

import argparse
import heapq
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import duckdb

from tlm.low_r_regime_basket import (
    LowRRegimeBasketConfig,
    RegimeEdge,
    _build_result,
    _json_default,
    _open_trade,
    _simulate_trade_exit,
)


FULL_YEARS = tuple(range(2019, 2026))
PARTIAL_YEARS = (2026,)
POINT_VALUE = 20.0
ROUND_TRIP_COST_USD = 15.0


@dataclass(frozen=True)
class CandidateStats:
    candidate_id: str
    group_level: str
    scan_type: str
    direction_label: str
    session_bucket: str
    dow: int | None
    trend_bin: int | None
    volume_bin: int | None
    range_bin: int | None
    total_trades: int
    fixed_net_pnl: float
    fixed_avg_pnl: float
    fixed_profit_factor: float | None
    fixed_positive_years: int
    fixed_worst_year_pnl: float
    fixed_min_full_year_trades: int


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Search expanded high-edge NQ 1m strategies with low-R exits and annual trade floor."
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--date-from", default="2019-01-01")
    parser.add_argument("--date-to", default="2026-04-27")
    parser.add_argument("--min-full-year-trades", type=int, default=1000)
    parser.add_argument("--max-preselect", type=int, default=450)
    parser.add_argument("--max-results", type=int, default=120)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/profit_mining/expanded_high_edge_strategy_search_2019_2026.json"),
    )
    args = parser.parse_args()

    config = LowRRegimeBasketConfig(
        data_root=args.data_root,
        symbol=args.symbol,
        date_from=args.date_from,
        date_to=args.date_to,
        preset="top_net_2019_low_r",
        flatten_on_date_change=True,
    )
    pattern = str(Path(args.data_root) / "bars" / "1m" / args.symbol / "date=*" / "part-000.parquet")

    con = duckdb.connect(":memory:")
    try:
        print("building expanded feature table", flush=True)
        build_feature_table(con, pattern, args.date_from, args.date_to)
        bars = load_bars(con)
        coverage_days = coverage_days_by_year(bars)
        print("building expanded signal table", flush=True)
        build_signal_table(con)
        print("loading fixed-horizon high-edge preselection", flush=True)
        candidate_stats = load_candidate_stats(con)
        selected_stats = select_candidate_stats(candidate_stats, args.max_preselect)
        print(f"preselected {len(selected_stats)} of {len(candidate_stats)} groups", flush=True)
        signals_by_id = load_signals_by_candidate(con, [row.candidate_id for row in selected_stats])
        attach_entry_indexes(signals_by_id, bars)
    finally:
        con.close()

    single_results_by_param: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    all_results: list[dict[str, Any]] = []
    for params in low_r_parameter_grid():
        param_key = tuple(sorted(params.items()))
        print(f"evaluating singles {params}", flush=True)
        for stats in selected_stats:
            signals = signals_by_id.get(stats.candidate_id, [])
            if len(signals) < 20:
                continue
            best_for_candidate = None
            for take_profit_r in (0.5, 0.75, 1.0, 1.25, 1.5):
                edge = edge_from_stats(stats, take_profit_r)
                result = replay_result(
                    label=stats.candidate_id,
                    spec={
                        "name": "single_expanded_edge",
                        "candidate_ids": (stats.candidate_id,),
                        "fixed_horizon_preselect": asdict(stats),
                    },
                    bars=bars,
                    signals=signals,
                    edges=(edge,),
                    config=config_from_params(config, {**params, "max_concurrent_positions": 1}),
                    coverage_days=coverage_days,
                    min_full_year_trades=args.min_full_year_trades,
                    params={**params, "max_concurrent_positions": 1},
                )
                result["source_candidate_id"] = stats.candidate_id
                result["source_stats"] = asdict(stats)
                if best_for_candidate is None or single_edge_sort_key(result) > single_edge_sort_key(best_for_candidate):
                    best_for_candidate = result
            if best_for_candidate is not None:
                single_results_by_param[param_key].append(best_for_candidate)

        singles = single_results_by_param[param_key]
        specs = build_high_edge_specs(singles)
        print(f"evaluating {len(specs)} specs for {params}", flush=True)
        single_by_id = {row["source_candidate_id"]: row for row in singles}
        for max_positions in (1, 2, 3, 6, 12, 24, 99):
            combo_config = config_from_params(config, {**params, "max_concurrent_positions": max_positions})
            for spec in specs:
                selected = [single_by_id[candidate_id] for candidate_id in spec["candidate_ids"] if candidate_id in single_by_id]
                if len(selected) != len(spec["candidate_ids"]):
                    continue
                edges = tuple(row["edges"][0] for row in selected)
                selected_edges = tuple(RegimeEdge(**edge) for edge in edges)
                combo_spec = {**spec, "edge_candidate_ids": tuple(row["source_candidate_id"] for row in selected)}
                selected_signals = combine_signals(
                    [signals_by_id[row["source_candidate_id"]] for row in selected],
                    edge_order={row["source_candidate_id"]: index for index, row in enumerate(selected)},
                )
                all_results.append(
                    replay_result(
                        label=combo_spec["name"],
                        spec=combo_spec,
                        bars=bars,
                        signals=selected_signals,
                        edges=selected_edges,
                        config=combo_config,
                        coverage_days=coverage_days,
                        min_full_year_trades=args.min_full_year_trades,
                        params={**params, "max_concurrent_positions": max_positions},
                    )
                )

    all_results.sort(key=sort_key, reverse=True)
    qualified = [row for row in all_results if row["constraints"]["trade_floor_pass"] and row["metrics"]["net_pnl"] > 0]
    all_year_positive = [
        row for row in qualified if row["constraints"]["positive_years"] == len(FULL_YEARS) + len(PARTIAL_YEARS)
    ]
    stable = sorted(qualified, key=stable_sort_key, reverse=True)
    payload = {
        "schema_version": 1,
        "artifact": "expanded_high_edge_strategy_search",
        "date_from": args.date_from,
        "date_to": args.date_to,
        "constraints": {
            "full_years": list(FULL_YEARS),
            "partial_years": list(PARTIAL_YEARS),
            "min_full_year_trades": args.min_full_year_trades,
            "partial_years_use_annualized_trade_count": True,
            "high_edge_filter": "combo specs are built only from low-R single edges with positive net, positive average trade, and PF/average thresholds.",
        },
        "search_space": {
            "signal_families": signal_family_names(),
            "group_levels": group_level_names(),
            "preselected_group_count": len(selected_stats),
            "candidate_group_count": len(candidate_stats),
            "candidate_count": len(all_results),
            "qualified_positive_count": len(qualified),
        },
        "best_net": qualified[0] if qualified else (all_results[0] if all_results else None),
        "best_all_year_positive_net": all_year_positive[0] if all_year_positive else None,
        "best_stable": stable[0] if stable else (qualified[0] if qualified else None),
        "top_results": all_results[: args.max_results],
        "top_single_edges": sorted(
            [row for rows in single_results_by_param.values() for row in rows],
            key=single_edge_sort_key,
            reverse=True,
        )[: args.max_results],
        "notes": [
            "This search expands implementation families using ORB, VWAP reclaim/bounce, volume impulse, absorption/reversion, and session extreme ideas.",
            "Low-margin filler signals are excluded before combination; only independently positive high-edge single rules can enter specs.",
            "Concurrency is selected by exact replay over 1, 2, 3, 6, 12, 24, and 99 max open positions.",
            "2026 is incomplete, so its trade floor uses annualized count.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")
    print(args.output)
    for key in ("best_net", "best_all_year_positive_net", "best_stable"):
        row = payload.get(key)
        if not row:
            continue
        metrics = row["metrics"]
        print(
            key,
            row["label"],
            "net=",
            round(metrics["net_pnl"], 2),
            "trades=",
            metrics["trade_count"],
            "pf=",
            round(metrics["profit_factor"], 3) if metrics.get("profit_factor") is not None else None,
            "dd=",
            round(metrics["max_drawdown"], 2),
            "npdd=",
            round(metrics.get("net_pnl_to_max_drawdown") or 0.0, 2),
            "avg_hold=",
            round(row["avg_hold_bars"], 2),
            "max_pos=",
            row["params"]["max_concurrent_positions"],
            "edges=",
            row["edge_count"],
            "positive_years=",
            row["constraints"]["positive_years"],
        )
    return 0


def build_feature_table(
    con: duckdb.DuckDBPyConnection,
    pattern: str,
    date_from: str,
    date_to: str,
) -> None:
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE feature_base AS
        WITH raw AS (
          SELECT timestamp, open, high, low, close, tick_count,
                 (timestamp AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York') AS ny_ts,
                 CAST(timestamp AS DATE) AS utc_date
          FROM read_parquet(?, union_by_name=true)
          WHERE timestamp >= CAST(? AS TIMESTAMP)
            AND timestamp < CAST(? AS TIMESTAMP) + INTERVAL 1 DAY
        ), returns AS (
          SELECT *,
                 CAST(ny_ts AS DATE) AS ny_date,
                 CAST(strftime(ny_ts, '%w') AS INTEGER) AS dow,
                 CAST(strftime(ny_ts, '%H') AS INTEGER) * 60 + CAST(strftime(ny_ts, '%M') AS INTEGER) AS ny_min,
                 close - lag(close, 1) OVER (ORDER BY timestamp) AS ret1,
                 close - lag(close, 3) OVER (ORDER BY timestamp) AS ret3,
                 close - lag(close, 5) OVER (ORDER BY timestamp) AS ret5,
                 close - lag(close, 15) OVER (ORDER BY timestamp) AS ret15,
                 lag(close, 1) OVER (ORDER BY timestamp) AS prev_close,
                 lag(high, 1) OVER (ORDER BY timestamp) AS prev_high,
                 lag(low, 1) OVER (ORDER BY timestamp) AS prev_low,
                 lag(open, 1) OVER (ORDER BY timestamp) AS prev_open
          FROM raw
        ), indicators AS (
          SELECT *,
                 avg(close) OVER (ORDER BY timestamp ROWS BETWEEN 9 PRECEDING AND 1 PRECEDING) AS ma9,
                 avg(close) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS ma20,
                 avg(close) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS ma50,
                 avg(close) OVER (ORDER BY timestamp ROWS BETWEEN 200 PRECEDING AND 1 PRECEDING) AS ma200,
                 stddev_pop(close) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS close_std20,
                 stddev_pop(close) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS close_std50,
                 avg(tick_count) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS vol50,
                 avg(high - low) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS range20,
                 avg(high - low) OVER (ORDER BY timestamp ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING) AS range60,
                 avg(greatest(ret1, 0)) OVER (ORDER BY timestamp ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING) AS avg_gain14,
                 avg(greatest(-ret1, 0)) OVER (ORDER BY timestamp ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING) AS avg_loss14,
                 max(high) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS high20_prev,
                 min(low) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS low20_prev,
                 max(high) OVER (ORDER BY timestamp ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING) AS high60_prev,
                 min(low) OVER (ORDER BY timestamp ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING) AS low60_prev,
                 lead(open, 1) OVER (ORDER BY timestamp) AS entry_open,
                 lead(timestamp, 1) OVER (ORDER BY timestamp) AS entry_time,
                 lead(close, 16) OVER (ORDER BY timestamp) AS fixed_exit_close,
                 lead(timestamp, 16) OVER (ORDER BY timestamp) AS fixed_exit_time
          FROM returns
        ), daily_levels AS (
          SELECT *,
                 max(high) OVER (
                   PARTITION BY ny_date ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                 ) AS session_high_prev,
                 min(low) OVER (
                   PARTITION BY ny_date ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                 ) AS session_low_prev,
                 first_value(open) OVER (
                   PARTITION BY ny_date ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                 ) AS session_open,
                 sum(((high + low + close) / 3.0) * greatest(tick_count, 1)) OVER (
                   PARTITION BY ny_date ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                 ) / NULLIF(sum(greatest(tick_count, 1)) OVER (
                   PARTITION BY ny_date ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                 ), 0) AS session_vwap,
                 max(CASE WHEN ny_min BETWEEN 570 AND 599 THEN high END) OVER (
                   PARTITION BY ny_date ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                 ) AS opening_high_prev,
                 min(CASE WHEN ny_min BETWEEN 570 AND 599 THEN low END) OVER (
                   PARTITION BY ny_date ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                 ) AS opening_low_prev
          FROM indicators
        ), prior_days AS (
          SELECT ny_date,
                 lag(max(high)) OVER (ORDER BY ny_date) AS prior_day_high,
                 lag(min(low)) OVER (ORDER BY ny_date) AS prior_day_low,
                 lag(max(close) FILTER (WHERE ny_min <= 1019)) OVER (ORDER BY ny_date) AS prior_day_close
          FROM daily_levels
          GROUP BY ny_date
        ), final AS (
          SELECT d.*,
                 p.prior_day_high,
                 p.prior_day_low,
                 p.prior_day_close,
                 CASE
                   WHEN ny_min BETWEEN 0 AND 359 THEN 'ny_0000_0559'
                   WHEN ny_min BETWEEN 360 AND 569 THEN 'ny_0600_0929'
                   WHEN ny_min BETWEEN 570 AND 719 THEN 'ny_0930_1159'
                   WHEN ny_min BETWEEN 720 AND 959 THEN 'ny_1200_1559'
                   WHEN ny_min BETWEEN 960 AND 1079 THEN 'ny_1600_1759'
                   ELSE 'ny_1800_2359'
                 END AS session_bucket,
                 CASE WHEN close > ma50 THEN 1 ELSE -1 END AS trend_bin,
                 CASE WHEN close > ma200 THEN 1 ELSE -1 END AS major_trend_bin,
                 CASE
                   WHEN tick_count >= vol50 * 2.0 THEN 3
                   WHEN tick_count >= vol50 * 1.5 THEN 2
                   WHEN tick_count >= vol50 THEN 1
                   WHEN tick_count < vol50 * 0.7 THEN -1
                   ELSE 0
                 END AS volume_bin,
                 CASE
                   WHEN (high - low) >= range20 * 2.0 THEN 3
                   WHEN (high - low) >= range20 * 1.5 THEN 2
                   WHEN (high - low) >= range20 THEN 1
                   WHEN (high - low) < range20 * 0.7 THEN -1
                   ELSE 0
                 END AS range_bin,
                 CASE WHEN close_std20 > 0 THEN (close - ma20) / close_std20 ELSE 0 END AS z20,
                 CASE WHEN close_std50 > 0 THEN (close - ma50) / close_std50 ELSE 0 END AS z50,
                 CASE WHEN vol50 > 0 THEN tick_count / vol50 ELSE 1 END AS volume_ratio,
                 CASE WHEN range20 > 0 THEN (high - low) / range20 ELSE 1 END AS range_ratio,
                 close - session_vwap AS vwap_dist,
                 lag(close - session_vwap, 1) OVER (ORDER BY timestamp) AS prev_vwap_dist,
                 CASE WHEN high > low THEN (close - open) / (high - low) ELSE 0 END AS body_to_range,
                 CASE WHEN high > low THEN (close - low) / (high - low) ELSE 0.5 END AS close_location,
                 CASE
                   WHEN avg_loss14 > 0 THEN 100.0 - 100.0 / (1.0 + avg_gain14 / avg_loss14)
                   WHEN avg_gain14 > 0 THEN 100.0
                   ELSE 50.0
                 END AS rsi14,
                 CASE
                   WHEN high20_prev > low20_prev THEN (close - low20_prev) / (high20_prev - low20_prev)
                   ELSE 0.5
                 END AS stoch20
          FROM daily_levels d
          LEFT JOIN prior_days p USING (ny_date)
        )
        SELECT *
        FROM final
        WHERE entry_open IS NOT NULL
          AND fixed_exit_close IS NOT NULL
          AND ma9 IS NOT NULL
          AND ma20 IS NOT NULL
          AND ma50 IS NOT NULL
          AND ma200 IS NOT NULL
          AND close_std20 IS NOT NULL
          AND close_std50 IS NOT NULL
          AND vol50 IS NOT NULL
          AND range20 IS NOT NULL
          AND range60 IS NOT NULL
        """,
        [pattern, date_from, date_to],
    )


def build_signal_table(con: duckdb.DuckDBPyConnection) -> None:
    fixed_pnl = (
        f"CASE WHEN direction = 1 "
        f"THEN (fixed_exit_close - entry_open) * {POINT_VALUE} - {ROUND_TRIP_COST_USD} "
        f"ELSE (entry_open - fixed_exit_close) * {POINT_VALUE} - {ROUND_TRIP_COST_USD} END"
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE raw_signals AS
        SELECT timestamp, entry_time, entry_open, range20,
               CAST(strftime(timestamp, '%Y') AS INTEGER) AS year,
               scan_type, direction,
               CASE WHEN direction = 1 THEN 'long' ELSE 'short' END AS direction_label,
               session_bucket, dow, trend_bin, volume_bin, range_bin,
               {fixed_pnl} AS fixed_pnl
        FROM (
          SELECT *, 'opening_range_breakout' AS scan_type, 1 AS direction
          FROM feature_base
          WHERE opening_high_prev IS NOT NULL
            AND ny_min BETWEEN 600 AND 959
            AND close > opening_high_prev
            AND trend_bin = 1
            AND volume_bin >= 0
          UNION ALL
          SELECT *, 'opening_range_breakout', -1
          FROM feature_base
          WHERE opening_low_prev IS NOT NULL
            AND ny_min BETWEEN 600 AND 959
            AND close < opening_low_prev
            AND trend_bin = -1
            AND volume_bin >= 0
          UNION ALL
          SELECT *, 'opening_range_retest_reclaim', 1
          FROM feature_base
          WHERE opening_high_prev IS NOT NULL
            AND ny_min BETWEEN 600 AND 959
            AND close > opening_high_prev
            AND low <= opening_high_prev + range20 * 0.25
            AND close_location >= 0.60
            AND trend_bin = 1
          UNION ALL
          SELECT *, 'opening_range_retest_reclaim', -1
          FROM feature_base
          WHERE opening_low_prev IS NOT NULL
            AND ny_min BETWEEN 600 AND 959
            AND close < opening_low_prev
            AND high >= opening_low_prev - range20 * 0.25
            AND close_location <= 0.40
            AND trend_bin = -1
          UNION ALL
          SELECT *, 'vwap_reclaim_continuation', 1
          FROM feature_base
          WHERE prev_vwap_dist < 0
            AND vwap_dist >= 0
            AND ret1 > 0
            AND trend_bin = 1
            AND volume_bin >= 0
          UNION ALL
          SELECT *, 'vwap_reclaim_continuation', -1
          FROM feature_base
          WHERE prev_vwap_dist > 0
            AND vwap_dist <= 0
            AND ret1 < 0
            AND trend_bin = -1
            AND volume_bin >= 0
          UNION ALL
          SELECT *, 'vwap_pullback_bounce', 1
          FROM feature_base
          WHERE close > session_vwap
            AND low <= session_vwap + range20 * 0.20
            AND close_location >= 0.65
            AND trend_bin = 1
          UNION ALL
          SELECT *, 'vwap_pullback_bounce', -1
          FROM feature_base
          WHERE close < session_vwap
            AND high >= session_vwap - range20 * 0.20
            AND close_location <= 0.35
            AND trend_bin = -1
          UNION ALL
          SELECT *, 'high_volume_impulse_continuation', 1
          FROM feature_base
          WHERE ret3 >= range20 * 0.75
            AND close_location >= 0.65
            AND volume_ratio >= 1.35
            AND trend_bin = 1
          UNION ALL
          SELECT *, 'high_volume_impulse_continuation', -1
          FROM feature_base
          WHERE ret3 <= -range20 * 0.75
            AND close_location <= 0.35
            AND volume_ratio >= 1.35
            AND trend_bin = -1
          UNION ALL
          SELECT *, 'range_expansion_continuation', 1
          FROM feature_base
          WHERE body_to_range >= 0.60
            AND range_bin >= 2
            AND volume_bin >= 0
            AND trend_bin = 1
          UNION ALL
          SELECT *, 'range_expansion_continuation', -1
          FROM feature_base
          WHERE body_to_range <= -0.60
            AND range_bin >= 2
            AND volume_bin >= 0
            AND trend_bin = -1
          UNION ALL
          SELECT *, 'donchian20_breakout', 1
          FROM feature_base
          WHERE close > high20_prev
            AND volume_bin >= 0
            AND trend_bin = 1
          UNION ALL
          SELECT *, 'donchian20_breakout', -1
          FROM feature_base
          WHERE close < low20_prev
            AND volume_bin >= 0
            AND trend_bin = -1
          UNION ALL
          SELECT *, 'prior_day_breakout', 1
          FROM feature_base
          WHERE prior_day_high IS NOT NULL
            AND close > prior_day_high
            AND volume_bin >= 0
            AND trend_bin = 1
          UNION ALL
          SELECT *, 'prior_day_breakout', -1
          FROM feature_base
          WHERE prior_day_low IS NOT NULL
            AND close < prior_day_low
            AND volume_bin >= 0
            AND trend_bin = -1
          UNION ALL
          SELECT *, 'prior_day_rejection', -1
          FROM feature_base
          WHERE prior_day_high IS NOT NULL
            AND high > prior_day_high
            AND close < prior_day_high
            AND z50 >= 0.75
          UNION ALL
          SELECT *, 'prior_day_rejection', 1
          FROM feature_base
          WHERE prior_day_low IS NOT NULL
            AND low < prior_day_low
            AND close > prior_day_low
            AND z50 <= -0.75
          UNION ALL
          SELECT *, 'selling_absorption_reversal', 1
          FROM feature_base
          WHERE volume_ratio >= 1.60
            AND ret5 < 0
            AND close_location >= 0.65
            AND body_to_range >= 0
            AND z50 <= -0.50
          UNION ALL
          SELECT *, 'buying_absorption_reversal', -1
          FROM feature_base
          WHERE volume_ratio >= 1.60
            AND ret5 > 0
            AND close_location <= 0.35
            AND body_to_range <= 0
            AND z50 >= 0.50
          UNION ALL
          SELECT *, 'trend_pullback_reclaim', 1
          FROM feature_base
          WHERE trend_bin = 1
            AND close > ma20
            AND ret5 < 0
            AND ret1 > 0
          UNION ALL
          SELECT *, 'trend_pullback_reclaim', -1
          FROM feature_base
          WHERE trend_bin = -1
            AND close < ma20
            AND ret5 > 0
            AND ret1 < 0
          UNION ALL
          SELECT *, 'low_volume_drift', 1
          FROM feature_base
          WHERE trend_bin = 1
            AND volume_bin = -1
            AND ret1 > 0
          UNION ALL
          SELECT *, 'low_volume_drift', -1
          FROM feature_base
          WHERE trend_bin = -1
            AND volume_bin = -1
            AND ret1 < 0
          UNION ALL
          SELECT *, 'session_extreme_reversion', -1
          FROM feature_base
          WHERE session_high_prev IS NOT NULL
            AND close >= session_high_prev - range20 * 0.10
            AND z50 >= 1.0
            AND volume_bin <= 1
          UNION ALL
          SELECT *, 'session_extreme_reversion', 1
          FROM feature_base
          WHERE session_low_prev IS NOT NULL
            AND close <= session_low_prev + range20 * 0.10
            AND z50 <= -1.0
            AND volume_bin <= 1
          UNION ALL
          SELECT *, 'rsi_extreme_reversal', 1
          FROM feature_base
          WHERE rsi14 <= 28.0
            AND z20 <= -0.75
            AND close_location >= 0.55
            AND volume_bin <= 2
          UNION ALL
          SELECT *, 'rsi_extreme_reversal', -1
          FROM feature_base
          WHERE rsi14 >= 72.0
            AND z20 >= 0.75
            AND close_location <= 0.45
            AND volume_bin <= 2
          UNION ALL
          SELECT *, 'stoch_extreme_reversal', 1
          FROM feature_base
          WHERE stoch20 <= 0.15
            AND close_location >= 0.60
            AND z50 <= -0.35
          UNION ALL
          SELECT *, 'stoch_extreme_reversal', -1
          FROM feature_base
          WHERE stoch20 >= 0.85
            AND close_location <= 0.40
            AND z50 >= 0.35
          UNION ALL
          SELECT *, 'ma_reacceleration', 1
          FROM feature_base
          WHERE ma9 > ma20
            AND ma20 > ma50
            AND prev_close <= ma9
            AND close > ma9
            AND ret1 > 0
            AND volume_bin >= 0
          UNION ALL
          SELECT *, 'ma_reacceleration', -1
          FROM feature_base
          WHERE ma9 < ma20
            AND ma20 < ma50
            AND prev_close >= ma9
            AND close < ma9
            AND ret1 < 0
            AND volume_bin >= 0
          UNION ALL
          SELECT *, 'prior_close_reclaim', 1
          FROM feature_base
          WHERE prior_day_close IS NOT NULL
            AND prev_close < prior_day_close
            AND close >= prior_day_close
            AND close > session_vwap
            AND volume_bin >= 0
          UNION ALL
          SELECT *, 'prior_close_reclaim', -1
          FROM feature_base
          WHERE prior_day_close IS NOT NULL
            AND prev_close > prior_day_close
            AND close <= prior_day_close
            AND close < session_vwap
            AND volume_bin >= 0
          UNION ALL
          SELECT *, 'midday_zscore_reversion', 1
          FROM feature_base
          WHERE ny_min BETWEEN 720 AND 899
            AND z20 <= -1.25
            AND close_location >= 0.60
            AND volume_bin <= 1
          UNION ALL
          SELECT *, 'midday_zscore_reversion', -1
          FROM feature_base
          WHERE ny_min BETWEEN 720 AND 899
            AND z20 >= 1.25
            AND close_location <= 0.40
            AND volume_bin <= 1
          UNION ALL
          SELECT *, 'closing_drive_continuation', 1
          FROM feature_base
          WHERE ny_min BETWEEN 900 AND 1019
            AND close > session_vwap
            AND ret15 > range60 * 0.50
            AND close_location >= 0.60
            AND volume_bin >= 0
          UNION ALL
          SELECT *, 'closing_drive_continuation', -1
          FROM feature_base
          WHERE ny_min BETWEEN 900 AND 1019
            AND close < session_vwap
            AND ret15 < -range60 * 0.50
            AND close_location <= 0.40
            AND volume_bin >= 0
        ) raw
        WHERE fixed_exit_time IS NOT NULL
          AND (epoch(fixed_exit_time) - epoch(entry_time)) / 60.0 BETWEEN 15 AND 17
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE candidate_signals AS
        WITH grouped AS (
          SELECT *, 'scan_session_dow_trend_vol_range' AS group_level,
                 scan_type || '|dir=' || direction_label
                   || '|sess=' || session_bucket
                   || '|dow=' || CAST(dow AS VARCHAR)
                   || '|trend=' || CAST(trend_bin AS VARCHAR)
                   || '|vol=' || CAST(volume_bin AS VARCHAR)
                   || '|range=' || CAST(range_bin AS VARCHAR) AS candidate_id
          FROM raw_signals
          UNION ALL
          SELECT *, 'scan_session_dow_trend_vol' AS group_level,
                 scan_type || '|dir=' || direction_label
                   || '|sess=' || session_bucket
                   || '|dow=' || CAST(dow AS VARCHAR)
                   || '|trend=' || CAST(trend_bin AS VARCHAR)
                   || '|vol=' || CAST(volume_bin AS VARCHAR) AS candidate_id
          FROM raw_signals
          UNION ALL
          SELECT *, 'scan_session_trend_vol_range' AS group_level,
                 scan_type || '|dir=' || direction_label
                   || '|sess=' || session_bucket
                   || '|trend=' || CAST(trend_bin AS VARCHAR)
                   || '|vol=' || CAST(volume_bin AS VARCHAR)
                   || '|range=' || CAST(range_bin AS VARCHAR) AS candidate_id
          FROM raw_signals
          UNION ALL
          SELECT *, 'scan_session_dow_trend' AS group_level,
                 scan_type || '|dir=' || direction_label
                   || '|sess=' || session_bucket
                   || '|dow=' || CAST(dow AS VARCHAR)
                   || '|trend=' || CAST(trend_bin AS VARCHAR) AS candidate_id
          FROM raw_signals
        )
        SELECT *
        FROM grouped
        """
    )


def signal_family_names() -> list[str]:
    return [
        "opening_range_breakout",
        "opening_range_retest_reclaim",
        "vwap_reclaim_continuation",
        "vwap_pullback_bounce",
        "high_volume_impulse_continuation",
        "range_expansion_continuation",
        "donchian20_breakout",
        "prior_day_breakout",
        "prior_day_rejection",
        "selling_absorption_reversal",
        "buying_absorption_reversal",
        "trend_pullback_reclaim",
        "low_volume_drift",
        "session_extreme_reversion",
        "rsi_extreme_reversal",
        "stoch_extreme_reversal",
        "ma_reacceleration",
        "prior_close_reclaim",
        "midday_zscore_reversion",
        "closing_drive_continuation",
    ]


def group_level_names() -> list[str]:
    return [
        "scan_session_dow_trend_vol_range",
        "scan_session_dow_trend_vol",
        "scan_session_trend_vol_range",
        "scan_session_dow_trend",
    ]


def load_candidate_stats(con: duckdb.DuckDBPyConnection) -> list[CandidateStats]:
    rows = con.execute(
        """
        SELECT candidate_id,
               any_value(group_level) AS group_level,
               any_value(scan_type) AS scan_type,
               any_value(direction_label) AS direction_label,
               any_value(session_bucket) AS session_bucket,
               CASE WHEN contains(candidate_id, '|dow=') THEN any_value(dow) ELSE NULL END AS dow,
               CASE WHEN contains(candidate_id, '|trend=') THEN any_value(trend_bin) ELSE NULL END AS trend_bin,
               CASE WHEN contains(candidate_id, '|vol=') THEN any_value(volume_bin) ELSE NULL END AS volume_bin,
               CASE WHEN contains(candidate_id, '|range=') THEN any_value(range_bin) ELSE NULL END AS range_bin,
               year,
               count(*) AS trades,
               sum(fixed_pnl) AS net_pnl,
               sum(CASE WHEN fixed_pnl > 0 THEN fixed_pnl ELSE 0 END) AS gross_profit,
               sum(CASE WHEN fixed_pnl < 0 THEN fixed_pnl ELSE 0 END) AS gross_loss
        FROM candidate_signals
        GROUP BY candidate_id, year
        ORDER BY candidate_id, year
        """
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        candidate_id = str(row[0])
        item = grouped.setdefault(
            candidate_id,
            {
                "candidate_id": candidate_id,
                "group_level": str(row[1]),
                "scan_type": str(row[2]),
                "direction_label": str(row[3]),
                "session_bucket": str(row[4]),
                "dow": None if row[5] is None else int(row[5]),
                "trend_bin": None if row[6] is None else int(row[6]),
                "volume_bin": None if row[7] is None else int(row[7]),
                "range_bin": None if row[8] is None else int(row[8]),
                "yearly_pnl": {year: 0.0 for year in (*FULL_YEARS, *PARTIAL_YEARS)},
                "yearly_trades": {year: 0 for year in (*FULL_YEARS, *PARTIAL_YEARS)},
                "gross_profit": 0.0,
                "gross_loss": 0.0,
            },
        )
        year = int(row[9])
        if year not in item["yearly_pnl"]:
            continue
        item["yearly_trades"][year] = int(row[10])
        item["yearly_pnl"][year] = float(row[11] or 0.0)
        item["gross_profit"] += float(row[12] or 0.0)
        item["gross_loss"] += float(row[13] or 0.0)
    stats = []
    for item in grouped.values():
        total_trades = sum(item["yearly_trades"].values())
        fixed_net = sum(item["yearly_pnl"].values())
        gross_loss = float(item["gross_loss"])
        stats.append(
            CandidateStats(
                candidate_id=str(item["candidate_id"]),
                group_level=str(item["group_level"]),
                scan_type=str(item["scan_type"]),
                direction_label=str(item["direction_label"]),
                session_bucket=str(item["session_bucket"]),
                dow=item["dow"],
                trend_bin=item["trend_bin"],
                volume_bin=item["volume_bin"],
                range_bin=item["range_bin"],
                total_trades=total_trades,
                fixed_net_pnl=fixed_net,
                fixed_avg_pnl=fixed_net / total_trades if total_trades else 0.0,
                fixed_profit_factor=float(item["gross_profit"]) / abs(gross_loss) if gross_loss else None,
                fixed_positive_years=sum(1 for value in item["yearly_pnl"].values() if value > 0),
                fixed_worst_year_pnl=min(item["yearly_pnl"].values()),
                fixed_min_full_year_trades=min(item["yearly_trades"][year] for year in FULL_YEARS),
            )
        )
    return stats


def select_candidate_stats(stats: Sequence[CandidateStats], max_preselect: int) -> list[CandidateStats]:
    viable = [
        row
        for row in stats
        if row.total_trades >= 250
        and row.fixed_net_pnl > 0
        and row.fixed_avg_pnl >= 5.0
        and (row.fixed_profit_factor or 0.0) >= 1.02
        and row.fixed_min_full_year_trades >= 15
    ]

    def rank(row: CandidateStats) -> tuple[float, ...]:
        return (
            row.fixed_net_pnl,
            row.fixed_avg_pnl,
            float(row.fixed_profit_factor or 0.0),
            float(row.fixed_positive_years),
            float(row.fixed_min_full_year_trades),
            float(row.total_trades),
        )

    return sorted({row.candidate_id: row for row in viable}.values(), key=rank, reverse=True)[:max_preselect]


def load_signals_by_candidate(
    con: duckdb.DuckDBPyConnection,
    candidate_ids: Sequence[str],
) -> dict[str, list[dict[str, Any]]]:
    if not candidate_ids:
        return {}
    values = ", ".join(f"('{sql_string(candidate_id)}')" for candidate_id in candidate_ids)
    rows = con.execute(
        f"""
        WITH selected(candidate_id) AS (VALUES {values})
        SELECT s.candidate_id, s.timestamp, s.entry_time, s.entry_open, s.range20,
               s.scan_type, s.direction, s.direction_label
        FROM candidate_signals s
        JOIN selected USING (candidate_id)
        ORDER BY s.candidate_id, s.timestamp
        """
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        candidate_id = str(row[0])
        dedupe_key = (candidate_id, row[1], row[2], row[5], int(row[6]))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        grouped[candidate_id].append(
            {
                "candidate_id": candidate_id,
                "bar_index": 0,
                "signal_time": row[1],
                "entry_time": row[2],
                "entry_open": float(row[3]),
                "range20": float(row[4] or 0.0),
                "scan_type": str(row[5]),
                "direction": int(row[6]),
                "direction_label": str(row[7]),
                "edge_index": 0,
            }
        )
    return dict(grouped)


def attach_entry_indexes(signals_by_id: dict[str, list[dict[str, Any]]], bars: Sequence[dict[str, Any]]) -> None:
    bars_by_entry_time = {bar["timestamp"]: index for index, bar in enumerate(bars)}
    for signals in signals_by_id.values():
        for signal in signals:
            signal["entry_index"] = bars_by_entry_time.get(signal["entry_time"])


def load_bars(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT row_number() OVER (ORDER BY timestamp)-1 AS bar_index,
               timestamp, open, high, low, close, coalesce(range20, high-low) AS range20
        FROM feature_base
        ORDER BY timestamp
        """
    ).fetchall()
    return [
        {
            "bar_index": int(row[0]),
            "timestamp": row[1],
            "open": float(row[2]),
            "high": float(row[3]),
            "low": float(row[4]),
            "close": float(row[5]),
            "range20": float(row[6] or 0.0),
        }
        for row in rows
    ]


def replay_low_r_exits_fast(
    bars: Sequence[dict[str, Any]],
    signals: Sequence[dict[str, Any]],
    edges: Sequence[RegimeEdge],
    config: LowRRegimeBasketConfig,
):
    if config.max_concurrent_positions == 1:
        closed = []
        occupied_until = -1
        for signal in signals:
            entry_index = signal.get("entry_index")
            if entry_index is None or entry_index <= occupied_until:
                continue
            edge = edges[int(signal["edge_index"])]
            trade = _open_trade(signal, edge, int(entry_index), config)
            closed_trade = _simulate_trade_exit(trade, bars, config)
            closed.append(closed_trade)
            occupied_until = trade.entry_index + closed_trade.bars_held
        return closed

    closed = []
    open_exit_indexes: list[tuple[int, int]] = []
    serial = 0
    for signal in signals:
        entry_index = signal.get("entry_index")
        if entry_index is None:
            continue
        entry_index = int(entry_index)
        while open_exit_indexes and open_exit_indexes[0][0] <= entry_index:
            heapq.heappop(open_exit_indexes)
        if len(open_exit_indexes) >= config.max_concurrent_positions:
            continue
        edge = edges[int(signal["edge_index"])]
        trade = _open_trade(signal, edge, entry_index, config)
        closed_trade = _simulate_trade_exit(trade, bars, config)
        closed.append(closed_trade)
        heapq.heappush(open_exit_indexes, (trade.entry_index + closed_trade.bars_held, serial))
        serial += 1
    closed.sort(key=lambda trade: (trade.exit_time, trade.entry_time, trade.edge_index))
    return closed


def low_r_parameter_grid() -> list[dict[str, Any]]:
    rows = []
    for max_hold_minutes in (60, 120, 300):
        for stop_range_multiple in (6.0, 10.0):
            rows.append(
                {
                    "max_hold_minutes": max_hold_minutes,
                    "stop_range_multiple": stop_range_multiple,
                    "min_stop_points": 8.0,
                    "max_stop_points": 90.0,
                    "flatten_on_date_change": True,
                }
            )
    return rows


def config_from_params(config: LowRRegimeBasketConfig, params: dict[str, Any]) -> LowRRegimeBasketConfig:
    return replace(
        config,
        max_hold_minutes=int(params["max_hold_minutes"]),
        stop_range_multiple=float(params["stop_range_multiple"]),
        min_stop_points=float(params["min_stop_points"]),
        max_stop_points=float(params["max_stop_points"]),
        max_concurrent_positions=int(params["max_concurrent_positions"]),
        flatten_on_date_change=bool(params["flatten_on_date_change"]),
    )


def edge_from_stats(stats: CandidateStats, take_profit_r: float) -> RegimeEdge:
    return RegimeEdge(
        scan_type=stats.scan_type,
        direction_label=stats.direction_label,
        horizon_minutes=120,
        session_bucket=stats.session_bucket,
        dow=-99 if stats.dow is None else stats.dow,
        trend_bin=0 if stats.trend_bin is None else stats.trend_bin,
        volume_bin=0 if stats.volume_bin is None else stats.volume_bin,
        range_bin=0 if stats.range_bin is None else stats.range_bin,
        take_profit_r=take_profit_r,
    )


def replay_result(
    label: str,
    spec: dict[str, Any],
    bars: Sequence[dict[str, Any]],
    signals: Sequence[dict[str, Any]],
    edges: Sequence[RegimeEdge],
    config: LowRRegimeBasketConfig,
    coverage_days: dict[int, int],
    min_full_year_trades: int,
    params: dict[str, Any],
) -> dict[str, Any]:
    trades = replay_low_r_exits_fast(bars, signals, edges, config)
    result = _build_result(bars, signals, trades, edges, config)
    hold_values = [trade.bars_held for trade in trades]
    yearly = result["yearly_results"]
    return {
        "label": label,
        "spec": spec,
        "edge_count": len(edges),
        "params": dict(params),
        "metrics": result["metrics"],
        "avg_hold_bars": sum(hold_values) / len(hold_values) if hold_values else 0.0,
        "median_hold_bars": sorted(hold_values)[len(hold_values) // 2] if hold_values else 0,
        "edge_summary": result["edge_summary"],
        "exit_reasons": result["exit_reasons"],
        "yearly_results": yearly,
        "constraints": constraint_summary(yearly, min_full_year_trades, coverage_days),
        "edges": [asdict(edge) for edge in edges],
        "trades": result["trades"],
    }


def build_high_edge_specs(single_edges: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    strict = [row for row in single_edges if is_high_edge(row, min_avg=80.0, min_pf=1.08)]
    medium = [row for row in single_edges if is_high_edge(row, min_avg=50.0, min_pf=1.05)]
    positive = [row for row in single_edges if is_high_edge(row, min_avg=25.0, min_pf=1.02)]
    robust = [
        row
        for row in single_edges
        if row["metrics"]["net_pnl"] > 0
        and row["constraints"]["positive_years"] >= 5
        and float(row["metrics"].get("profit_factor") or 0.0) >= 1.02
        and float(row["metrics"].get("avg_trade_net_pnl") or 0.0) >= 25.0
    ]
    specs: dict[tuple[str, ...], dict[str, Any]] = {}
    for group_name, rows in {
        "strict_expanded_high_edge": strict,
        "medium_expanded_high_edge": medium,
        "positive_expanded_edge": positive,
        "robust_expanded_positive_edge": robust,
    }.items():
        ranked = sorted(rows, key=single_edge_sort_key, reverse=True)
        if not ranked:
            continue
        for size in (1, 2, 3, 5, 8, 12, 16, 24, 32):
            prefix = ranked[:size]
            ids = tuple(row["source_candidate_id"] for row in prefix)
            specs[ids] = {"name": f"{group_name}_top_{len(ids)}", "candidate_ids": ids}
        for scan_type in sorted({row["edges"][0]["scan_type"] for row in ranked}):
            scan_rows = [row for row in ranked if row["edges"][0]["scan_type"] == scan_type]
            ids = tuple(row["source_candidate_id"] for row in scan_rows[:24])
            specs[ids] = {"name": f"{group_name}_{scan_type}_top_{len(ids)}", "candidate_ids": ids}
    return list(specs.values())


def is_high_edge(row: dict[str, Any], min_avg: float, min_pf: float) -> bool:
    metrics = row["metrics"]
    return (
        float(metrics["net_pnl"]) > 0
        and float(metrics["avg_trade_net_pnl"]) >= min_avg
        and float(metrics.get("profit_factor") or 0.0) >= min_pf
    )


def combine_signals(
    signal_groups: Sequence[Sequence[dict[str, Any]]],
    edge_order: dict[str, int],
) -> list[dict[str, Any]]:
    combined = []
    seen: set[tuple[Any, ...]] = set()
    for group in signal_groups:
        for signal in group:
            candidate_id = signal["candidate_id"]
            edge_index = edge_order[candidate_id]
            dedupe_key = (
                signal["signal_time"],
                signal["entry_time"],
                signal["scan_type"],
                signal["direction"],
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            combined.append({**signal, "edge_index": edge_index})
    combined.sort(key=lambda row: (row["signal_time"], row["edge_index"]))
    return combined


def coverage_days_by_year(bars: Sequence[dict[str, Any]]) -> dict[int, int]:
    by_year: dict[int, set] = {}
    for bar in bars:
        timestamp = bar["timestamp"]
        by_year.setdefault(timestamp.year, set()).add(timestamp.date())
    return {year: len(days) for year, days in by_year.items()}


def constraint_summary(
    yearly: Sequence[dict[str, Any]],
    min_full_year_trades: int,
    coverage_days: dict[int, int],
) -> dict[str, Any]:
    by_year = {int(row["year"]): row for row in yearly}
    full_counts = [int(by_year.get(year, {}).get("trade_count") or 0) for year in FULL_YEARS]
    partial_annualized = [
        annualized_trade_count(
            int(by_year.get(year, {}).get("trade_count") or 0),
            coverage_days.get(year, 365),
        )
        for year in PARTIAL_YEARS
    ]
    pnl_values = [float(by_year.get(year, {}).get("net_pnl") or 0.0) for year in (*FULL_YEARS, *PARTIAL_YEARS)]
    return {
        "full_year_min_trades": min(full_counts) if full_counts else 0,
        "partial_year_min_annualized_trades": min(partial_annualized) if partial_annualized else 0.0,
        "trade_floor_pass": all(value > min_full_year_trades for value in full_counts)
        and all(value > min_full_year_trades for value in partial_annualized),
        "positive_years": sum(1 for value in pnl_values if value > 0),
        "worst_year_pnl": min(pnl_values) if pnl_values else 0.0,
    }


def annualized_trade_count(trade_count: int, covered_days: int) -> float:
    if covered_days <= 0:
        return 0.0
    return trade_count * 365.0 / min(covered_days, 365)


def single_edge_sort_key(row: dict[str, Any]) -> tuple[float, ...]:
    metrics = row["metrics"]
    constraints = row["constraints"]
    return (
        float(metrics["avg_trade_net_pnl"]),
        float(metrics.get("profit_factor") or 0.0),
        float(metrics["net_pnl"]),
        float(constraints["positive_years"]),
        float(constraints["full_year_min_trades"]),
        -float(row["avg_hold_bars"]),
    )


def sort_key(row: dict[str, Any]) -> tuple[float, ...]:
    metrics = row["metrics"]
    constraints = row["constraints"]
    return (
        1.0 if constraints["trade_floor_pass"] and metrics["net_pnl"] > 0 else 0.0,
        float(metrics["net_pnl"]),
        float(constraints["positive_years"]),
        float(metrics.get("net_pnl_to_max_drawdown") or 0.0),
        float(metrics.get("profit_factor") or 0.0),
        -float(row["avg_hold_bars"]),
    )


def stable_sort_key(row: dict[str, Any]) -> tuple[float, ...]:
    metrics = row["metrics"]
    constraints = row["constraints"]
    return (
        1.0 if constraints["trade_floor_pass"] and metrics["net_pnl"] > 0 else 0.0,
        float(constraints["positive_years"]),
        float(metrics["net_pnl"]),
        float(metrics.get("net_pnl_to_max_drawdown") or 0.0),
        float(metrics.get("profit_factor") or 0.0),
        float(constraints["worst_year_pnl"]),
        -float(row["avg_hold_bars"]),
    )


def sql_string(value: str) -> str:
    return value.replace("'", "''")


if __name__ == "__main__":
    raise SystemExit(main())
