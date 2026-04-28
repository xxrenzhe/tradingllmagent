from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tlm.feature_catalog import FEATURE_CATALOG, feature_catalog_by_name, feature_readiness_report
from tlm.strategy import load_strategy_spec, parse_strategy_spec
from tlm.strategy_generation import (
    GENERATED_STRATEGY_COMPLEXITY_LIMIT,
    feature_combo_generation_manifest,
    generate_feature_combo_strategy_specs,
    write_feature_combo_strategy_specs,
)
from tlm.variants import parameter_grid_metadata


class StrategyGenerationTests(unittest.TestCase):
    def test_feature_catalog_has_100_plus_unique_features(self) -> None:
        by_name = feature_catalog_by_name()

        self.assertGreaterEqual(len(FEATURE_CATALOG), 100)
        self.assertEqual(len(by_name), len(FEATURE_CATALOG))
        self.assertIn("vwap_dist", by_name)
        self.assertIn("order_book_imbalance_l1", by_name)
        self.assertTrue(all(feature.required_inputs for feature in FEATURE_CATALOG))
        self.assertTrue(all(feature.implementation_status for feature in FEATURE_CATALOG))

    def test_feature_readiness_report_groups_statuses(self) -> None:
        report = feature_readiness_report()

        self.assertGreaterEqual(report["feature_count"], 100)
        self.assertGreater(report["usable_for_bar_research_count"], 80)
        self.assertGreater(report["external_required_count"], 0)
        self.assertIn("implemented", report["by_status"])
        self.assertIn("external_required", report["by_status"])

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
                self.assertIn("signal_grammar", raw)
                grammar_text = str(raw["signal_grammar"])
                self.assertIn("spread_ticks", grammar_text)
                self.assertIn("atr_14", grammar_text)
                self.assertIn("generation_id", raw["generation"])
                self.assertIn("feature_combo_hash", raw["generation"])
                self.assertEqual(raw["generation"]["parameter_grid_hash"], metadata.parameter_grid_hash)
                self.assertLessEqual(
                    raw["generation"]["complexity_score"],
                    GENERATED_STRATEGY_COMPLEXITY_LIMIT,
                )
                self.assertFalse(metadata.high_risk_budget)

    def test_write_feature_combo_strategy_specs_outputs_valid_specs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "manifest.json"
            paths = write_feature_combo_strategy_specs(
                Path(temp_dir),
                count=3,
                random_seed=5,
                manifest_path=manifest_path,
            )

            self.assertEqual(len(paths), 3)
            self.assertTrue(manifest_path.exists())
            for path in paths:
                spec = load_strategy_spec(path)
                self.assertEqual(spec.symbol, "NQmain")
                self.assertEqual(spec.timeframe, "1m")

    def test_feature_combo_generation_manifest_is_stable(self) -> None:
        specs = generate_feature_combo_strategy_specs(count=2, random_seed=3)
        first = feature_combo_generation_manifest(specs, random_seed=3)
        second = feature_combo_generation_manifest(specs, random_seed=3)

        self.assertEqual(first, second)
        self.assertEqual(first["strategy_count"], 2)
        self.assertTrue(first["strategy_manifest_hash"])
