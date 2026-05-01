from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from math import ceil, floor
from typing import Literal, Sequence


PivotKind = Literal["high", "low"]
Direction = Literal["long", "short"]
StructureEventType = Literal["BOS", "CHOCH"]


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    tick_count: float = 0.0
    bid_close: float | None = None
    ask_close: float | None = None
    avg_spread: float | None = None


@dataclass(frozen=True)
class Pivot:
    timestamp: datetime
    price: float
    kind: PivotKind
    source_timeframe: str
    confirmed_at: datetime
    index: int
    broken: bool = False


@dataclass(frozen=True)
class StructureEvent:
    timestamp: datetime
    direction: Direction
    event_type: StructureEventType
    pivot: Pivot
    close_price: float
    source_timeframe: str
    index: int


@dataclass(frozen=True)
class OrderBlock:
    timestamp: datetime
    direction: Direction
    low: float
    high: float
    body_low: float
    body_high: float
    origin_index: int
    created_by: StructureEvent
    mitigated: bool = False
    invalidated: bool = False


@dataclass(frozen=True)
class FairValueGap:
    timestamp: datetime
    direction: Direction
    low: float
    high: float
    origin_index: int


@dataclass(frozen=True)
class LiquiditySweep:
    timestamp: datetime
    direction: Direction
    pbl_price: float
    sweep_extreme: float
    reclaim_close: float
    reclaim_bars: int
    index: int


@dataclass(frozen=True)
class SmcSetup:
    direction: Direction
    htf_ob: OrderBlock
    pbl: Pivot
    sweep: LiquiditySweep
    htf_range: tuple[float, float]
    discount_premium_context: str


@dataclass(frozen=True)
class SmcSignal:
    direction: Direction
    entry_price: float
    stop_price: float
    take_profit_price: float
    quantity_hint: int | None
    reason: str
    audit: dict


@dataclass(frozen=True)
class SmcState:
    state: str
    active_setup: SmcSetup | None = None
    pending_signal: SmcSignal | None = None
    active_bracket_id: str | None = None


def normalize_bars(rows: Sequence[Bar | dict]) -> list[Bar]:
    return [row if isinstance(row, Bar) else _bar_from_mapping(row) for row in rows]


def aggregate_bars(rows: Sequence[Bar | dict], timeframe_minutes: int = 15) -> list[Bar]:
    bars = normalize_bars(rows)
    if timeframe_minutes <= 0:
        raise ValueError("timeframe_minutes must be positive")
    if not bars:
        return []

    aggregated: list[Bar] = []
    current_bucket = _bucket_start(bars[0].timestamp, timeframe_minutes)
    bucket_bars: list[Bar] = []
    for bar in bars:
        bucket = _bucket_start(bar.timestamp, timeframe_minutes)
        if bucket != current_bucket and bucket_bars:
            aggregated.append(_aggregate_bucket(current_bucket, timeframe_minutes, bucket_bars))
            bucket_bars = []
            current_bucket = bucket
        bucket_bars.append(bar)
    if bucket_bars:
        aggregated.append(_aggregate_bucket(current_bucket, timeframe_minutes, bucket_bars))
    return aggregated


def round_to_tick(price: float, tick_size: float, mode: Literal["nearest", "up", "down"] = "nearest") -> float:
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    scaled = price / tick_size
    if mode == "nearest":
        ticks = round(scaled)
    elif mode == "up":
        ticks = ceil(scaled)
    elif mode == "down":
        ticks = floor(scaled)
    else:
        raise ValueError(f"unsupported tick rounding mode: {mode}")
    return round(ticks * tick_size, 10)


def ticks_between(left_price: float, right_price: float, tick_size: float) -> int:
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    return int(round(abs(left_price - right_price) / tick_size))


def detect_pivots(
    rows: Sequence[Bar | dict],
    *,
    left: int,
    right: int,
    source_timeframe: str,
    equal_level_tolerance_ticks: int = 0,
    tick_size: float = 0.25,
) -> list[Pivot]:
    bars = normalize_bars(rows)
    if left <= 0 or right <= 0:
        raise ValueError("left and right must be positive")
    pivots: list[Pivot] = []
    for index in range(left, len(bars) - right):
        window = bars[index - left : index + right + 1]
        candidate = bars[index]
        if _is_unique_extreme(candidate.high, [bar.high for bar in window], "high", equal_level_tolerance_ticks, tick_size):
            pivots.append(
                Pivot(
                    timestamp=candidate.timestamp,
                    price=candidate.high,
                    kind="high",
                    source_timeframe=source_timeframe,
                    confirmed_at=bars[index + right].timestamp,
                    index=index,
                )
            )
        if _is_unique_extreme(candidate.low, [bar.low for bar in window], "low", equal_level_tolerance_ticks, tick_size):
            pivots.append(
                Pivot(
                    timestamp=candidate.timestamp,
                    price=candidate.low,
                    kind="low",
                    source_timeframe=source_timeframe,
                    confirmed_at=bars[index + right].timestamp,
                    index=index,
                )
            )
    return sorted(pivots, key=lambda pivot: (pivot.confirmed_at, pivot.index, pivot.kind))


def detect_structure_events(
    rows: Sequence[Bar | dict],
    pivots: Sequence[Pivot],
    *,
    break_buffer_ticks: int = 1,
    tick_size: float = 0.25,
    source_timeframe: str = "1m",
) -> list[StructureEvent]:
    bars = normalize_bars(rows)
    available_pivots = sorted(pivots, key=lambda pivot: (pivot.confirmed_at, pivot.index))
    events: list[StructureEvent] = []
    broken_indexes: set[tuple[PivotKind, int]] = set()
    trend: Direction | None = None
    for index, bar in enumerate(bars):
        usable = [pivot for pivot in available_pivots if pivot.confirmed_at <= bar.timestamp]
        high_pivots = [pivot for pivot in usable if pivot.kind == "high" and ("high", pivot.index) not in broken_indexes]
        low_pivots = [pivot for pivot in usable if pivot.kind == "low" and ("low", pivot.index) not in broken_indexes]
        high_pivot = high_pivots[-1] if high_pivots else None
        low_pivot = low_pivots[-1] if low_pivots else None
        if high_pivot is not None and bar.close > high_pivot.price + break_buffer_ticks * tick_size:
            event_type: StructureEventType = "BOS" if trend == "long" else "CHOCH"
            event = StructureEvent(bar.timestamp, "long", event_type, high_pivot, bar.close, source_timeframe, index)
            events.append(event)
            broken_indexes.add(("high", high_pivot.index))
            trend = "long"
        if low_pivot is not None and bar.close < low_pivot.price - break_buffer_ticks * tick_size:
            event_type = "BOS" if trend == "short" else "CHOCH"
            event = StructureEvent(bar.timestamp, "short", event_type, low_pivot, bar.close, source_timeframe, index)
            events.append(event)
            broken_indexes.add(("low", low_pivot.index))
            trend = "short"
    return events


def detect_fair_value_gaps(rows: Sequence[Bar | dict]) -> list[FairValueGap]:
    bars = normalize_bars(rows)
    gaps: list[FairValueGap] = []
    for index in range(2, len(bars)):
        current = bars[index]
        left = bars[index - 2]
        if current.low > left.high:
            gaps.append(FairValueGap(current.timestamp, "long", left.high, current.low, index))
        if current.high < left.low:
            gaps.append(FairValueGap(current.timestamp, "short", current.high, left.low, index))
    return gaps


def find_order_block(
    rows: Sequence[Bar | dict],
    event: StructureEvent,
    *,
    min_ob_ticks: int = 1,
    max_ob_ticks: int = 10_000,
    tick_size: float = 0.25,
) -> OrderBlock | None:
    bars = normalize_bars(rows)
    start_index = max(0, event.pivot.index)
    search = range(start_index, event.index + 1)
    selected_index: int | None = None
    if event.direction == "long":
        for index in reversed(list(search)):
            if bars[index].close < bars[index].open:
                selected_index = index
                break
    else:
        for index in reversed(list(search)):
            if bars[index].close > bars[index].open:
                selected_index = index
                break
    if selected_index is None:
        return None
    candle = bars[selected_index]
    body_low = min(candle.open, candle.close)
    body_high = max(candle.open, candle.close)
    if event.direction == "long":
        low = candle.low
        high = body_high
    else:
        low = body_low
        high = candle.high
    height = ticks_between(low, high, tick_size)
    if height < min_ob_ticks or height > max_ob_ticks:
        return None
    return OrderBlock(candle.timestamp, event.direction, low, high, body_low, body_high, selected_index, event)


def update_order_block_status(block: OrderBlock, rows: Sequence[Bar | dict], *, start_index: int | None = None) -> OrderBlock:
    bars = normalize_bars(rows)
    first_index = block.origin_index + 1 if start_index is None else start_index
    mitigated = block.mitigated
    invalidated = block.invalidated
    for bar in bars[first_index:]:
        if block.direction == "long":
            if bar.low <= block.high:
                mitigated = True
            if bar.close < block.low:
                invalidated = True
                break
        else:
            if bar.high >= block.low:
                mitigated = True
            if bar.close > block.high:
                invalidated = True
                break
    return replace(block, mitigated=mitigated, invalidated=invalidated)


def find_pbl(
    pivots: Sequence[Pivot],
    block: OrderBlock,
    *,
    before_index: int,
    pbl_clearance_ticks: int = 4,
    tick_size: float = 0.25,
) -> Pivot | None:
    clearance = pbl_clearance_ticks * tick_size
    if block.direction == "long":
        candidates = [
            pivot
            for pivot in pivots
            if pivot.kind == "low"
            and block.origin_index < pivot.index < before_index
            and pivot.price > block.high + clearance
        ]
    else:
        candidates = [
            pivot
            for pivot in pivots
            if pivot.kind == "high"
            and block.origin_index < pivot.index < before_index
            and pivot.price < block.low - clearance
        ]
    return candidates[-1] if candidates else None


def detect_liquidity_sweep(
    rows: Sequence[Bar | dict],
    *,
    direction: Direction,
    pbl_price: float,
    start_index: int,
    sweep_buffer_ticks: int = 1,
    max_reclaim_bars: int = 3,
    tick_size: float = 0.25,
) -> LiquiditySweep | None:
    bars = normalize_bars(rows)
    if start_index < 0:
        raise ValueError("start_index must be non-negative")
    buffer = sweep_buffer_ticks * tick_size
    for index in range(start_index, len(bars)):
        bar = bars[index]
        if direction == "long":
            pierced = bar.low < pbl_price - buffer
            if not pierced:
                continue
            if bar.close > pbl_price:
                return LiquiditySweep(bar.timestamp, direction, pbl_price, bar.low, bar.close, 0, index)
            sweep_extreme = bar.low
            for reclaim_offset in range(1, max_reclaim_bars + 1):
                reclaim_index = index + reclaim_offset
                if reclaim_index >= len(bars):
                    break
                reclaim_bar = bars[reclaim_index]
                sweep_extreme = min(sweep_extreme, reclaim_bar.low)
                if reclaim_bar.close > pbl_price:
                    return LiquiditySweep(
                        reclaim_bar.timestamp,
                        direction,
                        pbl_price,
                        sweep_extreme,
                        reclaim_bar.close,
                        reclaim_offset,
                        reclaim_index,
                    )
        else:
            pierced = bar.high > pbl_price + buffer
            if not pierced:
                continue
            if bar.close < pbl_price:
                return LiquiditySweep(bar.timestamp, direction, pbl_price, bar.high, bar.close, 0, index)
            sweep_extreme = bar.high
            for reclaim_offset in range(1, max_reclaim_bars + 1):
                reclaim_index = index + reclaim_offset
                if reclaim_index >= len(bars):
                    break
                reclaim_bar = bars[reclaim_index]
                sweep_extreme = max(sweep_extreme, reclaim_bar.high)
                if reclaim_bar.close < pbl_price:
                    return LiquiditySweep(
                        reclaim_bar.timestamp,
                        direction,
                        pbl_price,
                        sweep_extreme,
                        reclaim_bar.close,
                        reclaim_offset,
                        reclaim_index,
                    )
    return None


def _bar_from_mapping(row: dict) -> Bar:
    return Bar(
        timestamp=row["timestamp"],
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        tick_count=float(row.get("tick_count", 0.0) or 0.0),
        bid_close=float(row["bid_close"]) if row.get("bid_close") is not None else None,
        ask_close=float(row["ask_close"]) if row.get("ask_close") is not None else None,
        avg_spread=float(row["avg_spread"]) if row.get("avg_spread") is not None else None,
    )


def _bucket_start(timestamp: datetime, timeframe_minutes: int) -> datetime:
    minute = (timestamp.minute // timeframe_minutes) * timeframe_minutes
    return timestamp.replace(minute=minute, second=0, microsecond=0)


def _aggregate_bucket(bucket_start: datetime, timeframe_minutes: int, bars: Sequence[Bar]) -> Bar:
    timestamp = bucket_start + timedelta(minutes=timeframe_minutes)
    open_price = bars[0].open
    high = max(bar.high for bar in bars)
    low = min(bar.low for bar in bars)
    close = bars[-1].close
    tick_count = sum(bar.tick_count for bar in bars)
    spreads = [bar.avg_spread for bar in bars if bar.avg_spread is not None]
    return Bar(
        timestamp=timestamp,
        open=open_price,
        high=high,
        low=low,
        close=close,
        tick_count=tick_count,
        bid_close=bars[-1].bid_close,
        ask_close=bars[-1].ask_close,
        avg_spread=sum(spreads) / len(spreads) if spreads else None,
    )


def _is_unique_extreme(
    candidate: float,
    values: Sequence[float],
    kind: PivotKind,
    equal_level_tolerance_ticks: int,
    tick_size: float,
) -> bool:
    tolerance = equal_level_tolerance_ticks * tick_size
    if kind == "high":
        extreme = max(values)
        if candidate != extreme:
            return False
        return sum(1 for value in values if abs(value - candidate) <= tolerance) == 1
    extreme = min(values)
    if candidate != extreme:
        return False
    return sum(1 for value in values if abs(value - candidate) <= tolerance) == 1
