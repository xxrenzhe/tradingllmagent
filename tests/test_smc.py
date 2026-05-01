from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from tlm.smc import (
    Bar,
    StructureEvent,
    aggregate_bars,
    detect_fair_value_gaps,
    detect_liquidity_sweep,
    detect_pivots,
    detect_structure_events,
    find_order_block,
    find_pbl,
    round_to_tick,
    ticks_between,
    update_order_block_status,
)


def bar(index: int, open_price: float, high: float, low: float, close: float) -> Bar:
    return Bar(
        timestamp=datetime(2025, 1, 2, 9, 30) + timedelta(minutes=index),
        open=open_price,
        high=high,
        low=low,
        close=close,
        tick_count=10 + index,
        avg_spread=0.5,
    )


class SmcPrimitiveTests(unittest.TestCase):
    def test_aggregate_bars_uses_completed_bucket_close_time(self) -> None:
        bars = [bar(index, 100 + index, 101 + index, 99 + index, 100.5 + index) for index in range(15)]

        aggregated = aggregate_bars(bars, timeframe_minutes=15)

        self.assertEqual(len(aggregated), 1)
        self.assertEqual(aggregated[0].timestamp, datetime(2025, 1, 2, 9, 45))
        self.assertEqual(aggregated[0].open, bars[0].open)
        self.assertEqual(aggregated[0].high, bars[-1].high)
        self.assertEqual(aggregated[0].low, bars[0].low)
        self.assertEqual(aggregated[0].close, bars[-1].close)
        self.assertEqual(aggregated[0].tick_count, sum(item.tick_count for item in bars))

    def test_pivot_is_visible_only_after_right_side_confirmation(self) -> None:
        bars = [
            bar(0, 95, 100, 90, 95),
            bar(1, 96, 101, 91, 96),
            bar(2, 100, 110, 92, 100),
            bar(3, 108, 109, 93, 111),
            bar(4, 100, 104, 94, 100),
            bar(5, 111, 112, 95, 112),
            bar(6, 112, 113, 96, 112),
        ]

        pivots = detect_pivots(bars, left=2, right=2, source_timeframe="1m")
        high_pivot = next(pivot for pivot in pivots if pivot.kind == "high" and pivot.index == 2)
        events = detect_structure_events(bars, pivots, break_buffer_ticks=1, tick_size=0.25)

        self.assertEqual(high_pivot.confirmed_at, bars[4].timestamp)
        self.assertEqual(len([event for event in events if event.direction == "long"]), 1)
        self.assertEqual(events[0].timestamp, bars[5].timestamp)

    def test_structure_break_uses_close_not_wick(self) -> None:
        bars = [
            bar(0, 95, 100, 90, 95),
            bar(1, 96, 101, 91, 96),
            bar(2, 100, 110, 92, 100),
            bar(3, 101, 103, 93, 101),
            bar(4, 102, 104, 94, 102),
            bar(5, 108, 112, 95, 109),
            bar(6, 111, 113, 96, 112),
        ]

        pivots = detect_pivots(bars, left=2, right=2, source_timeframe="1m")
        events = detect_structure_events(bars, pivots, break_buffer_ticks=1, tick_size=0.25)

        self.assertEqual(len([event for event in events if event.direction == "long"]), 1)
        self.assertEqual(events[0].timestamp, bars[6].timestamp)

    def test_equal_highs_inside_tolerance_are_not_stable_pivots(self) -> None:
        bars = [
            bar(0, 95, 100, 90, 95),
            bar(1, 96, 101, 91, 96),
            bar(2, 100, 110.00, 92, 100),
            bar(3, 101, 109.75, 93, 101),
            bar(4, 102, 104, 94, 102),
            bar(5, 103, 105, 95, 103),
        ]

        pivots = detect_pivots(
            bars,
            left=2,
            right=2,
            source_timeframe="1m",
            equal_level_tolerance_ticks=2,
            tick_size=0.25,
        )

        self.assertFalse([pivot for pivot in pivots if pivot.kind == "high" and pivot.index == 2])

    def test_order_block_detection_and_mitigation_are_deterministic(self) -> None:
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 101, 102, 100, 101),
            bar(2, 103, 110, 102, 104),
            bar(3, 104, 105, 103, 104),
            bar(4, 104, 106, 103, 105),
            bar(5, 105, 106, 100, 101),
            bar(6, 101, 113, 101, 112),
            bar(7, 112, 114, 111, 113),
            bar(8, 113, 114, 104, 105),
            bar(9, 105, 106, 99, 99),
        ]
        pivot = detect_pivots(bars, left=2, right=2, source_timeframe="1m")[0]
        event = StructureEvent(bars[6].timestamp, "long", "CHOCH", pivot, bars[6].close, "1m", 6)

        block = find_order_block(bars, event, min_ob_ticks=1, max_ob_ticks=80, tick_size=0.25)
        self.assertIsNotNone(block)
        assert block is not None

        self.assertEqual(block.origin_index, 5)
        self.assertEqual(block.low, 100)
        self.assertEqual(block.high, 105)

        mitigated = update_order_block_status(block, bars, start_index=7)
        self.assertTrue(mitigated.mitigated)
        self.assertTrue(mitigated.invalidated)

    def test_fair_value_gap_detection(self) -> None:
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 101, 102, 100, 101),
            bar(2, 103, 106, 104, 105),
            bar(3, 105, 106, 104, 105),
            bar(4, 101, 102, 96, 97),
        ]

        gaps = detect_fair_value_gaps(bars)

        self.assertEqual(gaps[0].direction, "long")
        self.assertEqual(gaps[0].low, 101)
        self.assertEqual(gaps[0].high, 104)
        self.assertEqual(gaps[-1].direction, "short")

    def test_pbl_and_liquidity_sweep_require_pierce_and_reclaim(self) -> None:
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 101, 102, 100, 101),
            bar(2, 103, 110, 102, 104),
            bar(3, 104, 105, 103, 104),
            bar(4, 104, 106, 103, 105),
            bar(5, 105, 106, 100, 101),
            bar(6, 101, 113, 101, 112),
            bar(7, 112, 114, 111, 113),
            bar(8, 113, 114, 108, 109),
            bar(9, 109, 110, 109, 109.5),
            bar(10, 109.5, 110, 104.5, 109),
            bar(11, 109, 110, 108, 109.5),
        ]
        pivots = detect_pivots(bars, left=1, right=1, source_timeframe="1m")
        pivot = next(item for item in pivots if item.kind == "high" and item.index == 2)
        event = StructureEvent(bars[6].timestamp, "long", "CHOCH", pivot, bars[6].close, "1m", 6)
        block = find_order_block(bars, event, min_ob_ticks=1, max_ob_ticks=80, tick_size=0.25)
        self.assertIsNotNone(block)
        assert block is not None

        pbl = find_pbl(pivots, block, before_index=10, pbl_clearance_ticks=2, tick_size=0.25)
        self.assertIsNotNone(pbl)
        assert pbl is not None
        self.assertEqual(pbl.kind, "low")
        self.assertEqual(pbl.price, 108)

        sweep = detect_liquidity_sweep(
            bars,
            direction="long",
            pbl_price=pbl.price,
            start_index=10,
            sweep_buffer_ticks=1,
            max_reclaim_bars=2,
            tick_size=0.25,
        )

        self.assertIsNotNone(sweep)
        assert sweep is not None
        self.assertEqual(sweep.sweep_extreme, 104.5)
        self.assertEqual(sweep.reclaim_close, 109)
        self.assertEqual(sweep.reclaim_bars, 0)

    def test_tick_rounding_and_distance(self) -> None:
        self.assertEqual(round_to_tick(100.13, 0.25), 100.25)
        self.assertEqual(round_to_tick(100.13, 0.25, mode="down"), 100.0)
        self.assertEqual(round_to_tick(100.13, 0.25, mode="up"), 100.25)
        self.assertEqual(ticks_between(100.0, 101.25, 0.25), 5)


if __name__ == "__main__":
    unittest.main()
