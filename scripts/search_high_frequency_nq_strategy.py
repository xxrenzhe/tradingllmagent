#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import duckdb


YEARS = tuple(range(2010, 2027))
FULL_YEARS = tuple(range(2011, 2026))
POINT_VALUE = 20.0
ROUND_TRIP_COST_USD = 15.0
HORIZONS = (1, 2, 3, 5, 10, 15)


@dataclass(frozen=True)
class RuleStats:
    candidate_id: str
    group_level: str
    scan_type: str
    horizon_minutes: int
    direction_label: str
    session_bucket: str
    trend_bin: int | None
    volume_bin: int | None
    range_bin: int | None
    trades: int
    net_pnl: float
    avg_pnl: float
    win_rate: float
    profit_factor: float | None
    avg_hold_minutes: float
    yearly_pnl: tuple[float, ...]
    yearly_trades: tuple[int, ...]

    def min_year_trades(self) -> int:
        return min(self.yearly_trades)

    def min_full_year_trades(self) -> int:
        return min(self.yearly_trades[1:16])

    def positive_years(self) -> int:
        return sum(1 for value in self.yearly_pnl if value > 0)

    def worst_year_pnl(self) -> float:
        return min(self.yearly_pnl)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "group_level": self.group_level,
            "scan_type": self.scan_type,
            "horizon_minutes": self.horizon_minutes,
            "direction_label": self.direction_label,
            "session_bucket": self.session_bucket,
            "trend_bin": self.trend_bin,
            "volume_bin": self.volume_bin,
            "range_bin": self.range_bin,
            "trades": self.trades,
            "net_pnl": self.net_pnl,
            "avg_trade_net_pnl": self.avg_pnl,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "avg_hold_minutes": self.avg_hold_minutes,
            "yearly_pnl": dict(zip(YEARS, self.yearly_pnl, strict=True)),
            "yearly_trades": dict(zip(YEARS, self.yearly_trades, strict=True)),
            "positive_years": self.positive_years(),
            "worst_year_pnl": self.worst_year_pnl(),
        }


@dataclass(frozen=True)
class BeamCandidate:
    candidate_ids: tuple[str, ...]
    yearly_pnl: tuple[float, ...]
    yearly_trades: tuple[int, ...]
    net_pnl: float
    avg_hold_minutes: float

    def min_year_trades(self) -> int:
        return min(self.yearly_trades)

    def min_full_year_trades(self) -> int:
        return min(self.yearly_trades[1:16])

    def positive_years(self) -> int:
        return sum(1 for value in self.yearly_pnl if value > 0)

    def worst_year_pnl(self) -> float:
        return min(self.yearly_pnl)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Search NQ 1m strategies with hard annual trade-count constraints."
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--date-from", default="2010-06-06")
    parser.add_argument("--date-to", default="2026-04-27")
    parser.add_argument("--horizons", default="1,2,3,5,10,15")
    parser.add_argument("--min-year-trades", type=int, default=1000)
    parser.add_argument("--max-rule-pool", type=int, default=360)
    parser.add_argument("--max-rules-per-strategy", type=int, default=18)
    parser.add_argument("--beam-width", type=int, default=1000)
    parser.add_argument("--exact-candidates", type=int, default=160)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/profit_mining/high_frequency_nq_strategy_search.json"),
    )
    args = parser.parse_args()

    global HORIZONS
    HORIZONS = tuple(int(raw.strip()) for raw in args.horizons.split(",") if raw.strip())
    if not HORIZONS:
        raise ValueError("--horizons must include at least one integer minute horizon")

    pattern = str(Path(args.data_root) / "bars" / "1m" / args.symbol / "date=*" / "part-000.parquet")
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4")
    try:
        print("loading coverage", flush=True)
        coverage = load_coverage(con, pattern, args.date_from, args.date_to)
        print("building features", flush=True)
        build_feature_table(con, pattern, args.date_from, args.date_to)
        print("building signals", flush=True)
        build_signal_table(con)
        print("loading rule stats", flush=True)
        rules = load_rule_stats(con)
        print(f"loaded {len(rules)} rule groups", flush=True)
        rule_pool = select_rule_pool(rules, args.max_rule_pool)
        print(f"selected {len(rule_pool)} rule groups", flush=True)
        print("running beam search", flush=True)
        beam_candidates = beam_search(
            rule_pool,
            max_rules_per_strategy=args.max_rules_per_strategy,
            beam_width=args.beam_width,
            min_year_trades=args.min_year_trades,
        )
        print(f"beam candidates {len(beam_candidates)}", flush=True)
        print("running exact replay", flush=True)
        exact_results = exact_replay_candidates(
            con,
            rule_pool,
            beam_candidates[: args.exact_candidates],
            min_year_trades=args.min_year_trades,
            coverage=coverage,
        )
    finally:
        con.close()

    exact_results.sort(key=lambda item: exact_sort_key(item, args.min_year_trades), reverse=True)
    best_actual = next(
        (item for item in exact_results if item["constraints"]["actual_all_years_gt_min_trades"]),
        exact_results[0] if exact_results else None,
    )
    payload = {
        "schema_version": 1,
        "artifact": "high_frequency_nq_strategy_search",
        "symbol": args.symbol,
        "timeframe": "1m",
        "date_from": args.date_from,
        "date_to": args.date_to,
        "cost_model": {
            "point_value": POINT_VALUE,
            "round_trip_cost_usd": ROUND_TRIP_COST_USD,
            "note": "Round-trip cost approximates $5 commission/fees plus two $5 slippage ticks.",
        },
        "constraints": {
            "min_year_trades": args.min_year_trades,
            "hard_constraint": "actual trade_count > min_year_trades for every calendar year in local data",
            "full_years": list(FULL_YEARS),
            "partial_years": [2010, 2026],
            "partial_years_are_not_annualized_for_the_hard_constraint": True,
        },
        "coverage": coverage,
        "search_space": {
            "horizons_minutes": list(HORIZONS),
            "families": signal_family_names(),
            "group_levels": [
                "family_session",
                "family_session_trend",
            ],
            "rule_pool_size": len(rule_pool),
            "beam_candidate_count": len(beam_candidates),
            "exact_candidate_count": len(exact_results),
            "max_rules_per_strategy": args.max_rules_per_strategy,
            "objective": "maximize exact net PnL subject to annual trade-count floor",
        },
        "best_actual_all_years": best_actual,
        "top_exact": exact_results[:30],
        "top_single_rules": [rule.to_dict() for rule in rule_pool[:60]],
        "notes": [
            "Signals are evaluated after a 1-minute bar closes and enter at the next 1-minute open.",
            "Exact replay permits at most one open NQ position; overlapping signals are skipped until the fixed-horizon exit.",
            "If multiple selected rules fire on the same timestamp, the higher-ranked rule is used once.",
            "The hard trade-count constraint uses actual calendar-year counts, so 2010 and 2026 are harder because local data is partial.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True, default=json_default) + "\n", encoding="utf-8")
    print(args.output)
    if best_actual:
        metrics = best_actual["metrics"]
        print(
            "best_actual_all_years",
            "net=",
            round(metrics["net_pnl"], 2),
            "trades=",
            metrics["trade_count"],
            "min_year_trades=",
            best_actual["constraints"]["min_year_trades"],
            "pf=",
            None if metrics["profit_factor"] is None else round(metrics["profit_factor"], 3),
            "dd=",
            round(metrics["max_drawdown"], 2),
            "avg_hold=",
            round(metrics["avg_hold_minutes"], 2),
            "rules=",
            best_actual["rule_count"],
        )
    return 0


def load_coverage(
    con: duckdb.DuckDBPyConnection,
    pattern: str,
    date_from: str,
    date_to: str,
) -> dict[str, dict[str, Any]]:
    rows = con.execute(
        """
        WITH bars AS (
          SELECT timestamp,
                 CAST(strftime(timestamp, '%Y') AS INTEGER) AS year
          FROM read_parquet(?, union_by_name=true)
          WHERE timestamp >= CAST(? AS TIMESTAMP)
            AND timestamp < CAST(? AS TIMESTAMP) + INTERVAL 1 DAY
        )
        SELECT year, min(timestamp), max(timestamp), count(*) AS bars
        FROM bars
        GROUP BY year
        ORDER BY year
        """,
        [pattern, date_from, date_to],
    ).fetchall()
    return {
        str(year): {
            "first_timestamp": first.isoformat(),
            "last_timestamp": last.isoformat(),
            "bars": int(bars),
        }
        for year, first, last, bars in rows
    }


def build_feature_table(
    con: duckdb.DuckDBPyConnection,
    pattern: str,
    date_from: str,
    date_to: str,
) -> None:
    lead_columns = ",\n                 ".join(
        f"lead(close, {horizon + 1}) OVER (ORDER BY timestamp) AS exit_close_{horizon},\n"
        f"                 lead(timestamp, {horizon + 1}) OVER (ORDER BY timestamp) AS exit_ts_{horizon}"
        for horizon in HORIZONS
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE feature_base AS
        WITH raw AS (
          SELECT timestamp, open, high, low, close, tick_count,
                 (timestamp AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York') AS ny_ts
          FROM read_parquet(?, union_by_name=true)
          WHERE timestamp >= CAST(? AS TIMESTAMP)
            AND timestamp < CAST(? AS TIMESTAMP) + INTERVAL 1 DAY
        ), returns AS (
          SELECT *,
                 CAST(ny_ts AS DATE) AS ny_date,
                 CAST(strftime(ny_ts, '%Y') AS INTEGER) AS year,
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
                 lead(timestamp, 1) OVER (ORDER BY timestamp) AS entry_ts,
                 {lead_columns}
          FROM returns
        ), session_features AS (
          SELECT *,
                 sum(((high + low + close) / 3.0) * greatest(tick_count, 1)) OVER (
                   PARTITION BY ny_date ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                 ) / NULLIF(sum(greatest(tick_count, 1)) OVER (
                   PARTITION BY ny_date ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                 ), 0) AS session_vwap,
                 max(high) OVER (
                   PARTITION BY ny_date ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                 ) AS session_high_prev,
                 min(low) OVER (
                   PARTITION BY ny_date ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                 ) AS session_low_prev,
                 max(CASE WHEN ny_min BETWEEN 570 AND 599 THEN high END) OVER (
                   PARTITION BY ny_date ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                 ) AS opening_high_prev,
                 min(CASE WHEN ny_min BETWEEN 570 AND 599 THEN low END) OVER (
                   PARTITION BY ny_date ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                 ) AS opening_low_prev
          FROM indicators
        ), final AS (
          SELECT *,
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
          FROM session_features
        )
        SELECT *
        FROM final
        WHERE entry_open IS NOT NULL
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
    signal_queries = [signal_query_for_horizon(horizon) for horizon in HORIZONS]
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE raw_signals AS
        WITH all_signals AS (
          {" UNION ALL ".join(signal_queries)}
        )
        SELECT *
        FROM all_signals
        WHERE pnl IS NOT NULL
          AND exit_timestamp IS NOT NULL
          AND entry_timestamp IS NOT NULL
          AND (epoch(exit_timestamp) - epoch(entry_timestamp)) / 60.0
                BETWEEN horizon_minutes AND horizon_minutes + 2
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE candidate_signals AS
        WITH grouped AS (
          SELECT *, 'family_session' AS group_level,
                 scan_type || '|h=' || CAST(horizon_minutes AS VARCHAR)
                   || '|dir=' || direction_label
                   || '|sess=' || session_bucket AS candidate_id
          FROM raw_signals
          UNION ALL
          SELECT *, 'family_session_trend' AS group_level,
                 scan_type || '|h=' || CAST(horizon_minutes AS VARCHAR)
                   || '|dir=' || direction_label
                   || '|sess=' || session_bucket
                   || '|trend=' || CAST(trend_bin AS VARCHAR) AS candidate_id
          FROM raw_signals
        )
        SELECT *
        FROM grouped
        """
    )


def signal_family_names() -> list[str]:
    return [
        "candle_momentum_40",
        "candle_momentum_60",
        "impulse_continuation_1",
        "impulse_continuation_3",
        "impulse_reversal_5",
        "trend_pullback_reclaim",
        "ma50_stretch_reversion",
        "bollinger20_reversion",
        "vwap_side_continuation",
        "vwap_reclaim_continuation",
        "donchian20_breakout",
        "donchian60_breakout",
        "failed_donchian20_reversion",
        "range_expansion_continuation",
        "range_expansion_reversion",
        "rsi14_reversion",
        "rsi14_momentum",
        "stoch20_reversion",
        "session_extreme_reversion",
        "opening_range_breakout",
        "low_volume_drift",
    ]


def signal_query_for_horizon(horizon: int) -> str:
    pnl = (
        f"CASE WHEN direction = 1 "
        f"THEN (exit_close_{horizon} - entry_open) * {POINT_VALUE} - {ROUND_TRIP_COST_USD} "
        f"ELSE (entry_open - exit_close_{horizon}) * {POINT_VALUE} - {ROUND_TRIP_COST_USD} END"
    )
    return f"""
    SELECT timestamp, entry_ts AS entry_timestamp, exit_ts_{horizon} AS exit_timestamp,
           year, '{horizon}'::INTEGER AS horizon_minutes, scan_type, session_bucket,
           trend_bin, volume_bin, range_bin,
           CASE WHEN direction = 1 THEN 'long' ELSE 'short' END AS direction_label,
           entry_open AS entry_price, exit_close_{horizon} AS exit_price,
           (epoch(exit_ts_{horizon}) - epoch(entry_ts)) / 60.0 AS hold_minutes,
           {pnl} AS pnl
    FROM (
      SELECT *, 'candle_momentum_40' AS scan_type, 1 AS direction
      FROM feature_base
      WHERE body_to_range >= 0.40 AND close > open AND ret1 > 0
      UNION ALL
      SELECT *, 'candle_momentum_40', -1
      FROM feature_base
      WHERE body_to_range <= -0.40 AND close < open AND ret1 < 0
      UNION ALL
      SELECT *, 'candle_momentum_60', 1
      FROM feature_base
      WHERE body_to_range >= 0.60 AND close > open AND ret1 > 0
      UNION ALL
      SELECT *, 'candle_momentum_60', -1
      FROM feature_base
      WHERE body_to_range <= -0.60 AND close < open AND ret1 < 0
      UNION ALL
      SELECT *, 'impulse_continuation_1', 1
      FROM feature_base
      WHERE ret1 >= range20 * 0.35 AND close_location >= 0.65
      UNION ALL
      SELECT *, 'impulse_continuation_1', -1
      FROM feature_base
      WHERE ret1 <= -range20 * 0.35 AND close_location <= 0.35
      UNION ALL
      SELECT *, 'impulse_continuation_3', 1
      FROM feature_base
      WHERE ret3 >= range20 * 0.75 AND ret1 > 0 AND close_location >= 0.60
      UNION ALL
      SELECT *, 'impulse_continuation_3', -1
      FROM feature_base
      WHERE ret3 <= -range20 * 0.75 AND ret1 < 0 AND close_location <= 0.40
      UNION ALL
      SELECT *, 'impulse_reversal_5', 1
      FROM feature_base
      WHERE ret5 <= -range20 * 1.25 AND close_location >= 0.65 AND body_to_range > 0
      UNION ALL
      SELECT *, 'impulse_reversal_5', -1
      FROM feature_base
      WHERE ret5 >= range20 * 1.25 AND close_location <= 0.35 AND body_to_range < 0
      UNION ALL
      SELECT *, 'trend_pullback_reclaim', 1
      FROM feature_base
      WHERE trend_bin = 1 AND close > ma20 AND ret5 < 0 AND ret1 > 0
      UNION ALL
      SELECT *, 'trend_pullback_reclaim', -1
      FROM feature_base
      WHERE trend_bin = -1 AND close < ma20 AND ret5 > 0 AND ret1 < 0
      UNION ALL
      SELECT *, 'ma50_stretch_reversion', 1
      FROM feature_base
      WHERE z50 <= -1.25 AND close_location >= 0.55
      UNION ALL
      SELECT *, 'ma50_stretch_reversion', -1
      FROM feature_base
      WHERE z50 >= 1.25 AND close_location <= 0.45
      UNION ALL
      SELECT *, 'bollinger20_reversion', 1
      FROM feature_base
      WHERE z20 <= -1.75 AND close_location >= 0.50
      UNION ALL
      SELECT *, 'bollinger20_reversion', -1
      FROM feature_base
      WHERE z20 >= 1.75 AND close_location <= 0.50
      UNION ALL
      SELECT *, 'vwap_side_continuation', 1
      FROM feature_base
      WHERE close > session_vwap AND ret1 > 0 AND trend_bin = 1
      UNION ALL
      SELECT *, 'vwap_side_continuation', -1
      FROM feature_base
      WHERE close < session_vwap AND ret1 < 0 AND trend_bin = -1
      UNION ALL
      SELECT *, 'vwap_reclaim_continuation', 1
      FROM feature_base
      WHERE prev_vwap_dist < 0 AND vwap_dist >= 0 AND ret1 > 0
      UNION ALL
      SELECT *, 'vwap_reclaim_continuation', -1
      FROM feature_base
      WHERE prev_vwap_dist > 0 AND vwap_dist <= 0 AND ret1 < 0
      UNION ALL
      SELECT *, 'donchian20_breakout', 1
      FROM feature_base
      WHERE close > high20_prev AND volume_bin >= 0
      UNION ALL
      SELECT *, 'donchian20_breakout', -1
      FROM feature_base
      WHERE close < low20_prev AND volume_bin >= 0
      UNION ALL
      SELECT *, 'donchian60_breakout', 1
      FROM feature_base
      WHERE close > high60_prev AND volume_bin >= 0
      UNION ALL
      SELECT *, 'donchian60_breakout', -1
      FROM feature_base
      WHERE close < low60_prev AND volume_bin >= 0
      UNION ALL
      SELECT *, 'failed_donchian20_reversion', -1
      FROM feature_base
      WHERE high > high20_prev AND close < high20_prev AND z50 >= 0.75
      UNION ALL
      SELECT *, 'failed_donchian20_reversion', 1
      FROM feature_base
      WHERE low < low20_prev AND close > low20_prev AND z50 <= -0.75
      UNION ALL
      SELECT *, 'range_expansion_continuation', 1
      FROM feature_base
      WHERE body_to_range >= 0.60 AND range_bin >= 2 AND volume_bin >= 0
      UNION ALL
      SELECT *, 'range_expansion_continuation', -1
      FROM feature_base
      WHERE body_to_range <= -0.60 AND range_bin >= 2 AND volume_bin >= 0
      UNION ALL
      SELECT *, 'range_expansion_reversion', -1
      FROM feature_base
      WHERE body_to_range >= 0.65 AND range_bin >= 2 AND z50 >= 1.0
      UNION ALL
      SELECT *, 'range_expansion_reversion', 1
      FROM feature_base
      WHERE body_to_range <= -0.65 AND range_bin >= 2 AND z50 <= -1.0
      UNION ALL
      SELECT *, 'rsi14_reversion', 1
      FROM feature_base
      WHERE rsi14 <= 30 AND close_location >= 0.45
      UNION ALL
      SELECT *, 'rsi14_reversion', -1
      FROM feature_base
      WHERE rsi14 >= 70 AND close_location <= 0.55
      UNION ALL
      SELECT *, 'rsi14_momentum', 1
      FROM feature_base
      WHERE rsi14 >= 60 AND ret1 > 0 AND trend_bin = 1
      UNION ALL
      SELECT *, 'rsi14_momentum', -1
      FROM feature_base
      WHERE rsi14 <= 40 AND ret1 < 0 AND trend_bin = -1
      UNION ALL
      SELECT *, 'stoch20_reversion', 1
      FROM feature_base
      WHERE stoch20 <= 0.10 AND close_location >= 0.50
      UNION ALL
      SELECT *, 'stoch20_reversion', -1
      FROM feature_base
      WHERE stoch20 >= 0.90 AND close_location <= 0.50
      UNION ALL
      SELECT *, 'session_extreme_reversion', -1
      FROM feature_base
      WHERE session_high_prev IS NOT NULL
        AND close >= session_high_prev - range20 * 0.10
        AND z50 >= 1.00
        AND volume_bin <= 1
      UNION ALL
      SELECT *, 'session_extreme_reversion', 1
      FROM feature_base
      WHERE session_low_prev IS NOT NULL
        AND close <= session_low_prev + range20 * 0.10
        AND z50 <= -1.00
        AND volume_bin <= 1
      UNION ALL
      SELECT *, 'opening_range_breakout', 1
      FROM feature_base
      WHERE opening_high_prev IS NOT NULL
        AND ny_min >= 600 AND ny_min <= 959
        AND close > opening_high_prev
        AND volume_bin >= 0
      UNION ALL
      SELECT *, 'opening_range_breakout', -1
      FROM feature_base
      WHERE opening_low_prev IS NOT NULL
        AND ny_min >= 600 AND ny_min <= 959
        AND close < opening_low_prev
        AND volume_bin >= 0
      UNION ALL
      SELECT *, 'low_volume_drift', 1
      FROM feature_base
      WHERE trend_bin = 1 AND volume_bin = -1 AND ret1 > 0
      UNION ALL
      SELECT *, 'low_volume_drift', -1
      FROM feature_base
      WHERE trend_bin = -1 AND volume_bin = -1 AND ret1 < 0
    ) raw
    """


def load_rule_stats(con: duckdb.DuckDBPyConnection) -> list[RuleStats]:
    rows = con.execute(
        """
        SELECT candidate_id, any_value(group_level) AS group_level, any_value(scan_type) AS scan_type,
               any_value(horizon_minutes) AS horizon_minutes, any_value(direction_label) AS direction_label,
               any_value(session_bucket) AS session_bucket,
               CASE WHEN contains(candidate_id, '|trend=') THEN any_value(trend_bin) ELSE NULL END AS trend_bin,
               CASE WHEN contains(candidate_id, '|vol=') THEN any_value(volume_bin) ELSE NULL END AS volume_bin,
               CASE WHEN contains(candidate_id, '|range=') THEN any_value(range_bin) ELSE NULL END AS range_bin,
               year, count(*) AS trades, sum(pnl) AS net_pnl,
               avg(pnl) AS avg_pnl,
               avg(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
               sum(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) AS gross_profit,
               sum(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) AS gross_loss,
               avg(hold_minutes) AS avg_hold_minutes
        FROM candidate_signals
        GROUP BY candidate_id, year
        ORDER BY candidate_id, year
        """
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        candidate_id = str(row[0])
        current = grouped.setdefault(
            candidate_id,
            {
                "candidate_id": candidate_id,
                "group_level": str(row[1]),
                "scan_type": str(row[2]),
                "horizon_minutes": int(row[3]),
                "direction_label": str(row[4]),
                "session_bucket": str(row[5]),
                "trend_bin": None if row[6] is None else int(row[6]),
                "volume_bin": None if row[7] is None else int(row[7]),
                "range_bin": None if row[8] is None else int(row[8]),
                "yearly_pnl": {year: 0.0 for year in YEARS},
                "yearly_trades": {year: 0 for year in YEARS},
                "wins": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
                "hold_x_trades": 0.0,
            },
        )
        year = int(row[9])
        trades = int(row[10])
        net_pnl = float(row[11] or 0.0)
        win_rate = float(row[13] or 0.0)
        gross_profit = float(row[14] or 0.0)
        gross_loss = float(row[15] or 0.0)
        avg_hold = float(row[16] or 0.0)
        current["yearly_pnl"][year] = net_pnl
        current["yearly_trades"][year] = trades
        current["wins"] += win_rate * trades
        current["gross_profit"] += gross_profit
        current["gross_loss"] += gross_loss
        current["hold_x_trades"] += avg_hold * trades

    stats = []
    for item in grouped.values():
        yearly_pnl = tuple(float(item["yearly_pnl"][year]) for year in YEARS)
        yearly_trades = tuple(int(item["yearly_trades"][year]) for year in YEARS)
        trades = sum(yearly_trades)
        net_pnl = sum(yearly_pnl)
        gross_loss = float(item["gross_loss"])
        gross_profit = float(item["gross_profit"])
        stats.append(
            RuleStats(
                candidate_id=str(item["candidate_id"]),
                group_level=str(item["group_level"]),
                scan_type=str(item["scan_type"]),
                horizon_minutes=int(item["horizon_minutes"]),
                direction_label=str(item["direction_label"]),
                session_bucket=str(item["session_bucket"]),
                trend_bin=item["trend_bin"],
                volume_bin=item["volume_bin"],
                range_bin=item["range_bin"],
                trades=trades,
                net_pnl=net_pnl,
                avg_pnl=net_pnl / trades if trades else 0.0,
                win_rate=float(item["wins"]) / trades if trades else 0.0,
                profit_factor=gross_profit / abs(gross_loss) if gross_loss else None,
                avg_hold_minutes=float(item["hold_x_trades"]) / trades if trades else 0.0,
                yearly_pnl=yearly_pnl,
                yearly_trades=yearly_trades,
            )
        )
    return stats


def select_rule_pool(rules: Sequence[RuleStats], max_rule_pool: int) -> list[RuleStats]:
    viable = [
        rule
        for rule in rules
        if rule.trades >= 800
        and rule.net_pnl > 0
        and rule.avg_pnl > 0
        and rule.min_full_year_trades() >= 20
    ]

    def rank(rule: RuleStats) -> tuple[float, ...]:
        return (
            rule.net_pnl,
            float(rule.min_year_trades()),
            float(rule.positive_years()),
            rule.avg_pnl,
            float(rule.trades),
            -float(rule.horizon_minutes),
        )

    return sorted({rule.candidate_id: rule for rule in viable}.values(), key=rank, reverse=True)[:max_rule_pool]


def beam_search(
    rule_pool: Sequence[RuleStats],
    max_rules_per_strategy: int,
    beam_width: int,
    min_year_trades: int,
) -> list[BeamCandidate]:
    empty = BeamCandidate(
        candidate_ids=(),
        yearly_pnl=tuple(0.0 for _ in YEARS),
        yearly_trades=tuple(0 for _ in YEARS),
        net_pnl=0.0,
        avg_hold_minutes=0.0,
    )
    by_id = {rule.candidate_id: rule for rule in rule_pool}
    beam = [empty]
    completed: dict[tuple[str, ...], BeamCandidate] = {}
    for _ in range(max_rules_per_strategy):
        next_beam: dict[tuple[str, ...], BeamCandidate] = {}
        for candidate in beam:
            used = set(candidate.candidate_ids)
            for rule in rule_pool:
                if rule.candidate_id in used:
                    continue
                candidate_ids = tuple(sorted((*candidate.candidate_ids, rule.candidate_id)))
                if candidate_ids in next_beam:
                    continue
                yearly_pnl = tuple(
                    a + b for a, b in zip(candidate.yearly_pnl, rule.yearly_pnl, strict=True)
                )
                yearly_trades = tuple(
                    a + b for a, b in zip(candidate.yearly_trades, rule.yearly_trades, strict=True)
                )
                avg_hold = sum(by_id[candidate_id].avg_hold_minutes for candidate_id in candidate_ids) / len(
                    candidate_ids
                )
                next_beam[candidate_ids] = BeamCandidate(
                    candidate_ids=candidate_ids,
                    yearly_pnl=yearly_pnl,
                    yearly_trades=yearly_trades,
                    net_pnl=sum(yearly_pnl),
                    avg_hold_minutes=avg_hold,
                )
        ranked = sorted(
            next_beam.values(),
            key=lambda candidate: beam_sort_key(candidate, min_year_trades),
            reverse=True,
        )
        beam = ranked[:beam_width]
        for candidate in beam:
            if candidate.min_year_trades() > min_year_trades:
                completed[candidate.candidate_ids] = candidate
    if not completed:
        completed = {candidate.candidate_ids: candidate for candidate in beam}
    return sorted(
        completed.values(),
        key=lambda candidate: beam_sort_key(candidate, min_year_trades),
        reverse=True,
    )


def beam_sort_key(candidate: BeamCandidate, min_year_trades: int) -> tuple[float, ...]:
    actual_constraint = candidate.min_year_trades() > min_year_trades
    full_constraint = candidate.min_full_year_trades() > min_year_trades
    trade_deficit = min(0, candidate.min_year_trades() - min_year_trades)
    return (
        1.0 if actual_constraint else 0.0,
        1.0 if full_constraint else 0.0,
        float(trade_deficit),
        candidate.net_pnl,
        float(candidate.positive_years()),
        candidate.worst_year_pnl(),
        -candidate.avg_hold_minutes,
        -float(len(candidate.candidate_ids)),
    )


def exact_replay_candidates(
    con: duckdb.DuckDBPyConnection,
    rule_pool: Sequence[RuleStats],
    candidates: Sequence[BeamCandidate],
    min_year_trades: int,
    coverage: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rule_by_id = {rule.candidate_id: rule for rule in rule_pool}
    rule_rank = {
        rule.candidate_id: rank
        for rank, rule in enumerate(
            sorted(rule_pool, key=lambda rule: (rule.net_pnl, rule.avg_pnl, rule.trades), reverse=True)
        )
    }
    exact_results = []
    seen: set[tuple[str, ...]] = set()
    for candidate in candidates:
        candidate_ids = tuple(candidate.candidate_ids)
        if not candidate_ids or candidate_ids in seen:
            continue
        seen.add(candidate_ids)
        rules = [rule_by_id[candidate_id] for candidate_id in candidate_ids]
        selected_values = ", ".join(
            f"('{sql_string(rule.candidate_id)}', {rule_rank[rule.candidate_id]})" for rule in rules
        )
        rows = con.execute(
            f"""
            WITH selected(candidate_id, rule_rank) AS (VALUES {selected_values})
            SELECT s.timestamp, s.entry_timestamp, s.exit_timestamp, s.year, s.candidate_id,
                   selected.rule_rank, s.pnl, s.hold_minutes
            FROM candidate_signals s
            JOIN selected ON s.candidate_id = selected.candidate_id
            ORDER BY s.timestamp, selected.rule_rank
            """
        ).fetchall()
        metrics, sample_trades = replay_rows(rows, min_year_trades, coverage)
        exact_results.append(
            {
                "rule_count": len(rules),
                "constraints": {
                    "min_year_trades": min(metrics["yearly"][str(year)]["trades"] for year in YEARS),
                    "min_full_year_trades": min(metrics["yearly"][str(year)]["trades"] for year in FULL_YEARS),
                    "actual_all_years_gt_min_trades": all(
                        metrics["yearly"][str(year)]["trades"] > min_year_trades for year in YEARS
                    ),
                    "actual_full_years_gt_min_trades": all(
                        metrics["yearly"][str(year)]["trades"] > min_year_trades for year in FULL_YEARS
                    ),
                },
                "metrics": metrics,
                "additive_beam": {
                    "net_pnl": candidate.net_pnl,
                    "min_year_trades": candidate.min_year_trades(),
                    "min_full_year_trades": candidate.min_full_year_trades(),
                    "positive_years": candidate.positive_years(),
                    "worst_year_pnl": candidate.worst_year_pnl(),
                    "avg_hold_minutes": candidate.avg_hold_minutes,
                    "yearly_pnl": dict(zip(YEARS, candidate.yearly_pnl, strict=True)),
                    "yearly_trades": dict(zip(YEARS, candidate.yearly_trades, strict=True)),
                },
                "rules": [rule.to_dict() for rule in rules],
                "sample_trades": sample_trades[:12],
            }
        )
    return exact_results


def replay_rows(
    rows: Sequence[tuple[Any, ...]],
    min_year_trades: int,
    coverage: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    yearly = {year: {"pnl": 0.0, "trades": 0} for year in YEARS}
    selected_trades = []
    trades: list[tuple[float, float]] = []
    occupied_until: datetime | None = None
    last_signal_timestamp: datetime | None = None
    for timestamp, entry_timestamp, exit_timestamp, year, candidate_id, _rank, pnl, hold_minutes in rows:
        if timestamp == last_signal_timestamp:
            continue
        if occupied_until is not None and timestamp < occupied_until:
            continue
        year_int = int(year)
        pnl_value = float(pnl)
        hold_value = float(hold_minutes)
        yearly.setdefault(year_int, {"pnl": 0.0, "trades": 0})
        yearly[year_int]["pnl"] += pnl_value
        yearly[year_int]["trades"] += 1
        selected_trades.append(
            {
                "timestamp": timestamp.isoformat(),
                "entry_timestamp": entry_timestamp.isoformat(),
                "exit_timestamp": exit_timestamp.isoformat(),
                "year": year_int,
                "candidate_id": str(candidate_id),
                "pnl": pnl_value,
                "hold_minutes": hold_value,
            }
        )
        trades.append((pnl_value, hold_value))
        occupied_until = exit_timestamp
        last_signal_timestamp = timestamp

    pnls = [trade[0] for trade in trades]
    holds = [trade[1] for trade in trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    yearly_payload: dict[str, dict[str, Any]] = {}
    for year in YEARS:
        year_key = str(year)
        trades_in_year = int(yearly.get(year, {"trades": 0})["trades"])
        pnl_in_year = float(yearly.get(year, {"pnl": 0.0})["pnl"])
        bars = int(coverage.get(year_key, {}).get("bars", 0))
        yearly_payload[year_key] = {
            "pnl": pnl_in_year,
            "trades": trades_in_year,
            "annualized_trades_by_bar_coverage": trades_in_year * 350000.0 / bars if bars else None,
            "gt_min_trades": trades_in_year > min_year_trades,
        }

    gross_profit = sum(wins)
    gross_loss = sum(losses)
    metrics = {
        "trade_count": len(pnls),
        "net_pnl": sum(pnls),
        "avg_trade_net_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
        "win_rate": len(wins) / len(pnls) if pnls else 0.0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": gross_profit / abs(gross_loss) if gross_loss else None,
        "max_drawdown": max_drawdown,
        "net_pnl_to_max_drawdown": sum(pnls) / max_drawdown if max_drawdown else None,
        "avg_hold_minutes": sum(holds) / len(holds) if holds else 0.0,
        "positive_years": sum(1 for item in yearly_payload.values() if item["pnl"] > 0),
        "worst_year_pnl": min(item["pnl"] for item in yearly_payload.values()),
        "yearly": yearly_payload,
    }
    return metrics, selected_trades


def exact_sort_key(result: dict[str, Any], min_year_trades: int) -> tuple[float, ...]:
    metrics = result["metrics"]
    constraints = result["constraints"]
    trade_deficit = min(0, int(constraints["min_year_trades"]) - min_year_trades)
    return (
        1.0 if constraints["actual_all_years_gt_min_trades"] else 0.0,
        1.0 if constraints["actual_full_years_gt_min_trades"] else 0.0,
        float(trade_deficit),
        float(metrics["net_pnl"]),
        float(metrics["positive_years"]),
        float(metrics.get("net_pnl_to_max_drawdown") or 0.0),
        -float(metrics["avg_hold_minutes"]),
        -float(result["rule_count"]),
    )


def sql_string(value: str) -> str:
    return value.replace("'", "''")


def json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


if __name__ == "__main__":
    raise SystemExit(main())
