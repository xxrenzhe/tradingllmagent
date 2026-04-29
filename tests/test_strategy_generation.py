from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tlm.feature_catalog import FEATURE_CATALOG, feature_catalog_by_name, feature_readiness_report
from tlm.strategy import load_strategy_spec, parse_strategy_spec
from tlm.strategy_generation import (
    DEFAULT_SPREAD_GATE_TICKS,
    GENERATED_STRATEGY_COMPLEXITY_LIMIT,
    PRIMARY_NQ_FUTURES_FAMILIES,
    PRIMARY_NQ_MAX_TRADES_PER_DAY,
    VOL_EXECUTION_AWARE_FAMILIES,
    candidate_expected_move_floor,
    feature_combo_generation_manifest,
    generate_feature_combo_strategy_specs,
    generate_primary_nq_strategy_specs,
    generate_vol_strategy_specs,
    primary_nq_strategy_generation_manifest,
    spread_gate_ticks_for_symbol,
    write_feature_combo_strategy_specs,
    write_primary_nq_strategy_specs,
    write_vol_strategy_specs,
    vol_strategy_generation_manifest,
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
                spread_filter = raw["signal_grammar"]["filters"]["all"][0]
                self.assertEqual(spread_filter["feature"], "spread_ticks")
                self.assertEqual(spread_filter["value"], 16)
                self.assertEqual(raw["generation"]["spread_gate_ticks"], 16)
                self.assertIn("atr_14", grammar_text)
                self.assertIn("generation_id", raw["generation"])
                self.assertIn("feature_combo_hash", raw["generation"])
                self.assertFalse(raw["generation"]["primary_research_track"])
                self.assertTrue(raw["generation"]["legacy_random_search"])
                self.assertEqual(raw["generation"]["parameter_grid_hash"], metadata.parameter_grid_hash)
                self.assertLessEqual(
                    raw["generation"]["complexity_score"],
                    GENERATED_STRATEGY_COMPLEXITY_LIMIT,
                )
                self.assertFalse(metadata.high_risk_budget)

    def test_spread_gate_uses_symbol_specific_calibration(self) -> None:
        self.assertEqual(spread_gate_ticks_for_symbol("NQmain"), 16)
        self.assertEqual(spread_gate_ticks_for_symbol("UNKNOWN"), DEFAULT_SPREAD_GATE_TICKS)

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

    def test_primary_nq_strategy_generation_outputs_primary_research_specs(self) -> None:
        specs = generate_primary_nq_strategy_specs()

        self.assertEqual(len(specs), len(PRIMARY_NQ_FUTURES_FAMILIES))
        self.assertLessEqual(len(PRIMARY_NQ_FUTURES_FAMILIES), 4)
        self.assertEqual({spec["strategy_family"] for spec in specs}, set(PRIMARY_NQ_FUTURES_FAMILIES))
        for raw in specs:
            with self.subTest(strategy=raw["name"]):
                spec = parse_strategy_spec(raw)
                self.assertEqual(spec.symbol, "NQ_CME")
                self.assertEqual(raw["generation"]["method"], "primary_nq_futures_seed")
                self.assertTrue(raw["generation"]["primary_research_track"])
                self.assertFalse(raw["generation"]["legacy_random_search"])
                self.assertEqual(raw["generation"]["candidate_family_set"], list(PRIMARY_NQ_FUTURES_FAMILIES))
                self.assertEqual(raw["risk"]["max_trades_per_day"], PRIMARY_NQ_MAX_TRADES_PER_DAY)
                self.assertEqual(
                    raw["generation"]["expected_move_floor_usd"],
                    candidate_expected_move_floor(raw["strategy_family"]),
                )
                self.assertGreaterEqual(raw["generation"]["expected_move_floor_usd"], 45.0)
                self.assertTrue(raw["primary_candidate_constraints"]["requires_multi_condition_confirmation"])
                for side in ("long", "short"):
                    self.assertGreaterEqual(len(raw["signal_grammar"]["entry"][side]["all"]), 3)

    def test_write_primary_nq_strategy_specs_outputs_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "manifest.json"
            paths = write_primary_nq_strategy_specs(Path(temp_dir), manifest_path=manifest_path)
            specs = [load_strategy_spec(path) for path in paths]
            manifest = primary_nq_strategy_generation_manifest([spec.raw for spec in specs])

            self.assertEqual(len(paths), len(PRIMARY_NQ_FUTURES_FAMILIES))
            self.assertTrue(manifest_path.exists())
            self.assertEqual(manifest["strategy_count"], len(PRIMARY_NQ_FUTURES_FAMILIES))
            self.assertEqual(manifest["method"], "primary_nq_futures_seed")
            self.assertTrue(manifest["primary_research_track"])
            self.assertEqual(manifest["candidate_family_set"], list(PRIMARY_NQ_FUTURES_FAMILIES))
            self.assertEqual(
                {record["primary_template"] for record in manifest["strategies"]},
                {
                    "opening_range_breakout_retest_with_confirmation",
                    "trend_pullback_after_impulse",
                    "volatility_expansion_after_compression",
                    "regime_filtered_continuation",
                },
            )

    def test_feature_combo_generation_manifest_is_stable(self) -> None:
        specs = generate_feature_combo_strategy_specs(count=2, random_seed=3)
        first = feature_combo_generation_manifest(specs, random_seed=3)
        second = feature_combo_generation_manifest(specs, random_seed=3)

        self.assertEqual(first, second)
        self.assertEqual(first["strategy_count"], 2)
        self.assertFalse(first["primary_research_track"])
        self.assertTrue(first["legacy_random_search"])
        self.assertTrue(first["strategy_manifest_hash"])

    def test_vol_strategy_generation_outputs_execution_aware_specs(self) -> None:
        specs = generate_vol_strategy_specs()

        self.assertEqual(len(specs), len(VOL_EXECUTION_AWARE_FAMILIES))
        self.assertEqual({spec["strategy_family"] for spec in specs}, set(VOL_EXECUTION_AWARE_FAMILIES))
        for raw in specs:
            with self.subTest(strategy=raw["name"]):
                spec = parse_strategy_spec(raw)
                self.assertEqual(spec.symbol, "NQ_CME")
                self.assertEqual(spec.timeframe, "1m")
                self.assertEqual(raw["generation"]["method"], "deterministic_vol_execution_seed")
                self.assertEqual(
                    raw["generation"]["execution_assumption"],
                    "ohlcv_pre_screen_requires_quote_replay_before_paper_shadow",
                )
                grammar_text = str(raw["signal_grammar"])
                self.assertIn("bar_volume", str(raw["feature_set"]))
                self.assertIn("low_volume_filter", grammar_text)
                self.assertIn("spread_ticks", grammar_text)

    def test_write_vol_strategy_specs_outputs_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "manifest.json"
            paths = write_vol_strategy_specs(Path(temp_dir), manifest_path=manifest_path)
            specs = [load_strategy_spec(path) for path in paths]
            manifest = vol_strategy_generation_manifest([spec.raw for spec in specs])

            self.assertEqual(len(paths), len(VOL_EXECUTION_AWARE_FAMILIES))
            self.assertTrue(manifest_path.exists())
            self.assertEqual(manifest["strategy_count"], len(VOL_EXECUTION_AWARE_FAMILIES))
            self.assertIn("vol_quote_replay_report.json", manifest["required_next_artifacts"])
