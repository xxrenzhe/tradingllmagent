from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Sequence


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
        return _blocked_signal(strategy, current, "spread_above_limit", spread_ticks)
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
) -> dict[str, Any]:
    payload = _no_signal(strategy, reason, bar)
    payload["signal_class"] = "blocked"
    payload["blocked"] = True
    payload["risk_context"] = {"spread_ticks": spread_ticks}
    return payload


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
