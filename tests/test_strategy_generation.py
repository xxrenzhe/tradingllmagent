from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tlm.feature_catalog import FEATURE_CATALOG, feature_catalog_by_name
from tlm.strategy import load_strategy_spec, parse_strategy_spec
from tlm.strategy_generation import generate_feature_combo_strategy_specs, write_feature_combo_strategy_specs
from tlm.variants import parameter_grid_metadata


class StrategyGenerationTests(unittest.TestCase):
    def test_feature_catalog_has_100_plus_unique_features(self) -> None:
        by_name = feature_catalog_by_name()

        self.assertGreaterEqual(len(FEATURE_CATALOG), 100)
        self.assertEqual(len(by_name), len(FEATURE_CATALOG))
        self.assertIn("vwap_dist", by_name)
        self.assertIn("order_book_imbalance_l1", by_name)

    def test_feature_combo_strategy_generation_is_deterministic_and_bounded(self) -> None:
        first = generate_feature_combo_strategy_specs(count=14, random_seed=11)
        second = generate_feature_combo_strategy_specs(count=14, random_seed=11)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 14)
        self.assertEqual(
            {spec["strategy_family"] for spec in first},
            {
                "opening_range_breakout",
                "trend_pullback",
                "volatility_expansion",
                "intraday_momentum",
                "regime_filtered_mean_reversion",
                "time_of_day_edge",
                "gap_fade_or_continuation",
            },
        )
        for raw in first:
            with self.subTest(strategy=raw["name"]):
                spec = parse_strategy_spec(raw)
                metadata = parameter_grid_metadata(spec, max_trials=1)
                self.assertGreaterEqual(len(raw["feature_set"]), 4)
                self.assertFalse(metadata.high_risk_budget)

    def test_write_feature_combo_strategy_specs_outputs_valid_specs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = write_feature_combo_strategy_specs(Path(temp_dir), count=3, random_seed=5)

            self.assertEqual(len(paths), 3)
            for path in paths:
                spec = load_strategy_spec(path)
                self.assertEqual(spec.symbol, "NQmain")
                self.assertEqual(spec.timeframe, "1m")
