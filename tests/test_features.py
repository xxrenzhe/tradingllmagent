from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from tlm.feature_catalog import feature_readiness_report
from tlm.features import compute_executable_features, executable_feature_names, feature_snapshot_hash, feature_value


class ExecutableFeatureTests(unittest.TestCase):
    def test_compute_core_intraday_features_from_bars(self) -> None:
        start = datetime(2025, 1, 2, 13, 30)
        bars = []
        for index in range(40):
            close = 100.0 + index * 0.5
            bars.append(
                {
                    "symbol": "NQmain",
                    "timestamp": start + timedelta(minutes=index),
                    "open": close - 0.25,
                    "high": close + 0.75,
                    "low": close - 0.5,
                    "close": close,
                    "bid_close": close - 0.25,
                    "ask_close": close + 0.25,
                    "tick_count": 20 + index,
                    "avg_spread": 0.5,
                }
            )

        enriched = compute_executable_features(bars, session_trade="13:30-20:45", flatten="20:55")
        last = enriched[-1]

        self.assertEqual(len(enriched), len(bars))
        self.assertAlmostEqual(feature_value(last, "return_5m"), 2.5)
        self.assertAlmostEqual(feature_value(last, "spread_ticks"), 2.0)
        self.assertEqual(feature_value(last, "minutes_since_open"), 39)
        self.assertIsNotNone(feature_value(last, "atr_14"))
        self.assertIsNotNone(feature_value(last, "close_zscore_20"))
        self.assertIn("vwap_dist", last["features"])
        self.assertEqual(feature_snapshot_hash(enriched), feature_snapshot_hash(enriched))

    def test_feature_readiness_exposes_executable_subset(self) -> None:
        report = feature_readiness_report()

        self.assertGreaterEqual(report["executable_feature_count"], 30)
        self.assertIn("vwap_dist", report["executable_features"])
        self.assertIn("atr_14", executable_feature_names())

    def test_databento_tick_count_is_exposed_as_bar_volume(self) -> None:
        start = datetime(2025, 1, 2, 13, 30)
        bars = []
        for index in range(25):
            close = 100.0 + index * 0.25
            volume = 10 if index < 20 else 60
            bars.append(
                {
                    "timestamp": start + timedelta(minutes=index),
                    "open": close - 0.25,
                    "high": close + 0.5,
                    "low": close - 0.5,
                    "close": close,
                    "tick_count": volume,
                    "avg_spread": None,
                }
            )

        enriched = compute_executable_features(bars)
        last = enriched[-1]

        self.assertEqual(feature_value(last, "bar_volume"), 60.0)
        self.assertEqual(feature_value(last, "volume_1m"), 60.0)
        self.assertEqual(feature_value(last, "tick_count_1m"), 60.0)
        self.assertGreaterEqual(feature_value(last, "relative_volume_20"), 2.0)
        self.assertEqual(feature_value(last, "volume_spike_flag"), 1.0)
        self.assertIn("multi_timeframe_volume_confirm", last["features"])


if __name__ == "__main__":
    unittest.main()
