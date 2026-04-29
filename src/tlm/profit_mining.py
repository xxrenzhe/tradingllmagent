from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
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
REGIME_REPLAY_BASKET_LIMIT = 6
REGIME_REPLAY_EDGE_LIMIT = 12


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
            "cost_stress_usd_per_trade": [
                {"label": "configured_cost", "additional_round_trip_ticks": 0, "extra_usd_per_trade": 0.0},
                {
                    "label": "configured_cost_plus_1_tick",
                    "additional_round_trip_ticks": 1,
                    "extra_usd_per_trade": float(cost_model.tick_value),
                },
                {
                    "label": "configured_cost_plus_2_ticks",
                    "additional_round_trip_ticks": 2,
                    "extra_usd_per_trade": float(cost_model.tick_value) * 2.0,
                },
            ],
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
    optimized_regime_basket_subsets = _dedupe_payloads_by_hash(optimized_regime_basket_subsets, "subset_hash")
    optimized_regime_basket_subsets = sorted(
        optimized_regime_basket_subsets,
        key=lambda row: (
            float(row["one_trade_per_timestamp"].get("profit_factor") or 0),
            float(row["one_trade_per_timestamp"].get("net_pnl") or 0),
        ),
        reverse=True,
    )
    adaptive_recent_regime_candidates = [
        {
            "basket_id": replay["basket_id"],
            "basket_hash": replay["basket_hash"],
            **candidate,
        }
        for replay in regime_basket_replays
        for candidate in replay.get("adaptive_recent_regime_candidates", [])
    ]
    adaptive_recent_regime_candidates = _dedupe_payloads_by_hash(adaptive_recent_regime_candidates, "candidate_hash")
    adaptive_recent_regime_candidates = sorted(
        adaptive_recent_regime_candidates,
        key=lambda row: (
            _profit_factor_score(row["test"].get("profit_factor")),
            float(row["test"].get("net_pnl") or 0),
            _profit_factor_score(row["full_after_activation"].get("profit_factor")),
        ),
        reverse=True,
    )
    yearly_profitable_candidates = [
        {
            "basket_id": replay["basket_id"],
            "basket_hash": replay["basket_hash"],
            **candidate,
        }
        for replay in regime_basket_replays
        for candidate in replay.get("yearly_profitable_candidates", [])
    ]
    yearly_profitable_candidates = _dedupe_payloads_by_hash(yearly_profitable_candidates, "candidate_hash")
    yearly_profitable_candidates = sorted(
        yearly_profitable_candidates,
        key=lambda row: (
            float(row["full_after_activation"].get("net_pnl") or 0),
            float(row["test"].get("net_pnl") or 0),
            _profit_factor_score(row["full_after_activation"].get("profit_factor")),
        ),
        reverse=True,
    )
    full_history_yearly_profitable_candidates = [
        candidate
        for candidate in yearly_profitable_candidates
        if candidate.get("full_history_candidate")
    ]
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
        "evaluation_periods": _evaluation_periods(date_from, date_to, walk_forward),
        "target": {
            "min_annual_trades": min_annual_trades,
            "min_win_probability": min_win_probability,
            "requires_net_pnl_positive": True,
            "cost_adjusted": True,
        },
        "cost_model": cost_model.to_dict(),
        "round_trip_cost_usd": round_trip_cost_usd,
        "timeframe_minutes": bar_minutes,
        "data_semantics": _ohlcv_data_semantics(symbol_config.alias, timeframe),
        "plan12_review": _plan12_review_status(),
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
                "bar_volume_alias_tick_count",
                "range_regime",
                "breakout_20_previous_range",
                "close_zscore_50",
                "body_to_range",
                "intraday_opening_window_utc",
            ],
            "ohlcv_family_scans": [
                "breakout_continuation",
                "failed_breakout_reversion",
                "opening_range_breakout",
                "range_expansion_continuation",
                "outside_bar_momentum",
                "session_extreme_reversion",
                "trend_pullback_reclaim",
                "vwap_reclaim_continuation",
                "zscore_mean_reversion",
                "volume_climax_reversion",
                "low_volume_drift",
            ],
            "ohlcv_family_horizon_specs": _horizon_spec_dicts(context, "ohlcv_family"),
            "volume_feature_status": {
                "implemented": [
                    "bar_volume=tick_count",
                    "relative_volume_20_or_50",
                    "volume_bin",
                    "volume_price_confirm",
                    "volume_climax_reversion",
                    "low_volume_filter",
                ],
                "not_available_from_ohlcv_only": [
                    "bid_ask_spread",
                    "limit_order_fill_rate",
                    "queue_position",
                    "adverse_selection_from_top_of_book",
                ],
                "next_ohlcv_expansion": [
                    "cross_timeframe_confirmation_1m_5m_15m",
                    "session_normalized_volume_percentile",
                    "event_window_volume_attribution",
                ],
            },
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
            "adaptive_recent_regime_candidate_count": len(adaptive_recent_regime_candidates),
            "best_adaptive_recent_regime_candidate": adaptive_recent_regime_candidates[0]
            if adaptive_recent_regime_candidates
            else None,
            "yearly_profitable_candidate_count": len(yearly_profitable_candidates),
            "best_yearly_profitable_candidate": yearly_profitable_candidates[0]
            if yearly_profitable_candidates
            else None,
            "full_history_yearly_profitable_candidate_count": len(full_history_yearly_profitable_candidates),
            "best_full_history_yearly_profitable_candidate": full_history_yearly_profitable_candidates[0]
            if full_history_yearly_profitable_candidates
            else None,
            "earliest_yearly_profitable_activation_year": min(
                int(candidate["activation_start_year"]) for candidate in yearly_profitable_candidates
            )
            if yearly_profitable_candidates
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
    _ensure_regime_feature_table(con, context)
    rows = []
    for horizon_bars, horizon_minutes in _horizon_specs(context, "ohlcv_family"):
        rows.extend(
            _fetch_dicts(
                con,
                f"""
                WITH raw AS (
                  SELECT *,
                         lead(close, {horizon_bars}) OVER (ORDER BY timestamp) AS future_close,
                         lead(timestamp, {horizon_bars}) OVER (ORDER BY timestamp) AS future_ts
                  FROM regime_feature_base
                ), feats AS (
                  SELECT *,
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
                  SELECT 'opening_range_breakout', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, 1, future_close, close
                  FROM feats
                  WHERE opening_high_prev IS NOT NULL AND moday >= 840 AND close > opening_high_prev
                    AND trend_bin = 1 AND volume_bin >= 1
                  UNION ALL
                  SELECT 'opening_range_breakout', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, -1, future_close, close
                  FROM feats
                  WHERE opening_low_prev IS NOT NULL AND moday >= 840 AND close < opening_low_prev
                    AND trend_bin = -1 AND volume_bin >= 1
                  UNION ALL
                  SELECT 'failed_breakout_reversion', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, -1, future_close, close
                  FROM feats
                  WHERE high > high20_prev AND close < high20_prev AND z50 >= 1.0 AND abs(body_to_range) <= 0.45
                  UNION ALL
                  SELECT 'failed_breakout_reversion', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, 1, future_close, close
                  FROM feats
                  WHERE low < low20_prev AND close > low20_prev AND z50 <= -1.0 AND abs(body_to_range) <= 0.45
                  UNION ALL
                  SELECT 'vwap_reclaim_continuation', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, 1, future_close, close
                  FROM feats
                  WHERE prev_vwap_dist < 0 AND vwap_dist >= 0 AND trend_bin = 1 AND volume_bin >= 0 AND ret1 > 0
                  UNION ALL
                  SELECT 'vwap_reclaim_continuation', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, -1, future_close, close
                  FROM feats
                  WHERE prev_vwap_dist > 0 AND vwap_dist <= 0 AND trend_bin = -1 AND volume_bin >= 0 AND ret1 < 0
                  UNION ALL
                  SELECT 'outside_bar_momentum', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, 1, future_close, close
                  FROM feats
                  WHERE prev_high IS NOT NULL AND high >= prev_high AND low <= prev_low
                    AND body_to_range >= 0.55 AND close > open AND volume_bin >= 1
                  UNION ALL
                  SELECT 'outside_bar_momentum', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, -1, future_close, close
                  FROM feats
                  WHERE prev_low IS NOT NULL AND high >= prev_high AND low <= prev_low
                    AND body_to_range <= -0.55 AND close < open AND volume_bin >= 1
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
                  SELECT 'session_extreme_reversion', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, -1, future_close, close
                  FROM feats
                  WHERE session_high_prev IS NOT NULL AND close >= session_high_prev - range20*0.1
                    AND z50 >= 1.25 AND volume_bin <= 1 AND abs(body_to_range) <= 0.45
                  UNION ALL
                  SELECT 'session_extreme_reversion', {horizon_minutes}, session_bucket, dow,
                         trend_bin, volume_bin, range_bin, 1, future_close, close
                  FROM feats
                  WHERE session_low_prev IS NOT NULL AND close <= session_low_prev + range20*0.1
                    AND z50 <= -1.25 AND volume_bin <= 1 AND abs(body_to_range) <= 0.45
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
    for key_names in (
        ("scan_type",),
        ("session_bucket",),
        ("direction_label",),
        ("horizon_minutes",),
        ("dow",),
        ("scan_type", "session_bucket"),
        ("scan_type", "direction_label"),
        ("scan_type", "horizon_minutes"),
        ("scan_type", "dow"),
        ("session_bucket", "direction_label"),
        ("session_bucket", "dow"),
        ("scan_type", "session_bucket", "dow"),
    ):
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for edge in eligible:
            key = tuple(str(edge.get(key_name)) for key_name in key_names)
            groups.setdefault(key, []).append(edge)
        for key, group_edges in sorted(groups.items()):
            key_label = ",".join(f"{key_name}:{value}" for key_name, value in zip(key_names, key))
            basket = _regime_basket(
                key_label,
                group_edges,
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


def _dedupe_payloads_by_hash(rows: Sequence[dict[str, Any]], hash_key: str) -> list[dict[str, Any]]:
    by_hash = {}
    for row in rows:
        row_hash = row.get(hash_key)
        if row_hash is None:
            by_hash[id(row)] = row
        else:
            by_hash.setdefault(row_hash, row)
    return list(by_hash.values())


def _ensure_regime_feature_table(con: duckdb.DuckDBPyConnection, context: dict[str, Any]) -> None:
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE regime_feature_base AS
        WITH raw AS (
          SELECT timestamp, open, high, low, close, tick_count,
                 CAST(timestamp AS DATE) AS bar_date,
                 CAST(strftime(timestamp, '%H') AS INTEGER)*60 + CAST(strftime(timestamp, '%M') AS INTEGER) AS moday,
                 CAST(strftime(timestamp, '%w') AS INTEGER) AS dow,
                 close - lag(close, 1) OVER (ORDER BY timestamp) AS ret1,
                 close - lag(close, 5) OVER (ORDER BY timestamp) AS ret5,
                 lag(high, 1) OVER (ORDER BY timestamp) AS prev_high,
                 lag(low, 1) OVER (ORDER BY timestamp) AS prev_low,
                 avg(close) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS ma20,
                 avg(close) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS ma50,
                 stddev_pop(close) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS close_std50,
                 avg(tick_count) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS vol50,
                 avg(high-low) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS range20,
                 max(high) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS high20_prev,
                 min(low) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS low20_prev,
                 sum(((high+low+close)/3.0)*greatest(tick_count, 1)) OVER (
                   PARTITION BY CAST(timestamp AS DATE)
                   ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                 ) / NULLIF(sum(greatest(tick_count, 1)) OVER (
                   PARTITION BY CAST(timestamp AS DATE)
                   ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                 ), 0) AS session_vwap,
                 max(high) OVER (
                   PARTITION BY CAST(timestamp AS DATE)
                   ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                 ) AS session_high_prev,
                 min(low) OVER (
                   PARTITION BY CAST(timestamp AS DATE)
                   ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                 ) AS session_low_prev,
                 max(CASE WHEN (CAST(strftime(timestamp, '%H') AS INTEGER)*60 + CAST(strftime(timestamp, '%M') AS INTEGER)) BETWEEN 810 AND 839 THEN high END) OVER (
                   PARTITION BY CAST(timestamp AS DATE)
                   ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                 ) AS opening_high_prev,
                 min(CASE WHEN (CAST(strftime(timestamp, '%H') AS INTEGER)*60 + CAST(strftime(timestamp, '%M') AS INTEGER)) BETWEEN 810 AND 839 THEN low END) OVER (
                   PARTITION BY CAST(timestamp AS DATE)
                   ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                 ) AS opening_low_prev
          FROM read_parquet('{context["parquet_glob"]}')
          WHERE timestamp >= '{context["date_from"]}' AND timestamp < '{context["date_to_exclusive"]}'
        )
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
               close - session_vwap AS vwap_dist,
               lag(close - session_vwap, 1) OVER (ORDER BY timestamp) AS prev_vwap_dist,
               CASE WHEN high > low THEN (close-open)/(high-low) ELSE 0 END AS body_to_range
        FROM raw
        WHERE ma20 IS NOT NULL AND ma50 IS NOT NULL AND close_std50 IS NOT NULL
          AND vol50 IS NOT NULL AND range20 IS NOT NULL
        """
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
    baskets = (regime_first.get("qualified_regime_baskets") or [])[:REGIME_REPLAY_BASKET_LIMIT]
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
    edges = _rank_regime_edges_for_replay(basket.get("constituent_edges") or [])[:REGIME_REPLAY_EDGE_LIMIT]
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
        "adaptive_recent_regime_candidates": _adaptive_recent_regime_candidates(edges, signals, context),
        "yearly_profitable_candidates": _yearly_profitable_candidates(edges, signals, context),
        "replay_mode_caveat": (
            "independent_rule_slots allows each constituent edge to trade; "
            "one_trade_per_timestamp keeps the first constituent edge per bar to reduce overlap."
        ),
    }
    payload["replay_hash"] = stable_hash(payload)
    return payload


def _rank_regime_edges_for_replay(edges: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        edges,
        key=lambda edge: (
            float(edge.get("break_even_cost_usd") or 0.0),
            float(edge.get("cost_adjusted_net_pnl") or 0.0),
            float(edge.get("cost_adjusted_win_probability") or 0.0),
        ),
        reverse=True,
    )


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


def _adaptive_recent_regime_candidates(
    edges: Sequence[dict[str, Any]],
    signals: Sequence[dict[str, Any]],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    if not edges or not signals:
        return []
    years = sorted({_signal_timestamp(signal).year for signal in signals})
    if len(years) < 4:
        return []
    test_start_year = max(years) - 2
    if test_start_year <= min(years):
        return []

    subset_specs = _adaptive_subset_specs(edges)
    candidates = []
    for subset, selection_rule in subset_specs.items():
        subset_set = set(subset)
        subset_signals = [signal for signal in signals if int(signal["rule_index"]) in subset_set]
        for activation_start_year in years:
            if activation_start_year >= test_start_year:
                continue
            active_signals = [
                signal
                for signal in subset_signals
                if _signal_timestamp(signal).year >= activation_start_year
            ]
            train_signals = [
                signal
                for signal in active_signals
                if _signal_timestamp(signal).year < test_start_year
            ]
            test_signals = [
                signal
                for signal in active_signals
                if _signal_timestamp(signal).year >= test_start_year
            ]
            train_one_trade = _one_trade_per_timestamp(train_signals)
            test_one_trade = _one_trade_per_timestamp(test_signals)
            full_one_trade = _one_trade_per_timestamp(active_signals)
            if not train_one_trade or not test_one_trade:
                continue
            train_metrics = _period_replay_metrics(train_one_trade, context)
            test_metrics = _period_replay_metrics(test_one_trade, context)
            full_metrics = _period_replay_metrics(full_one_trade, context)
            if not _passes_open_regime_filter(train_metrics, test_metrics, full_metrics, context):
                continue
            payload = {
                "selection_rule": selection_rule,
                "activation_start_year": activation_start_year,
                "train_period": _signals_period(train_one_trade),
                "test_period": _signals_period(test_one_trade),
                "edge_indexes": list(subset),
                "edge_count": len(subset),
                "train": train_metrics,
                "test": test_metrics,
                "full_after_activation": full_metrics,
                "constituent_edges": [_edge_identity(edges[index]) for index in subset],
                "caveat": (
                    "Adaptive candidate selected from historical yearly behavior; "
                    "treat as research until paper-traded forward."
                ),
            }
            payload["candidate_hash"] = stable_hash(payload)
            candidates.append(payload)

    return sorted(
        candidates,
        key=lambda row: (
            _profit_factor_score(row["test"].get("profit_factor")),
            float(row["test"].get("net_pnl") or 0),
            _profit_factor_score(row["full_after_activation"].get("profit_factor")),
        ),
        reverse=True,
    )[:20]


def _yearly_profitable_candidates(
    edges: Sequence[dict[str, Any]],
    signals: Sequence[dict[str, Any]],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    if not edges or not signals:
        return []
    years = sorted({_signal_timestamp(signal).year for signal in signals})
    if len(years) < 4:
        return []
    test_start_year = max(years) - 2
    subset_specs = _adaptive_subset_specs(edges)
    candidates = []
    for subset, selection_rule in subset_specs.items():
        subset_set = set(subset)
        subset_signals = [signal for signal in signals if int(signal["rule_index"]) in subset_set]
        for activation_start_year in years:
            if activation_start_year >= test_start_year:
                continue
            active_signals = [
                signal
                for signal in subset_signals
                if _signal_timestamp(signal).year >= activation_start_year
            ]
            active_years = sorted({_signal_timestamp(signal).year for signal in active_signals})
            if len(active_years) < 3:
                continue
            train_signals = [
                signal
                for signal in active_signals
                if _signal_timestamp(signal).year < test_start_year
            ]
            test_signals = [
                signal
                for signal in active_signals
                if _signal_timestamp(signal).year >= test_start_year
            ]
            train_one_trade = _one_trade_per_timestamp(train_signals)
            test_one_trade = _one_trade_per_timestamp(test_signals)
            full_one_trade = _one_trade_per_timestamp(active_signals)
            if not train_one_trade or not test_one_trade:
                continue
            train_metrics = _period_replay_metrics(train_one_trade, context)
            test_metrics = _period_replay_metrics(test_one_trade, context)
            full_metrics = _period_replay_metrics(full_one_trade, context)
            if not _passes_yearly_profitable_filter(train_metrics, test_metrics, full_metrics):
                continue
            payload = {
                "selection_rule": selection_rule,
                "activation_start_year": activation_start_year,
                "full_history_candidate": activation_start_year == min(years),
                "profitable_year_count": len(full_metrics["yearly_results"]),
                "train_period": _signals_period(train_one_trade),
                "test_period": _signals_period(test_one_trade),
                "edge_indexes": list(subset),
                "edge_count": len(subset),
                "train": train_metrics,
                "test": test_metrics,
                "full_after_activation": full_metrics,
                "constituent_edges": [_edge_identity(edges[index]) for index in subset],
                "objective": "all_active_years_net_pnl_positive_then_maximize_full_net_pnl",
                "caveat": (
                    "Annual profitability is evaluated only from activation_start_year onward; "
                    "full_history_candidate=true is required for all available history."
                ),
            }
            payload["strategy_analysis"] = _strategy_analysis(
                selection_rule,
                full_metrics,
                train_metrics,
                test_metrics,
                payload["constituent_edges"],
            )
            payload["candidate_hash"] = stable_hash(payload)
            candidates.append(payload)
    return sorted(
        candidates,
        key=lambda row: (
            float(row["full_after_activation"].get("net_pnl") or 0),
            float(row["test"].get("net_pnl") or 0),
            _profit_factor_score(row["full_after_activation"].get("profit_factor")),
        ),
        reverse=True,
    )[:20]


def _adaptive_subset_specs(edges: Sequence[dict[str, Any]]) -> dict[tuple[int, ...], str]:
    edge_indexes = list(range(len(edges)))
    subset_specs: dict[tuple[int, ...], str] = {tuple(edge_indexes): "all_edges"}
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
    for dow in sorted({str(edge.get("dow")) for edge in edges}):
        subset = tuple(index for index in edge_indexes if str(edges[index].get("dow")) == dow)
        subset_specs.setdefault(subset, f"dow:{dow}")
    for direction_label in sorted({str(edge.get("direction_label")) for edge in edges}):
        subset = tuple(index for index in edge_indexes if str(edges[index].get("direction_label")) == direction_label)
        subset_specs.setdefault(subset, f"direction_label:{direction_label}")
    for horizon_minutes in sorted({str(edge.get("horizon_minutes")) for edge in edges}):
        subset = tuple(index for index in edge_indexes if str(edges[index].get("horizon_minutes")) == horizon_minutes)
        subset_specs.setdefault(subset, f"horizon_minutes:{horizon_minutes}")
    for scan_type in sorted({str(edge.get("scan_type")) for edge in edges}):
        for session_bucket in sorted({str(edge.get("session_bucket")) for edge in edges}):
            subset = tuple(
                index
                for index in edge_indexes
                if str(edges[index].get("scan_type")) == scan_type
                and str(edges[index].get("session_bucket")) == session_bucket
            )
            subset_specs.setdefault(subset, f"scan_type:{scan_type},session_bucket:{session_bucket}")
    for scan_type in sorted({str(edge.get("scan_type")) for edge in edges}):
        for dow in sorted({str(edge.get("dow")) for edge in edges}):
            subset = tuple(
                index
                for index in edge_indexes
                if str(edges[index].get("scan_type")) == scan_type and str(edges[index].get("dow")) == dow
            )
            subset_specs.setdefault(subset, f"scan_type:{scan_type},dow:{dow}")
    for scan_type in sorted({str(edge.get("scan_type")) for edge in edges}):
        for direction_label in sorted({str(edge.get("direction_label")) for edge in edges}):
            subset = tuple(
                index
                for index in edge_indexes
                if str(edges[index].get("scan_type")) == scan_type
                and str(edges[index].get("direction_label")) == direction_label
            )
            subset_specs.setdefault(subset, f"scan_type:{scan_type},direction_label:{direction_label}")
    for scan_type in sorted({str(edge.get("scan_type")) for edge in edges}):
        for horizon_minutes in sorted({str(edge.get("horizon_minutes")) for edge in edges}):
            subset = tuple(
                index
                for index in edge_indexes
                if str(edges[index].get("scan_type")) == scan_type
                and str(edges[index].get("horizon_minutes")) == horizon_minutes
            )
            subset_specs.setdefault(subset, f"scan_type:{scan_type},horizon_minutes:{horizon_minutes}")
    for session_bucket in sorted({str(edge.get("session_bucket")) for edge in edges}):
        for dow in sorted({str(edge.get("dow")) for edge in edges}):
            subset = tuple(
                index
                for index in edge_indexes
                if str(edges[index].get("session_bucket")) == session_bucket and str(edges[index].get("dow")) == dow
            )
            subset_specs.setdefault(subset, f"session_bucket:{session_bucket},dow:{dow}")
    return {subset: label for subset, label in subset_specs.items() if subset}


def _passes_open_regime_filter(
    train_metrics: dict[str, Any],
    test_metrics: dict[str, Any],
    full_metrics: dict[str, Any],
    context: dict[str, Any],
) -> bool:
    return (
        full_metrics["target_qualified"]
        and train_metrics["net_pnl"] > 0
        and test_metrics["net_pnl"] > 0
        and _profit_factor_score(test_metrics["profit_factor"]) > 1.0
        and test_metrics["annual_trades"] > float(context["min_annual_trades"]) * 0.5
        and test_metrics["win_probability"] > 0.50
    )


def _passes_yearly_profitable_filter(
    train_metrics: dict[str, Any],
    test_metrics: dict[str, Any],
    full_metrics: dict[str, Any],
) -> bool:
    return (
        full_metrics["target_qualified"]
        and train_metrics["net_pnl"] > 0
        and test_metrics["net_pnl"] > 0
        and _all_years_profitable(train_metrics)
        and _all_years_profitable(test_metrics)
        and _all_years_profitable(full_metrics)
    )


def _all_years_profitable(metrics: dict[str, Any]) -> bool:
    yearly_results = metrics.get("yearly_results") or []
    return bool(yearly_results) and all(float(row.get("net_pnl") or 0) > 0 for row in yearly_results)


def _strategy_analysis(
    selection_rule: str,
    full_metrics: dict[str, Any],
    train_metrics: dict[str, Any],
    test_metrics: dict[str, Any],
    constituent_edges: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    yearly_results = full_metrics.get("yearly_results") or []
    weakest_year = min(yearly_results, key=lambda row: float(row.get("net_pnl") or 0)) if yearly_results else None
    strongest_year = max(yearly_results, key=lambda row: float(row.get("net_pnl") or 0)) if yearly_results else None
    losing_year_count = sum(1 for row in yearly_results if float(row.get("net_pnl") or 0) <= 0)
    strategy_profile = _strategy_profile(constituent_edges or [])
    strengths = [
        "Every active calendar year is net profitable.",
        "Meets the annual trade frequency, win-probability, and positive-PnL target after activation.",
    ]
    if float(test_metrics.get("net_pnl") or 0) > 0:
        strengths.append("Most recent test window is net profitable.")
    if _profit_factor_score(test_metrics.get("profit_factor")) > _profit_factor_score(train_metrics.get("profit_factor")):
        strengths.append("Recent test profit factor is stronger than the training period.")
    if float(full_metrics.get("annual_trades") or 0) > 2000:
        strengths.append("High trade count reduces dependence on a tiny number of events.")
    if strategy_profile["dominant_volume_profile"] != "mixed":
        strengths.append(f"Clear VOL dependency: {strategy_profile['dominant_volume_profile']}.")

    weaknesses = []
    if weakest_year and float(weakest_year.get("net_pnl") or 0) < float(full_metrics.get("net_pnl") or 0) * 0.05:
        weaknesses.append(f"Weakest year has thin profit: {weakest_year['year']} net_pnl={weakest_year['net_pnl']}.")
    if (full_metrics.get("profit_factor") or 0) < 1.3:
        weaknesses.append("Profit factor is positive but not thick; execution degradation can matter.")
    if float(full_metrics.get("max_drawdown") or 0) > 0 and float(full_metrics.get("return_to_drawdown") or 0) < 10:
        weaknesses.append("Return-to-drawdown is moderate; risk sizing needs restraint.")
    if "all_edges" in selection_rule:
        weaknesses.append("Uses a broad edge basket, so constituent overlap and regime drift should be monitored.")
    if losing_year_count:
        weaknesses.append(f"Contains {losing_year_count} non-profitable active years.")
    if strategy_profile["dominant_session_bucket"] != "mixed":
        weaknesses.append(
            f"Session concentration: dominant_session_bucket={strategy_profile['dominant_session_bucket']}."
        )
    if full_metrics.get("cost_stress"):
        fragile = [
            row["label"]
            for row in full_metrics["cost_stress"]
            if row["extra_usd_per_trade"] > 0 and row["net_pnl"] <= 0
        ]
        if fragile:
            weaknesses.append(f"Cost fragile under stress scenarios: {', '.join(fragile)}.")
    return {
        "strengths": strengths,
        "weaknesses": weaknesses,
        "strategy_profile": strategy_profile,
        "weakest_year": weakest_year,
        "strongest_year": strongest_year,
        "active_year_count": len(yearly_results),
        "losing_year_count": losing_year_count,
    }


def _period_replay_metrics(signals: Sequence[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any]:
    ordered = sorted(signals, key=lambda signal: signal["timestamp"])
    period_days = _signals_covered_days(ordered)
    metrics = _signal_metrics(ordered, period_days)
    return {
        **metrics,
        "target_qualified": (
            metrics["annual_trades"] > float(context["min_annual_trades"])
            and metrics["win_probability"] > float(context["min_win_probability"])
            and metrics["net_pnl"] > 0
        ),
        "yearly_results": _yearly_signal_results(ordered),
        "cost_stress": _cost_stress_metrics(ordered, period_days, context),
    }


def _profit_factor_score(value: Any) -> float:
    if value is None:
        return float("inf")
    return float(value)


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
    _ensure_regime_feature_table(con, context)
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
          SELECT *,
                 lead(close, {horizon_bars}) OVER (ORDER BY timestamp) AS future_close,
                 lead(timestamp, {horizon_bars}) OVER (ORDER BY timestamp) AS future_ts
          FROM regime_feature_base
        ), feats AS (
          SELECT *,
                 CASE WHEN close > high20_prev THEN 1 WHEN close < low20_prev THEN -1 ELSE 0 END AS breakout20
          FROM raw
          WHERE future_close IS NOT NULL AND ma20 IS NOT NULL AND ma50 IS NOT NULL AND close_std50 IS NOT NULL
            AND vol50 IS NOT NULL AND range20 IS NOT NULL
            AND (epoch(future_ts)-epoch(timestamp))/60.0 BETWEEN {horizon_minutes} AND {horizon_minutes + int(context["continuity_tolerance_minutes"])}
        ), signals AS (
          SELECT timestamp, future_ts, 'breakout_continuation' AS scan_type, session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1 AS direction, future_close, close
          FROM feats
          WHERE breakout20 = 1 AND trend_bin = 1 AND volume_bin >= 1
          UNION ALL
          SELECT timestamp, future_ts, 'breakout_continuation', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE breakout20 = -1 AND trend_bin = -1 AND volume_bin >= 1
          UNION ALL
          SELECT timestamp, future_ts, 'opening_range_breakout', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1, future_close, close
          FROM feats
          WHERE opening_high_prev IS NOT NULL AND moday >= 840 AND close > opening_high_prev
            AND trend_bin = 1 AND volume_bin >= 1
          UNION ALL
          SELECT timestamp, future_ts, 'opening_range_breakout', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE opening_low_prev IS NOT NULL AND moday >= 840 AND close < opening_low_prev
            AND trend_bin = -1 AND volume_bin >= 1
          UNION ALL
          SELECT timestamp, future_ts, 'failed_breakout_reversion', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE high > high20_prev AND close < high20_prev AND z50 >= 1.0 AND abs(body_to_range) <= 0.45
          UNION ALL
          SELECT timestamp, future_ts, 'failed_breakout_reversion', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1, future_close, close
          FROM feats
          WHERE low < low20_prev AND close > low20_prev AND z50 <= -1.0 AND abs(body_to_range) <= 0.45
          UNION ALL
          SELECT timestamp, future_ts, 'vwap_reclaim_continuation', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1, future_close, close
          FROM feats
          WHERE prev_vwap_dist < 0 AND vwap_dist >= 0 AND trend_bin = 1 AND volume_bin >= 0 AND ret1 > 0
          UNION ALL
          SELECT timestamp, future_ts, 'vwap_reclaim_continuation', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE prev_vwap_dist > 0 AND vwap_dist <= 0 AND trend_bin = -1 AND volume_bin >= 0 AND ret1 < 0
          UNION ALL
          SELECT timestamp, future_ts, 'outside_bar_momentum', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1, future_close, close
          FROM feats
          WHERE prev_high IS NOT NULL AND high >= prev_high AND low <= prev_low
            AND body_to_range >= 0.55 AND close > open AND volume_bin >= 1
          UNION ALL
          SELECT timestamp, future_ts, 'outside_bar_momentum', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE prev_low IS NOT NULL AND high >= prev_high AND low <= prev_low
            AND body_to_range <= -0.55 AND close < open AND volume_bin >= 1
          UNION ALL
          SELECT timestamp, future_ts, 'range_expansion_continuation', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1, future_close, close
          FROM feats
          WHERE body_to_range >= 0.6 AND range_bin >= 2 AND volume_bin >= 1
          UNION ALL
          SELECT timestamp, future_ts, 'range_expansion_continuation', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE body_to_range <= -0.6 AND range_bin >= 2 AND volume_bin >= 1
          UNION ALL
          SELECT timestamp, future_ts, 'trend_pullback_reclaim', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1, future_close, close
          FROM feats
          WHERE trend_bin = 1 AND ret5 < 0 AND ret1 > 0 AND close > ma20
          UNION ALL
          SELECT timestamp, future_ts, 'trend_pullback_reclaim', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE trend_bin = -1 AND ret5 > 0 AND ret1 < 0 AND close < ma20
          UNION ALL
          SELECT timestamp, future_ts, 'zscore_mean_reversion', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE z50 >= 2 AND volume_bin <= 1
          UNION ALL
          SELECT timestamp, future_ts, 'zscore_mean_reversion', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1, future_close, close
          FROM feats
          WHERE z50 <= -2 AND volume_bin <= 1
          UNION ALL
          SELECT timestamp, future_ts, 'volume_climax_reversion', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE z50 >= 1.5 AND volume_bin >= 2 AND abs(body_to_range) <= 0.35
          UNION ALL
          SELECT timestamp, future_ts, 'volume_climax_reversion', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1, future_close, close
          FROM feats
          WHERE z50 <= -1.5 AND volume_bin >= 2 AND abs(body_to_range) <= 0.35
          UNION ALL
          SELECT timestamp, future_ts, 'session_extreme_reversion', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE session_high_prev IS NOT NULL AND close >= session_high_prev - range20*0.1
            AND z50 >= 1.25 AND volume_bin <= 1 AND abs(body_to_range) <= 0.45
          UNION ALL
          SELECT timestamp, future_ts, 'session_extreme_reversion', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1, future_close, close
          FROM feats
          WHERE session_low_prev IS NOT NULL AND close <= session_low_prev + range20*0.1
            AND z50 <= -1.25 AND volume_bin <= 1 AND abs(body_to_range) <= 0.45
          UNION ALL
          SELECT timestamp, future_ts, 'low_volume_drift', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1, future_close, close
          FROM feats
          WHERE trend_bin = 1 AND volume_bin = -1 AND ret1 > 0
          UNION ALL
          SELECT timestamp, future_ts, 'low_volume_drift', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1, future_close, close
          FROM feats
          WHERE trend_bin = -1 AND volume_bin = -1 AND ret1 < 0
        ), matched AS (
          SELECT s.timestamp, s.future_ts AS exit_timestamp, e.rule_index, {horizon_minutes} AS horizon_minutes,
                 e.scan_type,
                 CASE WHEN e.direction = 1 THEN 'long' ELSE 'short' END AS direction_label,
                 s.session_bucket, s.dow, s.trend_bin, s.volume_bin, s.range_bin,
                 s.close AS entry_price, s.future_close AS exit_price,
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
        SELECT timestamp, exit_timestamp, rule_index, horizon_minutes, scan_type, direction_label,
               session_bucket, dow, trend_bin, volume_bin, range_bin, entry_price, exit_price, pnl
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
    metrics = _signal_metrics(ordered_signals, day_count)
    annual_trades = metrics["annual_trades"]
    win_probability = metrics["win_probability"]
    return {
        **metrics,
        "yearly_results": _yearly_signal_results(ordered_signals),
        "cost_stress": _cost_stress_metrics(ordered_signals, day_count, context),
        "target_qualified": (
            annual_trades > float(context["min_annual_trades"])
            and win_probability > float(context["min_win_probability"])
            and metrics["net_pnl"] > 0
        ),
    }


def _cost_stress_metrics(
    signals: Sequence[dict[str, Any]],
    day_count: int,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    scenarios = context.get("cost_stress_usd_per_trade") or []
    if not scenarios:
        return []
    stressed = []
    for scenario in scenarios:
        extra_cost = float(scenario.get("extra_usd_per_trade") or 0.0)
        adjusted_signals = [
            {**signal, "pnl": float(signal["pnl"]) - extra_cost}
            for signal in signals
        ]
        metrics = _signal_metrics(adjusted_signals, day_count)
        stressed.append(
            {
                "label": scenario.get("label"),
                "additional_round_trip_ticks": scenario.get("additional_round_trip_ticks"),
                "extra_usd_per_trade": extra_cost,
                "trades": metrics["trades"],
                "annual_trades": metrics["annual_trades"],
                "net_pnl": metrics["net_pnl"],
                "win_probability": metrics["win_probability"],
                "avg_pnl": metrics["avg_pnl"],
                "profit_factor": metrics["profit_factor"],
                "max_drawdown": metrics["max_drawdown"],
                "return_to_drawdown": metrics["return_to_drawdown"],
            }
        )
    return stressed


def _strategy_profile(edges: Sequence[dict[str, Any]]) -> dict[str, Any]:
    scan_types = Counter(str(edge.get("scan_type")) for edge in edges)
    sessions = Counter(str(edge.get("session_bucket")) for edge in edges)
    directions = Counter(str(edge.get("direction_label")) for edge in edges)
    volume_profiles = Counter(_volume_profile(edge) for edge in edges)
    families = Counter(_strategy_family(edge) for edge in edges)
    return {
        "edge_count": len(edges),
        "families": dict(sorted(families.items())),
        "scan_types": dict(sorted(scan_types.items())),
        "directions": dict(sorted(directions.items())),
        "volume_profiles": dict(sorted(volume_profiles.items())),
        "dominant_volume_profile": _dominant_counter_label(volume_profiles),
        "session_buckets": dict(sorted(sessions.items())),
        "dominant_session_bucket": _dominant_counter_label(sessions),
    }


def _strategy_family(edge: dict[str, Any]) -> str:
    scan_type = str(edge.get("scan_type"))
    if scan_type in {
        "breakout_continuation",
        "opening_range_breakout",
        "outside_bar_momentum",
        "range_expansion_continuation",
        "trend_pullback_reclaim",
        "vwap_reclaim_continuation",
        "low_volume_drift",
    }:
        return "trend"
    if scan_type in {
        "failed_breakout_reversion",
        "session_extreme_reversion",
        "zscore_mean_reversion",
        "volume_climax_reversion",
    }:
        return "mean_reversion"
    return "feature_scan"


def _volume_profile(edge: dict[str, Any]) -> str:
    scan_type = str(edge.get("scan_type"))
    volume_bin = int(edge.get("volume_bin") or 0)
    if scan_type == "volume_climax_reversion":
        return "volume_climax_reversion"
    if scan_type in {
        "breakout_continuation",
        "opening_range_breakout",
        "outside_bar_momentum",
        "range_expansion_continuation",
    } and volume_bin >= 1:
        return "volume_confirmed_breakout"
    if scan_type == "vwap_reclaim_continuation":
        return "vwap_volume_reclaim"
    if scan_type == "low_volume_drift" or volume_bin < 0:
        return "low_volume_drift"
    if volume_bin >= 2:
        return "high_volume"
    if volume_bin >= 1:
        return "normal_volume"
    return "volume_neutral"


def _dominant_counter_label(counter: Counter[str]) -> str:
    if not counter:
        return "none"
    [(label, count), *rest] = counter.most_common()
    if rest and rest[0][1] == count:
        return "mixed"
    return label


def _signal_metrics(signals: Sequence[dict[str, Any]], day_count: int) -> dict[str, Any]:
    trades = len(signals)
    net_pnl = sum(float(signal["pnl"]) for signal in signals)
    wins = [float(signal["pnl"]) for signal in signals if float(signal["pnl"]) > 0]
    losses = [float(signal["pnl"]) for signal in signals if float(signal["pnl"]) < 0]
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for signal in signals:
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
    }


def _yearly_signal_results(signals: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_year: dict[int, list[dict[str, Any]]] = {}
    for signal in signals:
        timestamp = _signal_timestamp(signal)
        by_year.setdefault(timestamp.year, []).append(signal)
    results = []
    for year, year_signals in sorted(by_year.items()):
        ordered = sorted(year_signals, key=lambda signal: signal["timestamp"])
        first_day = _signal_timestamp(ordered[0]).date()
        last_day = _signal_timestamp(ordered[-1]).date()
        covered_days = max(1, (last_day - first_day).days + 1)
        metrics = _signal_metrics(ordered, covered_days)
        results.append(
            {
                "year": year,
                "period_from": first_day.isoformat(),
                "period_to": last_day.isoformat(),
                "covered_days": covered_days,
                **metrics,
            }
        )
    return results


def _signals_period(signals: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(signals, key=lambda signal: signal["timestamp"])
    if not ordered:
        return {"from": None, "to": None, "covered_days": 0}
    first_day = _signal_timestamp(ordered[0]).date()
    last_day = _signal_timestamp(ordered[-1]).date()
    return {
        "from": first_day.isoformat(),
        "to": last_day.isoformat(),
        "covered_days": _signals_covered_days(ordered),
    }


def _signals_covered_days(signals: Sequence[dict[str, Any]]) -> int:
    if not signals:
        return 0
    first_day = _signal_timestamp(signals[0]).date()
    last_day = _signal_timestamp(signals[-1]).date()
    return max(1, (last_day - first_day).days + 1)


def _signal_timestamp(signal: dict[str, Any]) -> datetime:
    value = signal["timestamp"]
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise TypeError(f"Unsupported signal timestamp type: {type(value)!r}")


def _evaluation_periods(date_from: date, date_to: date, walk_forward: dict[str, Any]) -> dict[str, Any]:
    return {
        "full_backtest": {
            "role": "full_sample_report",
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
        },
        "walk_forward": {
            "mode": walk_forward.get("mode"),
            "train_years": walk_forward.get("train_years"),
            "test_years": walk_forward.get("test_years"),
            "step_years": walk_forward.get("step_years"),
            "windows": [
                {
                    "fold": index + 1,
                    "train": {
                        "from": window.get("train_from"),
                        "to": window.get("train_to"),
                    },
                    "test": {
                        "from": window.get("test_from"),
                        "to": window.get("test_to"),
                    },
                    "train_candidate_count": window.get("train_candidate_count"),
                    "test_candidate_count": window.get("test_candidate_count"),
                    "matched_stable_candidate_count": window.get("matched_stable_candidate_count"),
                }
                for index, window in enumerate(walk_forward.get("windows", []))
            ],
        },
        "yearly_results_location": (
            "regime_basket_replays[*].one_trade_per_timestamp.yearly_results and "
            "regime_basket_replays[*].optimized_subsets[*].one_trade_per_timestamp.yearly_results"
        ),
    }


def _ohlcv_data_semantics(symbol: str, timeframe: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "source": "Databento OHLCV bars",
        "volume_field_mapping": {
            "bar_volume": "tick_count",
            "tick_count": "Databento OHLCV volume mapped into the local bar schema",
        },
        "execution_fields_available": {
            "ohlc": True,
            "bar_volume": True,
            "bid_ask_spread": False,
            "top_of_book_size": False,
            "limit_order_fill": False,
        },
        "execution_caveat": (
            "OHLCV research can identify historical gross or cost-adjusted bar edges, "
            "but cannot prove market-order spread cost, limit-order fill rate, queue position, or adverse selection."
        ),
    }


def _plan12_review_status() -> dict[str, Any]:
    return {
        "implemented_in_this_report": [
            "OHLCV-only profitability search across simple trend and mean-reversion families",
            "VOL-aware regime bins using bar_volume=tick_count relative to recent volume",
            "session/time bucket attribution on candidate edges",
            "train/test/final period metadata and yearly results for replay candidates",
            "additional 1 and 2 tick per-trade cost stress for replayed candidate baskets",
            "strategy profile cards for candidate family, direction, VOL dependency, and session concentration",
        ],
        "partially_implemented": [
            "VOL feature set covers relative volume, volume breakout, climax reversion, and low-volume filters; "
            "session-normalized percentile and explicit multi-timeframe confirmation remain next steps.",
            "Execution cost is stress-tested from OHLCV PnL; real spread, fill probability, and queue effects still need quote data.",
        ],
        "not_implemented_without_more_data": [
            "TBBO/MBP quote replay",
            "limit-order fill and missed-fill simulation",
            "event-window spread/slippage attribution",
            "paper shadow live execution review",
        ],
        "live_trading_readiness": "research_only_until_quote_replay_and_paper_shadow_pass",
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
