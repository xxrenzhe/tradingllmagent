from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

import duckdb

from .bars import timeframe_minutes
from .config import CostModelConfig, SymbolConfig
from .storage import compute_data_version_hash, write_json
from .variants import stable_hash


DEFAULT_CLOSE_HORIZONS = (5, 15, 30, 60, 120)
DEFAULT_TP_SL_HORIZONS = (5, 15, 30, 60)
DEFAULT_TP_SL_PAIRS = (
    (0.5, 4),
    (1, 4),
    (1, 8),
    (2, 8),
    (2, 12),
    (3, 12),
    (4, 16),
    (6, 24),
)

OHLCV_FAMILY_HORIZONS = (5, 15, 30, 60, 120)


def _minute_horizon_specs(requested_minutes: Sequence[int], bar_minutes: int) -> list[tuple[int, int]]:
    specs = []
    for target_minutes in requested_minutes:
        horizon_bars = max(1, (target_minutes + bar_minutes - 1) // bar_minutes)
        specs.append((horizon_bars, horizon_bars * bar_minutes))
    return specs


def _horizon_specs(context: dict[str, Any], family: str) -> list[tuple[int, int]]:
    bar_minutes = int(context["timeframe_minutes"])
    if family == "close":
        return _minute_horizon_specs(DEFAULT_CLOSE_HORIZONS, bar_minutes)
    if family == "tp_sl":
        return _minute_horizon_specs(DEFAULT_TP_SL_HORIZONS, bar_minutes)
    if family == "ohlcv_family":
        return _minute_horizon_specs(OHLCV_FAMILY_HORIZONS, bar_minutes)
    raise ValueError(f"Unknown horizon family: {family}")


def _horizon_spec_dicts(context: dict[str, Any], family: str) -> list[dict[str, int]]:
    return [
        {
            "horizon_bars": horizon_bars,
            "horizon_minutes": horizon_minutes,
        }
        for horizon_bars, horizon_minutes in _horizon_specs(context, family)
    ]


def mine_databento_nq_profitable_strategies(
    *,
    data_root: Path,
    symbol_config: SymbolConfig,
    cost_model: CostModelConfig,
    date_from: date,
    date_to: date,
    timeframe: str = "1m",
    output_path: Path | None = None,
    min_annual_trades: float = 1000,
    min_win_probability: float = 0.53,
    max_candidates: int = 50,
) -> dict[str, Any]:
    bar_files = _bar_files(data_root, symbol_config.alias, timeframe, date_from, date_to)
    if not bar_files:
        raise ValueError(f"No {symbol_config.alias} {timeframe} bars found between {date_from} and {date_to}")

    bar_minutes = timeframe_minutes(timeframe)
    round_trip_cost_usd = cost_model.round_trip_fees_usd + 2 * cost_model.slippage_ticks_per_side * cost_model.tick_value
    con = duckdb.connect(":memory:")
    try:
        parquet_glob = _duckdb_glob(data_root, symbol_config.alias, timeframe)
        context = {
            "parquet_glob": parquet_glob,
            "date_from": date_from.isoformat(),
            "date_to_exclusive": (date_to + timedelta(days=1)).isoformat(),
            "point_value": float(symbol_config.point_value),
            "timeframe_minutes": bar_minutes,
            "continuity_tolerance_minutes": max(2, bar_minutes * 2),
            "min_annual_trades": float(min_annual_trades),
            "min_win_probability": float(min_win_probability),
            "max_candidates": int(max_candidates),
        }
        cost_adjusted = _run_close_to_close_scans(con, context, round_trip_cost_usd)
        cost_adjusted.extend(_run_tp_sl_scans(con, context, round_trip_cost_usd))
        cost_adjusted.extend(_run_ohlcv_family_scans(con, context, round_trip_cost_usd))
        gross_candidates = _run_tp_sl_scans(con, context, 0.0)
        gross_candidates.extend(_run_ohlcv_family_scans(con, context, 0.0))
        walk_forward = _run_walk_forward_stability_search(con, context, date_from, date_to, round_trip_cost_usd)
        regime_first = _run_regime_first_edge_search(con, context, round_trip_cost_usd)
        regime_basket_replays = _run_regime_basket_replays(con, context, regime_first, round_trip_cost_usd)
    finally:
        con.close()

    qualified = sorted(
        _dedupe_candidates(cost_adjusted),
        key=lambda row: (float(row.get("net_pnl", 0)), float(row.get("avg_pnl", 0))),
        reverse=True,
    )
    gross_only = []
    for row in _dedupe_candidates(gross_candidates):
        cost_adjusted_net = float(row["net_pnl"]) - float(row["trades"]) * round_trip_cost_usd
        if cost_adjusted_net <= 0:
            gross_only.append(
                {
                    **row,
                    "gross_net_pnl": row["net_pnl"],
                    "cost_adjusted_net_pnl": cost_adjusted_net,
                    "round_trip_cost_usd": round_trip_cost_usd,
                    "diagnosis": "gross edge does not survive configured round-trip cost",
                }
            )
    gross_only = sorted(gross_only, key=lambda row: float(row["gross_net_pnl"]), reverse=True)[:max_candidates]

    qualified_regime_basket_replays = [
        replay
        for replay in regime_basket_replays
        if replay["one_trade_per_timestamp"]["target_qualified"]
    ]
    optimized_regime_basket_subsets = [
        {
            "basket_id": replay["basket_id"],
            "basket_hash": replay["basket_hash"],
            **subset,
        }
        for replay in regime_basket_replays
        for subset in replay.get("optimized_subsets", [])
    ]
    optimized_regime_basket_subsets = sorted(
        optimized_regime_basket_subsets,
        key=lambda row: (
            float(row["one_trade_per_timestamp"].get("profit_factor") or 0),
            float(row["one_trade_per_timestamp"].get("net_pnl") or 0),
        ),
        reverse=True,
    )
    regime_basket_found = bool(regime_first.get("qualified_regime_baskets"))
    report = {
        "schema_version": 1,
        "artifact": "databento_nq_profit_strategy_mining_report",
        "status": (
            "target_found"
            if qualified
            else "executable_regime_basket_found"
            if qualified_regime_basket_replays
            else "regime_basket_found"
            if regime_basket_found
            else "not_found"
        ),
        "symbol": symbol_config.alias,
        "timeframe": timeframe,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "target": {
            "min_annual_trades": min_annual_trades,
            "min_win_probability": min_win_probability,
            "requires_net_pnl_positive": True,
            "cost_adjusted": True,
        },
        "cost_model": cost_model.to_dict(),
        "round_trip_cost_usd": round_trip_cost_usd,
        "timeframe_minutes": bar_minutes,
        "data_version_hash": compute_data_version_hash(
            bar_files,
            {
                "artifact": "databento_nq_profit_strategy_mining_report",
                "symbol": symbol_config.alias,
                "timeframe": timeframe,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
            },
        ),
        "search_space": {
            "close_to_close_horizons": list(DEFAULT_CLOSE_HORIZONS),
            "close_to_close_horizon_specs": _horizon_spec_dicts(context, "close"),
            "tp_sl_horizons": list(DEFAULT_TP_SL_HORIZONS),
            "tp_sl_horizon_specs": _horizon_spec_dicts(context, "tp_sl"),
            "tp_sl_pairs": [{"take_profit_points": tp, "stop_loss_points": stop} for tp, stop in DEFAULT_TP_SL_PAIRS],
            "feature_conditions": [
                "utc_time_bucket",
                "entry_phase_mod_horizon",
                "previous_1m_return_sign",
                "previous_5m_return_bin",
                "trend_vs_ma20_or_ma50",
                "relative_volume_bin",
                "range_regime",
                "breakout_20_previous_range",
                "close_zscore_50",
                "body_to_range",
                "intraday_opening_window_utc",
            ],
            "ohlcv_family_scans": [
                "breakout_continuation",
                "range_expansion_continuation",
                "trend_pullback_reclaim",
                "zscore_mean_reversion",
                "volume_climax_reversion",
                "low_volume_drift",
            ],
            "ohlcv_family_horizon_specs": _horizon_spec_dicts(context, "ohlcv_family"),
        },
        "summary": {
            "bar_file_count": len(bar_files),
            "qualified_candidate_count": len(qualified),
            "gross_only_candidate_count": len(gross_only),
            "walk_forward_window_count": len(walk_forward["windows"]),
            "walk_forward_stable_candidate_count": len(walk_forward["stable_candidates"]),
            "regime_first_candidate_count": len(regime_first["top_regime_edges"]),
            "qualified_regime_basket_count": len(regime_first.get("qualified_regime_baskets", [])),
            "qualified_regime_basket_replay_count": len(qualified_regime_basket_replays),
            "optimized_regime_basket_subset_count": len(optimized_regime_basket_subsets),
            "best_optimized_regime_basket_subset": optimized_regime_basket_subsets[0]
            if optimized_regime_basket_subsets
            else None,
            "best_cost_adjusted_net_pnl": qualified[0]["net_pnl"] if qualified else None,
            "best_gross_net_pnl": gross_only[0]["gross_net_pnl"] if gross_only else None,
            "best_regime_break_even_cost_usd": regime_first["top_regime_edges"][0]["break_even_cost_usd"]
            if regime_first["top_regime_edges"]
            else None,
        },
        "qualified_candidates": qualified[:max_candidates],
        "gross_only_candidates": gross_only,
        "walk_forward": walk_forward,
        "regime_first": regime_first,
        "regime_basket_replays": regime_basket_replays,
        "blocked_next_steps": [] if qualified or qualified_regime_basket_replays else [
            "No single scanned candidate met annual_trades > 1000, win_probability > 0.53, and cost-adjusted net_pnl > 0.",
            "Stay in OHLCV-only research mode: add walk-forward family search before trusting any in-sample candidate.",
            "Prefer session-normalized OHLCV features, volatility-regime splits, and simpler risk filters over higher-dimensional curve fitting.",
        ],
    }
    report["report_hash"] = stable_hash({key: value for key, value in report.items() if key != "report_hash"})
    if output_path is not None:
        write_json(output_path, report)
    return report


def _run_regime_first_edge_search(
    con: duckdb.DuckDBPyConnection,
    context: dict[str, Any],
    round_trip_cost_usd: float,
) -> dict[str, Any]:
    rows = []
    for horizon_bars, horizon_minutes in _horizon_specs(context, "ohlcv_family"):
        rows.extend(
            _fetch_dicts(
                con,
                f"""
                WITH raw AS (
                  SELECT timestamp, open, high, low, close, tick_count,
                         CAST(strftime(timestamp, '%H') AS INTEGER)*60 + CAST(strftime(timestamp, '%M') AS INTEGER) AS moday,
                         CAST(strftime(timestamp, '%w') AS INTEGER) AS dow,
                         close - lag(close, 1) OVER (ORDER BY timestamp) AS ret1,
                         close - lag(close, 5) OVER (ORDER BY timestamp) AS ret5,
                         avg(close) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS ma20,
                         avg(close) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS ma50,
                         stddev_pop(close) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS close_std50,
                         avg(tick_count) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS vol50,
                         avg(high-low) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS range20,
                         max(high) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS high20_prev,
                         min(low) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS low20_prev,
                         lead(close, {horizon_bars}) OVER (ORDER BY timestamp) AS future_close,
                         lead(timestamp, {horizon_bars}) OVER (ORDER BY timestamp) AS future_ts
                  FROM read_parquet('{context["parquet_glob"]}')
                  WHERE timestamp >= '{context["date_from"]}' AND timestamp < '{context["date_to_exclusive"]}'
                ), feats AS (
                  SELECT *,
                         CASE
                           WHEN moday BETWEEN 0 AND 359 THEN 'utc_0000_0559'
                           WHEN moday BETWEEN 360 AND 719 THEN 'utc_0600_1159'
                           WHEN moday BETWEEN 720 AND 1019 THEN 'utc_1200_1659'
                           WHEN moday BETWEEN 1020 AND 1259 THEN 'utc_1700_2059'
                           ELSE 'utc_2100_2359'
                         END AS session_bucket,
                         CASE WHEN close > ma50 THEN 1 ELSE -1 END AS trend_bin,
                         CASE WHEN tick_count >= vol50*2.0 THEN 3 WHEN tick_count >= vol50*1.5 THEN 2 WHEN tick_count >= vol50 THEN 1 WHEN tick_count < vol50*0.7 THEN -1 ELSE 0 END AS volume_bin,
                         CASE WHEN (high-low) >= range20*2.0 THEN 3 WHEN (high-low) >= range20*1.5 THEN 2 WHEN (high-low) >= range20 THEN 1 WHEN (high-low) < range20*0.7 THEN -1 ELSE 0 END AS range_bin,
                         CASE WHEN close_std50 > 0 THEN (close-ma50)/close_std50 ELSE 0 END AS z50,
                         CASE WHEN high > low THEN (close-open)/(high-low) ELSE 0 END AS body_to_range,
                         CASE WHEN close > high20_prev THEN 1 WHEN close < low20_prev THEN -1 ELSE 0 END AS breakout20
                  FROM raw
                  WHERE future_close IS NOT NULL AND ma20 IS NOT NULL AND ma50 IS NOT NULL AND close_std50 IS NOT NULL
                    AND vol50 IS NOT NULL AND range20 IS NOT NULL
                    AND (epoch(future_ts)-epoch(timestamp))/60.0 BETWEEN {horizon_minutes} AND {horizon_minutes + int(context["continuity_tolerance_minutes"])}
                ), signals AS (
                  SELECT 'breakout_continuation' AS scan_type, {horizon_minutes} AS horizon_minutes, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, 1 AS direction, future_close, close
                  FROM feats
                  WHERE breakout20 = 1 AND trend_bin = 1 AND volume_bin >= 1
                  UNION ALL
                  SELECT 'breakout_continuation', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, -1, future_close, close
                  FROM feats
                  WHERE breakout20 = -1 AND trend_bin = -1 AND volume_bin >= 1
                  UNION ALL
                  SELECT 'range_expansion_continuation', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, 1, future_close, close
                  FROM feats
                  WHERE body_to_range >= 0.6 AND range_bin >= 2 AND volume_bin >= 1
                  UNION ALL
                  SELECT 'range_expansion_continuation', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, -1, future_close, close
                  FROM feats
                  WHERE body_to_range <= -0.6 AND range_bin >= 2 AND volume_bin >= 1
                  UNION ALL
                  SELECT 'trend_pullback_reclaim', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, 1, future_close, close
                  FROM feats
                  WHERE trend_bin = 1 AND ret5 < 0 AND ret1 > 0 AND close > ma20
                  UNION ALL
                  SELECT 'trend_pullback_reclaim', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, -1, future_close, close
                  FROM feats
                  WHERE trend_bin = -1 AND ret5 > 0 AND ret1 < 0 AND close < ma20
                  UNION ALL
                  SELECT 'zscore_mean_reversion', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, -1, future_close, close
                  FROM feats
                  WHERE z50 >= 2 AND volume_bin <= 1
                  UNION ALL
                  SELECT 'zscore_mean_reversion', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, 1, future_close, close
                  FROM feats
                  WHERE z50 <= -2 AND volume_bin <= 1
                  UNION ALL
                  SELECT 'volume_climax_reversion', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, -1, future_close, close
                  FROM feats
                  WHERE z50 >= 1.5 AND volume_bin >= 2 AND abs(body_to_range) <= 0.35
                  UNION ALL
                  SELECT 'volume_climax_reversion', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, 1, future_close, close
                  FROM feats
                  WHERE z50 <= -1.5 AND volume_bin >= 2 AND abs(body_to_range) <= 0.35
                  UNION ALL
                  SELECT 'low_volume_drift', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, 1, future_close, close
                  FROM feats
                  WHERE trend_bin = 1 AND volume_bin = -1 AND ret1 > 0
                  UNION ALL
                  SELECT 'low_volume_drift', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, -1, future_close, close
                  FROM feats
                  WHERE trend_bin = -1 AND volume_bin = -1 AND ret1 < 0
                ), pnl AS (
                  SELECT scan_type, horizon_minutes, session_bucket, dow, trend_bin, volume_bin, range_bin, direction,
                         CASE WHEN direction = 1
                           THEN (future_close-close)*{context["point_value"]}
                           ELSE (close-future_close)*{context["point_value"]}
                         END AS gross_pnl
                  FROM signals
                )
                SELECT scan_type, horizon_minutes, session_bucket, dow, trend_bin, volume_bin, range_bin, direction,
                       count(*) AS trades,
                       count(*) / (
                         SELECT count(DISTINCT CAST(timestamp AS DATE))
                         FROM read_parquet('{context["parquet_glob"]}')
                         WHERE timestamp >= '{context["date_from"]}' AND timestamp < '{context["date_to_exclusive"]}'
                       ) * 365 AS annual_trades,
                       sum(gross_pnl) AS gross_net_pnl,
                       sum(gross_pnl) - count(*) * {round_trip_cost_usd} AS cost_adjusted_net_pnl,
                       avg(gross_pnl) AS gross_avg_pnl,
                       avg(gross_pnl) - {round_trip_cost_usd} AS cost_adjusted_avg_pnl,
                       avg(CASE WHEN gross_pnl > 0 THEN 1.0 ELSE 0.0 END) AS gross_win_probability,
                       avg(CASE WHEN gross_pnl - {round_trip_cost_usd} > 0 THEN 1.0 ELSE 0.0 END) AS cost_adjusted_win_probability,
                       sum(CASE WHEN gross_pnl > 0 THEN gross_pnl ELSE 0 END)/NULLIF(abs(sum(CASE WHEN gross_pnl < 0 THEN gross_pnl ELSE 0 END)), 0) AS gross_profit_factor,
                       sum(gross_pnl)/count(*) AS break_even_cost_usd
                FROM pnl
                GROUP BY ALL
                HAVING annual_trades > 100
                   AND trades >= 100
                   AND gross_net_pnl > 0
                ORDER BY break_even_cost_usd DESC
                LIMIT {context["max_candidates"]}
                """,
            )
        )
    annotated = []
    for row in rows:
        payload = {
            **row,
            "direction_label": "long" if int(row.get("direction") or 0) == 1 else "short",
            "round_trip_cost_usd": round_trip_cost_usd,
            "cost_survives": float(row.get("break_even_cost_usd") or 0) > round_trip_cost_usd,
        }
        payload["regime_edge_hash"] = stable_hash(payload)
        annotated.append(payload)
    annotated = sorted(
        annotated,
        key=lambda row: (float(row.get("break_even_cost_usd") or 0), float(row.get("gross_net_pnl") or 0)),
        reverse=True,
    )
    baskets = _build_regime_edge_baskets(
        annotated,
        min_annual_trades=float(context["min_annual_trades"]),
        min_win_probability=float(context["min_win_probability"]),
    )
    return {
        "mode": "gross_edge_by_ohlcv_regime_before_hard_target_filter",
        "minimum_annual_trades": 100,
        "round_trip_cost_usd": round_trip_cost_usd,
        "top_regime_edges": annotated[: int(context["max_candidates"])],
        "cost_surviving_edges": [row for row in annotated if row["cost_survives"]][: int(context["max_candidates"])],
        "qualified_regime_baskets": baskets,
    }


def _build_regime_edge_baskets(
    edges: Sequence[dict[str, Any]],
    *,
    min_annual_trades: float,
    min_win_probability: float,
) -> list[dict[str, Any]]:
    eligible = [
        edge
        for edge in edges
        if float(edge.get("cost_adjusted_net_pnl") or 0) > 0
        and float(edge.get("cost_adjusted_win_probability") or 0) > min_win_probability
    ]
    baskets = []
    for key_name in ("scan_type", "session_bucket", "direction_label"):
        for value in sorted({str(edge.get(key_name)) for edge in eligible}):
            basket = _regime_basket(
                f"{key_name}:{value}",
                [edge for edge in eligible if str(edge.get(key_name)) == value],
                min_annual_trades,
                min_win_probability,
            )
            if basket:
                baskets.append(basket)
    all_basket = _regime_basket("all_eligible_regime_edges", eligible, min_annual_trades, min_win_probability)
    if all_basket:
        baskets.append(all_basket)
    return sorted(
        baskets,
        key=lambda row: (float(row["cost_adjusted_net_pnl"]), float(row["annual_trades"])),
        reverse=True,
    )


def _regime_basket(
    basket_id: str,
    edges: Sequence[dict[str, Any]],
    min_annual_trades: float,
    min_win_probability: float,
) -> dict[str, Any] | None:
    if not edges:
        return None
    trades = sum(int(edge.get("trades") or 0) for edge in edges)
    if trades <= 0:
        return None
    annual_trades = sum(float(edge.get("annual_trades") or 0) for edge in edges)
    cost_adjusted_net_pnl = sum(float(edge.get("cost_adjusted_net_pnl") or 0) for edge in edges)
    weighted_win_probability = sum(
        float(edge.get("cost_adjusted_win_probability") or 0) * int(edge.get("trades") or 0)
        for edge in edges
    ) / trades
    if annual_trades <= min_annual_trades or weighted_win_probability <= min_win_probability or cost_adjusted_net_pnl <= 0:
        return None
    payload = {
        "basket_id": basket_id,
        "edge_count": len(edges),
        "trades": trades,
        "annual_trades": annual_trades,
        "cost_adjusted_net_pnl": cost_adjusted_net_pnl,
        "weighted_cost_adjusted_win_probability": weighted_win_probability,
        "avg_break_even_cost_usd": sum(float(edge.get("break_even_cost_usd") or 0) for edge in edges) / len(edges),
        "constituent_edges": [
            {
                "scan_type": edge.get("scan_type"),
                "horizon_minutes": edge.get("horizon_minutes"),
                "session_bucket": edge.get("session_bucket"),
                "dow": edge.get("dow"),
                "direction_label": edge.get("direction_label"),
                "trend_bin": edge.get("trend_bin"),
                "volume_bin": edge.get("volume_bin"),
                "range_bin": edge.get("range_bin"),
                "annual_trades": edge.get("annual_trades"),
                "cost_adjusted_net_pnl": edge.get("cost_adjusted_net_pnl"),
                "cost_adjusted_win_probability": edge.get("cost_adjusted_win_probability"),
                "break_even_cost_usd": edge.get("break_even_cost_usd"),
            }
            for edge in edges
        ],
        "caveat": "Basket metrics are aggregate regime-edge estimates; replay constituent rules before treating as one executable strategy.",
    }
    payload["basket_hash"] = stable_hash(payload)
    return payload


def _run_regime_basket_replays(
    con: duckdb.DuckDBPyConnection,
    context: dict[str, Any],
    regime_first: dict[str, Any],
    round_trip_cost_usd: float,
) -> list[dict[str, Any]]:
    baskets = regime_first.get("qualified_regime_baskets") or []
    return [
        _replay_regime_basket(con, context, basket, round_trip_cost_usd)
        for basket in baskets
    ]


def _replay_regime_basket(
    con: duckdb.DuckDBPyConnection,
    context: dict[str, Any],
    basket: dict[str, Any],
    round_trip_cost_usd: float,
) -> dict[str, Any]:
    signals = []
    edges = basket.get("constituent_edges") or []
    for horizon_minutes in sorted({int(edge["horizon_minutes"]) for edge in edges}):
        horizon_edges = [
            (index, edge)
            for index, edge in enumerate(edges)
            if int(edge["horizon_minutes"]) == horizon_minutes
        ]
        signals.extend(
            _replay_regime_edges_for_horizon(
                con,
                context,
                horizon_minutes=horizon_minutes,
                indexed_edges=horizon_edges,
                round_trip_cost_usd=round_trip_cost_usd,
            )
        )
    day_count = _trading_day_count(con, context)
    independent = _replay_metrics(signals, day_count, context)
    one_trade = _replay_metrics(_one_trade_per_timestamp(signals), day_count, context)
    payload = {
        "basket_id": basket.get("basket_id"),
        "basket_hash": basket.get("basket_hash"),
        "edge_count": len(edges),
        "round_trip_cost_usd": round_trip_cost_usd,
        "independent_rule_slots": independent,
        "one_trade_per_timestamp": one_trade,
        "optimized_subsets": _optimized_regime_basket_subsets(edges, signals, day_count, context),
        "replay_mode_caveat": (
            "independent_rule_slots allows each constituent edge to trade; "
            "one_trade_per_timestamp keeps the first constituent edge per bar to reduce overlap."
        ),
    }
    payload["replay_hash"] = stable_hash(payload)
    return payload


def _optimized_regime_basket_subsets(
    edges: Sequence[dict[str, Any]],
    signals: Sequence[dict[str, Any]],
    day_count: int,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    if not edges or not signals:
        return []
    edge_indexes = list(range(len(edges)))
    subset_specs: dict[tuple[int, ...], str] = {}
    for label, key in (
        ("top_break_even_cost", lambda index: float(edges[index].get("break_even_cost_usd") or 0)),
        ("top_win_probability", lambda index: float(edges[index].get("cost_adjusted_win_probability") or 0)),
        ("top_net_pnl", lambda index: float(edges[index].get("cost_adjusted_net_pnl") or 0)),
    ):
        ordered = sorted(edge_indexes, key=key, reverse=True)
        for size in range(1, len(ordered) + 1):
            subset_specs.setdefault(tuple(sorted(ordered[:size])), label)
    for scan_type in sorted({str(edge.get("scan_type")) for edge in edges}):
        subset = tuple(index for index in edge_indexes if str(edges[index].get("scan_type")) == scan_type)
        subset_specs.setdefault(subset, f"scan_type:{scan_type}")
    for session_bucket in sorted({str(edge.get("session_bucket")) for edge in edges}):
        subset = tuple(index for index in edge_indexes if str(edges[index].get("session_bucket")) == session_bucket)
        subset_specs.setdefault(subset, f"session_bucket:{session_bucket}")

    candidates = []
    for subset, selection_rule in subset_specs.items():
        subset_set = set(subset)
        subset_signals = [signal for signal in signals if int(signal["rule_index"]) in subset_set]
        one_trade = _one_trade_per_timestamp(subset_signals)
        metrics = _replay_metrics(one_trade, day_count, context)
        if not metrics["target_qualified"]:
            continue
        payload = {
            "selection_rule": selection_rule,
            "edge_indexes": list(subset),
            "edge_count": len(subset),
            "one_trade_per_timestamp": metrics,
            "constituent_edges": [_edge_identity(edges[index]) for index in subset],
        }
        payload["subset_hash"] = stable_hash(payload)
        candidates.append(payload)

    return sorted(
        candidates,
        key=lambda row: (
            float(row["one_trade_per_timestamp"].get("profit_factor") or 0),
            float(row["one_trade_per_timestamp"].get("net_pnl") or 0),
            float(row["one_trade_per_timestamp"].get("annual_trades") or 0),
        ),
        reverse=True,
    )[:20]


def _edge_identity(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "scan_type": edge.get("scan_type"),
        "horizon_minutes": edge.get("horizon_minutes"),
        "session_bucket": edge.get("session_bucket"),
        "dow": edge.get("dow"),
        "direction_label": edge.get("direction_label"),
        "trend_bin": edge.get("trend_bin"),
        "volume_bin": edge.get("volume_bin"),
        "range_bin": edge.get("range_bin"),
    }


def _one_trade_per_timestamp(signals: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_timestamp = {}
    for signal in sorted(signals, key=lambda row: (row["timestamp"], int(row["rule_index"]))):
        by_timestamp.setdefault(signal["timestamp"], signal)
    return list(by_timestamp.values())


def _replay_regime_edges_for_horizon(
    con: duckdb.DuckDBPyConnection,
    context: dict[str, Any],
    *,
    horizon_minutes: int,
    indexed_edges: Sequence[tuple[int, dict[str, Any]]],
    round_trip_cost_usd: float,
) -> list[dict[str, Any]]:
    if not indexed_edges:
        return []
    horizon_bars = max(1, (horizon_minutes + int(context["timeframe_minutes"]) - 1) // int(context["timeframe_minutes"]))
    values = ", ".join(
        "("
        f"{index}, "
        f"'{_sql_string(str(edge['scan_type']))}', "
        f"'{_sql_string(str(edge['session_bucket']))}', "
        f"{int(edge['dow'])}, "
        f"{int(edge['trend_bin'])}, "
        f"{int(edge['volume_bin'])}, "
        f"{int(edge['range_bin'])}, "
        f"{1 if edge['direction_label'] == 'long' else -1}"
        ")"
        for index, edge in indexed_edges
    )
    return _fetch_dicts(
        con,
        f"""
        WITH edges(rule_index, scan_type, session_bucket, dow, trend_bin, volume_bin, range_bin, direction) AS (
          VALUES {values}
        ), raw AS (
          SELECT timestamp, open, high, low, close, tick_count,
                 CAST(strftime(timestamp, '%H') AS INTEGER)*60 + CAST(strftime(timestamp, '%M') AS INTEGER) AS moday,
                 CAST(strftime(timestamp, '%w') AS INTEGER) AS dow,
                 close - lag(close, 1) OVER (ORDER BY timestamp) AS ret1,
                 close - lag(close, 5) OVER (ORDER BY timestamp) AS ret5,
                 avg(close) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS ma20,
                 avg(close) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS ma50,
                 stddev_pop(close) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS close_std50,
                 avg(tick_count) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS vol50,
                 avg(high-low) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS range20,
                 max(high) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS high20_prev,
                 min(low) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS low20_prev,
                 lead(close, {horizon_bars}) OVER (ORDER BY timestamp) AS future_close,
                 lead(timestamp, {horizon_bars}) OVER (ORDER BY timestamp) AS future_ts
          FROM read_parquet('{context["parquet_glob"]}')
          WHERE timestamp >= '{context["date_from"]}' AND timestamp < '{context["date_to_exclusive"]}'
        ), feats AS (
          SELECT *,
                 CASE
                   WHEN moday BETWEEN 0 AND 359 THEN 'utc_0000_0559'
                   WHEN moday BETWEEN 360 AND 719 THEN 'utc_0600_1159'
                   WHEN moday BETWEEN 720 AND 1019 THEN 'utc_1200_1659'
                   WHEN moday BETWEEN 1020 AND 1259 THEN 'utc_1700_2059'
                   ELSE 'utc_2100_2359'
                 END AS session_bucket,
                 CASE WHEN close > ma50 THEN 1 ELSE -1 END AS trend_bin,
                 CASE WHEN tick_count >= vol50*2.0 THEN 3 WHEN tick_count >= vol50*1.5 THEN 2 WHEN tick_count >= vol50 THEN 1 WHEN tick_count < vol50*0.7 THEN -1 ELSE 0 END AS volume_bin,
                 CASE WHEN (high-low) >= range20*2.0 THEN 3 WHEN (high-low) >= range20*1.5 THEN 2 WHEN (high-low) >= range20 THEN 1 WHEN (high-low) < range20*0.7 THEN -1 ELSE 0 END AS range_bin,
                 CASE WHEN close_std50 > 0 THEN (close-ma50)/close_std50 ELSE 0 END AS z50,
                 CASE WHEN high > low THEN (close-open)/(high-low) ELSE 0 END AS body_to_range,
                 CASE WHEN close > high20_prev THEN 1 WHEN close < low20_prev THEN -1 ELSE 0 END AS breakout20
          FROM raw
          WHERE future_close IS NOT NULL AND ma20 IS NOT NULL AND ma50 IS NOT NULL AND close_std50 IS NOT NULL
            AND vol50 IS NOT NULL AND range20 IS NOT NULL
            AND (epoch(future_ts)-epoch(timestamp))/60.0 BETWEEN {horizon_minutes} AND {horizon_minutes + int(context["continuity_tolerance_minutes"])}
        ), signals AS (
          SELECT timestamp, 'breakout_continuation' AS scan_type, session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1 AS direction, future_close, close
          FROM feats
          WHERE breakout20 = 1 AND trend_bin = 1 AND volume_bin >= 1
          UNION ALL
          SELECT timestamp, 'breakout_continuation', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE breakout20 = -1 AND trend_bin = -1 AND volume_bin >= 1
          UNION ALL
          SELECT timestamp, 'range_expansion_continuation', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1, future_close, close
          FROM feats
          WHERE body_to_range >= 0.6 AND range_bin >= 2 AND volume_bin >= 1
          UNION ALL
          SELECT timestamp, 'range_expansion_continuation', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE body_to_range <= -0.6 AND range_bin >= 2 AND volume_bin >= 1
          UNION ALL
          SELECT timestamp, 'trend_pullback_reclaim', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1, future_close, close
          FROM feats
          WHERE trend_bin = 1 AND ret5 < 0 AND ret1 > 0 AND close > ma20
          UNION ALL
          SELECT timestamp, 'trend_pullback_reclaim', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE trend_bin = -1 AND ret5 > 0 AND ret1 < 0 AND close < ma20
          UNION ALL
          SELECT timestamp, 'zscore_mean_reversion', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE z50 >= 2 AND volume_bin <= 1
          UNION ALL
          SELECT timestamp, 'zscore_mean_reversion', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1, future_close, close
          FROM feats
          WHERE z50 <= -2 AND volume_bin <= 1
          UNION ALL
          SELECT timestamp, 'volume_climax_reversion', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE z50 >= 1.5 AND volume_bin >= 2 AND abs(body_to_range) <= 0.35
          UNION ALL
          SELECT timestamp, 'volume_climax_reversion', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1, future_close, close
          FROM feats
          WHERE z50 <= -1.5 AND volume_bin >= 2 AND abs(body_to_range) <= 0.35
          UNION ALL
          SELECT timestamp, 'low_volume_drift', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1, future_close, close
          FROM feats
          WHERE trend_bin = 1 AND volume_bin = -1 AND ret1 > 0
          UNION ALL
          SELECT timestamp, 'low_volume_drift', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE trend_bin = -1 AND volume_bin = -1 AND ret1 < 0
        ), matched AS (
          SELECT s.timestamp, e.rule_index, {horizon_minutes} AS horizon_minutes,
                 CASE WHEN e.direction = 1
                   THEN (s.future_close-s.close)*{context["point_value"]} - {round_trip_cost_usd}
                   ELSE (s.close-s.future_close)*{context["point_value"]} - {round_trip_cost_usd}
                 END AS pnl
          FROM signals s
          JOIN edges e
            ON s.scan_type = e.scan_type
           AND s.session_bucket = e.session_bucket
           AND s.dow = e.dow
           AND s.trend_bin = e.trend_bin
           AND s.volume_bin = e.volume_bin
           AND s.range_bin = e.range_bin
           AND s.direction = e.direction
        )
        SELECT timestamp, rule_index, horizon_minutes, pnl
        FROM matched
        ORDER BY timestamp, rule_index
        """,
    )


def _replay_metrics(
    signals: Sequence[dict[str, Any]],
    day_count: int,
    context: dict[str, Any],
) -> dict[str, Any]:
    ordered_signals = sorted(signals, key=lambda signal: signal["timestamp"])
    trades = len(ordered_signals)
    net_pnl = sum(float(signal["pnl"]) for signal in ordered_signals)
    wins = [float(signal["pnl"]) for signal in ordered_signals if float(signal["pnl"]) > 0]
    losses = [float(signal["pnl"]) for signal in ordered_signals if float(signal["pnl"]) < 0]
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for signal in ordered_signals:
        equity += float(signal["pnl"])
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    annual_trades = trades / day_count * 365 if day_count else 0.0
    win_probability = len(wins) / trades if trades else 0.0
    profit_factor = sum(wins) / abs(sum(losses)) if losses else None
    return {
        "trades": trades,
        "annual_trades": annual_trades,
        "net_pnl": net_pnl,
        "win_probability": win_probability,
        "avg_pnl": net_pnl / trades if trades else None,
        "profit_factor": profit_factor,
        "max_drawdown": max_drawdown,
        "return_to_drawdown": net_pnl / max_drawdown if max_drawdown else None,
        "target_qualified": (
            annual_trades > float(context["min_annual_trades"])
            and win_probability > float(context["min_win_probability"])
            and net_pnl > 0
        ),
    }


def _trading_day_count(con: duckdb.DuckDBPyConnection, context: dict[str, Any]) -> int:
    return int(
        con.execute(
            f"""
            SELECT count(DISTINCT CAST(timestamp AS DATE))
            FROM read_parquet('{context["parquet_glob"]}')
            WHERE timestamp >= '{context["date_from"]}' AND timestamp < '{context["date_to_exclusive"]}'
            """
        ).fetchone()[0]
        or 0
    )


def _sql_string(value: str) -> str:
    return value.replace("'", "''")


def _run_all_candidate_scans(con: duckdb.DuckDBPyConnection, context: dict[str, Any], cost: float) -> list[dict[str, Any]]:
    candidates = _run_close_to_close_scans(con, context, cost)
    candidates.extend(_run_tp_sl_scans(con, context, cost))
    candidates.extend(_run_ohlcv_family_scans(con, context, cost))
    return _dedupe_candidates(candidates)


def _run_walk_forward_stability_search(
    con: duckdb.DuckDBPyConnection,
    base_context: dict[str, Any],
    date_from: date,
    date_to: date,
    cost: float,
    *,
    train_years: int = 8,
    test_years: int = 1,
    step_years: int = 8,
) -> dict[str, Any]:
    windows = _walk_forward_windows(date_from, date_to, train_years=train_years, test_years=test_years, step_years=step_years)
    rows = []
    stable = []
    for window in windows:
        train_context = {
            **base_context,
            "date_from": window["train_from"],
            "date_to_exclusive": _next_day_iso(date.fromisoformat(window["train_to"])),
            "max_candidates": 50,
        }
        test_context = {
            **base_context,
            "date_from": window["test_from"],
            "date_to_exclusive": _next_day_iso(date.fromisoformat(window["test_to"])),
            "max_candidates": 50,
        }
        train_candidates = _run_all_candidate_scans(con, train_context, cost)
        test_candidates = _run_all_candidate_scans(con, test_context, cost)
        test_by_rule = {candidate["rule_hash"]: candidate for candidate in test_candidates}
        matched = []
        for train_candidate in train_candidates:
            test_candidate = test_by_rule.get(train_candidate["rule_hash"])
            if not test_candidate:
                continue
            matched.append(
                {
                    "rule_hash": train_candidate["rule_hash"],
                    "rule": train_candidate["rule"],
                    "train": _candidate_metrics(train_candidate),
                    "test": _candidate_metrics(test_candidate),
                }
            )
        matched = sorted(matched, key=lambda row: row["test"]["net_pnl"], reverse=True)
        rows.append(
            {
                **window,
                "train_candidate_count": len(train_candidates),
                "test_candidate_count": len(test_candidates),
                "matched_stable_candidate_count": len(matched),
                "matched_stable_candidates": matched[:20],
            }
        )
        stable.extend({**candidate, "window": window} for candidate in matched)
    return {
        "mode": "train_window_candidate_must_independently_pass_next_test_window",
        "train_years": train_years,
        "test_years": test_years,
        "step_years": step_years,
        "windows": rows,
        "stable_candidates": stable[:50],
    }


def _run_close_to_close_scans(con: duckdb.DuckDBPyConnection, context: dict[str, Any], cost: float) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for horizon_bars, horizon_minutes in _horizon_specs(context, "close"):
        rows = _fetch_dicts(
            con,
            f"""
            WITH raw AS (
              SELECT timestamp, close, tick_count,
                     CAST(strftime(timestamp, '%H') AS INTEGER)*60 + CAST(strftime(timestamp, '%M') AS INTEGER) AS moday,
                     CAST(strftime(timestamp, '%w') AS INTEGER) AS dow,
                     close - lag(close, 1) OVER (ORDER BY timestamp) AS ret1,
                     close - lag(close, 5) OVER (ORDER BY timestamp) AS ret5,
                     avg(close) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS ma50,
                     avg(tick_count) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS vol50,
                     lead(close, {horizon_bars}) OVER (ORDER BY timestamp) AS future_close,
                     lead(timestamp, {horizon_bars}) OVER (ORDER BY timestamp) AS future_ts
              FROM read_parquet('{context["parquet_glob"]}')
              WHERE timestamp >= '{context["date_from"]}' AND timestamp < '{context["date_to_exclusive"]}'
            ), feats AS (
              SELECT *,
                     floor(moday/240)*240 AS bucket_start,
                     moday % {horizon_minutes} AS phase,
                     CASE WHEN ret1 > 0 THEN 1 WHEN ret1 < 0 THEN -1 ELSE 0 END AS ret1_sign,
                     CASE WHEN ret5 > 2 THEN 2 WHEN ret5 > 0 THEN 1 WHEN ret5 < -2 THEN -2 WHEN ret5 < 0 THEN -1 ELSE 0 END AS ret5_bin,
                     CASE WHEN close > ma50 THEN 1 ELSE -1 END AS trend_bin,
                     CASE WHEN tick_count >= vol50*1.5 THEN 2 WHEN tick_count >= vol50 THEN 1 WHEN tick_count < vol50*0.7 THEN -1 ELSE 0 END AS volume_bin
              FROM raw
              WHERE future_close IS NOT NULL AND ma50 IS NOT NULL AND vol50 IS NOT NULL
                AND (epoch(future_ts)-epoch(timestamp))/60.0 BETWEEN {horizon_minutes} AND {horizon_minutes + int(context["continuity_tolerance_minutes"])}
            ), pnl AS (
              SELECT 'close_to_close_feature_scan' AS scan_type, {horizon_minutes} AS horizon_minutes,
                     bucket_start, phase, dow, ret1_sign, ret5_bin, trend_bin, volume_bin, 0 AS range_bin,
                     NULL::DOUBLE AS take_profit_points, NULL::DOUBLE AS stop_loss_points,
                     1 AS direction, (future_close-close)*{context["point_value"]}-{cost} AS pnl
              FROM feats
              UNION ALL
              SELECT 'close_to_close_feature_scan', {horizon_minutes},
                     bucket_start, phase, dow, ret1_sign, ret5_bin, trend_bin, volume_bin, 0 AS range_bin,
                     NULL::DOUBLE, NULL::DOUBLE,
                     -1, (close-future_close)*{context["point_value"]}-{cost}
              FROM feats
            )
            {_candidate_select_sql(context)}
            """,
        )
        candidates.extend(_annotate_rows(rows, cost))
    return candidates


def _run_ohlcv_family_scans(con: duckdb.DuckDBPyConnection, context: dict[str, Any], cost: float) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for horizon_bars, horizon_minutes in _horizon_specs(context, "ohlcv_family"):
        rows = _fetch_dicts(
            con,
            f"""
            WITH raw AS (
              SELECT timestamp, open, high, low, close, tick_count,
                     CAST(strftime(timestamp, '%H') AS INTEGER)*60 + CAST(strftime(timestamp, '%M') AS INTEGER) AS moday,
                     CAST(strftime(timestamp, '%w') AS INTEGER) AS dow,
                     close - lag(close, 1) OVER (ORDER BY timestamp) AS ret1,
                     close - lag(close, 5) OVER (ORDER BY timestamp) AS ret5,
                     avg(close) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS ma20,
                     avg(close) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS ma50,
                     stddev_pop(close) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS close_std50,
                     avg(tick_count) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS vol20,
                     avg(tick_count) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS vol50,
                     avg(high-low) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS range20,
                     max(high) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS high20_prev,
                     min(low) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS low20_prev,
                     lead(close, {horizon_bars}) OVER (ORDER BY timestamp) AS future_close,
                     lead(timestamp, {horizon_bars}) OVER (ORDER BY timestamp) AS future_ts
              FROM read_parquet('{context["parquet_glob"]}')
              WHERE timestamp >= '{context["date_from"]}' AND timestamp < '{context["date_to_exclusive"]}'
            ), feats AS (
              SELECT *,
                     floor(moday/120)*120 AS bucket_start,
                     moday % {horizon_minutes} AS phase,
                     CASE WHEN close > ma50 THEN 1 ELSE -1 END AS trend_bin,
                     CASE WHEN tick_count >= vol50*2.0 THEN 3 WHEN tick_count >= vol50*1.5 THEN 2 WHEN tick_count >= vol50 THEN 1 WHEN tick_count < vol50*0.7 THEN -1 ELSE 0 END AS volume_bin,
                     CASE WHEN (high-low) >= range20*2.0 THEN 3 WHEN (high-low) >= range20*1.5 THEN 2 WHEN (high-low) >= range20 THEN 1 WHEN (high-low) < range20*0.7 THEN -1 ELSE 0 END AS range_bin,
                     CASE WHEN close_std50 > 0 THEN (close-ma50)/close_std50 ELSE 0 END AS z50,
                     CASE WHEN high > low THEN (close-open)/(high-low) ELSE 0 END AS body_to_range,
                     CASE WHEN close > high20_prev THEN 1 WHEN close < low20_prev THEN -1 ELSE 0 END AS breakout20
              FROM raw
              WHERE future_close IS NOT NULL AND ma20 IS NOT NULL AND ma50 IS NOT NULL AND close_std50 IS NOT NULL
                AND vol20 IS NOT NULL AND vol50 IS NOT NULL AND range20 IS NOT NULL
                AND (epoch(future_ts)-epoch(timestamp))/60.0 BETWEEN {horizon_minutes} AND {horizon_minutes + int(context["continuity_tolerance_minutes"])}
            ), signals AS (
              SELECT 'breakout_continuation' AS scan_type, {horizon_minutes} AS horizon_minutes, bucket_start, phase, dow,
                     trend_bin, volume_bin, range_bin, 1 AS direction, future_close, close
              FROM feats
              WHERE breakout20 = 1 AND trend_bin = 1 AND volume_bin >= 1
              UNION ALL
              SELECT 'breakout_continuation', {horizon_minutes}, bucket_start, phase, dow,
                     trend_bin, volume_bin, range_bin, -1, future_close, close
              FROM feats
              WHERE breakout20 = -1 AND trend_bin = -1 AND volume_bin >= 1
              UNION ALL
              SELECT 'range_expansion_continuation', {horizon_minutes}, bucket_start, phase, dow,
                     trend_bin, volume_bin, range_bin, 1, future_close, close
              FROM feats
              WHERE body_to_range >= 0.6 AND range_bin >= 2 AND volume_bin >= 1
              UNION ALL
              SELECT 'range_expansion_continuation', {horizon_minutes}, bucket_start, phase, dow,
                     trend_bin, volume_bin, range_bin, -1, future_close, close
              FROM feats
              WHERE body_to_range <= -0.6 AND range_bin >= 2 AND volume_bin >= 1
              UNION ALL
              SELECT 'trend_pullback_reclaim', {horizon_minutes}, bucket_start, phase, dow,
                     trend_bin, volume_bin, range_bin, 1, future_close, close
              FROM feats
              WHERE trend_bin = 1 AND ret5 < 0 AND ret1 > 0 AND close > ma20
              UNION ALL
              SELECT 'trend_pullback_reclaim', {horizon_minutes}, bucket_start, phase, dow,
                     trend_bin, volume_bin, range_bin, -1, future_close, close
              FROM feats
              WHERE trend_bin = -1 AND ret5 > 0 AND ret1 < 0 AND close < ma20
              UNION ALL
              SELECT 'zscore_mean_reversion', {horizon_minutes}, bucket_start, phase, dow,
                     trend_bin, volume_bin, range_bin, -1, future_close, close
              FROM feats
              WHERE z50 >= 2 AND volume_bin <= 1
              UNION ALL
              SELECT 'zscore_mean_reversion', {horizon_minutes}, bucket_start, phase, dow,
                     trend_bin, volume_bin, range_bin, 1, future_close, close
              FROM feats
              WHERE z50 <= -2 AND volume_bin <= 1
              UNION ALL
              SELECT 'volume_climax_reversion', {horizon_minutes}, bucket_start, phase, dow,
                     trend_bin, volume_bin, range_bin, -1, future_close, close
              FROM feats
              WHERE z50 >= 1.5 AND volume_bin >= 2 AND abs(body_to_range) <= 0.35
              UNION ALL
              SELECT 'volume_climax_reversion', {horizon_minutes}, bucket_start, phase, dow,
                     trend_bin, volume_bin, range_bin, 1, future_close, close
              FROM feats
              WHERE z50 <= -1.5 AND volume_bin >= 2 AND abs(body_to_range) <= 0.35
              UNION ALL
              SELECT 'low_volume_drift', {horizon_minutes}, bucket_start, phase, dow,
                     trend_bin, volume_bin, range_bin, 1, future_close, close
              FROM feats
              WHERE trend_bin = 1 AND volume_bin = -1 AND ret1 > 0
              UNION ALL
              SELECT 'low_volume_drift', {horizon_minutes}, bucket_start, phase, dow,
                     trend_bin, volume_bin, range_bin, -1, future_close, close
              FROM feats
              WHERE trend_bin = -1 AND volume_bin = -1 AND ret1 < 0
            ), pnl AS (
              SELECT scan_type, horizon_minutes, bucket_start, phase, dow,
                     NULL::INTEGER AS ret1_sign, NULL::INTEGER AS ret5_bin,
                     trend_bin, volume_bin, range_bin,
                     NULL::DOUBLE AS take_profit_points, NULL::DOUBLE AS stop_loss_points,
                     direction,
                     CASE WHEN direction = 1
                       THEN (future_close-close)*{context["point_value"]}-{cost}
                       ELSE (close-future_close)*{context["point_value"]}-{cost}
                     END AS pnl
              FROM signals
            )
            {_candidate_select_sql(context)}
            """,
        )
        candidates.extend(_annotate_rows(rows, cost))
    return candidates


def _run_tp_sl_scans(con: duckdb.DuckDBPyConnection, context: dict[str, Any], cost: float) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    values = ",".join(f"({tp},{stop})" for tp, stop in DEFAULT_TP_SL_PAIRS)
    for horizon_bars, horizon_minutes in _horizon_specs(context, "tp_sl"):
        rows = _fetch_dicts(
            con,
            f"""
            WITH params(take_profit_points, stop_loss_points) AS (VALUES {values}),
            raw AS (
              SELECT timestamp, high, low, close,
                     CAST(strftime(timestamp, '%H') AS INTEGER)*60 + CAST(strftime(timestamp, '%M') AS INTEGER) AS moday,
                     CAST(strftime(timestamp, '%w') AS INTEGER) AS dow,
                     close - lag(close, 1) OVER (ORDER BY timestamp) AS ret1,
                     avg(close) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS ma20,
                     lead(close, {horizon_bars}) OVER (ORDER BY timestamp) AS future_close,
                     lead(timestamp, {horizon_bars}) OVER (ORDER BY timestamp) AS future_ts,
                     max(high) OVER (ORDER BY timestamp ROWS BETWEEN 1 FOLLOWING AND {horizon_bars} FOLLOWING) AS fwd_high,
                     min(low) OVER (ORDER BY timestamp ROWS BETWEEN 1 FOLLOWING AND {horizon_bars} FOLLOWING) AS fwd_low
              FROM read_parquet('{context["parquet_glob"]}')
              WHERE timestamp >= '{context["date_from"]}' AND timestamp < '{context["date_to_exclusive"]}'
            ), feats AS (
              SELECT *,
                     floor(moday/240)*240 AS bucket_start,
                     moday % {horizon_minutes} AS phase,
                     CASE WHEN ret1 > 0 THEN 1 WHEN ret1 < 0 THEN -1 ELSE 0 END AS ret1_sign,
                     0 AS ret5_bin,
                     CASE WHEN close > ma20 THEN 1 ELSE -1 END AS trend_bin,
                     0 AS volume_bin,
                     0 AS range_bin
              FROM raw
              WHERE future_close IS NOT NULL AND ma20 IS NOT NULL
                AND (epoch(future_ts)-epoch(timestamp))/60.0 BETWEEN {horizon_minutes} AND {horizon_minutes + int(context["continuity_tolerance_minutes"])}
            ), pnl AS (
              SELECT 'tp_sl_feature_scan' AS scan_type, {horizon_minutes} AS horizon_minutes,
                     bucket_start, phase, dow, ret1_sign, ret5_bin, trend_bin, volume_bin, range_bin,
                     p.take_profit_points, p.stop_loss_points, 1 AS direction,
                     CASE
                       WHEN fwd_low <= close-p.stop_loss_points THEN -p.stop_loss_points*{context["point_value"]}-{cost}
                       WHEN fwd_high >= close+p.take_profit_points THEN p.take_profit_points*{context["point_value"]}-{cost}
                       ELSE (future_close-close)*{context["point_value"]}-{cost}
                     END AS pnl
              FROM feats CROSS JOIN params p
              UNION ALL
              SELECT 'tp_sl_feature_scan', {horizon_minutes},
                     bucket_start, phase, dow, ret1_sign, ret5_bin, trend_bin, volume_bin, range_bin,
                     p.take_profit_points, p.stop_loss_points, -1,
                     CASE
                       WHEN fwd_high >= close+p.stop_loss_points THEN -p.stop_loss_points*{context["point_value"]}-{cost}
                       WHEN fwd_low <= close-p.take_profit_points THEN p.take_profit_points*{context["point_value"]}-{cost}
                       ELSE (close-future_close)*{context["point_value"]}-{cost}
                     END
              FROM feats CROSS JOIN params p
            )
            {_candidate_select_sql(context)}
            """,
        )
        candidates.extend(_annotate_rows(rows, cost))
    return candidates


def _candidate_select_sql(context: dict[str, Any]) -> str:
    return f"""
    SELECT scan_type, horizon_minutes, bucket_start, phase, dow, ret1_sign, ret5_bin, trend_bin, volume_bin, range_bin,
           take_profit_points, stop_loss_points, direction,
           count(*) AS trades,
           count(*) / (
             SELECT count(DISTINCT CAST(timestamp AS DATE))
             FROM read_parquet('{context["parquet_glob"]}')
             WHERE timestamp >= '{context["date_from"]}' AND timestamp < '{context["date_to_exclusive"]}'
           ) * 365 AS annual_trades,
           sum(pnl) AS net_pnl,
           avg(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_probability,
           avg(pnl) AS avg_pnl,
           sum(CASE WHEN pnl > 0 THEN pnl ELSE 0 END)/NULLIF(abs(sum(CASE WHEN pnl < 0 THEN pnl ELSE 0 END)), 0) AS profit_factor
    FROM pnl
    GROUP BY ALL
    HAVING annual_trades > {context["min_annual_trades"]}
       AND win_probability > {context["min_win_probability"]}
       AND net_pnl > 0
    ORDER BY net_pnl DESC
    LIMIT {context["max_candidates"]}
    """


def _fetch_dicts(con: duckdb.DuckDBPyConnection, query: str) -> list[dict[str, Any]]:
    rows = con.execute(query).fetchall()
    columns = [column[0] for column in con.description]
    return [
        {column: _json_scalar(value) for column, value in zip(columns, row)}
        for row in rows
    ]


def _annotate_rows(rows: Sequence[dict[str, Any]], cost: float) -> list[dict[str, Any]]:
    annotated = []
    for row in rows:
        rule = _candidate_rule(row)
        payload = {
            **row,
            "round_trip_cost_usd": cost,
            "rule": rule,
            "rule_hash": stable_hash(rule),
        }
        payload["candidate_hash"] = stable_hash(payload)
        annotated.append(payload)
    return annotated


def _candidate_rule(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "scan_type": row.get("scan_type"),
        "direction": "long" if int(row.get("direction") or 0) == 1 else "short",
        "entry_filter": {
            "utc_bucket_start_minute": row.get("bucket_start"),
            "phase_mod_horizon": row.get("phase"),
            "day_of_week": row.get("dow"),
            "ret1_sign": row.get("ret1_sign"),
            "ret5_bin": row.get("ret5_bin"),
            "trend_bin": row.get("trend_bin"),
            "volume_bin": row.get("volume_bin"),
            "range_bin": row.get("range_bin"),
        },
        "exit": {
            "horizon_minutes": row.get("horizon_minutes"),
            "take_profit_points": row.get("take_profit_points"),
            "stop_loss_points": row.get("stop_loss_points"),
        },
    }


def _dedupe_candidates(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_hash = {}
    for row in rows:
        by_hash.setdefault(row["candidate_hash"], row)
    return list(by_hash.values())


def _candidate_metrics(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "trades": row.get("trades"),
        "annual_trades": row.get("annual_trades"),
        "net_pnl": row.get("net_pnl"),
        "win_probability": row.get("win_probability"),
        "avg_pnl": row.get("avg_pnl"),
        "profit_factor": row.get("profit_factor"),
        "round_trip_cost_usd": row.get("round_trip_cost_usd"),
    }


def _walk_forward_windows(
    date_from: date,
    date_to: date,
    *,
    train_years: int,
    test_years: int,
    step_years: int,
) -> list[dict[str, str]]:
    windows = []
    start_year = date_from.year
    while True:
        train_from = max(date_from, date(start_year, 1, 1))
        train_to = min(date(start_year + train_years - 1, 12, 31), date_to)
        test_from = date(start_year + train_years, 1, 1)
        test_to = min(date(start_year + train_years + test_years - 1, 12, 31), date_to)
        if test_from > date_to or train_to <= train_from:
            break
        windows.append(
            {
                "train_from": train_from.isoformat(),
                "train_to": train_to.isoformat(),
                "test_from": test_from.isoformat(),
                "test_to": test_to.isoformat(),
            }
        )
        start_year += step_years
    return windows


def _next_day_iso(value: date) -> str:
    return (value + timedelta(days=1)).isoformat()


def _bar_files(data_root: Path, symbol: str, timeframe: str, date_from: date, date_to: date) -> list[Path]:
    root = data_root / "bars" / timeframe / symbol
    files = []
    current = date_from
    while current <= date_to:
        day_dir = root / f"date={current.isoformat()}"
        files.extend(sorted(day_dir.glob("*.parquet")))
        current += timedelta(days=1)
    return files


def _duckdb_glob(data_root: Path, symbol: str, timeframe: str) -> str:
    return str(data_root / "bars" / timeframe / symbol / "date=*" / "*.parquet").replace("'", "''")


def _json_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value
