from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from tlm.ibkr_signals import build_one_minute_bars, build_signal_candidate


def snapshot(offset_seconds: int, last: float, *, bid: float | None = None, ask: float | None = None) -> dict:
    timestamp = datetime(2026, 4, 29, 14, 30, tzinfo=UTC) + timedelta(seconds=offset_seconds)
    return {
        "symbol": "MNQ",
        "snapshot_time": timestamp.isoformat(),
        "last": last,
        "bid": bid if bid is not None else last - 0.25,
        "ask": ask if ask is not None else last,
    }


class IbkrSignalEngineTests(unittest.TestCase):
    def test_build_one_minute_bars_groups_snapshots(self) -> None:
        bars = build_one_minute_bars(
            [
                snapshot(0, 19000.0),
                snapshot(20, 19001.0),
                snapshot(70, 19002.0),
            ]
        )

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].open, 19000.0)
        self.assertEqual(bars[0].high, 19001.0)
        self.assertEqual(bars[0].close, 19001.0)
        self.assertEqual(bars[0].tick_count, 2)
        self.assertEqual(bars[1].bar_time.minute, 31)

    def test_build_signal_candidate_emits_strong_review_for_range_breakout(self) -> None:
        snapshots = []
        for minute, price in enumerate([19000.0, 19000.5, 19001.0, 19000.75, 19001.25, 19002.0]):
            snapshots.append(snapshot(minute * 60, price, bid=price - 0.25, ask=price))
        bars = build_one_minute_bars(snapshots)
        strategy = {
            "strategy_id": "mnq_breakout_v1",
            "strategy_spec_hash": "hash_1",
            "module_id": "module_breakout",
            "symbol": "MNQ",
            "timeframe": "1m",
            "family": "range_breakout",
            "lookback_bars": 3,
            "breakout_ticks": 1,
            "enabled": True,
        }

        signal = build_signal_candidate(strategy, bars, max_spread_ticks=2)

        self.assertEqual(signal["signal_class"], "strong_review")
        self.assertEqual(signal["side"], "BUY")
        self.assertEqual(signal["signal_type"], "entry")
        self.assertIn("range_breakout_up", signal["trigger_reasons"])
        self.assertEqual(signal["risk_context"]["spread_ticks"], 1.0)
        self.assertEqual(signal["bar_1m"]["close"], 19002.0)

    def test_build_signal_candidate_blocks_high_spread(self) -> None:
        snapshots = []
        for minute, price in enumerate([19000.0, 19000.5, 19001.0, 19000.75, 19001.25, 19002.0]):
            bid = price - 1.0 if minute == 5 else price - 0.25
            snapshots.append(snapshot(minute * 60, price, bid=bid, ask=price))
        bars = build_one_minute_bars(snapshots)

        signal = build_signal_candidate(
            {"strategy_id": "mnq_breakout_v1", "symbol": "MNQ", "timeframe": "1m", "lookback_bars": 3},
            bars,
            max_spread_ticks=2,
        )

        self.assertEqual(signal["signal_class"], "blocked")
        self.assertTrue(signal["blocked"])
        self.assertIn("spread_above_limit", signal["reasons"])

    def test_build_signal_candidate_requires_sufficient_lookback(self) -> None:
        bars = build_one_minute_bars([snapshot(0, 19000.0), snapshot(60, 19000.25)])

        signal = build_signal_candidate(
            {"strategy_id": "mnq_breakout_v1", "symbol": "MNQ", "timeframe": "1m", "lookback_bars": 3},
            bars,
        )

        self.assertEqual(signal["signal_class"], "none")
        self.assertIn("insufficient_lookback", signal["reasons"])


if __name__ == "__main__":
    unittest.main()
