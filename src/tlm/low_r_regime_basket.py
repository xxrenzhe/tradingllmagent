from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Sequence

import duckdb

from .metrics import calculate_metrics


@dataclass(frozen=True)
class RegimeEdge:
    scan_type: str
    direction_label: str
    horizon_minutes: int
    session_bucket: str
    dow: int
    trend_bin: int
    volume_bin: int
    range_bin: int
    take_profit_r: float

    @property
    def direction(self) -> int:
        return 1 if self.direction_label == "long" else -1


@dataclass(frozen=True)
class LowRRegimeBasketConfig:
    symbol: str = "NQ_CME"
    timeframe: str = "1m"
    data_root: str = "data"
    date_from: str = "2020-01-01"
    date_to: str = "2026-04-27"
    preset: str = "simple_robust_low_r"
    starting_equity: float = 100_000.0
    point_value: float = 20.0
    tick_size: float = 0.25
    round_trip_fees_usd: float = 15.0
    slippage_ticks_per_side: float = 1.0
    stop_range_multiple: float = 10.0
    min_stop_points: float = 8.0
    max_stop_points: float = 90.0
    max_hold_minutes: int = 300
    max_concurrent_positions: int = 12
    flatten_on_date_change: bool = True
    conservative_intrabar_ordering: bool = True


@dataclass(frozen=True)
class LowRRegimeTrade:
    edge_index: int
    scan_type: str
    side: str
    signal_time: datetime
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    take_profit_r: float
    stop_points: float
    bars_held: int
    gross_pnl: float
    costs: float
    net_pnl: float
    exit_reason: str

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["signal_time"] = self.signal_time.isoformat()
        payload["entry_time"] = self.entry_time.isoformat()
        payload["exit_time"] = self.exit_time.isoformat()
        return payload


@dataclass
class _OpenTrade:
    edge_index: int
    edge: RegimeEdge
    signal_time: datetime
    entry_index: int
    entry_time: datetime
    entry_price: float
    stop_loss: float
    take_profit: float
    stop_points: float


TOP_NET_2019_LOW_R_EDGES: tuple[RegimeEdge, ...] = (
    RegimeEdge("opening_range_breakout", "long", 120, "utc_1700_2059", 3, 1, 1, 0, 1.25),
    RegimeEdge("low_volume_drift", "long", 120, "utc_2100_2359", 0, 1, -1, 0, 0.75),
    RegimeEdge("opening_range_breakout", "long", 120, "utc_1700_2059", 3, 1, 1, 1, 1.25),
    RegimeEdge("opening_range_breakout", "long", 120, "utc_1700_2059", 4, 1, 1, 0, 1.25),
    RegimeEdge("low_volume_drift", "long", 120, "utc_1700_2059", 3, 1, -1, 1, 0.75),
    RegimeEdge("opening_range_breakout", "long", 120, "utc_1700_2059", 1, 1, 1, 0, 1.25),
    RegimeEdge("opening_range_breakout", "long", 120, "utc_1200_1659", 1, 1, 1, 1, 1.25),
    RegimeEdge("opening_range_breakout", "long", 120, "utc_1200_1659", 1, 1, 1, 0, 1.25),
    RegimeEdge("zscore_mean_reversion", "long", 120, "utc_0600_1159", 3, -1, 1, 0, 1.5),
    RegimeEdge("breakout_continuation", "long", 120, "utc_1200_1659", 1, 1, 3, 3, 1.5),
    RegimeEdge("trend_pullback_reclaim", "long", 120, "utc_1200_1659", 1, 1, -1, -1, 1.0),
)


SIMPLE_ROBUST_LOW_R_EDGES: tuple[RegimeEdge, ...] = tuple(
    TOP_NET_2019_LOW_R_EDGES[index] for index in (0, 2, 3, 5, 8, 10)
)


ANNUAL_2023_LOW_VOLUME_LOW_R_EDGES: tuple[RegimeEdge, ...] = (
    RegimeEdge("low_volume_drift", "long", 120, "utc_2100_2359", 0, 1, -1, 0, 0.75),
    RegimeEdge("low_volume_drift", "long", 120, "utc_1700_2059", 3, 1, -1, 1, 0.75),
    RegimeEdge("low_volume_drift", "long", 120, "utc_1700_2059", 3, 1, -1, 0, 0.75),
    RegimeEdge("low_volume_drift", "long", 120, "utc_1700_2059", 3, 1, -1, -1, 0.75),
    RegimeEdge("low_volume_drift", "long", 120, "utc_2100_2359", 3, 1, -1, -1, 0.75),
    RegimeEdge("low_volume_drift", "long", 120, "utc_1200_1659", 1, 1, -1, -1, 0.75),
    RegimeEdge("low_volume_drift", "long", 120, "utc_1700_2059", 1, 1, -1, -1, 0.75),
    RegimeEdge("low_volume_drift", "long", 120, "utc_2100_2359", 3, 1, -1, 0, 0.75),
    RegimeEdge("low_volume_drift", "long", 120, "utc_1200_1659", 1, 1, -1, 0, 0.75),
    RegimeEdge("low_volume_drift", "long", 120, "utc_1700_2059", 4, 1, -1, 1, 0.75),
)


PRESETS: dict[str, tuple[RegimeEdge, ...]] = {
    "simple_robust_low_r": SIMPLE_ROBUST_LOW_R_EDGES,
    "top_net_2019_low_r": TOP_NET_2019_LOW_R_EDGES,
    "annual_2023_low_volume_low_r": ANNUAL_2023_LOW_VOLUME_LOW_R_EDGES,
}

DEFAULT_TAKE_PROFIT_R_CANDIDATES = (0.5, 0.75, 1.0, 1.25, 1.5)


def run_low_r_regime_basket_backtest(config: LowRRegimeBasketConfig | None = None) -> dict:
    config = config or LowRRegimeBasketConfig()
    edges = PRESETS[config.preset]
    return run_low_r_regime_basket_backtest_with_edges(config, edges)


def run_low_r_regime_basket_backtest_with_edges(
    config: LowRRegimeBasketConfig,
    edges: Sequence[RegimeEdge],
) -> dict:
    pattern = _bar_pattern(config)
    con = duckdb.connect(":memory:")
    try:
        _ensure_feature_table(con, pattern, config)
        bars = _load_feature_bars(con)
        signals = _load_signals(con, edges, config)
    finally:
        con.close()
    trades = _replay_low_r_exits(bars, signals, edges, config)
    return _build_result(bars, signals, trades, edges, config)


def optimize_low_r_take_profit(
    config: LowRRegimeBasketConfig | None = None,
    take_profit_candidates: Sequence[float] = DEFAULT_TAKE_PROFIT_R_CANDIDATES,
    min_win_rate: float = 0.54,
    min_positive_year_ratio: float = 0.75,
) -> dict:
    config = config or LowRRegimeBasketConfig()
    base_edges = PRESETS[config.preset]
    candidates = tuple(sorted({float(value) for value in take_profit_candidates if 0 < float(value) <= 1.5}))
    if not candidates:
        raise ValueError("take_profit_candidates must include at least one value in (0, 1.5].")
    pattern = _bar_pattern(config)
    con = duckdb.connect(":memory:")
    try:
        _ensure_feature_table(con, pattern, config)
        bars = _load_feature_bars(con)
        base_signals = _load_signals(con, base_edges, config)
    finally:
        con.close()

    scan_types = sorted({edge.scan_type for edge in base_edges})
    evaluated = []
    profiles = _take_profit_profiles(scan_types, candidates)
    for profile in profiles:
        evaluated.append(
            _evaluate_take_profit_profile(
                bars,
                base_signals,
                base_edges,
                config,
                profile,
                min_win_rate,
                min_positive_year_ratio,
            )
        )
    evaluated.sort(
        key=lambda row: (
            row["score"],
            row["metrics"].get("net_pnl_to_max_drawdown") or 0,
            row["metrics"].get("profit_factor") or 0,
            row["metrics"].get("net_pnl") or 0,
        ),
        reverse=True,
    )
    best = evaluated[0] if evaluated else None
    best_result = None
    if best is not None:
        best_edges = _edges_with_take_profit_profile(base_edges, best["take_profit_profile"])
        best_trades = _replay_low_r_exits(bars, base_signals, best_edges, config)
        best_result = _build_result(bars, base_signals, best_trades, best_edges, config)
    return {
        "strategy_name": "low_r_regime_basket_tp_optimization",
        "config": asdict(config),
        "candidate_count": len(evaluated),
        "take_profit_candidates": list(candidates),
        "min_win_rate": min_win_rate,
        "min_positive_year_ratio": min_positive_year_ratio,
        "best": best,
        "top_candidates": evaluated[:20],
        "best_result": best_result,
    }


def _take_profit_profiles(scan_types: Sequence[str], candidates: Sequence[float]) -> list[dict[str, float]]:
    exhaustive_count = len(candidates) ** len(scan_types)
    if exhaustive_count <= 250:
        return [
            dict(zip(scan_types, profile_values, strict=True))
            for profile_values in product(candidates, repeat=len(scan_types))
        ]
    profiles: dict[tuple[tuple[str, float], ...], dict[str, float]] = {}

    def add(profile: dict[str, float]) -> None:
        profiles[tuple(sorted(profile.items()))] = dict(profile)

    for value in candidates:
        add({scan_type: value for scan_type in scan_types})
    seed = {scan_type: 0.75 for scan_type in scan_types}
    add(seed)
    for scan_type in scan_types:
        for value in candidates:
            profile = dict(seed)
            profile[scan_type] = value
            add(profile)
    return list(profiles.values())


def _evaluate_take_profit_profile(
    bars: Sequence[dict],
    signals: Sequence[dict],
    base_edges: Sequence[RegimeEdge],
    config: LowRRegimeBasketConfig,
    profile: dict[str, float],
    min_win_rate: float,
    min_positive_year_ratio: float,
) -> dict:
    edges = _edges_with_take_profit_profile(base_edges, profile)
    trades = _replay_low_r_exits(bars, signals, edges, config)
    result = _build_result(bars, signals, trades, edges, config)
    score = _optimization_score(result, min_win_rate, min_positive_year_ratio)
    return {
        "score": score,
        "take_profit_profile": profile,
        "metrics": result["metrics"],
        "yearly_results": result["yearly_results"],
        "exit_reasons": result["exit_reasons"],
        "edge_summary": result["edge_summary"],
    }


def _bar_pattern(config: LowRRegimeBasketConfig) -> str:
    return str(
        Path(config.data_root)
        / "bars"
        / config.timeframe
        / config.symbol
        / "date=*"
        / "part-000.parquet"
    )


def _ensure_feature_table(con: duckdb.DuckDBPyConnection, pattern: str, config: LowRRegimeBasketConfig) -> None:
    con.execute(
        """
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
                 max(CASE WHEN (CAST(strftime(timestamp, '%H') AS INTEGER)*60 + CAST(strftime(timestamp, '%M') AS INTEGER)) BETWEEN 810 AND 839 THEN high END) OVER (
                   PARTITION BY CAST(timestamp AS DATE)
                   ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                 ) AS opening_high_prev,
                 min(CASE WHEN (CAST(strftime(timestamp, '%H') AS INTEGER)*60 + CAST(strftime(timestamp, '%M') AS INTEGER)) BETWEEN 810 AND 839 THEN low END) OVER (
                   PARTITION BY CAST(timestamp AS DATE)
                   ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                 ) AS opening_low_prev
          FROM read_parquet(?, union_by_name=true)
          WHERE timestamp >= CAST(? AS TIMESTAMP) AND timestamp < CAST(? AS TIMESTAMP) + INTERVAL 1 DAY
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
        """,
        [pattern, config.date_from, config.date_to],
    )


def _load_feature_bars(con: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = con.execute(
        """
        SELECT row_number() OVER (ORDER BY timestamp)-1 AS bar_index,
               timestamp, open, high, low, close, coalesce(range20, high-low) AS range20
        FROM regime_feature_base
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


def _load_signals(
    con: duckdb.DuckDBPyConnection,
    edges: Sequence[RegimeEdge],
    config: LowRRegimeBasketConfig,
) -> list[dict]:
    edge_values = ", ".join(
        "("
        f"{index}, "
        f"'{_sql_string(edge.scan_type)}', "
        f"'{_sql_string(edge.session_bucket)}', "
        f"{edge.dow}, {edge.trend_bin}, {edge.volume_bin}, {edge.range_bin}, {edge.direction}"
        ")"
        for index, edge in enumerate(edges)
    )
    rows = con.execute(
        f"""
        WITH edges(edge_index, scan_type, session_bucket, dow, trend_bin, volume_bin, range_bin, direction) AS (
          VALUES {edge_values}
        ), raw AS (
          SELECT row_number() OVER (ORDER BY timestamp)-1 AS bar_index, *,
                 lead(open, 1) OVER (ORDER BY timestamp) AS entry_open,
                 lead(timestamp, 1) OVER (ORDER BY timestamp) AS entry_time
          FROM regime_feature_base
        ), feats AS (
          SELECT *,
                 CASE WHEN close > high20_prev THEN 1 WHEN close < low20_prev THEN -1 ELSE 0 END AS breakout20
          FROM raw
          WHERE entry_open IS NOT NULL
            AND ma20 IS NOT NULL AND ma50 IS NOT NULL AND close_std50 IS NOT NULL
            AND vol50 IS NOT NULL AND range20 IS NOT NULL
        ), signals AS (
          SELECT bar_index, timestamp, entry_time, entry_open, range20,
                 'breakout_continuation' AS scan_type, session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1 AS direction
          FROM feats
          WHERE breakout20 = 1 AND trend_bin = 1 AND volume_bin >= 1
          UNION ALL
          SELECT bar_index, timestamp, entry_time, entry_open, range20,
                 'breakout_continuation', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1
          FROM feats
          WHERE breakout20 = -1 AND trend_bin = -1 AND volume_bin >= 1
          UNION ALL
          SELECT bar_index, timestamp, entry_time, entry_open, range20,
                 'opening_range_breakout', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1
          FROM feats
          WHERE opening_high_prev IS NOT NULL AND moday >= 840 AND close > opening_high_prev
            AND trend_bin = 1 AND volume_bin >= 1
          UNION ALL
          SELECT bar_index, timestamp, entry_time, entry_open, range20,
                 'opening_range_breakout', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1
          FROM feats
          WHERE opening_low_prev IS NOT NULL AND moday >= 840 AND close < opening_low_prev
            AND trend_bin = -1 AND volume_bin >= 1
          UNION ALL
          SELECT bar_index, timestamp, entry_time, entry_open, range20,
                 'trend_pullback_reclaim', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1
          FROM feats
          WHERE trend_bin = 1 AND ret5 < 0 AND ret1 > 0 AND close > ma20
          UNION ALL
          SELECT bar_index, timestamp, entry_time, entry_open, range20,
                 'trend_pullback_reclaim', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1
          FROM feats
          WHERE trend_bin = -1 AND ret5 > 0 AND ret1 < 0 AND close < ma20
          UNION ALL
          SELECT bar_index, timestamp, entry_time, entry_open, range20,
                 'zscore_mean_reversion', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1
          FROM feats
          WHERE z50 >= 2 AND volume_bin <= 1
          UNION ALL
          SELECT bar_index, timestamp, entry_time, entry_open, range20,
                 'zscore_mean_reversion', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1
          FROM feats
          WHERE z50 <= -2 AND volume_bin <= 1
          UNION ALL
          SELECT bar_index, timestamp, entry_time, entry_open, range20,
                 'low_volume_drift', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, 1
          FROM feats
          WHERE trend_bin = 1 AND volume_bin = -1 AND ret1 > 0
          UNION ALL
          SELECT bar_index, timestamp, entry_time, entry_open, range20,
                 'low_volume_drift', session_bucket, dow,
                 trend_bin, volume_bin, range_bin, -1
          FROM feats
          WHERE trend_bin = -1 AND volume_bin = -1 AND ret1 < 0
        ), matched AS (
          SELECT s.bar_index, s.timestamp AS signal_time, s.entry_time, s.entry_open, s.range20,
                 e.edge_index, e.scan_type, e.direction
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
        SELECT *
        FROM matched
        ORDER BY signal_time, edge_index
        """,
    ).fetchall()
    return [
        {
            "bar_index": int(row[0]),
            "signal_time": row[1],
            "entry_time": row[2],
            "entry_open": float(row[3]),
            "range20": float(row[4] or 0.0),
            "edge_index": int(row[5]),
            "scan_type": row[6],
            "direction": int(row[7]),
        }
        for row in rows
    ]


def _replay_low_r_exits(
    bars: Sequence[dict],
    signals: Sequence[dict],
    edges: Sequence[RegimeEdge],
    config: LowRRegimeBasketConfig,
) -> list[LowRRegimeTrade]:
    if config.max_concurrent_positions == 1:
        return _replay_low_r_exits_single_slot(bars, signals, edges, config)
    bars_by_entry_time = {bar["timestamp"]: index for index, bar in enumerate(bars)}
    closed: list[LowRRegimeTrade] = []
    open_trades: list[_OpenTrade] = []
    signals_by_entry_index: dict[int, list[dict]] = {}
    for signal in signals:
        entry_index = bars_by_entry_time.get(signal["entry_time"])
        if entry_index is None:
            continue
        signals_by_entry_index.setdefault(entry_index, []).append(signal)

    for index, bar in enumerate(bars):
        still_open: list[_OpenTrade] = []
        for trade in open_trades:
            if config.flatten_on_date_change and bar["timestamp"].date() != trade.entry_time.date():
                previous_index = max(trade.entry_index, index - 1)
                previous_bar = bars[previous_index]
                closed.append(
                    _close_open_trade(
                        trade,
                        previous_index,
                        previous_bar,
                        previous_bar["close"],
                        "date_change_flatten",
                        config,
                    )
                )
                continue
            closed_trade = _maybe_close_trade(trade, index, bar, config)
            if closed_trade is None:
                still_open.append(trade)
            else:
                closed.append(closed_trade)
        open_trades = still_open
        available_slots = max(config.max_concurrent_positions - len(open_trades), 0)
        if available_slots <= 0:
            continue
        for signal in signals_by_entry_index.get(index, [])[:available_slots]:
            edge = edges[signal["edge_index"]]
            open_trades.append(_open_trade(signal, edge, index, config))
    final_index = len(bars) - 1
    if final_index >= 0:
        final_bar = bars[final_index]
        for trade in open_trades:
            closed.append(_close_open_trade(trade, final_index, final_bar, final_bar["close"], "end_of_data", config))
    return closed


def _replay_low_r_exits_single_slot(
    bars: Sequence[dict],
    signals: Sequence[dict],
    edges: Sequence[RegimeEdge],
    config: LowRRegimeBasketConfig,
) -> list[LowRRegimeTrade]:
    bars_by_entry_time = {bar["timestamp"]: index for index, bar in enumerate(bars)}
    closed: list[LowRRegimeTrade] = []
    occupied_until = -1
    for signal in signals:
        entry_index = bars_by_entry_time.get(signal["entry_time"])
        if entry_index is None or entry_index <= occupied_until:
            continue
        edge = edges[signal["edge_index"]]
        trade = _open_trade(signal, edge, entry_index, config)
        closed_trade = _simulate_trade_exit(trade, bars, config)
        closed.append(closed_trade)
        occupied_until = trade.entry_index + closed_trade.bars_held
    return closed


def _simulate_trade_exit(
    trade: _OpenTrade,
    bars: Sequence[dict],
    config: LowRRegimeBasketConfig,
) -> LowRRegimeTrade:
    last_index = min(trade.entry_index + config.max_hold_minutes, len(bars) - 1)
    for index in range(trade.entry_index + 1, last_index + 1):
        if config.flatten_on_date_change and bars[index]["timestamp"].date() != trade.entry_time.date():
            previous_index = max(trade.entry_index, index - 1)
            previous_bar = bars[previous_index]
            return _close_open_trade(
                trade,
                previous_index,
                previous_bar,
                previous_bar["close"],
                "date_change_flatten",
                config,
            )
        closed_trade = _maybe_close_trade(trade, index, bars[index], config)
        if closed_trade is not None:
            return closed_trade
    final_bar = bars[last_index]
    return _close_open_trade(trade, last_index, final_bar, final_bar["close"], "time_exit", config)


def _open_trade(signal: dict, edge: RegimeEdge, entry_index: int, config: LowRRegimeBasketConfig) -> _OpenTrade:
    entry_price = float(signal["entry_open"])
    stop_points = _stop_points(float(signal["range20"]), config)
    if edge.direction == 1:
        stop_loss = entry_price - stop_points
        take_profit = entry_price + stop_points * edge.take_profit_r
    else:
        stop_loss = entry_price + stop_points
        take_profit = entry_price - stop_points * edge.take_profit_r
    return _OpenTrade(
        edge_index=int(signal["edge_index"]),
        edge=edge,
        signal_time=signal["signal_time"],
        entry_index=entry_index,
        entry_time=signal["entry_time"],
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        stop_points=stop_points,
    )


def _stop_points(range20: float, config: LowRRegimeBasketConfig) -> float:
    raw_stop = range20 * config.stop_range_multiple
    bounded = max(config.min_stop_points, min(raw_stop, config.max_stop_points))
    ticks = round(bounded / config.tick_size)
    return max(ticks * config.tick_size, config.tick_size)


def _maybe_close_trade(
    trade: _OpenTrade,
    index: int,
    bar: dict,
    config: LowRRegimeBasketConfig,
) -> LowRRegimeTrade | None:
    if index <= trade.entry_index:
        return None
    direction = trade.edge.direction
    stop_hit = bar["low"] <= trade.stop_loss if direction == 1 else bar["high"] >= trade.stop_loss
    target_hit = bar["high"] >= trade.take_profit if direction == 1 else bar["low"] <= trade.take_profit
    if stop_hit and target_hit and config.conservative_intrabar_ordering:
        return _close_open_trade(trade, index, bar, trade.stop_loss, "stop_loss_conservative", config)
    if target_hit:
        return _close_open_trade(trade, index, bar, trade.take_profit, "take_profit", config)
    if stop_hit:
        return _close_open_trade(trade, index, bar, trade.stop_loss, "stop_loss", config)
    if index - trade.entry_index >= config.max_hold_minutes:
        return _close_open_trade(trade, index, bar, bar["close"], "time_exit", config)
    return None


def _close_open_trade(
    trade: _OpenTrade,
    index: int,
    bar: dict,
    exit_price: float,
    exit_reason: str,
    config: LowRRegimeBasketConfig,
) -> LowRRegimeTrade:
    slippage_points = config.slippage_ticks_per_side * config.tick_size * 2
    if trade.edge.direction == 1:
        gross_points = exit_price - trade.entry_price - slippage_points
    else:
        gross_points = trade.entry_price - exit_price - slippage_points
    gross_pnl = gross_points * config.point_value
    net_pnl = gross_pnl - config.round_trip_fees_usd
    return LowRRegimeTrade(
        edge_index=trade.edge_index,
        scan_type=trade.edge.scan_type,
        side=trade.edge.direction_label,
        signal_time=trade.signal_time,
        entry_time=trade.entry_time,
        exit_time=bar["timestamp"],
        entry_price=trade.entry_price,
        exit_price=exit_price,
        stop_loss=trade.stop_loss,
        take_profit=trade.take_profit,
        take_profit_r=trade.edge.take_profit_r,
        stop_points=trade.stop_points,
        bars_held=index - trade.entry_index,
        gross_pnl=gross_pnl,
        costs=config.round_trip_fees_usd,
        net_pnl=net_pnl,
        exit_reason=exit_reason,
    )


def _build_result(
    bars: Sequence[dict],
    signals: Sequence[dict],
    trades: Sequence[LowRRegimeTrade],
    edges: Sequence[RegimeEdge],
    config: LowRRegimeBasketConfig,
) -> dict:
    trade_pnls = [trade.net_pnl for trade in trades]
    equity = [config.starting_equity]
    for pnl in trade_pnls:
        equity.append(equity[-1] + pnl)
    days = len({bar["timestamp"].date() for bar in bars}) or 1
    metrics = calculate_metrics(trade_pnls, equity, config.starting_equity, days).to_dict()
    return {
        "strategy_name": "low_r_regime_basket_v1",
        "symbol": config.symbol,
        "timeframe": config.timeframe,
        "bar_count": len(bars),
        "signal_count": len(signals),
        "filled_signal_count": len(trades),
        "metrics": metrics,
        "config": asdict(config),
        "edge_count": len(edges),
        "edge_summary": _edge_summary(edges),
        "exit_reasons": dict(sorted(Counter(trade.exit_reason for trade in trades).items())),
        "yearly_results": _yearly_results(trades),
        "trades": [trade.to_dict() for trade in trades],
    }


def _edge_summary(edges: Sequence[RegimeEdge]) -> dict:
    return {
        "scan_types": dict(sorted(Counter(edge.scan_type for edge in edges).items())),
        "take_profit_r": dict(sorted(Counter(edge.take_profit_r for edge in edges).items())),
        "sessions": dict(sorted(Counter(edge.session_bucket for edge in edges).items())),
    }


def _edges_with_take_profit_profile(
    edges: Sequence[RegimeEdge],
    profile: dict[str, float],
) -> tuple[RegimeEdge, ...]:
    return tuple(replace(edge, take_profit_r=profile.get(edge.scan_type, edge.take_profit_r)) for edge in edges)


def _optimization_score(result: dict, min_win_rate: float, min_positive_year_ratio: float) -> float:
    metrics = result["metrics"]
    win_rate = float(metrics.get("win_rate") or 0.0)
    net_pnl = float(metrics.get("net_pnl") or 0.0)
    max_drawdown = float(metrics.get("max_drawdown") or 0.0)
    yearly = result.get("yearly_results") or []
    positive_year_ratio = (
        sum(1 for row in yearly if float(row.get("net_pnl") or 0.0) > 0) / len(yearly)
        if yearly
        else 0.0
    )
    if win_rate < min_win_rate or positive_year_ratio < min_positive_year_ratio or net_pnl <= 0:
        return -1_000_000_000.0 + net_pnl
    drawdown_penalty = max_drawdown * 0.75
    return net_pnl - drawdown_penalty + positive_year_ratio * 50_000.0 + win_rate * 25_000.0


def _yearly_results(trades: Sequence[LowRRegimeTrade]) -> list[dict]:
    by_year: dict[int, list[float]] = {}
    for trade in trades:
        by_year.setdefault(trade.exit_time.year, []).append(trade.net_pnl)
    rows = []
    for year, pnls in sorted(by_year.items()):
        equity = [0.0]
        for pnl in pnls:
            equity.append(equity[-1] + pnl)
        metrics = calculate_metrics(pnls, equity, 100_000.0, 365).to_dict()
        rows.append({"year": year, **metrics})
    return rows


def _sql_string(value: str) -> str:
    return value.replace("'", "''")


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest low-R NQ regime basket strategy.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--date-from", default="2020-01-01")
    parser.add_argument("--date-to", default="2026-04-27")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="simple_robust_low_r")
    parser.add_argument("--stop-range-multiple", type=float, default=10.0)
    parser.add_argument("--min-stop-points", type=float, default=8.0)
    parser.add_argument("--max-stop-points", type=float, default=90.0)
    parser.add_argument("--max-hold-minutes", type=int, default=300)
    parser.add_argument("--max-concurrent-positions", type=int, default=12)
    parser.add_argument("--allow-overnight", action="store_true")
    parser.add_argument("--optimize-tp", action="store_true")
    parser.add_argument("--tp-candidates", default="0.5,0.75,1.0,1.25,1.5")
    parser.add_argument("--min-win-rate", type=float, default=0.54)
    parser.add_argument("--min-positive-year-ratio", type=float, default=0.75)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    config = LowRRegimeBasketConfig(
        data_root=args.data_root,
        symbol=args.symbol,
        date_from=args.date_from,
        date_to=args.date_to,
        preset=args.preset,
        stop_range_multiple=args.stop_range_multiple,
        min_stop_points=args.min_stop_points,
        max_stop_points=args.max_stop_points,
        max_hold_minutes=args.max_hold_minutes,
        max_concurrent_positions=args.max_concurrent_positions,
        flatten_on_date_change=not args.allow_overnight,
    )
    if args.optimize_tp:
        tp_candidates = tuple(float(raw.strip()) for raw in args.tp_candidates.split(",") if raw.strip())
        result = optimize_low_r_take_profit(
            config,
            take_profit_candidates=tp_candidates,
            min_win_rate=args.min_win_rate,
            min_positive_year_ratio=args.min_positive_year_ratio,
        )
    else:
        result = run_low_r_regime_basket_backtest(config)
    payload = json.dumps(result, indent=2, default=_json_default)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
