from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

import duckdb

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

    round_trip_cost_usd = cost_model.round_trip_fees_usd + 2 * cost_model.slippage_ticks_per_side * cost_model.tick_value
    con = duckdb.connect(":memory:")
    try:
        parquet_glob = _duckdb_glob(data_root, symbol_config.alias, timeframe)
        context = {
            "parquet_glob": parquet_glob,
            "date_from": date_from.isoformat(),
            "date_to_exclusive": (date_to + timedelta(days=1)).isoformat(),
            "point_value": float(symbol_config.point_value),
            "min_annual_trades": float(min_annual_trades),
            "min_win_probability": float(min_win_probability),
            "max_candidates": int(max_candidates),
        }
        cost_adjusted = _run_close_to_close_scans(con, context, round_trip_cost_usd)
        cost_adjusted.extend(_run_tp_sl_scans(con, context, round_trip_cost_usd))
        gross_candidates = _run_tp_sl_scans(con, context, 0.0)
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

    report = {
        "schema_version": 1,
        "artifact": "databento_nq_profit_strategy_mining_report",
        "status": "target_found" if qualified else "not_found",
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
            "tp_sl_horizons": list(DEFAULT_TP_SL_HORIZONS),
            "tp_sl_pairs": [{"take_profit_points": tp, "stop_loss_points": stop} for tp, stop in DEFAULT_TP_SL_PAIRS],
            "feature_conditions": [
                "utc_time_bucket",
                "entry_phase_mod_horizon",
                "previous_1m_return_sign",
                "previous_5m_return_bin",
                "trend_vs_ma20_or_ma50",
                "relative_volume_bin",
                "range_regime",
            ],
        },
        "summary": {
            "bar_file_count": len(bar_files),
            "qualified_candidate_count": len(qualified),
            "gross_only_candidate_count": len(gross_only),
            "best_cost_adjusted_net_pnl": qualified[0]["net_pnl"] if qualified else None,
            "best_gross_net_pnl": gross_only[0]["gross_net_pnl"] if gross_only else None,
        },
        "qualified_candidates": qualified[:max_candidates],
        "gross_only_candidates": gross_only,
        "blocked_next_steps": [] if qualified else [
            "No scanned candidate met annual_trades > 1000, win_probability > 0.53, and cost-adjusted net_pnl > 0.",
            "Download TBBO/MBP-1 before promoting any gross-only candidate.",
            "Extend search with order-book features, not more OHLCV-only curve fitting.",
        ],
    }
    report["report_hash"] = stable_hash({key: value for key, value in report.items() if key != "report_hash"})
    if output_path is not None:
        write_json(output_path, report)
    return report


def _run_close_to_close_scans(con: duckdb.DuckDBPyConnection, context: dict[str, Any], cost: float) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for horizon in DEFAULT_CLOSE_HORIZONS:
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
                     lead(close, {horizon}) OVER (ORDER BY timestamp) AS future_close,
                     lead(timestamp, {horizon}) OVER (ORDER BY timestamp) AS future_ts
              FROM read_parquet('{context["parquet_glob"]}')
              WHERE timestamp >= '{context["date_from"]}' AND timestamp < '{context["date_to_exclusive"]}'
            ), feats AS (
              SELECT *,
                     floor(moday/240)*240 AS bucket_start,
                     moday % {horizon} AS phase,
                     CASE WHEN ret1 > 0 THEN 1 WHEN ret1 < 0 THEN -1 ELSE 0 END AS ret1_sign,
                     CASE WHEN ret5 > 2 THEN 2 WHEN ret5 > 0 THEN 1 WHEN ret5 < -2 THEN -2 WHEN ret5 < 0 THEN -1 ELSE 0 END AS ret5_bin,
                     CASE WHEN close > ma50 THEN 1 ELSE -1 END AS trend_bin,
                     CASE WHEN tick_count >= vol50*1.5 THEN 2 WHEN tick_count >= vol50 THEN 1 WHEN tick_count < vol50*0.7 THEN -1 ELSE 0 END AS volume_bin
              FROM raw
              WHERE future_close IS NOT NULL AND ma50 IS NOT NULL AND vol50 IS NOT NULL
                AND (epoch(future_ts)-epoch(timestamp))/60.0 BETWEEN {horizon} AND {horizon + 2}
            ), pnl AS (
              SELECT 'close_to_close_feature_scan' AS scan_type, {horizon} AS horizon_minutes,
                     bucket_start, phase, dow, ret1_sign, ret5_bin, trend_bin, volume_bin,
                     NULL::DOUBLE AS take_profit_points, NULL::DOUBLE AS stop_loss_points,
                     1 AS direction, (future_close-close)*{context["point_value"]}-{cost} AS pnl
              FROM feats
              UNION ALL
              SELECT 'close_to_close_feature_scan', {horizon},
                     bucket_start, phase, dow, ret1_sign, ret5_bin, trend_bin, volume_bin,
                     NULL::DOUBLE, NULL::DOUBLE,
                     -1, (close-future_close)*{context["point_value"]}-{cost}
              FROM feats
            )
            {_candidate_select_sql(context)}
            """,
        )
        candidates.extend(_annotate_rows(rows, cost))
    return candidates


def _run_tp_sl_scans(con: duckdb.DuckDBPyConnection, context: dict[str, Any], cost: float) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    values = ",".join(f"({tp},{stop})" for tp, stop in DEFAULT_TP_SL_PAIRS)
    for horizon in DEFAULT_TP_SL_HORIZONS:
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
                     lead(close, {horizon}) OVER (ORDER BY timestamp) AS future_close,
                     lead(timestamp, {horizon}) OVER (ORDER BY timestamp) AS future_ts,
                     max(high) OVER (ORDER BY timestamp ROWS BETWEEN 1 FOLLOWING AND {horizon} FOLLOWING) AS fwd_high,
                     min(low) OVER (ORDER BY timestamp ROWS BETWEEN 1 FOLLOWING AND {horizon} FOLLOWING) AS fwd_low
              FROM read_parquet('{context["parquet_glob"]}')
              WHERE timestamp >= '{context["date_from"]}' AND timestamp < '{context["date_to_exclusive"]}'
            ), feats AS (
              SELECT *,
                     floor(moday/240)*240 AS bucket_start,
                     moday % {horizon} AS phase,
                     CASE WHEN ret1 > 0 THEN 1 WHEN ret1 < 0 THEN -1 ELSE 0 END AS ret1_sign,
                     0 AS ret5_bin,
                     CASE WHEN close > ma20 THEN 1 ELSE -1 END AS trend_bin,
                     0 AS volume_bin
              FROM raw
              WHERE future_close IS NOT NULL AND ma20 IS NOT NULL
                AND (epoch(future_ts)-epoch(timestamp))/60.0 BETWEEN {horizon} AND {horizon + 2}
            ), pnl AS (
              SELECT 'tp_sl_feature_scan' AS scan_type, {horizon} AS horizon_minutes,
                     bucket_start, phase, dow, ret1_sign, ret5_bin, trend_bin, volume_bin,
                     p.take_profit_points, p.stop_loss_points, 1 AS direction,
                     CASE
                       WHEN fwd_low <= close-p.stop_loss_points THEN -p.stop_loss_points*{context["point_value"]}-{cost}
                       WHEN fwd_high >= close+p.take_profit_points THEN p.take_profit_points*{context["point_value"]}-{cost}
                       ELSE (future_close-close)*{context["point_value"]}-{cost}
                     END AS pnl
              FROM feats CROSS JOIN params p
              UNION ALL
              SELECT 'tp_sl_feature_scan', {horizon},
                     bucket_start, phase, dow, ret1_sign, ret5_bin, trend_bin, volume_bin,
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
    SELECT scan_type, horizon_minutes, bucket_start, phase, dow, ret1_sign, ret5_bin, trend_bin, volume_bin,
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
        payload = {
            **row,
            "round_trip_cost_usd": cost,
            "rule": _candidate_rule(row),
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
