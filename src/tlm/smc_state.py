from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from .smc import (
    Bar,
    Direction,
    LiquiditySweep,
    OrderBlock,
    Pivot,
    SmcSetup,
    SmcSignal,
    StructureEvent,
    detect_liquidity_sweep,
    find_order_block,
    normalize_bars,
    round_to_tick,
    ticks_between,
)


StateName = Literal[
    "IDLE",
    "FINDING_HTF_POI",
    "WAITING_FOR_PBL",
    "WAITING_FOR_SWEEP",
    "MONITORING_LTF_CHOCH",
    "ORDER_PENDING",
    "IN_POSITION",
    "COOLDOWN",
]


@dataclass(frozen=True)
class SmcLqemParameters:
    tick_size: float = 0.25
    htf_minutes: int = 15
    htf_swing_left: int = 3
    htf_swing_right: int = 3
    ltf_swing_left: int = 2
    ltf_swing_right: int = 2
    break_buffer_ticks: int = 1
    min_htf_range_ticks: int = 80
    min_ob_ticks: int = 8
    max_ob_ticks: int = 120
    min_micro_ob_ticks: int = 4
    max_micro_ob_ticks: int = 60
    pbl_clearance_ticks: int = 4
    sweep_buffer_ticks: int = 1
    max_reclaim_bars: int = 3
    stop_buffer_ticks: int = 4
    min_stop_ticks: int = 8
    max_stop_ticks: int = 80
    min_reward_r: float = 2.0
    default_take_profit_r: float = 3.0
    pending_ttl_bars: int = 10
    max_spread_ticks: float = 4.0
    cooldown_bars_after_cancel: int = 5
    cooldown_bars_after_exit: int = 15
    max_context_bars: int = 360


@dataclass(frozen=True)
class SmcTransition:
    timestamp: object
    from_state: StateName
    to_state: StateName
    reason: str
    audit: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SmcStateDecision:
    state: StateName
    signal: SmcSignal | None = None
    transition: SmcTransition | None = None
    cancellation_reason: str | None = None
    exit_reason: str | None = None


class SmcLqemStateMachine:
    def __init__(self, parameters: SmcLqemParameters | None = None) -> None:
        self.parameters = parameters or SmcLqemParameters()
        self.state: StateName = "IDLE"
        self.bars: list[Bar] = []
        self.active_ob: OrderBlock | None = None
        self.active_pbl: Pivot | None = None
        self.active_sweep: LiquiditySweep | None = None
        self.active_setup: SmcSetup | None = None
        self.pending_signal: SmcSignal | None = None
        self.pending_bar_index: int | None = None
        self.position_direction: Direction | None = None
        self.cooldown_started_index: int | None = None
        self.cooldown_bars_required = 0
        self.transition_log: list[SmcTransition] = []
        self._processed_htf_events: set[tuple] = set()
        self._processed_ltf_events: set[tuple] = set()
        self._sweep_search_index: int | None = None
        self._ltf_pivots: list[Pivot] = []
        self._ltf_events: list[StructureEvent] = []
        self._ltf_broken_indexes: set[tuple[str, int]] = set()
        self._ltf_trend: Direction | None = None
        self._htf_bars_completed: list[Bar] = []
        self._htf_pivots: list[Pivot] = []
        self._htf_events: list[StructureEvent] = []
        self._htf_broken_indexes: set[tuple[str, int]] = set()
        self._htf_trend: Direction | None = None
        self._htf_bucket_start = None
        self._htf_bucket_bars: list[Bar] = []

    def on_bar(self, row: Bar | dict, *, session_allowed: bool = True, force_flatten: bool = False) -> SmcStateDecision:
        bar = normalize_bars([row])[0]
        self.bars.append(bar)
        current_index = len(self.bars) - 1
        self._update_incremental_context(bar)

        if self.state == "ORDER_PENDING":
            decision = self._update_pending_order(bar, current_index, session_allowed)
            if decision.transition is not None:
                return decision

        if self.state == "IN_POSITION":
            decision = self._update_position(bar, current_index, force_flatten)
            if decision.transition is not None:
                return decision

        if self.state == "COOLDOWN":
            decision = self._update_cooldown(bar, current_index)
            if decision.transition is not None:
                return decision
            return SmcStateDecision(self.state)

        context = self._context()
        if self.state == "IDLE":
            transition = self._try_create_htf_poi(bar, context)
            if transition is not None:
                return SmcStateDecision(self.state, transition=transition)

        if self.state == "FINDING_HTF_POI":
            transition = self._transition(bar, "WAITING_FOR_PBL", "htf_ob_validated", {"htf_ob": _ob_audit(self.active_ob)} if self.active_ob else {})
            return SmcStateDecision(self.state, transition=transition)

        if self.state == "WAITING_FOR_PBL":
            transition = self._try_find_pbl(bar, context, current_index)
            if transition is not None:
                return SmcStateDecision(self.state, transition=transition)

        if self.state == "WAITING_FOR_SWEEP":
            transition = self._try_find_sweep(bar)
            if transition is not None:
                return SmcStateDecision(self.state, transition=transition)

        if self.state == "MONITORING_LTF_CHOCH":
            signal, transition = self._try_create_signal(bar, context, session_allowed)
            if transition is not None:
                return SmcStateDecision(self.state, signal=signal, transition=transition)

        return SmcStateDecision(self.state)

    def _context(self) -> dict:
        return {
            "htf_bars": self._htf_bars_completed,
            "htf_pivots": self._htf_pivots,
            "htf_events": self._htf_events,
            "ltf_pivots": self._ltf_pivots,
            "ltf_events": self._ltf_events,
        }

    def _update_incremental_context(self, bar: Bar) -> None:
        self._update_ltf_context()
        self._update_htf_context(bar)

    def _update_ltf_context(self) -> None:
        self._ltf_pivots.extend(
            _confirmed_pivots_for_latest_bar(
                self.bars,
                left=self.parameters.ltf_swing_left,
                right=self.parameters.ltf_swing_right,
                source_timeframe="1m",
            )
        )
        self._ltf_trend = _append_current_structure_events(
            bars=self.bars,
            pivots=self._ltf_pivots,
            events=self._ltf_events,
            broken_indexes=self._ltf_broken_indexes,
            trend=self._ltf_trend,
            break_buffer_ticks=self.parameters.break_buffer_ticks,
            tick_size=self.parameters.tick_size,
            source_timeframe="1m",
        )

    def _update_htf_context(self, bar: Bar) -> None:
        if self.parameters.htf_minutes == 1:
            self._append_completed_htf_bar(bar)
            return
        bucket = _bucket_start(bar, self.parameters.htf_minutes)
        if self._htf_bucket_start is None:
            self._htf_bucket_start = bucket
        if bucket != self._htf_bucket_start and self._htf_bucket_bars:
            self._append_completed_htf_bar(_aggregate_bucket(self._htf_bucket_start, self.parameters.htf_minutes, self._htf_bucket_bars))
            self._htf_bucket_bars = []
            self._htf_bucket_start = bucket
        self._htf_bucket_bars.append(bar)

    def _append_completed_htf_bar(self, bar: Bar) -> None:
        self._htf_bars_completed.append(bar)
        self._htf_pivots.extend(
            _confirmed_pivots_for_latest_bar(
                self._htf_bars_completed,
                left=self.parameters.htf_swing_left,
                right=self.parameters.htf_swing_right,
                source_timeframe=f"{self.parameters.htf_minutes}m",
            )
        )
        self._htf_trend = _append_current_structure_events(
            bars=self._htf_bars_completed,
            pivots=self._htf_pivots,
            events=self._htf_events,
            broken_indexes=self._htf_broken_indexes,
            trend=self._htf_trend,
            break_buffer_ticks=self.parameters.break_buffer_ticks,
            tick_size=self.parameters.tick_size,
            source_timeframe=f"{self.parameters.htf_minutes}m",
        )

    def _try_create_htf_poi(self, bar: Bar, context: dict) -> SmcTransition | None:
        htf_bars: list[Bar] = context["htf_bars"]
        htf_pivots: list[Pivot] = context["htf_pivots"]
        event = self._latest_unprocessed_event(context["htf_events"], self._processed_htf_events)
        if event is None:
            return None
        self._processed_htf_events.add(_event_key(event))
        htf_range = _latest_swing_range(htf_pivots)
        if htf_range is None:
            return None
        range_low, range_high = htf_range
        if ticks_between(range_low, range_high, self.parameters.tick_size) < self.parameters.min_htf_range_ticks:
            return None
        block = find_order_block(
            htf_bars,
            event,
            min_ob_ticks=self.parameters.min_ob_ticks,
            max_ob_ticks=self.parameters.max_ob_ticks,
            tick_size=self.parameters.tick_size,
        )
        if block is None:
            return None
        midpoint = (block.low + block.high) / 2
        equilibrium = (range_low + range_high) / 2
        if block.direction == "long" and midpoint > equilibrium:
            return None
        if block.direction == "short" and midpoint < equilibrium:
            return None
        self.active_ob = block
        self.active_pbl = None
        self.active_sweep = None
        self.active_setup = None
        self._sweep_search_index = None
        return self._transition(
            bar,
            "FINDING_HTF_POI",
            "htf_poi_detected",
            {
                "htf_ob": _ob_audit(block),
                "htf_range": {"low": range_low, "high": range_high},
                "structure_event": _event_audit(event),
            },
        )

    def _try_find_pbl(self, bar: Bar, context: dict, current_index: int) -> SmcTransition | None:
        if self.active_ob is None:
            return None
        pbl = self._find_pbl_by_time(context["ltf_pivots"], current_index)
        if pbl is None:
            return None
        self.active_pbl = pbl
        self._sweep_search_index = current_index
        return self._transition(bar, "WAITING_FOR_SWEEP", "pbl_confirmed", {"pbl": _pivot_audit(pbl)})

    def _try_find_sweep(self, bar: Bar) -> SmcTransition | None:
        if self.active_ob is None or self.active_pbl is None or self._sweep_search_index is None:
            return None
        sweep = detect_liquidity_sweep(
            self.bars,
            direction=self.active_ob.direction,
            pbl_price=self.active_pbl.price,
            start_index=self._sweep_search_index,
            sweep_buffer_ticks=self.parameters.sweep_buffer_ticks,
            max_reclaim_bars=self.parameters.max_reclaim_bars,
            tick_size=self.parameters.tick_size,
        )
        if sweep is None:
            return None
        sweep_bar = self.bars[sweep.index]
        if not _bar_touches_ob(sweep_bar, self.active_ob):
            return None
        self.active_sweep = sweep
        self.active_setup = SmcSetup(
            self.active_ob.direction,
            self.active_ob,
            self.active_pbl,
            sweep,
            (self.active_ob.low, self.active_ob.high),
            "discount" if self.active_ob.direction == "long" else "premium",
        )
        return self._transition(bar, "MONITORING_LTF_CHOCH", "liquidity_sweep_confirmed", {"sweep": _sweep_audit(sweep)})

    def _try_create_signal(
        self,
        bar: Bar,
        context: dict,
        session_allowed: bool,
    ) -> tuple[SmcSignal | None, SmcTransition | None]:
        if self.active_ob is None or self.active_sweep is None or not session_allowed:
            return None, None
        if bar.avg_spread is not None and bar.avg_spread / self.parameters.tick_size > self.parameters.max_spread_ticks:
            return None, None
        events = [
            event
            for event in context["ltf_events"]
            if event.direction == self.active_ob.direction and event.index > self.active_sweep.index
        ]
        event = self._latest_unprocessed_event(events, self._processed_ltf_events)
        if event is None:
            return None, None
        self._processed_ltf_events.add(_event_key(event))
        micro_ob = find_order_block(
            self.bars,
            event,
            min_ob_ticks=self.parameters.min_micro_ob_ticks,
            max_ob_ticks=self.parameters.max_micro_ob_ticks,
            tick_size=self.parameters.tick_size,
        )
        if micro_ob is None:
            return None, None
        signal = self._signal_from_micro_ob(event, micro_ob)
        if signal is None:
            return None, None
        self.pending_signal = signal
        self.pending_bar_index = len(self.bars) - 1
        return signal, self._transition(bar, "ORDER_PENDING", "ltf_choch_signal", signal.audit)

    def _signal_from_micro_ob(self, event: StructureEvent, micro_ob: OrderBlock) -> SmcSignal | None:
        if self.active_ob is None or self.active_pbl is None or self.active_sweep is None:
            return None
        tick_size = self.parameters.tick_size
        if micro_ob.direction == "long":
            entry = round_to_tick(micro_ob.high, tick_size)
            stop = round_to_tick(
                min(micro_ob.low, self.active_sweep.sweep_extreme) - self.parameters.stop_buffer_ticks * tick_size,
                tick_size,
                mode="down",
            )
            risk = entry - stop
            target = round_to_tick(entry + self.parameters.default_take_profit_r * risk, tick_size)
        else:
            entry = round_to_tick(micro_ob.low, tick_size)
            stop = round_to_tick(
                max(micro_ob.high, self.active_sweep.sweep_extreme) + self.parameters.stop_buffer_ticks * tick_size,
                tick_size,
                mode="up",
            )
            risk = stop - entry
            target = round_to_tick(entry - self.parameters.default_take_profit_r * risk, tick_size)
        stop_ticks = ticks_between(entry, stop, tick_size)
        if stop_ticks < self.parameters.min_stop_ticks or stop_ticks > self.parameters.max_stop_ticks:
            return None
        audit = {
            "direction": micro_ob.direction,
            "entry_price": entry,
            "stop_price": stop,
            "take_profit_price": target,
            "stop_ticks": stop_ticks,
            "htf_ob": _ob_audit(self.active_ob),
            "micro_ob": _ob_audit(micro_ob),
            "pbl": _pivot_audit(self.active_pbl),
            "sweep": _sweep_audit(self.active_sweep),
            "ltf_structure_event": _event_audit(event),
        }
        return SmcSignal(micro_ob.direction, entry, stop, target, None, "smc_lqem_ce", audit)

    def _update_pending_order(self, bar: Bar, current_index: int, session_allowed: bool) -> SmcStateDecision:
        if self.pending_signal is None or self.pending_bar_index is None:
            return SmcStateDecision(self.state)
        signal = self.pending_signal
        target_touched = (
            bar.high >= signal.take_profit_price if signal.direction == "long" else bar.low <= signal.take_profit_price
        )
        fill_touched = bar.low <= signal.entry_price if signal.direction == "long" else bar.high >= signal.entry_price
        ttl_expired = current_index - self.pending_bar_index > self.parameters.pending_ttl_bars
        if target_touched and not fill_touched:
            transition = self._cancel(bar, current_index, "target_reached_before_fill")
            return SmcStateDecision(self.state, transition=transition, cancellation_reason="target_reached_before_fill")
        if ttl_expired:
            transition = self._cancel(bar, current_index, "pending_ttl_expired")
            return SmcStateDecision(self.state, transition=transition, cancellation_reason="pending_ttl_expired")
        if not session_allowed:
            transition = self._cancel(bar, current_index, "session_closed_before_fill")
            return SmcStateDecision(self.state, transition=transition, cancellation_reason="session_closed_before_fill")
        if fill_touched:
            self.position_direction = signal.direction
            transition = self._transition(bar, "IN_POSITION", "parent_limit_filled", signal.audit)
            return SmcStateDecision(self.state, signal=signal, transition=transition)
        return SmcStateDecision(self.state)

    def _update_position(self, bar: Bar, current_index: int, force_flatten: bool) -> SmcStateDecision:
        if self.pending_signal is None:
            return SmcStateDecision(self.state)
        signal = self.pending_signal
        exit_reason: str | None = None
        if force_flatten:
            exit_reason = "force_flatten"
        elif signal.direction == "long":
            if bar.low <= signal.stop_price:
                exit_reason = "stop_loss"
            elif bar.high >= signal.take_profit_price:
                exit_reason = "take_profit"
        else:
            if bar.high >= signal.stop_price:
                exit_reason = "stop_loss"
            elif bar.low <= signal.take_profit_price:
                exit_reason = "take_profit"
        if exit_reason is None:
            return SmcStateDecision(self.state)
        transition = self._exit_to_cooldown(bar, current_index, exit_reason)
        return SmcStateDecision(self.state, transition=transition, exit_reason=exit_reason)

    def _update_cooldown(self, bar: Bar, current_index: int) -> SmcStateDecision:
        if self.cooldown_started_index is None:
            return SmcStateDecision(self.state)
        if current_index - self.cooldown_started_index < self.cooldown_bars_required:
            return SmcStateDecision(self.state)
        self._clear_active_trade()
        transition = self._transition(bar, "IDLE", "cooldown_complete")
        return SmcStateDecision(self.state, transition=transition)

    def _cancel(self, bar: Bar, current_index: int, reason: str) -> SmcTransition:
        self.cooldown_started_index = current_index
        self.cooldown_bars_required = self.parameters.cooldown_bars_after_cancel
        return self._transition(bar, "COOLDOWN", reason, self.pending_signal.audit if self.pending_signal else {})

    def _exit_to_cooldown(self, bar: Bar, current_index: int, reason: str) -> SmcTransition:
        self.cooldown_started_index = current_index
        self.cooldown_bars_required = self.parameters.cooldown_bars_after_exit
        return self._transition(bar, "COOLDOWN", reason, self.pending_signal.audit if self.pending_signal else {})

    def _clear_active_trade(self) -> None:
        self.active_ob = None
        self.active_pbl = None
        self.active_sweep = None
        self.active_setup = None
        self.pending_signal = None
        self.pending_bar_index = None
        self.position_direction = None
        self.cooldown_started_index = None
        self.cooldown_bars_required = 0
        self._sweep_search_index = None

    def _find_pbl_by_time(self, pivots: list[Pivot], current_index: int) -> Pivot | None:
        if self.active_ob is None:
            return None
        clearance = self.parameters.pbl_clearance_ticks * self.parameters.tick_size
        if self.active_ob.direction == "long":
            candidates = [
                pivot
                for pivot in pivots
                if pivot.kind == "low"
                and pivot.timestamp > self.active_ob.timestamp
                and pivot.index < current_index
                and pivot.price > self.active_ob.high + clearance
            ]
        else:
            candidates = [
                pivot
                for pivot in pivots
                if pivot.kind == "high"
                and pivot.timestamp > self.active_ob.timestamp
                and pivot.index < current_index
                and pivot.price < self.active_ob.low - clearance
            ]
        return candidates[-1] if candidates else None

    def _latest_unprocessed_event(self, events: list[StructureEvent], processed: set[tuple]) -> StructureEvent | None:
        for event in reversed(events):
            if _event_key(event) not in processed:
                return event
        return None

    def _transition(self, bar: Bar, to_state: StateName, reason: str, audit: dict | None = None) -> SmcTransition:
        transition = SmcTransition(bar.timestamp, self.state, to_state, reason, audit or {})
        self.state = to_state
        self.transition_log.append(transition)
        return transition


def _event_key(event: StructureEvent) -> tuple:
    return (
        event.source_timeframe,
        event.timestamp,
        event.index,
        event.direction,
        event.event_type,
        event.pivot.index,
    )


def _confirmed_pivots_for_latest_bar(
    bars: list[Bar],
    *,
    left: int,
    right: int,
    source_timeframe: str,
) -> list[Pivot]:
    candidate_index = len(bars) - right - 1
    if candidate_index < left:
        return []
    if candidate_index + right >= len(bars):
        return []
    window = bars[candidate_index - left : candidate_index + right + 1]
    candidate = bars[candidate_index]
    confirmed_at = bars[candidate_index + right].timestamp
    pivots = []
    highs = [bar.high for bar in window]
    lows = [bar.low for bar in window]
    if candidate.high == max(highs) and highs.count(candidate.high) == 1:
        pivots.append(
            Pivot(
                timestamp=candidate.timestamp,
                price=candidate.high,
                kind="high",
                source_timeframe=source_timeframe,
                confirmed_at=confirmed_at,
                index=candidate_index,
            )
        )
    if candidate.low == min(lows) and lows.count(candidate.low) == 1:
        pivots.append(
            Pivot(
                timestamp=candidate.timestamp,
                price=candidate.low,
                kind="low",
                source_timeframe=source_timeframe,
                confirmed_at=confirmed_at,
                index=candidate_index,
            )
        )
    return pivots


def _append_current_structure_events(
    *,
    bars: list[Bar],
    pivots: list[Pivot],
    events: list[StructureEvent],
    broken_indexes: set[tuple[str, int]],
    trend: Direction | None,
    break_buffer_ticks: int,
    tick_size: float,
    source_timeframe: str,
) -> Direction | None:
    if not bars:
        return trend
    index = len(bars) - 1
    bar = bars[-1]
    usable = [pivot for pivot in pivots if pivot.confirmed_at <= bar.timestamp]
    high_pivots = [pivot for pivot in usable if pivot.kind == "high" and ("high", pivot.index) not in broken_indexes]
    low_pivots = [pivot for pivot in usable if pivot.kind == "low" and ("low", pivot.index) not in broken_indexes]
    high_pivot = high_pivots[-1] if high_pivots else None
    low_pivot = low_pivots[-1] if low_pivots else None
    if high_pivot is not None and bar.close > high_pivot.price + break_buffer_ticks * tick_size:
        event_type = "BOS" if trend == "long" else "CHOCH"
        events.append(StructureEvent(bar.timestamp, "long", event_type, high_pivot, bar.close, source_timeframe, index))
        broken_indexes.add(("high", high_pivot.index))
        trend = "long"
    if low_pivot is not None and bar.close < low_pivot.price - break_buffer_ticks * tick_size:
        event_type = "BOS" if trend == "short" else "CHOCH"
        events.append(StructureEvent(bar.timestamp, "short", event_type, low_pivot, bar.close, source_timeframe, index))
        broken_indexes.add(("low", low_pivot.index))
        trend = "short"
    return trend


def _bucket_start(bar: Bar, timeframe_minutes: int):
    minute = (bar.timestamp.minute // timeframe_minutes) * timeframe_minutes
    return bar.timestamp.replace(minute=minute, second=0, microsecond=0)


def _aggregate_bucket(bucket_start, timeframe_minutes: int, bars: list[Bar]) -> Bar:
    spreads = [bar.avg_spread for bar in bars if bar.avg_spread is not None]
    return Bar(
        timestamp=bucket_start + timedelta(minutes=timeframe_minutes),
        open=bars[0].open,
        high=max(bar.high for bar in bars),
        low=min(bar.low for bar in bars),
        close=bars[-1].close,
        tick_count=sum(bar.tick_count for bar in bars),
        bid_close=bars[-1].bid_close,
        ask_close=bars[-1].ask_close,
        avg_spread=sum(spreads) / len(spreads) if spreads else None,
    )


def _latest_swing_range(pivots: list[Pivot]) -> tuple[float, float] | None:
    latest_high = next((pivot for pivot in reversed(pivots) if pivot.kind == "high"), None)
    latest_low = next((pivot for pivot in reversed(pivots) if pivot.kind == "low"), None)
    if latest_high is None or latest_low is None:
        return None
    return latest_low.price, latest_high.price


def _bar_touches_ob(bar: Bar, block: OrderBlock) -> bool:
    return bar.low <= block.high and bar.high >= block.low


def _pivot_audit(pivot: Pivot) -> dict:
    return {
        "timestamp": pivot.timestamp.isoformat(),
        "price": pivot.price,
        "kind": pivot.kind,
        "source_timeframe": pivot.source_timeframe,
        "confirmed_at": pivot.confirmed_at.isoformat(),
        "index": pivot.index,
    }


def _event_audit(event: StructureEvent) -> dict:
    return {
        "timestamp": event.timestamp.isoformat(),
        "direction": event.direction,
        "event_type": event.event_type,
        "close_price": event.close_price,
        "source_timeframe": event.source_timeframe,
        "index": event.index,
        "pivot": _pivot_audit(event.pivot),
    }


def _ob_audit(block: OrderBlock) -> dict:
    return {
        "timestamp": block.timestamp.isoformat(),
        "direction": block.direction,
        "low": block.low,
        "high": block.high,
        "body_low": block.body_low,
        "body_high": block.body_high,
        "origin_index": block.origin_index,
        "mitigated": block.mitigated,
        "invalidated": block.invalidated,
        "created_by": _event_audit(block.created_by),
    }


def _sweep_audit(sweep: LiquiditySweep) -> dict:
    return {
        "timestamp": sweep.timestamp.isoformat(),
        "direction": sweep.direction,
        "pbl_price": sweep.pbl_price,
        "sweep_extreme": sweep.sweep_extreme,
        "reclaim_close": sweep.reclaim_close,
        "reclaim_bars": sweep.reclaim_bars,
        "index": sweep.index,
    }
