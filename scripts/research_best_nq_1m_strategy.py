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
HORIZONS = (5, 10, 15, 30, 60, 120)
POINT_VALUE = 20.0
ROUND_TRIP_COST_USD = 15.0
OBJECTIVE = "balanced"


@dataclass(frozen=True)
class EdgeStats:
    edge_id: str
    scan_type: str
    horizon_minutes: int
    session_bucket: str
    dow: int
    direction_label: str
    trend_bin: int
    volume_bin: int
    range_bin: int
    trades: int
    net_pnl: float
    avg_pnl: float
    win_rate: float
    avg_hold_minutes: float
    yearly_pnl: tuple[float, ...]
    positive_years: int
    worst_year_pnl: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "scan_type": self.scan_type,
            "horizon_minutes": self.horizon_minutes,
            "session_bucket": self.session_bucket,
            "dow": self.dow,
            "direction_label": self.direction_label,
            "trend_bin": self.trend_bin,
            "volume_bin": self.volume_bin,
            "range_bin": self.range_bin,
            "trades": self.trades,
            "net_pnl": self.net_pnl,
            "avg_pnl": self.avg_pnl,
            "win_rate": self.win_rate,
            "avg_hold_minutes": self.avg_hold_minutes,
            "yearly_pnl": dict(zip(YEARS, self.yearly_pnl, strict=True)),
            "positive_years": self.positive_years,
            "worst_year_pnl": self.worst_year_pnl,
        }


@dataclass(frozen=True)
class BeamCandidate:
    edge_ids: tuple[str, ...]
    yearly_pnl: tuple[float, ...]
    net_pnl: float
    positive_years: int
    worst_year_pnl: float
    avg_hold_minutes: float


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Search simple, stable 1-minute NQ strategies across ORB/VWAP/trend/reversion edges."
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--date-from", default="2010-06-06")
    parser.add_argument("--date-to", default="2026-04-27")
    parser.add_argument("--max-edge-pool", type=int, default=220)
    parser.add_argument("--max-edges-per-strategy", type=int, default=10)
    parser.add_argument("--beam-width", type=int, default=600)
    parser.add_argument("--exact-candidates", type=int, default=120)
    parser.add_argument("--horizons", default="5,10,15,30,60,120")
    parser.add_argument("--objective", choices=("balanced", "net", "short"), default="balanced")
    parser.add_argument("--output", type=Path, default=Path("experiments/profit_mining/best_nq_1m_strategy_search.json"))
    args = parser.parse_args()
    global HORIZONS, OBJECTIVE
    HORIZONS = tuple(int(raw.strip()) for raw in args.horizons.split(",") if raw.strip())
    OBJECTIVE = args.objective
    if not HORIZONS:
        raise ValueError("--horizons must include at least one integer minute horizon")

    pattern = str(Path(args.data_root) / "bars" / "1m" / args.symbol / "date=*" / "part-000.parquet")
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4")
    try:
        build_feature_table(con, pattern, args.date_from, args.date_to)
        build_signal_table(con)
        edge_stats = load_edge_stats(con)
        edge_pool = select_edge_pool(edge_stats, args.max_edge_pool)
        beam_candidates = beam_search(edge_pool, args.max_edges_per_strategy, args.beam_width)
        exact_results = exact_replay_candidates(con, edge_pool, beam_candidates[: args.exact_candidates])
    finally:
        con.close()

    exact_results.sort(key=exact_sort_key, reverse=True)
    payload = {
        "schema_version": 1,
        "artifact": "best_nq_1m_strategy_search",
        "symbol": args.symbol,
        "timeframe": "1m",
        "date_from": args.date_from,
        "date_to": args.date_to,
        "cost_model": {
            "point_value": POINT_VALUE,
            "round_trip_cost_usd": ROUND_TRIP_COST_USD,
            "note": "Round-trip cost approximates $5 commission/fees plus two $5 slippage ticks.",
        },
        "search_space": {
            "horizons_minutes": list(HORIZONS),
            "families": [
                "opening_range_breakout",
                "breakout_continuation",
                "failed_breakout_reversion",
                "vwap_reclaim_continuation",
                "outside_bar_momentum",
                "range_expansion_continuation",
                "trend_pullback_reclaim",
                "zscore_mean_reversion",
                "volume_climax_reversion",
                "session_extreme_reversion",
                "low_volume_drift",
            ],
            "edge_pool_size": len(edge_pool),
            "beam_candidate_count": len(beam_candidates),
            "exact_candidate_count": len(exact_results),
            "max_edges_per_strategy": args.max_edges_per_strategy,
            "objective": OBJECTIVE,
        },
        "best_exact": exact_results[0] if exact_results else None,
        "top_exact": exact_results[:25],
        "top_single_edges": [edge.to_dict() for edge in edge_pool[:40]],
        "notes": [
            "Signals are generated on bar close and entered at the next 1-minute open.",
            "Exact replay allows at most one open position; overlapping signals are skipped until the current fixed-horizon exit.",
            "Partial years 2010 and 2026 are included because they are present in local data.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True, default=json_default) + "\n", encoding="utf-8")
    print(args.output)
    if exact_results:
        best = exact_results[0]
        print(
            "best",
            "all_years_positive=",
            best["all_years_positive"],
            "net=",
            round(best["metrics"]["net_pnl"], 2),
            "pf=",
            None if best["metrics"]["profit_factor"] is None else round(best["metrics"]["profit_factor"], 3),
            "dd=",
            round(best["metrics"]["max_drawdown"], 2),
            "avg_hold=",
            round(best["metrics"]["avg_hold_minutes"], 2),
            "edges=",
            best["edge_count"],
        )
    return 0


def build_feature_table(con: duckdb.DuckDBPyConnection, pattern: str, date_from: str, date_to: str) -> None:
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
        ), enriched AS (
          SELECT *,
                 CAST(ny_ts AS DATE) AS ny_date,
                 CAST(strftime(ny_ts, '%Y') AS INTEGER) AS year,
                 CAST(strftime(ny_ts, '%w') AS INTEGER) AS dow,
                 CAST(strftime(ny_ts, '%H') AS INTEGER) * 60 + CAST(strftime(ny_ts, '%M') AS INTEGER) AS ny_min,
                 close - lag(close, 1) OVER (ORDER BY timestamp) AS ret1,
                 close - lag(close, 5) OVER (ORDER BY timestamp) AS ret5,
                 lag(high, 1) OVER (ORDER BY timestamp) AS prev_high,
                 lag(low, 1) OVER (ORDER BY timestamp) AS prev_low,
                 avg(close) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS ma20,
                 avg(close) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS ma50,
                 stddev_pop(close) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS close_std50,
                 avg(tick_count) OVER (ORDER BY timestamp ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS vol50,
                 avg(high - low) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS range20,
                 max(high) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS high20_prev,
                 min(low) OVER (ORDER BY timestamp ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS low20_prev,
                 lead(open, 1) OVER (ORDER BY timestamp) AS entry_open,
                 lead(timestamp, 1) OVER (ORDER BY timestamp) AS entry_ts,
                 {lead_columns}
          FROM raw
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
          FROM enriched
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
                 CASE WHEN close_std50 > 0 THEN (close - ma50) / close_std50 ELSE 0 END AS z50,
                 close - session_vwap AS vwap_dist,
                 lag(close - session_vwap, 1) OVER (ORDER BY timestamp) AS prev_vwap_dist,
                 CASE WHEN high > low THEN (close - open) / (high - low) ELSE 0 END AS body_to_range
          FROM session_features
        )
        SELECT *
        FROM final
        WHERE entry_open IS NOT NULL
          AND ma20 IS NOT NULL
          AND ma50 IS NOT NULL
          AND close_std50 IS NOT NULL
          AND vol50 IS NOT NULL
          AND range20 IS NOT NULL
        """,
        [pattern, date_from, date_to],
    )


def build_signal_table(con: duckdb.DuckDBPyConnection) -> None:
    signal_queries = []
    for horizon in HORIZONS:
        signal_queries.append(signal_query_for_horizon(horizon))
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE candidate_signals AS
        WITH all_signals AS (
          {" UNION ALL ".join(signal_queries)}
        ), keyed AS (
          SELECT *,
                 scan_type || '|h=' || CAST(horizon_minutes AS VARCHAR)
                   || '|sess=' || session_bucket
                   || '|dow=' || CAST(dow AS VARCHAR)
                   || '|dir=' || direction_label
                   || '|trend=' || CAST(trend_bin AS VARCHAR)
                   || '|vol=' || CAST(volume_bin AS VARCHAR)
                   || '|range=' || CAST(range_bin AS VARCHAR) AS edge_id
          FROM all_signals
        )
        SELECT *
        FROM keyed
        WHERE pnl IS NOT NULL
          AND exit_timestamp IS NOT NULL
          AND entry_timestamp IS NOT NULL
          AND (epoch(exit_timestamp) - epoch(entry_timestamp)) / 60.0
                BETWEEN horizon_minutes AND horizon_minutes + 2
        """
    )


def signal_query_for_horizon(horizon: int) -> str:
    pnl = (
        f"CASE WHEN direction = 1 "
        f"THEN (exit_close_{horizon} - entry_open) * {POINT_VALUE} - {ROUND_TRIP_COST_USD} "
        f"ELSE (entry_open - exit_close_{horizon}) * {POINT_VALUE} - {ROUND_TRIP_COST_USD} END"
    )
    return f"""
    SELECT timestamp, entry_ts AS entry_timestamp, exit_ts_{horizon} AS exit_timestamp,
           year, '{horizon}'::INTEGER AS horizon_minutes, scan_type, session_bucket, dow,
           trend_bin, volume_bin, range_bin,
           CASE WHEN direction = 1 THEN 'long' ELSE 'short' END AS direction_label,
           entry_open AS entry_price, exit_close_{horizon} AS exit_price,
           (epoch(exit_ts_{horizon}) - epoch(entry_ts)) / 60.0 AS hold_minutes,
           {pnl} AS pnl
    FROM (
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'breakout_continuation' AS scan_type, 1 AS direction
      FROM feature_base
      WHERE close > high20_prev AND trend_bin = 1 AND volume_bin >= 1
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'breakout_continuation', -1
      FROM feature_base
      WHERE close < low20_prev AND trend_bin = -1 AND volume_bin >= 1
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'opening_range_breakout', 1
      FROM feature_base
      WHERE opening_high_prev IS NOT NULL AND ny_min >= 600 AND ny_min <= 959
        AND close > opening_high_prev AND trend_bin = 1 AND volume_bin >= 1
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'opening_range_breakout', -1
      FROM feature_base
      WHERE opening_low_prev IS NOT NULL AND ny_min >= 600 AND ny_min <= 959
        AND close < opening_low_prev AND trend_bin = -1 AND volume_bin >= 1
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'failed_breakout_reversion', -1
      FROM feature_base
      WHERE high > high20_prev AND close < high20_prev AND z50 >= 1.0 AND abs(body_to_range) <= 0.45
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'failed_breakout_reversion', 1
      FROM feature_base
      WHERE low < low20_prev AND close > low20_prev AND z50 <= -1.0 AND abs(body_to_range) <= 0.45
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'vwap_reclaim_continuation', 1
      FROM feature_base
      WHERE prev_vwap_dist < 0 AND vwap_dist >= 0 AND trend_bin = 1 AND volume_bin >= 0 AND ret1 > 0
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'vwap_reclaim_continuation', -1
      FROM feature_base
      WHERE prev_vwap_dist > 0 AND vwap_dist <= 0 AND trend_bin = -1 AND volume_bin >= 0 AND ret1 < 0
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'outside_bar_momentum', 1
      FROM feature_base
      WHERE prev_high IS NOT NULL AND high >= prev_high AND low <= prev_low
        AND body_to_range >= 0.55 AND close > open AND volume_bin >= 1
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'outside_bar_momentum', -1
      FROM feature_base
      WHERE prev_low IS NOT NULL AND high >= prev_high AND low <= prev_low
        AND body_to_range <= -0.55 AND close < open AND volume_bin >= 1
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'range_expansion_continuation', 1
      FROM feature_base
      WHERE body_to_range >= 0.6 AND range_bin >= 2 AND volume_bin >= 1
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'range_expansion_continuation', -1
      FROM feature_base
      WHERE body_to_range <= -0.6 AND range_bin >= 2 AND volume_bin >= 1
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'trend_pullback_reclaim', 1
      FROM feature_base
      WHERE trend_bin = 1 AND ret5 < 0 AND ret1 > 0 AND close > ma20
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'trend_pullback_reclaim', -1
      FROM feature_base
      WHERE trend_bin = -1 AND ret5 > 0 AND ret1 < 0 AND close < ma20
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'zscore_mean_reversion', -1
      FROM feature_base
      WHERE z50 >= 2 AND volume_bin <= 1
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'zscore_mean_reversion', 1
      FROM feature_base
      WHERE z50 <= -2 AND volume_bin <= 1
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'volume_climax_reversion', -1
      FROM feature_base
      WHERE z50 >= 1.5 AND volume_bin >= 2 AND abs(body_to_range) <= 0.35
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'volume_climax_reversion', 1
      FROM feature_base
      WHERE z50 <= -1.5 AND volume_bin >= 2 AND abs(body_to_range) <= 0.35
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'session_extreme_reversion', -1
      FROM feature_base
      WHERE session_high_prev IS NOT NULL AND close >= session_high_prev - range20 * 0.1
        AND z50 >= 1.25 AND volume_bin <= 1 AND abs(body_to_range) <= 0.45
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'session_extreme_reversion', 1
      FROM feature_base
      WHERE session_low_prev IS NOT NULL AND close <= session_low_prev + range20 * 0.1
        AND z50 <= -1.25 AND volume_bin <= 1 AND abs(body_to_range) <= 0.45
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'low_volume_drift', 1
      FROM feature_base
      WHERE trend_bin = 1 AND volume_bin = -1 AND ret1 > 0
      UNION ALL
      SELECT timestamp, entry_ts, entry_open, exit_ts_{horizon}, exit_close_{horizon},
             year, session_bucket, dow, trend_bin, volume_bin, range_bin,
             'low_volume_drift', -1
      FROM feature_base
      WHERE trend_bin = -1 AND volume_bin = -1 AND ret1 < 0
    ) raw_signals
    """


def load_edge_stats(con: duckdb.DuckDBPyConnection) -> list[EdgeStats]:
    rows = con.execute(
        """
        WITH edge_year AS (
          SELECT edge_id, scan_type, horizon_minutes, session_bucket, dow, direction_label,
                 trend_bin, volume_bin, range_bin, year,
                 count(*) AS trades,
                 sum(pnl) AS net_pnl,
                 avg(pnl) AS avg_pnl,
                 avg(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                 avg(hold_minutes) AS avg_hold_minutes
          FROM candidate_signals
          GROUP BY ALL
        ), edge_all AS (
          SELECT edge_id, any_value(scan_type) AS scan_type, any_value(horizon_minutes) AS horizon_minutes,
                 any_value(session_bucket) AS session_bucket, any_value(dow) AS dow,
                 any_value(direction_label) AS direction_label, any_value(trend_bin) AS trend_bin,
                 any_value(volume_bin) AS volume_bin, any_value(range_bin) AS range_bin,
                 sum(trades) AS trades, sum(net_pnl) AS net_pnl,
                 sum(net_pnl) / NULLIF(sum(trades), 0) AS avg_pnl,
                 sum(win_rate * trades) / NULLIF(sum(trades), 0) AS win_rate,
                 sum(avg_hold_minutes * trades) / NULLIF(sum(trades), 0) AS avg_hold_minutes,
                 sum(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) AS positive_years,
                 min(net_pnl) AS worst_year_pnl
          FROM edge_year
          GROUP BY edge_id
        )
        SELECT a.edge_id, a.scan_type, a.horizon_minutes, a.session_bucket, a.dow, a.direction_label,
               a.trend_bin, a.volume_bin, a.range_bin, a.trades, a.net_pnl, a.avg_pnl, a.win_rate,
               a.avg_hold_minutes, a.positive_years, a.worst_year_pnl,
               y2010.net_pnl, y2011.net_pnl, y2012.net_pnl, y2013.net_pnl, y2014.net_pnl,
               y2015.net_pnl, y2016.net_pnl, y2017.net_pnl, y2018.net_pnl, y2019.net_pnl,
               y2020.net_pnl, y2021.net_pnl, y2022.net_pnl, y2023.net_pnl, y2024.net_pnl,
               y2025.net_pnl, y2026.net_pnl
        FROM edge_all a
        LEFT JOIN edge_year y2010 ON a.edge_id = y2010.edge_id AND y2010.year = 2010
        LEFT JOIN edge_year y2011 ON a.edge_id = y2011.edge_id AND y2011.year = 2011
        LEFT JOIN edge_year y2012 ON a.edge_id = y2012.edge_id AND y2012.year = 2012
        LEFT JOIN edge_year y2013 ON a.edge_id = y2013.edge_id AND y2013.year = 2013
        LEFT JOIN edge_year y2014 ON a.edge_id = y2014.edge_id AND y2014.year = 2014
        LEFT JOIN edge_year y2015 ON a.edge_id = y2015.edge_id AND y2015.year = 2015
        LEFT JOIN edge_year y2016 ON a.edge_id = y2016.edge_id AND y2016.year = 2016
        LEFT JOIN edge_year y2017 ON a.edge_id = y2017.edge_id AND y2017.year = 2017
        LEFT JOIN edge_year y2018 ON a.edge_id = y2018.edge_id AND y2018.year = 2018
        LEFT JOIN edge_year y2019 ON a.edge_id = y2019.edge_id AND y2019.year = 2019
        LEFT JOIN edge_year y2020 ON a.edge_id = y2020.edge_id AND y2020.year = 2020
        LEFT JOIN edge_year y2021 ON a.edge_id = y2021.edge_id AND y2021.year = 2021
        LEFT JOIN edge_year y2022 ON a.edge_id = y2022.edge_id AND y2022.year = 2022
        LEFT JOIN edge_year y2023 ON a.edge_id = y2023.edge_id AND y2023.year = 2023
        LEFT JOIN edge_year y2024 ON a.edge_id = y2024.edge_id AND y2024.year = 2024
        LEFT JOIN edge_year y2025 ON a.edge_id = y2025.edge_id AND y2025.year = 2025
        LEFT JOIN edge_year y2026 ON a.edge_id = y2026.edge_id AND y2026.year = 2026
        WHERE a.trades >= 100 AND a.net_pnl > 0 AND a.avg_pnl > 0
        ORDER BY a.net_pnl DESC
        """
    ).fetchall()
    stats = []
    for row in rows:
        yearly = tuple(float(value or 0.0) for value in row[16:33])
        stats.append(
            EdgeStats(
                edge_id=str(row[0]),
                scan_type=str(row[1]),
                horizon_minutes=int(row[2]),
                session_bucket=str(row[3]),
                dow=int(row[4]),
                direction_label=str(row[5]),
                trend_bin=int(row[6]),
                volume_bin=int(row[7]),
                range_bin=int(row[8]),
                trades=int(row[9]),
                net_pnl=float(row[10]),
                avg_pnl=float(row[11]),
                win_rate=float(row[12]),
                avg_hold_minutes=float(row[13]),
                positive_years=sum(1 for value in yearly if value > 0),
                worst_year_pnl=min(yearly),
                yearly_pnl=yearly,
            )
        )
    return stats


def select_edge_pool(edge_stats: Sequence[EdgeStats], max_edge_pool: int) -> list[EdgeStats]:
    def rank(edge: EdgeStats) -> tuple[float, ...]:
        return (
            float(edge.positive_years),
            edge.net_pnl / max(edge.avg_hold_minutes, 1.0),
            edge.net_pnl,
            edge.avg_pnl,
            -float(edge.horizon_minutes),
        )

    deduped = {edge.edge_id: edge for edge in edge_stats}
    return sorted(deduped.values(), key=rank, reverse=True)[:max_edge_pool]


def beam_search(
    edge_pool: Sequence[EdgeStats],
    max_edges_per_strategy: int,
    beam_width: int,
) -> list[BeamCandidate]:
    empty = BeamCandidate((), tuple(0.0 for _ in YEARS), 0.0, 0, 0.0, 0.0)
    beam = [empty]
    completed: dict[tuple[str, ...], BeamCandidate] = {}
    by_id = {edge.edge_id: edge for edge in edge_pool}
    for _ in range(max_edges_per_strategy):
        next_beam: dict[tuple[str, ...], BeamCandidate] = {}
        for candidate in beam:
            used = set(candidate.edge_ids)
            for edge in edge_pool:
                if edge.edge_id in used:
                    continue
                edge_ids = tuple(sorted((*candidate.edge_ids, edge.edge_id)))
                if edge_ids in next_beam:
                    continue
                yearly = tuple(a + b for a, b in zip(candidate.yearly_pnl, edge.yearly_pnl, strict=True))
                net_pnl = sum(yearly)
                positive_years = sum(1 for value in yearly if value > 0)
                worst_year_pnl = min(yearly)
                avg_hold = sum(by_id[edge_id].avg_hold_minutes for edge_id in edge_ids) / len(edge_ids)
                next_beam[edge_ids] = BeamCandidate(
                    edge_ids=edge_ids,
                    yearly_pnl=yearly,
                    net_pnl=net_pnl,
                    positive_years=positive_years,
                    worst_year_pnl=worst_year_pnl,
                    avg_hold_minutes=avg_hold,
                )
        ranked = sorted(next_beam.values(), key=beam_sort_key, reverse=True)
        beam = ranked[:beam_width]
        for candidate in beam:
            if candidate.positive_years >= 16:
                completed[candidate.edge_ids] = candidate
    if not completed:
        completed = {candidate.edge_ids: candidate for candidate in beam[:beam_width]}
    return sorted(completed.values(), key=beam_sort_key, reverse=True)


def beam_sort_key(candidate: BeamCandidate) -> tuple[float, ...]:
    all_positive = candidate.positive_years == len(YEARS) and candidate.worst_year_pnl > 0
    if OBJECTIVE == "net":
        return (
            1.0 if all_positive else 0.0,
            float(candidate.positive_years),
            candidate.net_pnl,
            candidate.worst_year_pnl,
            candidate.net_pnl / max(candidate.avg_hold_minutes, 1.0),
            -float(len(candidate.edge_ids)),
            -candidate.avg_hold_minutes,
        )
    if OBJECTIVE == "short":
        return (
            1.0 if all_positive else 0.0,
            float(candidate.positive_years),
            -candidate.avg_hold_minutes,
            candidate.net_pnl,
            candidate.worst_year_pnl,
            -float(len(candidate.edge_ids)),
        )
    return (
        1.0 if all_positive else 0.0,
        float(candidate.positive_years),
        candidate.worst_year_pnl,
        candidate.net_pnl / max(candidate.avg_hold_minutes, 1.0),
        candidate.net_pnl,
        -float(len(candidate.edge_ids)),
        -candidate.avg_hold_minutes,
    )


def exact_replay_candidates(
    con: duckdb.DuckDBPyConnection,
    edge_pool: Sequence[EdgeStats],
    candidates: Sequence[BeamCandidate],
) -> list[dict[str, Any]]:
    edge_by_id = {edge.edge_id: edge for edge in edge_pool}
    exact_results = []
    for candidate in candidates:
        edges = [edge_by_id[edge_id] for edge_id in candidate.edge_ids]
        if not edges:
            continue
        edge_values = ", ".join(f"('{sql_string(edge.edge_id)}', {rank})" for rank, edge in enumerate(edges))
        rows = con.execute(
            f"""
            WITH selected(edge_id, edge_rank) AS (VALUES {edge_values})
            SELECT s.timestamp, s.entry_timestamp, s.exit_timestamp, s.year, s.edge_id,
                   selected.edge_rank, s.pnl, s.hold_minutes
            FROM candidate_signals s
            JOIN selected ON s.edge_id = selected.edge_id
            ORDER BY s.timestamp, selected.edge_rank
            """
        ).fetchall()
        metrics, trades = replay_rows(rows)
        yearly = metrics["yearly_pnl"]
        exact_results.append(
            {
                "edge_count": len(edges),
                "all_years_positive": all(value > 0 for value in yearly.values()),
                "positive_years": sum(1 for value in yearly.values() if value > 0),
                "worst_year_pnl": min(yearly.values()) if yearly else 0.0,
                "metrics": metrics,
                "additive_beam": {
                    "net_pnl": candidate.net_pnl,
                    "positive_years": candidate.positive_years,
                    "worst_year_pnl": candidate.worst_year_pnl,
                    "avg_hold_minutes": candidate.avg_hold_minutes,
                    "yearly_pnl": dict(zip(YEARS, candidate.yearly_pnl, strict=True)),
                },
                "edges": [edge.to_dict() for edge in edges],
                "sample_trades": trades[:10],
            }
        )
    return exact_results


def replay_rows(rows: Sequence[tuple[Any, ...]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    yearly = {year: 0.0 for year in YEARS}
    trades = []
    selected_trades = []
    occupied_until: datetime | None = None
    last_signal_timestamp: datetime | None = None
    for timestamp, entry_timestamp, exit_timestamp, year, edge_id, _edge_rank, pnl, hold_minutes in rows:
        if timestamp == last_signal_timestamp:
            continue
        if occupied_until is not None and timestamp < occupied_until:
            continue
        pnl_value = float(pnl)
        yearly[int(year)] = yearly.get(int(year), 0.0) + pnl_value
        selected = {
            "timestamp": timestamp.isoformat(),
            "entry_timestamp": entry_timestamp.isoformat(),
            "exit_timestamp": exit_timestamp.isoformat(),
            "year": int(year),
            "edge_id": str(edge_id),
            "pnl": pnl_value,
            "hold_minutes": float(hold_minutes),
        }
        selected_trades.append(selected)
        trades.append((pnl_value, float(hold_minutes)))
        occupied_until = exit_timestamp
        last_signal_timestamp = timestamp
    pnls = [trade[0] for trade in trades]
    holds = [trade[1] for trade in trades]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
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
        "yearly_pnl": yearly,
    }
    return metrics, selected_trades


def exact_sort_key(result: dict[str, Any]) -> tuple[float, ...]:
    metrics = result["metrics"]
    all_positive = bool(result["all_years_positive"])
    if OBJECTIVE == "net":
        return (
            1.0 if all_positive else 0.0,
            float(result["positive_years"]),
            float(metrics["net_pnl"]),
            float(result["worst_year_pnl"]),
            float(metrics.get("net_pnl_to_max_drawdown") or 0.0),
            -float(metrics["avg_hold_minutes"]),
            -float(result["edge_count"]),
        )
    if OBJECTIVE == "short":
        return (
            1.0 if all_positive else 0.0,
            float(result["positive_years"]),
            -float(metrics["avg_hold_minutes"]),
            float(metrics["net_pnl"]),
            float(result["worst_year_pnl"]),
            -float(result["edge_count"]),
        )
    return (
        1.0 if all_positive else 0.0,
        float(result["positive_years"]),
        float(result["worst_year_pnl"]),
        float(metrics["net_pnl"]) / max(float(metrics["avg_hold_minutes"]), 1.0),
        float(metrics["net_pnl"]),
        float(metrics.get("net_pnl_to_max_drawdown") or 0.0),
        -float(result["edge_count"]),
        -float(metrics["avg_hold_minutes"]),
    )


def sql_string(value: str) -> str:
    return value.replace("'", "''")


def json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


if __name__ == "__main__":
    raise SystemExit(main())
