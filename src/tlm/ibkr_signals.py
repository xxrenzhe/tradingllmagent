from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import pstdev
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from .expanded_high_edge import (
    EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_PRESET,
    EXPANDED_HIGH_EDGE_PRESETS,
    ExpandedHighEdge,
)
from .low_r_regime_basket import LowRRegimeBasketConfig, PRESETS, RegimeEdge, _stop_points


NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class OneMinuteBar:
    symbol: str
    bar_time: datetime
    open: float
    high: float
    low: float
    close: float
    bid: float | None
    ask: float | None
    tick_count: int

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bar_time": self.bar_time.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "bid": self.bid,
            "ask": self.ask,
            "spread": self.spread,
            "tick_count": self.tick_count,
        }


def build_one_minute_bars(snapshots: Sequence[dict[str, Any]]) -> list[OneMinuteBar]:
    grouped: dict[tuple[str, datetime], list[dict[str, Any]]] = {}
    for snapshot in snapshots:
        symbol = str(snapshot.get("symbol", "MNQ"))
        timestamp = _parse_datetime(snapshot.get("snapshot_time"))
        minute = timestamp.replace(second=0, microsecond=0)
        grouped.setdefault((symbol, minute), []).append(snapshot)
    bars = []
    for (symbol, minute), rows in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        ordered = sorted(rows, key=lambda row: _parse_datetime(row.get("snapshot_time")))
        prices = [float(row["last"]) for row in ordered if row.get("last") is not None]
        if not prices:
            continue
        last_row = ordered[-1]
        bars.append(
            OneMinuteBar(
                symbol=symbol,
                bar_time=minute,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                bid=_optional_float(last_row.get("bid")),
                ask=_optional_float(last_row.get("ask")),
                tick_count=len(prices),
            )
        )
    return bars


def coerce_one_minute_bars(rows: Sequence[dict[str, Any] | OneMinuteBar]) -> list[OneMinuteBar]:
    coerced: list[OneMinuteBar] = []
    for row in rows:
        if isinstance(row, OneMinuteBar):
            coerced.append(row)
            continue
        coerced.append(
            OneMinuteBar(
                symbol=str(row.get("symbol", "MNQ")),
                bar_time=_parse_datetime(row.get("bar_time")),
                open=float(row.get("open", row.get("close", 0.0))),
                high=float(row.get("high", row.get("close", 0.0))),
                low=float(row.get("low", row.get("close", 0.0))),
                close=float(row.get("close", 0.0)),
                bid=_optional_float(row.get("bid")),
                ask=_optional_float(row.get("ask")),
                tick_count=int(row.get("tick_count", 0) or 0),
            )
        )
    return coerced


def merge_one_minute_bars(
    historical: Sequence[dict[str, Any] | OneMinuteBar],
    live: Sequence[dict[str, Any] | OneMinuteBar],
    *,
    limit: int | None = None,
) -> list[OneMinuteBar]:
    merged: dict[tuple[str, datetime], OneMinuteBar] = {}
    for bar in [*coerce_one_minute_bars(historical), *coerce_one_minute_bars(live)]:
        merged[(bar.symbol, bar.bar_time)] = bar
    rows = sorted(merged.values(), key=lambda bar: (bar.symbol, bar.bar_time))
    if limit is not None and limit >= 0:
        return rows[-limit:]
    return rows


def build_signal_candidate(
    strategy: dict[str, Any],
    bars: Sequence[OneMinuteBar],
    *,
    tick_size: float = 0.25,
    max_spread_ticks: float = 2.0,
) -> dict[str, Any]:
    if not bars:
        return _no_signal(strategy, "no_bars")
    current = bars[-1]
    if not bool(strategy.get("enabled", True)):
        return _no_signal(strategy, "strategy_disabled", current)
    if str(strategy.get("symbol", current.symbol)) != current.symbol:
        return _no_signal(strategy, "symbol_mismatch", current)
    if str(strategy.get("timeframe", "1m")) != "1m":
        return _no_signal(strategy, "unsupported_timeframe", current)
    spread_ticks = None if current.spread is None else current.spread / tick_size
    if spread_ticks is None:
        return _no_signal(strategy, "spread_unavailable", current)
    if spread_ticks > max_spread_ticks:
        return _blocked_signal(strategy, current, "spread_above_limit", spread_ticks, max_spread_ticks)
    family = str(strategy.get("family", "range_breakout"))
    if family == "expanded_high_edge":
        return _build_expanded_high_edge_signal(strategy, bars, current, tick_size, max_spread_ticks, spread_ticks)
    if family == "low_r_regime_basket":
        return _build_low_r_signal(strategy, bars, current, tick_size, max_spread_ticks, spread_ticks)
    lookback = max(int(strategy.get("lookback_bars", 5)), 2)
    if len(bars) <= lookback:
        return _no_signal(strategy, "insufficient_lookback", current)
    previous = list(bars[-lookback - 1 : -1])
    breakout_ticks = max(float(strategy.get("breakout_ticks", 1.0)), 0.0)
    previous_high = max(bar.high for bar in previous)
    previous_low = min(bar.low for bar in previous)
    upper_trigger = previous_high + breakout_ticks * tick_size
    lower_trigger = previous_low - breakout_ticks * tick_size
    side = None
    trigger_reasons = []
    if current.close >= upper_trigger:
        side = "BUY"
        trigger_reasons = ["range_breakout_up", "close_above_lookback_high"]
    elif current.close <= lower_trigger:
        side = "SELL"
        trigger_reasons = ["range_breakout_down", "close_below_lookback_low"]
    if side is None:
        return _no_signal(strategy, "no_breakout", current)
    move_ticks = (
        (current.close - previous_high) / tick_size
        if side == "BUY"
        else (previous_low - current.close) / tick_size
    )
    payload = {
        "schema_version": 1,
        "source": "ibkr_local_signal_engine",
        "signal_id": _stable_hash(
            {
                "strategy": strategy.get("strategy_id"),
                "bar_time": current.bar_time.isoformat(),
                "side": side,
                "close": current.close,
            }
        ),
        "strategy_id": strategy.get("strategy_id"),
        "strategy_spec_hash": strategy.get("strategy_spec_hash"),
        "module_id": strategy.get("module_id"),
        "family": strategy.get("family", "range_breakout"),
        "symbol": current.symbol,
        "timeframe": "1m",
        "bar_1m": current.to_dict(),
        "signal_type": "entry",
        "side": side,
        "signal_class": "strong_review",
        "signal_strength": min(max(move_ticks / max(breakout_ticks, 1.0), 0.0), 5.0),
        "trigger_reasons": trigger_reasons,
        "risk_context": {
            "spread_ticks": spread_ticks,
            "max_spread_ticks": max_spread_ticks,
            "lookback_bars": lookback,
            "previous_high": previous_high,
            "previous_low": previous_low,
            "breakout_ticks": breakout_ticks,
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    payload["signal_hash"] = _stable_hash(payload)
    return payload


def _build_low_r_signal(
    strategy: dict[str, Any],
    bars: Sequence[OneMinuteBar],
    current: OneMinuteBar,
    tick_size: float,
    max_spread_ticks: float,
    spread_ticks: float,
) -> dict[str, Any]:
    preset = str(strategy.get("preset", "simple_robust_low_r"))
    edges = PRESETS.get(preset)
    if not edges:
        return _no_signal(strategy, "unknown_low_r_preset", current)
    feature = _latest_low_r_feature(bars)
    if feature is None:
        return _no_signal(strategy, "insufficient_low_r_history", current)
    matches = [
        {"edge_index": edge_index, "edge": edge}
        for edge_index, edge in enumerate(edges)
        if _low_r_edge_matches(edge, feature)
    ]
    if not matches:
        return _no_signal(strategy, "no_low_r_match", current)
    selected = matches[0]
    edge = selected["edge"]
    config = LowRRegimeBasketConfig(
        symbol=str(current.symbol),
        tick_size=tick_size,
        stop_range_multiple=float(strategy.get("stop_range_multiple", 10.0)),
        min_stop_points=float(strategy.get("min_stop_points", 8.0)),
        max_stop_points=float(strategy.get("max_stop_points", 90.0)),
        max_hold_minutes=int(strategy.get("max_holding_minutes", 300)),
    )
    stop_points = _stop_points(float(feature["range20"]), config)
    stop_loss_ticks = max(int(round(stop_points / tick_size)), 1)
    take_profit_ticks = max(int(round((stop_points * edge.take_profit_r) / tick_size)), 1)
    side = "BUY" if edge.direction == 1 else "SELL"
    payload = {
        "schema_version": 1,
        "source": "ibkr_local_signal_engine",
        "signal_id": _stable_hash(
            {
                "strategy": strategy.get("strategy_id"),
                "preset": preset,
                "bar_time": current.bar_time.isoformat(),
                "edge_index": selected["edge_index"],
                "side": side,
            }
        ),
        "strategy_id": strategy.get("strategy_id"),
        "strategy_spec_hash": strategy.get("strategy_spec_hash"),
        "module_id": strategy.get("module_id"),
        "family": "low_r_regime_basket",
        "symbol": current.symbol,
        "timeframe": "1m",
        "bar_1m": current.to_dict(),
        "signal_type": "entry",
        "side": side,
        "signal_class": "strong_review",
        "signal_strength": 1.0,
        "trigger_reasons": [
            f"low_r:{edge.scan_type}",
            f"preset:{preset}",
            f"session:{edge.session_bucket}",
        ],
        "risk_context": {
            "spread_ticks": spread_ticks,
            "max_spread_ticks": max_spread_ticks,
            "preset": preset,
            "edge_index": selected["edge_index"],
            "matched_edge_count": len(matches),
            "matched_edge_indexes": [match["edge_index"] for match in matches],
            "scan_type": edge.scan_type,
            "session_bucket": edge.session_bucket,
            "dow": feature["dow"],
            "trend_bin": feature["trend_bin"],
            "volume_bin": feature["volume_bin"],
            "range_bin": feature["range_bin"],
            "range20": feature["range20"],
            "stop_points": stop_points,
            "stop_loss_ticks": stop_loss_ticks,
            "take_profit_ticks": take_profit_ticks,
            "take_profit_r": edge.take_profit_r,
            "max_holding_minutes": config.max_hold_minutes,
        },
        "matched_edge": asdict(edge),
        "created_at": datetime.now(UTC).isoformat(),
    }
    payload["signal_hash"] = _stable_hash(payload)
    return payload


def _build_expanded_high_edge_signal(
    strategy: dict[str, Any],
    bars: Sequence[OneMinuteBar],
    current: OneMinuteBar,
    tick_size: float,
    max_spread_ticks: float,
    spread_ticks: float,
) -> dict[str, Any]:
    preset = str(strategy.get("preset", EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_PRESET))
    edges = EXPANDED_HIGH_EDGE_PRESETS.get(preset)
    if not edges:
        return _no_signal(strategy, "unknown_expanded_high_edge_preset", current)
    feature = _latest_expanded_high_edge_feature(bars)
    if feature is None:
        return _no_signal(strategy, "insufficient_expanded_high_edge_history", current)
    matches = [
        {"edge_index": edge_index, "edge": edge}
        for edge_index, edge in enumerate(edges)
        if _expanded_high_edge_matches(edge, feature)
    ]
    if not matches:
        return _no_signal(strategy, "no_expanded_high_edge_match", current)
    selected = matches[0]
    edge = selected["edge"]
    config = LowRRegimeBasketConfig(
        symbol=str(current.symbol),
        tick_size=tick_size,
        stop_range_multiple=float(strategy.get("stop_range_multiple", 6.0)),
        min_stop_points=float(strategy.get("min_stop_points", 8.0)),
        max_stop_points=float(strategy.get("max_stop_points", 90.0)),
        max_hold_minutes=int(strategy.get("max_holding_minutes", 300)),
        max_concurrent_positions=int(strategy.get("max_concurrent_positions", 24)),
    )
    stop_points = _stop_points(float(feature["range20"]), config)
    stop_loss_ticks = max(int(round(stop_points / tick_size)), 1)
    take_profit_ticks = max(int(round((stop_points * edge.take_profit_r) / tick_size)), 1)
    side = "BUY" if edge.direction == 1 else "SELL"
    payload = {
        "schema_version": 1,
        "source": "ibkr_local_signal_engine",
        "signal_id": _stable_hash(
            {
                "strategy": strategy.get("strategy_id"),
                "preset": preset,
                "bar_time": current.bar_time.isoformat(),
                "edge_index": selected["edge_index"],
                "side": side,
            }
        ),
        "strategy_id": strategy.get("strategy_id"),
        "strategy_spec_hash": strategy.get("strategy_spec_hash"),
        "module_id": strategy.get("module_id"),
        "family": "expanded_high_edge",
        "symbol": current.symbol,
        "timeframe": "1m",
        "bar_1m": current.to_dict(),
        "signal_type": "entry",
        "side": side,
        "signal_class": "strong_review",
        "signal_strength": min(1.0 + (len(matches) - 1) * 0.25, 5.0),
        "trigger_reasons": [
            f"expanded_high_edge:{edge.scan_type}",
            f"preset:{preset}",
            f"session:{edge.session_bucket}",
        ],
        "risk_context": {
            "spread_ticks": spread_ticks,
            "max_spread_ticks": max_spread_ticks,
            "preset": preset,
            "edge_index": selected["edge_index"],
            "matched_edge_count": len(matches),
            "matched_edge_indexes": [match["edge_index"] for match in matches],
            "scan_type": edge.scan_type,
            "session_bucket": edge.session_bucket,
            "dow": feature["dow"],
            "trend_bin": feature["trend_bin"],
            "volume_bin": feature["volume_bin"],
            "range_bin": feature["range_bin"],
            "range20": feature["range20"],
            "stop_points": stop_points,
            "stop_loss_ticks": stop_loss_ticks,
            "take_profit_ticks": take_profit_ticks,
            "take_profit_r": edge.take_profit_r,
            "max_holding_minutes": config.max_hold_minutes,
            "max_concurrent_positions": config.max_concurrent_positions,
        },
        "matched_edge": asdict(edge),
        "created_at": datetime.now(UTC).isoformat(),
    }
    payload["signal_hash"] = _stable_hash(payload)
    return payload


def _no_signal(strategy: dict[str, Any], reason: str, bar: OneMinuteBar | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": "ibkr_local_signal_engine",
        "strategy_id": strategy.get("strategy_id"),
        "strategy_spec_hash": strategy.get("strategy_spec_hash"),
        "module_id": strategy.get("module_id"),
        "symbol": bar.symbol if bar else strategy.get("symbol"),
        "timeframe": strategy.get("timeframe", "1m"),
        "signal_class": "none",
        "blocked": False,
        "reasons": [reason],
        "bar_1m": bar.to_dict() if bar else None,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _blocked_signal(
    strategy: dict[str, Any],
    bar: OneMinuteBar,
    reason: str,
    spread_ticks: float,
    max_spread_ticks: float,
) -> dict[str, Any]:
    payload = _no_signal(strategy, reason, bar)
    payload["signal_class"] = "blocked"
    payload["blocked"] = True
    payload["risk_context"] = {"spread_ticks": spread_ticks, "max_spread_ticks": max_spread_ticks}
    return payload


def _latest_expanded_high_edge_feature(bars: Sequence[OneMinuteBar]) -> dict[str, Any] | None:
    if len(bars) < 201:
        return None
    current = bars[-1]
    prev20 = list(bars[-21:-1])
    prev50 = list(bars[-51:-1])
    prev200 = list(bars[-201:-1])
    if len(prev20) < 20 or len(prev50) < 50 or len(prev200) < 200:
        return None
    ma20 = sum(bar.close for bar in prev20) / len(prev20)
    ma50 = sum(bar.close for bar in prev50) / len(prev50)
    ma200 = sum(bar.close for bar in prev200) / len(prev200)
    close_std50 = pstdev([bar.close for bar in prev50]) if len(prev50) > 1 else 0.0
    vol50 = sum(max(bar.tick_count, 1) for bar in prev50) / len(prev50)
    range20 = sum(bar.high - bar.low for bar in prev20) / len(prev20)
    high20_prev = max(bar.high for bar in prev20)
    low20_prev = min(bar.low for bar in prev20)
    ny_current = _ny_timestamp(current.bar_time)
    ny_date = ny_current.date()
    ny_min = _minute_of_day(ny_current)
    session_bars = [bar for bar in bars if _ny_timestamp(bar.bar_time).date() == ny_date]
    prior_session_bars = [bar for bar in session_bars if bar.bar_time < current.bar_time]
    if not session_bars:
        return None
    opening_range_bars = [
        bar
        for bar in prior_session_bars
        if 570 <= _minute_of_day(_ny_timestamp(bar.bar_time)) <= 599
    ]
    earlier_dates = sorted(
        {_ny_timestamp(bar.bar_time).date() for bar in bars if _ny_timestamp(bar.bar_time).date() < ny_date}
    )
    prior_day_bars = [bar for bar in bars if earlier_dates and _ny_timestamp(bar.bar_time).date() == earlier_dates[-1]]
    prior_day_high = max((bar.high for bar in prior_day_bars), default=None)
    prior_day_low = min((bar.low for bar in prior_day_bars), default=None)
    session_vwap = _vwap(session_bars)
    previous_session_vwap = _vwap(prior_session_bars)
    previous_bar = bars[-2]
    current_range = current.high - current.low
    trend_bin = 1 if current.close > ma50 else -1
    volume_bin = _volume_bin(max(current.tick_count, 1), vol50)
    range_bin = _range_bin(current_range, range20)
    z50 = (current.close - ma50) / close_std50 if close_std50 > 0 else 0.0
    volume_ratio = max(current.tick_count, 1) / vol50 if vol50 > 0 else 1.0
    ret1 = current.close - previous_bar.close
    ret3 = current.close - bars[-4].close
    ret5 = current.close - bars[-6].close
    close_location = (current.close - current.low) / current_range if current_range > 0 else 0.5
    body_to_range = (current.close - current.open) / current_range if current_range > 0 else 0.0
    vwap_dist = current.close - session_vwap if session_vwap is not None else None
    prev_vwap_dist = (
        previous_bar.close - previous_session_vwap
        if previous_session_vwap is not None and _ny_timestamp(previous_bar.bar_time).date() == ny_date
        else None
    )
    opening_high_prev = max((bar.high for bar in opening_range_bars), default=None)
    opening_low_prev = min((bar.low for bar in opening_range_bars), default=None)
    session_high_prev = max((bar.high for bar in prior_session_bars), default=None)
    session_low_prev = min((bar.low for bar in prior_session_bars), default=None)
    scan_matches: set[tuple[str, int]] = set()
    if opening_high_prev is not None and 600 <= ny_min <= 959 and current.close > opening_high_prev and trend_bin == 1 and volume_bin >= 0:
        scan_matches.add(("opening_range_breakout", 1))
    if opening_low_prev is not None and 600 <= ny_min <= 959 and current.close < opening_low_prev and trend_bin == -1 and volume_bin >= 0:
        scan_matches.add(("opening_range_breakout", -1))
    if prev_vwap_dist is not None and vwap_dist is not None and prev_vwap_dist < 0 <= vwap_dist and ret1 > 0 and trend_bin == 1 and volume_bin >= 0:
        scan_matches.add(("vwap_reclaim_continuation", 1))
    if prev_vwap_dist is not None and vwap_dist is not None and prev_vwap_dist > 0 >= vwap_dist and ret1 < 0 and trend_bin == -1 and volume_bin >= 0:
        scan_matches.add(("vwap_reclaim_continuation", -1))
    if session_vwap is not None and current.close > session_vwap and current.low <= session_vwap + range20 * 0.20 and close_location >= 0.65 and trend_bin == 1:
        scan_matches.add(("vwap_pullback_bounce", 1))
    if session_vwap is not None and current.close < session_vwap and current.high >= session_vwap - range20 * 0.20 and close_location <= 0.35 and trend_bin == -1:
        scan_matches.add(("vwap_pullback_bounce", -1))
    if ret3 >= range20 * 0.75 and close_location >= 0.65 and volume_ratio >= 1.35 and trend_bin == 1:
        scan_matches.add(("high_volume_impulse_continuation", 1))
    if ret3 <= -range20 * 0.75 and close_location <= 0.35 and volume_ratio >= 1.35 and trend_bin == -1:
        scan_matches.add(("high_volume_impulse_continuation", -1))
    if body_to_range >= 0.60 and range_bin >= 2 and volume_bin >= 0 and trend_bin == 1:
        scan_matches.add(("range_expansion_continuation", 1))
    if body_to_range <= -0.60 and range_bin >= 2 and volume_bin >= 0 and trend_bin == -1:
        scan_matches.add(("range_expansion_continuation", -1))
    if current.close > high20_prev and volume_bin >= 0 and trend_bin == 1:
        scan_matches.add(("donchian20_breakout", 1))
    if current.close < low20_prev and volume_bin >= 0 and trend_bin == -1:
        scan_matches.add(("donchian20_breakout", -1))
    if prior_day_high is not None and current.close > prior_day_high and volume_bin >= 0 and trend_bin == 1:
        scan_matches.add(("prior_day_breakout", 1))
    if prior_day_low is not None and current.close < prior_day_low and volume_bin >= 0 and trend_bin == -1:
        scan_matches.add(("prior_day_breakout", -1))
    if prior_day_high is not None and current.high > prior_day_high and current.close < prior_day_high and z50 >= 0.75:
        scan_matches.add(("prior_day_rejection", -1))
    if prior_day_low is not None and current.low < prior_day_low and current.close > prior_day_low and z50 <= -0.75:
        scan_matches.add(("prior_day_rejection", 1))
    if volume_ratio >= 1.60 and ret5 < 0 and close_location >= 0.65 and body_to_range >= 0 and z50 <= -0.50:
        scan_matches.add(("selling_absorption_reversal", 1))
    if volume_ratio >= 1.60 and ret5 > 0 and close_location <= 0.35 and body_to_range <= 0 and z50 >= 0.50:
        scan_matches.add(("buying_absorption_reversal", -1))
    if trend_bin == 1 and current.close > ma20 and ret5 < 0 and ret1 > 0:
        scan_matches.add(("trend_pullback_reclaim", 1))
    if trend_bin == -1 and current.close < ma20 and ret5 > 0 and ret1 < 0:
        scan_matches.add(("trend_pullback_reclaim", -1))
    if trend_bin == 1 and volume_bin == -1 and ret1 > 0:
        scan_matches.add(("low_volume_drift", 1))
    if trend_bin == -1 and volume_bin == -1 and ret1 < 0:
        scan_matches.add(("low_volume_drift", -1))
    if session_high_prev is not None and current.close >= session_high_prev - range20 * 0.10 and z50 >= 1.0 and volume_bin <= 1:
        scan_matches.add(("session_extreme_reversion", -1))
    if session_low_prev is not None and current.close <= session_low_prev + range20 * 0.10 and z50 <= -1.0 and volume_bin <= 1:
        scan_matches.add(("session_extreme_reversion", 1))
    return {
        "dow": int(ny_current.strftime("%w")),
        "session_bucket": _ny_session_bucket(ny_min),
        "trend_bin": trend_bin,
        "major_trend_bin": 1 if current.close > ma200 else -1,
        "volume_bin": volume_bin,
        "range_bin": range_bin,
        "range20": range20,
        "scan_matches": scan_matches,
    }


def _expanded_high_edge_matches(edge: ExpandedHighEdge, feature: dict[str, Any]) -> bool:
    return (
        (edge.scan_type, edge.direction) in feature["scan_matches"]
        and edge.session_bucket == feature["session_bucket"]
        and (edge.dow is None or edge.dow == feature["dow"])
        and (edge.trend_bin is None or edge.trend_bin == feature["trend_bin"])
        and (edge.volume_bin is None or edge.volume_bin == feature["volume_bin"])
        and (edge.range_bin is None or edge.range_bin == feature["range_bin"])
    )


def _latest_low_r_feature(bars: Sequence[OneMinuteBar]) -> dict[str, Any] | None:
    if len(bars) < 51:
        return None
    current = bars[-1]
    prev20 = list(bars[-21:-1])
    prev50 = list(bars[-51:-1])
    if len(prev20) < 20 or len(prev50) < 50:
        return None
    ma20 = sum(bar.close for bar in prev20) / len(prev20)
    ma50 = sum(bar.close for bar in prev50) / len(prev50)
    close_std50 = pstdev([bar.close for bar in prev50]) if len(prev50) > 1 else 0.0
    vol50 = sum(max(bar.tick_count, 1) for bar in prev50) / len(prev50)
    range20 = sum(bar.high - bar.low for bar in prev20) / len(prev20)
    high20_prev = max(bar.high for bar in prev20)
    low20_prev = min(bar.low for bar in prev20)
    session_bars = [bar for bar in bars if bar.bar_time.date() == current.bar_time.date()]
    if not session_bars:
        return None
    current_moday = _minute_of_day(current.bar_time)
    opening_range_bars = [
        bar
        for bar in session_bars
        if 810 <= _minute_of_day(bar.bar_time) <= 839 and bar.bar_time < current.bar_time
    ]
    trend_bin = 1 if current.close > ma50 else -1
    volume_bin = _volume_bin(max(current.tick_count, 1), vol50)
    current_range = current.high - current.low
    range_bin = _range_bin(current_range, range20)
    z50 = (current.close - ma50) / close_std50 if close_std50 > 0 else 0.0
    ret1 = current.close - bars[-2].close
    ret5 = current.close - bars[-6].close
    breakout20 = 1 if current.close > high20_prev else -1 if current.close < low20_prev else 0
    scan_matches: set[tuple[str, int]] = set()
    if breakout20 == 1 and trend_bin == 1 and volume_bin >= 1:
        scan_matches.add(("breakout_continuation", 1))
    if breakout20 == -1 and trend_bin == -1 and volume_bin >= 1:
        scan_matches.add(("breakout_continuation", -1))
    opening_high_prev = max((bar.high for bar in opening_range_bars), default=None)
    opening_low_prev = min((bar.low for bar in opening_range_bars), default=None)
    if opening_high_prev is not None and current_moday >= 840 and current.close > opening_high_prev and trend_bin == 1 and volume_bin >= 1:
        scan_matches.add(("opening_range_breakout", 1))
    if opening_low_prev is not None and current_moday >= 840 and current.close < opening_low_prev and trend_bin == -1 and volume_bin >= 1:
        scan_matches.add(("opening_range_breakout", -1))
    if trend_bin == 1 and ret5 < 0 and ret1 > 0 and current.close > ma20:
        scan_matches.add(("trend_pullback_reclaim", 1))
    if trend_bin == -1 and ret5 > 0 and ret1 < 0 and current.close < ma20:
        scan_matches.add(("trend_pullback_reclaim", -1))
    if z50 >= 2 and volume_bin <= 1:
        scan_matches.add(("zscore_mean_reversion", -1))
    if z50 <= -2 and volume_bin <= 1:
        scan_matches.add(("zscore_mean_reversion", 1))
    if trend_bin == 1 and volume_bin == -1 and ret1 > 0:
        scan_matches.add(("low_volume_drift", 1))
    if trend_bin == -1 and volume_bin == -1 and ret1 < 0:
        scan_matches.add(("low_volume_drift", -1))
    return {
        "dow": int(current.bar_time.strftime("%w")),
        "session_bucket": _session_bucket(current_moday),
        "trend_bin": trend_bin,
        "volume_bin": volume_bin,
        "range_bin": range_bin,
        "range20": range20,
        "scan_matches": scan_matches,
    }


def _low_r_edge_matches(edge: RegimeEdge, feature: dict[str, Any]) -> bool:
    return (
        (edge.scan_type, edge.direction) in feature["scan_matches"]
        and edge.session_bucket == feature["session_bucket"]
        and edge.dow == feature["dow"]
        and edge.trend_bin == feature["trend_bin"]
        and edge.volume_bin == feature["volume_bin"]
        and edge.range_bin == feature["range_bin"]
    )


def _minute_of_day(timestamp: datetime) -> int:
    return timestamp.hour * 60 + timestamp.minute


def _ny_timestamp(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(NEW_YORK)


def _ny_session_bucket(minute_of_day: int) -> str:
    if 0 <= minute_of_day <= 359:
        return "ny_0000_0559"
    if 360 <= minute_of_day <= 569:
        return "ny_0600_0929"
    if 570 <= minute_of_day <= 719:
        return "ny_0930_1159"
    if 720 <= minute_of_day <= 959:
        return "ny_1200_1559"
    if 960 <= minute_of_day <= 1079:
        return "ny_1600_1759"
    return "ny_1800_2359"


def _vwap(bars: Sequence[OneMinuteBar]) -> float | None:
    weighted = 0.0
    volume = 0
    for bar in bars:
        tick_count = max(bar.tick_count, 1)
        weighted += ((bar.high + bar.low + bar.close) / 3.0) * tick_count
        volume += tick_count
    if volume <= 0:
        return None
    return weighted / volume


def _session_bucket(minute_of_day: int) -> str:
    if 0 <= minute_of_day <= 359:
        return "utc_0000_0559"
    if 360 <= minute_of_day <= 719:
        return "utc_0600_1159"
    if 720 <= minute_of_day <= 1019:
        return "utc_1200_1659"
    if 1020 <= minute_of_day <= 1259:
        return "utc_1700_2059"
    return "utc_2100_2359"


def _volume_bin(tick_count: int, vol50: float) -> int:
    if vol50 <= 0:
        return 0
    if tick_count >= vol50 * 2.0:
        return 3
    if tick_count >= vol50 * 1.5:
        return 2
    if tick_count >= vol50:
        return 1
    if tick_count < vol50 * 0.7:
        return -1
    return 0


def _range_bin(current_range: float, range20: float) -> int:
    if range20 <= 0:
        return 0
    if current_range >= range20 * 2.0:
        return 3
    if current_range >= range20 * 1.5:
        return 2
    if current_range >= range20:
        return 1
    if current_range < range20 * 0.7:
        return -1
    return 0


def _stable_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
