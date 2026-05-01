from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, time
from pathlib import Path
from statistics import mean
from typing import Sequence
from zoneinfo import ZoneInfo

import duckdb

from .metrics import calculate_metrics


@dataclass(frozen=True)
class RoleRetestConfig:
    symbol: str = "NQ_CME"
    timeframe: str = "5m"
    swing_left_bars: int = 3
    swing_right_bars: int = 3
    lookback_bars: int = 80
    atr_window: int = 14
    atr_percentile_window: int = 100
    atr_low_percentile: float = 30.0
    atr_high_percentile: float = 85.0
    pending_order_ttl_bars: int = 5
    minimum_rr: float = 1.2
    pre_touch_cancel_progress: float = 0.75
    body_impulse_multiple: float = 1.5
    atr_impulse_multiple: float = 1.0
    trend_ema_fast: int = 20
    trend_ema_slow: int = 50
    trend_htf_multiple: int = 3
    strict_trend_alignment: bool = False
    tick_size: float = 0.25
    point_value: float = 20.0
    slippage_ticks_per_side: float = 1.0
    round_trip_fees_usd: float = 5.0
    stop_buffer_ticks: int = 1
    ob_entry_ratio: float = 0.5
    zone_mode: str = "swing"
    ob_lookback_bars: int = 12
    max_zone_atr: float = 0.0
    require_liquidity_sweep: bool = False
    liquidity_sweep_lookback: int = 40
    sweep_buffer_ticks: int = 0
    require_fvg: bool = False
    volume_window: int = 20
    volume_percentile_window: int = 100
    min_relative_volume: float = 0.0
    max_relative_volume: float = 0.0
    min_volume_percentile: float = 0.0
    max_volume_percentile: float = 100.0
    max_hold_bars: int = 0
    flatten_outside_session: bool = False
    session_timezone: str = "America/New_York"
    trade_sessions: tuple[str, ...] = ("09:35-11:30", "13:30-15:45")
    starting_equity: float = 100_000.0
    conservative_intrabar_ordering: bool = True


@dataclass(frozen=True)
class RoleRetestTrade:
    side: str
    setup_time: datetime
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    rr: float
    zone_high: float
    zone_low: float
    break_close: float
    break_body_to_atr: float
    atr_percentile_rank: float
    bars_to_fill: int
    bars_held: int
    mfe_points: float
    mae_points: float
    gross_pnl: float
    costs: float
    net_pnl: float
    entry_reason: str
    exit_reason: str

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["setup_time"] = self.setup_time.isoformat()
        payload["entry_time"] = self.entry_time.isoformat()
        payload["exit_time"] = self.exit_time.isoformat()
        return payload


@dataclass
class PendingSetup:
    direction: int
    entry: float
    stop: float
    target: float
    rr: float
    zone_high: float
    zone_low: float
    break_close: float
    break_body_to_atr: float
    atr_percentile_rank: float
    created_index: int
    created_time: datetime
    best_excursion: float


@dataclass
class OpenPosition:
    direction: int
    entry: float
    stop: float
    target: float
    rr: float
    zone_high: float
    zone_low: float
    break_close: float
    break_body_to_atr: float
    atr_percentile_rank: float
    setup_index: int
    setup_time: datetime
    entry_index: int
    entry_time: datetime
    mfe_points: float = 0.0
    mae_points: float = 0.0


def load_role_retest_bars(data_root: Path, symbol: str, timeframe: str, date_from: str, date_to: str) -> list[dict]:
    pattern = data_root / "bars" / timeframe / symbol / "date=*" / "part-000.parquet"
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            """
            SELECT symbol, timestamp, open, high, low, close, tick_count, avg_spread
            FROM read_parquet(?, union_by_name=true)
            WHERE DATE(timestamp) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
            ORDER BY timestamp
            """,
            [str(pattern), date_from, date_to],
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "symbol": row[0],
            "timestamp": row[1],
            "open": float(row[2]),
            "high": float(row[3]),
            "low": float(row[4]),
            "close": float(row[5]),
            "tick_count": float(row[6] or 0.0),
            "avg_spread": None if row[7] is None else float(row[7]),
        }
        for row in rows
    ]


def run_role_retest_backtest(bars: Sequence[dict], config: RoleRetestConfig | None = None) -> dict:
    config = config or RoleRetestConfig()
    enriched = _with_indicators(list(bars), config)
    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []
    last_swing_high: tuple[int, float] | None = None
    last_swing_low: tuple[int, float] | None = None
    pending: PendingSetup | None = None
    position: OpenPosition | None = None
    trades: list[RoleRetestTrade] = []
    canceled_setups_by_reason: Counter[str] = Counter()
    setup_count = 0

    for index, bar in enumerate(enriched):
        confirmed_pivot_index = index - config.swing_right_bars
        if confirmed_pivot_index >= config.swing_left_bars:
            pivot_high = _confirmed_pivot_high(enriched, confirmed_pivot_index, config)
            if pivot_high is not None:
                last_swing_high = (confirmed_pivot_index, pivot_high)
                swing_highs.insert(0, last_swing_high)
                swing_highs = swing_highs[:100]
            pivot_low = _confirmed_pivot_low(enriched, confirmed_pivot_index, config)
            if pivot_low is not None:
                last_swing_low = (confirmed_pivot_index, pivot_low)
                swing_lows.insert(0, last_swing_low)
                swing_lows = swing_lows[:100]

        if position is not None:
            _update_position_excursion(bar, position)
            exit_price, exit_reason = _position_exit(bar, index, position, config)
            if exit_price is not None and exit_reason is not None:
                trades.append(_close_trade(bar, index, position, exit_price, exit_reason, config))
                position = None

        if position is None and pending is not None:
            fill_allowed = _pending_fill_allowed(bar, pending, config)
            if fill_allowed:
                position = OpenPosition(
                    direction=pending.direction,
                    entry=pending.entry,
                    stop=pending.stop,
                    target=pending.target,
                    rr=pending.rr,
                    zone_high=pending.zone_high,
                    zone_low=pending.zone_low,
                    break_close=pending.break_close,
                    break_body_to_atr=pending.break_body_to_atr,
                    atr_percentile_rank=pending.atr_percentile_rank,
                    setup_index=pending.created_index,
                    setup_time=pending.created_time,
                    entry_index=index,
                    entry_time=bar["timestamp"],
                )
                pending = None
                _update_position_excursion(bar, position)
                exit_price, exit_reason = _position_exit(bar, index, position, config)
                if exit_price is not None and exit_reason is not None:
                    trades.append(_close_trade(bar, index, position, exit_price, exit_reason, config))
                    position = None
            else:
                pending, cancel_reason = _update_pending(bar, index, pending, config)
                if cancel_reason is not None:
                    canceled_setups_by_reason[cancel_reason] += 1

        if position is not None or pending is not None:
            continue
        if not _can_create_setup(bar, config):
            continue

        long_setup = _build_long_setup(index, enriched, last_swing_high, last_swing_low, swing_highs, swing_lows, config)
        short_setup = _build_short_setup(index, enriched, last_swing_high, last_swing_low, swing_highs, swing_lows, config)
        selected = _select_setup(long_setup, short_setup)
        if selected is None:
            continue
        setup_count += 1
        pending = selected

    trade_pnls = [trade.net_pnl for trade in trades]
    equity = [config.starting_equity]
    for pnl in trade_pnls:
        equity.append(equity[-1] + pnl)
    days = len({bar["timestamp"].date() for bar in enriched}) or 1
    metrics = calculate_metrics(trade_pnls, equity, config.starting_equity, days).to_dict()
    return {
        "strategy_name": "order_block_retest_limit_v1",
        "symbol": config.symbol,
        "timeframe": config.timeframe,
        "bar_count": len(enriched),
        "setup_count": setup_count,
        "filled_setup_count": len(trades),
        "canceled_setup_count": sum(canceled_setups_by_reason.values()),
        "canceled_setups_by_reason": dict(sorted(canceled_setups_by_reason.items())),
        "metrics": metrics,
        "config": asdict(config),
        "diagnostics": _build_diagnostics(trades, setup_count, canceled_setups_by_reason),
        "trades": [trade.to_dict() for trade in trades],
    }


def _with_indicators(bars: list[dict], config: RoleRetestConfig) -> list[dict]:
    previous_close = None
    atr_values: list[float] = []
    body_values: list[float] = []
    volume_values: list[float] = []
    htf_closes: list[float] = []
    htf_fast = None
    htf_slow = None
    fast_alpha = 2 / (config.trend_ema_fast + 1)
    slow_alpha = 2 / (config.trend_ema_slow + 1)
    for index, bar in enumerate(bars):
        true_range = bar["high"] - bar["low"]
        if previous_close is not None:
            true_range = max(true_range, abs(bar["high"] - previous_close), abs(bar["low"] - previous_close))
        atr_values.append(true_range)
        body_values.append(abs(bar["close"] - bar["open"]))
        volume_values.append(float(bar.get("tick_count") or bar.get("volume") or 0.0))
        atr_window = atr_values[-config.atr_window :]
        body_window = body_values[-20:]
        volume_window = volume_values[-config.volume_window :]
        bar["atr"] = mean(atr_window)
        bar["avg_body"] = mean(body_window)
        bar["bar_volume"] = volume_values[-1]
        bar["volume_ma"] = mean(volume_window) if volume_window else 0.0
        bar["relative_volume"] = bar["bar_volume"] / bar["volume_ma"] if bar["volume_ma"] else 1.0
        volume_percentile_window = volume_values[-config.volume_percentile_window :]
        bar["volume_percentile_rank"] = _rank_percentile(volume_percentile_window, bar["bar_volume"])
        percentile_window = atr_values[-config.atr_percentile_window :]
        bar["atr_low_gate"] = _percentile(percentile_window, config.atr_low_percentile)
        bar["atr_high_gate"] = _percentile(percentile_window, config.atr_high_percentile)
        if index % config.trend_htf_multiple == config.trend_htf_multiple - 1:
            htf_closes.append(bar["close"])
            htf_fast = bar["close"] if htf_fast is None else htf_fast + fast_alpha * (bar["close"] - htf_fast)
            htf_slow = bar["close"] if htf_slow is None else htf_slow + slow_alpha * (bar["close"] - htf_slow)
        previous_htf_slow = bars[index - 1].get("htf_slow") if index > 0 else None
        bar["htf_close"] = htf_closes[-1] if htf_closes else bar["close"]
        bar["htf_fast"] = htf_fast if htf_fast is not None else bar["close"]
        bar["htf_slow"] = htf_slow if htf_slow is not None else bar["close"]
        bar["htf_slow_rising"] = previous_htf_slow is not None and bar["htf_slow"] > previous_htf_slow
        bar["htf_slow_falling"] = previous_htf_slow is not None and bar["htf_slow"] < previous_htf_slow
        previous_close = bar["close"]
    return bars


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = round((len(ordered) - 1) * percentile / 100)
    return ordered[max(0, min(rank, len(ordered) - 1))]


def _rank_percentile(values: Sequence[float], value: float) -> float:
    if not values:
        return 50.0
    below_or_equal = sum(1 for item in values if item <= value)
    return below_or_equal / len(values) * 100.0


def _confirmed_pivot_high(bars: Sequence[dict], pivot_index: int, config: RoleRetestConfig) -> float | None:
    window = bars[pivot_index - config.swing_left_bars : pivot_index + config.swing_right_bars + 1]
    value = bars[pivot_index]["high"]
    return value if value == max(bar["high"] for bar in window) else None


def _confirmed_pivot_low(bars: Sequence[dict], pivot_index: int, config: RoleRetestConfig) -> float | None:
    window = bars[pivot_index - config.swing_left_bars : pivot_index + config.swing_right_bars + 1]
    value = bars[pivot_index]["low"]
    return value if value == min(bar["low"] for bar in window) else None


def _can_create_setup(bar: dict, config: RoleRetestConfig) -> bool:
    atr_ok = bar["atr"] >= bar["atr_low_gate"] and bar["atr"] <= bar["atr_high_gate"]
    return atr_ok and _volume_ok(bar, config) and _in_trade_session(bar["timestamp"], config)


def _volume_ok(bar: dict, config: RoleRetestConfig) -> bool:
    relative_volume = bar.get("relative_volume", 1.0)
    volume_percentile = bar.get("volume_percentile_rank", 50.0)
    if config.min_relative_volume > 0 and relative_volume < config.min_relative_volume:
        return False
    if config.max_relative_volume > 0 and relative_volume > config.max_relative_volume:
        return False
    if volume_percentile < config.min_volume_percentile:
        return False
    if volume_percentile > config.max_volume_percentile:
        return False
    return True


def _in_trade_session(timestamp: datetime, config: RoleRetestConfig) -> bool:
    local_time = timestamp.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(config.session_timezone)).time()
    return any(_time_in_range(local_time, session) for session in config.trade_sessions)


def _time_in_range(value: time, session: str) -> bool:
    start_raw, end_raw = session.split("-", 1)
    start = time(int(start_raw[:2]), int(start_raw[3:5]))
    end = time(int(end_raw[:2]), int(end_raw[3:5]))
    return start <= value <= end


def _build_long_setup(
    index: int,
    bars: Sequence[dict],
    last_swing_high: tuple[int, float] | None,
    last_swing_low: tuple[int, float] | None,
    swing_highs: Sequence[tuple[int, float]],
    swing_lows: Sequence[tuple[int, float]],
    config: RoleRetestConfig,
) -> PendingSetup | None:
    if last_swing_high is None or last_swing_low is None or index == 0:
        return None
    pivot_index, entry = last_swing_high
    if pivot_index < index - config.lookback_bars:
        return None
    bar = bars[index]
    previous = bars[index - 1]
    if not (previous["close"] <= entry < bar["close"]):
        return None
    if not _strong_impulse_up(index, bars, entry, config):
        return None
    if not _long_trend_ok(bar, config):
        return None
    if config.require_liquidity_sweep and not _long_liquidity_sweep(index, bars, last_swing_low, swing_lows, config):
        return None
    if config.require_fvg and not _bullish_fvg(index, bars):
        return None
    zone_high, zone_low = _long_zone(index, bars, entry, last_swing_low[1], config)
    if not _zone_size_ok(zone_high, zone_low, bar, config):
        return None
    entry = zone_high - (zone_high - zone_low) * config.ob_entry_ratio
    target = _nearest_higher_target(index, zone_high, swing_highs, config)
    if target is None:
        return None
    stop = zone_low - config.stop_buffer_ticks * config.tick_size
    return _setup_if_valid(
        1,
        entry,
        stop,
        target,
        zone_high,
        zone_low,
        bar,
        index,
        bar["timestamp"],
        bar["high"],
        config,
    )


def _build_short_setup(
    index: int,
    bars: Sequence[dict],
    last_swing_high: tuple[int, float] | None,
    last_swing_low: tuple[int, float] | None,
    swing_highs: Sequence[tuple[int, float]],
    swing_lows: Sequence[tuple[int, float]],
    config: RoleRetestConfig,
) -> PendingSetup | None:
    if last_swing_high is None or last_swing_low is None or index == 0:
        return None
    pivot_index, entry = last_swing_low
    if pivot_index < index - config.lookback_bars:
        return None
    bar = bars[index]
    previous = bars[index - 1]
    if not (previous["close"] >= entry > bar["close"]):
        return None
    if not _strong_impulse_down(index, bars, entry, config):
        return None
    if not _short_trend_ok(bar, config):
        return None
    if config.require_liquidity_sweep and not _short_liquidity_sweep(index, bars, last_swing_high, swing_highs, config):
        return None
    if config.require_fvg and not _bearish_fvg(index, bars):
        return None
    zone_high, zone_low = _short_zone(index, bars, last_swing_high[1], entry, config)
    if not _zone_size_ok(zone_high, zone_low, bar, config):
        return None
    entry = zone_low + (zone_high - zone_low) * config.ob_entry_ratio
    target = _nearest_lower_target(index, zone_low, swing_lows, config)
    if target is None:
        return None
    stop = zone_high + config.stop_buffer_ticks * config.tick_size
    return _setup_if_valid(
        -1,
        entry,
        stop,
        target,
        zone_high,
        zone_low,
        bar,
        index,
        bar["timestamp"],
        bar["low"],
        config,
    )


def _strong_impulse_up(index: int, bars: Sequence[dict], level: float, config: RoleRetestConfig) -> bool:
    bar = bars[index]
    body_impulse = abs(bar["close"] - bar["open"]) >= bar["avg_body"] * config.body_impulse_multiple
    range_impulse = bar["high"] - bar["low"] >= bar["atr"] * config.atr_impulse_multiple
    consecutive_away = index >= 2 and all(bars[index - offset]["close"] > level for offset in range(3))
    return bar["close"] > level and (body_impulse or range_impulse or consecutive_away)


def _strong_impulse_down(index: int, bars: Sequence[dict], level: float, config: RoleRetestConfig) -> bool:
    bar = bars[index]
    body_impulse = abs(bar["close"] - bar["open"]) >= bar["avg_body"] * config.body_impulse_multiple
    range_impulse = bar["high"] - bar["low"] >= bar["atr"] * config.atr_impulse_multiple
    consecutive_away = index >= 2 and all(bars[index - offset]["close"] < level for offset in range(3))
    return bar["close"] < level and (body_impulse or range_impulse or consecutive_away)


def _long_zone(index: int, bars: Sequence[dict], swing_high: float, swing_low: float, config: RoleRetestConfig) -> tuple[float, float]:
    if config.zone_mode != "opposite_candle":
        return swing_high, swing_low
    for candidate_index in range(index - 1, max(-1, index - config.ob_lookback_bars - 1), -1):
        candidate = bars[candidate_index]
        if candidate["close"] < candidate["open"]:
            return candidate["open"], candidate["low"]
    return swing_high, swing_low


def _short_zone(index: int, bars: Sequence[dict], swing_high: float, swing_low: float, config: RoleRetestConfig) -> tuple[float, float]:
    if config.zone_mode != "opposite_candle":
        return swing_high, swing_low
    for candidate_index in range(index - 1, max(-1, index - config.ob_lookback_bars - 1), -1):
        candidate = bars[candidate_index]
        if candidate["close"] > candidate["open"]:
            return candidate["high"], candidate["open"]
    return swing_high, swing_low


def _zone_size_ok(zone_high: float, zone_low: float, bar: dict, config: RoleRetestConfig) -> bool:
    if zone_high <= zone_low:
        return False
    if config.max_zone_atr <= 0:
        return True
    return (zone_high - zone_low) <= bar["atr"] * config.max_zone_atr


def _long_liquidity_sweep(
    index: int,
    bars: Sequence[dict],
    last_swing_low: tuple[int, float],
    swing_lows: Sequence[tuple[int, float]],
    config: RoleRetestConfig,
) -> bool:
    sweep_floor = last_swing_low[1]
    buffer = config.sweep_buffer_ticks * config.tick_size
    older_lows = [
        price
        for pivot_index, price in swing_lows
        if pivot_index < last_swing_low[0] and pivot_index >= index - config.liquidity_sweep_lookback
    ]
    if older_lows:
        sweep_floor = max(older_lows)
    start = max(0, index - config.liquidity_sweep_lookback)
    return min(bar["low"] for bar in bars[start : index + 1]) <= sweep_floor - buffer


def _short_liquidity_sweep(
    index: int,
    bars: Sequence[dict],
    last_swing_high: tuple[int, float],
    swing_highs: Sequence[tuple[int, float]],
    config: RoleRetestConfig,
) -> bool:
    sweep_ceiling = last_swing_high[1]
    buffer = config.sweep_buffer_ticks * config.tick_size
    older_highs = [
        price
        for pivot_index, price in swing_highs
        if pivot_index < last_swing_high[0] and pivot_index >= index - config.liquidity_sweep_lookback
    ]
    if older_highs:
        sweep_ceiling = min(older_highs)
    start = max(0, index - config.liquidity_sweep_lookback)
    return max(bar["high"] for bar in bars[start : index + 1]) >= sweep_ceiling + buffer


def _bullish_fvg(index: int, bars: Sequence[dict]) -> bool:
    return index >= 2 and bars[index]["low"] > bars[index - 2]["high"]


def _bearish_fvg(index: int, bars: Sequence[dict]) -> bool:
    return index >= 2 and bars[index]["high"] < bars[index - 2]["low"]


def _long_trend_ok(bar: dict, config: RoleRetestConfig) -> bool:
    if config.strict_trend_alignment:
        return bar["htf_close"] > bar["htf_slow"] and bar["htf_fast"] > bar["htf_slow"] and bar["htf_slow_rising"]
    return not (bar["htf_close"] < bar["htf_slow"] and bar["htf_fast"] < bar["htf_slow"] and bar["htf_slow_falling"])


def _short_trend_ok(bar: dict, config: RoleRetestConfig) -> bool:
    if config.strict_trend_alignment:
        return bar["htf_close"] < bar["htf_slow"] and bar["htf_fast"] < bar["htf_slow"] and bar["htf_slow_falling"]
    return not (bar["htf_close"] > bar["htf_slow"] and bar["htf_fast"] > bar["htf_slow"] and bar["htf_slow_rising"])


def _nearest_higher_target(index: int, entry: float, swing_highs: Sequence[tuple[int, float]], config: RoleRetestConfig) -> float | None:
    candidates = [price for pivot_index, price in swing_highs if pivot_index >= index - config.lookback_bars and price > entry]
    return min(candidates) if candidates else None


def _nearest_lower_target(index: int, entry: float, swing_lows: Sequence[tuple[int, float]], config: RoleRetestConfig) -> float | None:
    candidates = [price for pivot_index, price in swing_lows if pivot_index >= index - config.lookback_bars and price < entry]
    return max(candidates) if candidates else None


def _setup_if_valid(
    direction: int,
    entry: float,
    stop: float,
    target: float,
    zone_high: float,
    zone_low: float,
    break_bar: dict,
    index: int,
    timestamp: datetime,
    best_excursion: float,
    config: RoleRetestConfig,
) -> PendingSetup | None:
    risk = entry - stop if direction == 1 else stop - entry
    reward = target - entry if direction == 1 else entry - target
    if risk <= 0 or reward <= 0:
        return None
    rr = reward / risk
    if rr < config.minimum_rr:
        return None
    break_body_to_atr = abs(break_bar["close"] - break_bar["open"]) / break_bar["atr"] if break_bar["atr"] else 0.0
    atr_rank = _percentile_rank(break_bar["atr"], break_bar["atr_low_gate"], break_bar["atr_high_gate"])
    return PendingSetup(
        direction=direction,
        entry=entry,
        stop=stop,
        target=target,
        rr=rr,
        zone_high=zone_high,
        zone_low=zone_low,
        break_close=break_bar["close"],
        break_body_to_atr=break_body_to_atr,
        atr_percentile_rank=atr_rank,
        created_index=index,
        created_time=timestamp,
        best_excursion=best_excursion,
    )


def _select_setup(long_setup: PendingSetup | None, short_setup: PendingSetup | None) -> PendingSetup | None:
    if long_setup is None:
        return short_setup
    if short_setup is None:
        return long_setup
    return long_setup if long_setup.rr >= short_setup.rr else short_setup


def _pending_fill_allowed(bar: dict, pending: PendingSetup, config: RoleRetestConfig) -> bool:
    if pending.direction == 1:
        return bar["low"] <= pending.entry
    return bar["high"] >= pending.entry


def _update_pending(
    bar: dict,
    index: int,
    pending: PendingSetup,
    config: RoleRetestConfig,
) -> tuple[PendingSetup | None, str | None]:
    best_excursion = max(pending.best_excursion, bar["high"]) if pending.direction == 1 else min(pending.best_excursion, bar["low"])
    age = index - pending.created_index
    if pending.direction == 1:
        path_progress = (best_excursion - pending.entry) / (pending.target - pending.entry)
    else:
        path_progress = (pending.entry - best_excursion) / (pending.entry - pending.target)
    if age > config.pending_order_ttl_bars:
        return None, "ttl_expired"
    if path_progress >= config.pre_touch_cancel_progress:
        return None, "pre_touch_path_progress"
    pending.best_excursion = best_excursion
    return pending, None


def _update_position_excursion(bar: dict, position: OpenPosition) -> None:
    if position.direction == 1:
        position.mfe_points = max(position.mfe_points, bar["high"] - position.entry)
        position.mae_points = max(position.mae_points, position.entry - bar["low"])
    else:
        position.mfe_points = max(position.mfe_points, position.entry - bar["low"])
        position.mae_points = max(position.mae_points, bar["high"] - position.entry)


def _position_exit(bar: dict, index: int, position: OpenPosition, config: RoleRetestConfig) -> tuple[float | None, str | None]:
    if position.direction == 1:
        stop_hit = bar["low"] <= position.stop
        target_hit = bar["high"] >= position.target
        if stop_hit and target_hit:
            return (position.stop, "ambiguous_stop_first") if config.conservative_intrabar_ordering else (position.target, "ambiguous_target_first")
        if stop_hit:
            return position.stop, "stop_loss"
        if target_hit:
            return position.target, "take_profit"
    else:
        stop_hit = bar["high"] >= position.stop
        target_hit = bar["low"] <= position.target
        if stop_hit and target_hit:
            return (position.stop, "ambiguous_stop_first") if config.conservative_intrabar_ordering else (position.target, "ambiguous_target_first")
        if stop_hit:
            return position.stop, "stop_loss"
        if target_hit:
            return position.target, "take_profit"
    if config.max_hold_bars > 0 and index - position.entry_index >= config.max_hold_bars:
        return bar["close"], "max_hold_exit"
    if config.flatten_outside_session and not _in_trade_session(bar["timestamp"], config):
        return bar["close"], "session_exit"
    return None, None


def _close_trade(
    bar: dict,
    index: int,
    position: OpenPosition,
    exit_price: float,
    exit_reason: str,
    config: RoleRetestConfig,
) -> RoleRetestTrade:
    points = exit_price - position.entry if position.direction == 1 else position.entry - exit_price
    gross = points * config.point_value
    costs = config.slippage_ticks_per_side * 2 * config.tick_size * config.point_value + config.round_trip_fees_usd
    net = gross - costs
    return RoleRetestTrade(
        side="long" if position.direction == 1 else "short",
        setup_time=position.setup_time,
        entry_time=position.entry_time,
        exit_time=bar["timestamp"],
        entry_price=position.entry,
        exit_price=exit_price,
        stop_loss=position.stop,
        take_profit=position.target,
        rr=position.rr,
        zone_high=position.zone_high,
        zone_low=position.zone_low,
        break_close=position.break_close,
        break_body_to_atr=position.break_body_to_atr,
        atr_percentile_rank=position.atr_percentile_rank,
        bars_to_fill=position.entry_index - position.setup_index,
        bars_held=index - position.entry_index,
        mfe_points=position.mfe_points,
        mae_points=position.mae_points,
        gross_pnl=gross,
        costs=costs,
        net_pnl=net,
        entry_reason="order_block_retest_limit",
        exit_reason=exit_reason,
    )


def _percentile_rank(value: float, low_gate: float, high_gate: float) -> float:
    if high_gate <= low_gate:
        return 50.0
    return max(0.0, min(100.0, (value - low_gate) / (high_gate - low_gate) * 100.0))


def _build_diagnostics(
    trades: Sequence[RoleRetestTrade],
    setup_count: int,
    canceled_setups_by_reason: Counter[str],
) -> dict:
    winners = [trade for trade in trades if trade.net_pnl > 0]
    losers = [trade for trade in trades if trade.net_pnl < 0]
    filled = len(trades)
    return {
        "setup_count": setup_count,
        "filled_setup_count": filled,
        "fill_rate": filled / setup_count if setup_count else None,
        "canceled_setups_by_reason": dict(sorted(canceled_setups_by_reason.items())),
        "avg_bars_to_fill": mean([trade.bars_to_fill for trade in trades]) if trades else None,
        "avg_bars_held": mean([trade.bars_held for trade in trades]) if trades else None,
        "avg_rr_winners": mean([trade.rr for trade in winners]) if winners else None,
        "avg_rr_losers": mean([trade.rr for trade in losers]) if losers else None,
        "avg_mae_points_winners": mean([trade.mae_points for trade in winners]) if winners else None,
        "avg_mae_points_losers": mean([trade.mae_points for trade in losers]) if losers else None,
        "avg_mfe_points_winners": mean([trade.mfe_points for trade in winners]) if winners else None,
        "avg_mfe_points_losers": mean([trade.mfe_points for trade in losers]) if losers else None,
    }


def result_to_json(result: dict) -> str:
    return json.dumps(result, indent=2, sort_keys=True, default=str)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest the inferred role-retest limit strategy.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--output")
    parser.add_argument("--lookback-bars", type=int, default=80)
    parser.add_argument("--atr-low-percentile", type=float, default=30.0)
    parser.add_argument("--atr-high-percentile", type=float, default=85.0)
    parser.add_argument("--body-impulse-multiple", type=float, default=1.5)
    parser.add_argument("--atr-impulse-multiple", type=float, default=1.0)
    parser.add_argument("--minimum-rr", type=float, default=1.2)
    parser.add_argument("--ttl-bars", type=int, default=5)
    parser.add_argument("--ob-entry-ratio", type=float, default=0.5)
    parser.add_argument("--zone-mode", choices=("swing", "opposite_candle"), default="swing")
    parser.add_argument("--ob-lookback-bars", type=int, default=12)
    parser.add_argument("--max-zone-atr", type=float, default=0.0)
    parser.add_argument("--require-liquidity-sweep", action="store_true")
    parser.add_argument("--liquidity-sweep-lookback", type=int, default=40)
    parser.add_argument("--sweep-buffer-ticks", type=int, default=0)
    parser.add_argument("--require-fvg", action="store_true")
    parser.add_argument("--min-relative-volume", type=float, default=0.0)
    parser.add_argument("--max-relative-volume", type=float, default=0.0)
    parser.add_argument("--min-volume-percentile", type=float, default=0.0)
    parser.add_argument("--max-volume-percentile", type=float, default=100.0)
    parser.add_argument("--max-hold-bars", type=int, default=0)
    parser.add_argument("--flatten-outside-session", action="store_true")
    parser.add_argument("--trade-sessions", default="09:35-11:30,13:30-15:45")
    parser.add_argument("--strict-trend-alignment", action="store_true")
    args = parser.parse_args(argv)

    config = RoleRetestConfig(
        symbol=args.symbol,
        timeframe=args.timeframe,
        lookback_bars=args.lookback_bars,
        atr_low_percentile=args.atr_low_percentile,
        atr_high_percentile=args.atr_high_percentile,
        body_impulse_multiple=args.body_impulse_multiple,
        atr_impulse_multiple=args.atr_impulse_multiple,
        minimum_rr=args.minimum_rr,
        pending_order_ttl_bars=args.ttl_bars,
        ob_entry_ratio=args.ob_entry_ratio,
        zone_mode=args.zone_mode,
        ob_lookback_bars=args.ob_lookback_bars,
        max_zone_atr=args.max_zone_atr,
        require_liquidity_sweep=args.require_liquidity_sweep,
        liquidity_sweep_lookback=args.liquidity_sweep_lookback,
        sweep_buffer_ticks=args.sweep_buffer_ticks,
        require_fvg=args.require_fvg,
        min_relative_volume=args.min_relative_volume,
        max_relative_volume=args.max_relative_volume,
        min_volume_percentile=args.min_volume_percentile,
        max_volume_percentile=args.max_volume_percentile,
        max_hold_bars=args.max_hold_bars,
        flatten_outside_session=args.flatten_outside_session,
        trade_sessions=tuple(session.strip() for session in args.trade_sessions.split(",") if session.strip()),
        strict_trend_alignment=args.strict_trend_alignment,
    )
    bars = load_role_retest_bars(Path(args.data_root), config.symbol, config.timeframe, args.date_from, args.date_to)
    result = run_role_retest_backtest(bars, config)
    output = result_to_json(result)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
        print(output_path)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
