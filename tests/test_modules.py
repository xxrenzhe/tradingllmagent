from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tlm.cli import main
from tlm.modules import (
    build_target_frequency_pool,
    build_module_registry,
    load_module_performance_memory,
    module_can_enter_runtime,
    strategy_module_catalog,
    summarize_module_performance,
    transition_module_status,
    write_module_performance_memory,
)


class ModuleMemoryTests(unittest.TestCase):
    def test_module_catalog_exposes_expected_modules(self) -> None:
        catalog = strategy_module_catalog()
        module_ids = {module["module_id"] for module in catalog}

        self.assertIn("opening_range_breakout", module_ids)
        self.assertIn("trend_pullback", module_ids)

    def test_module_performance_summary_groups_records(self) -> None:
        records = [
            {
                "experiment_id": "exp_a",
                "module_id": "opening_range_breakout",
                "timeframe": "5m",
                "trade_count": 120,
                "expectancy": 12.5,
                "passed": True,
                "robustness_score": 0.62,
                "rejection_reasons": [],
                "parameter_stability_status": "stable",
            },
            {
                "experiment_id": "exp_b",
                "module_id": "opening_range_breakout",
                "timeframe": "15m",
                "trade_count": 30,
                "expectancy": -4.0,
                "passed": False,
                "robustness_score": None,
                "rejection_reasons": ["annual_trades_test"],
                "parameter_stability_status": "insufficient_neighbors",
            },
        ]

        summary = summarize_module_performance(records)
        module = summary["modules"][0]

        self.assertEqual(summary["evaluated_records"], 2)
        self.assertEqual(module["module_id"], "opening_range_breakout")
        self.assertEqual(module["evaluated_records"], 2)
        self.assertEqual(module["passed_records"], 1)
        self.assertEqual(module["total_trade_count"], 150)
        self.assertEqual(module["best_experiment_id"], "exp_a")
        self.assertEqual(module["rejection_reasons"], {"annual_trades_test": 1})
        self.assertEqual(module["timeframes"], ["15m", "5m"])
        self.assertIn("target_frequency_pool", summary)

    def test_target_frequency_pool_selects_rate_and_win_qualified_records(self) -> None:
        records = [
            {
                "experiment_id": "exp_a",
                "module_id": "z1iHTV6D",
                "strategy_name": "candidate_a",
                "timeframe": "15m",
                "passed": True,
                "trades_per_day": 1.1,
                "proxy_win_rate": 0.61,
                "expectancy": 8.0,
                "robustness_score": 0.7,
            },
            {
                "experiment_id": "exp_b",
                "module_id": "eTdXc6bw",
                "strategy_name": "candidate_b",
                "timeframe": "15m",
                "passed": True,
                "trades_per_day": 0.9,
                "proxy_win_rate": 0.58,
                "expectancy": 5.0,
                "robustness_score": 0.6,
            },
            {
                "experiment_id": "exp_c",
                "module_id": "blocked",
                "strategy_name": "weak_candidate",
                "passed": True,
                "trades_per_day": 1.3,
                "proxy_win_rate": 0.49,
            },
            {
                "experiment_id": "exp_d",
                "module_id": "rejected",
                "strategy_name": "rejected_candidate",
                "passed": False,
                "trades_per_day": 0.8,
                "proxy_win_rate": 0.7,
            },
        ]

        pool = build_target_frequency_pool(records)

        self.assertEqual(pool["status"], "target_met")
        self.assertEqual(pool["selected_count"], 2)
        self.assertAlmostEqual(pool["selected_trades_per_day"], 2.0)
        self.assertGreaterEqual(pool["weighted_proxy_win_rate"], 0.53)
        self.assertEqual(
            [row["strategy_name"] for row in pool["selected"]],
            ["candidate_a", "candidate_b"],
        )
        self.assertEqual(pool["rejected"]["proxy_win_rate_below_threshold"], 1)
        self.assertEqual(pool["rejected"]["not_passed"], 1)
        self.assertEqual(pool["schema_version"], 2)
        self.assertEqual(pool["diversity_report"]["selected_module_count"], 2)
        self.assertEqual(len(pool["candidate_quality_order"]), 2)

    def test_target_frequency_pool_limits_duplicate_modules(self) -> None:
        records = [
            {
                "experiment_id": "exp_a",
                "module_id": "same_module",
                "strategy_name": "best_duplicate",
                "strategy_spec_hash": "hash_a",
                "passed": True,
                "trades_per_day": 1.0,
                "proxy_win_rate": 0.7,
                "expectancy": 8.0,
                "robustness_score": 0.8,
            },
            {
                "experiment_id": "exp_b",
                "module_id": "same_module",
                "strategy_name": "lower_duplicate",
                "strategy_spec_hash": "hash_b",
                "passed": True,
                "trades_per_day": 1.0,
                "proxy_win_rate": 0.65,
                "expectancy": 5.0,
                "robustness_score": 0.7,
            },
            {
                "experiment_id": "exp_c",
                "module_id": "different_module",
                "strategy_name": "diversifier",
                "strategy_spec_hash": "hash_c",
                "passed": True,
                "trades_per_day": 1.1,
                "proxy_win_rate": 0.6,
            },
        ]

        pool = build_target_frequency_pool(records)

        self.assertEqual(pool["status"], "target_met")
        self.assertEqual(
            [row["strategy_name"] for row in pool["selected"]],
            ["best_duplicate", "diversifier"],
        )
        self.assertEqual(pool["diversity_report"]["selected_modules"], ["different_module", "same_module"])

    def test_target_frequency_pool_limits_correlation_groups(self) -> None:
        records = [
            {
                "experiment_id": "exp_a",
                "module_id": "module_a",
                "strategy_name": "best_correlated",
                "correlation_group": "session_breakout_cluster",
                "passed": True,
                "trades_per_day": 1.0,
                "proxy_win_rate": 0.7,
                "robustness_score": 0.8,
            },
            {
                "experiment_id": "exp_b",
                "module_id": "module_b",
                "strategy_name": "second_correlated",
                "correlation_group": "session_breakout_cluster",
                "passed": True,
                "trades_per_day": 1.0,
                "proxy_win_rate": 0.68,
                "robustness_score": 0.7,
            },
            {
                "experiment_id": "exp_c",
                "module_id": "module_c",
                "strategy_name": "uncorrelated",
                "correlation_group": "mean_reversion_cluster",
                "passed": True,
                "trades_per_day": 1.1,
                "proxy_win_rate": 0.6,
            },
        ]

        pool = build_target_frequency_pool(records)

        self.assertEqual(pool["status"], "target_met")
        self.assertEqual(
            [row["strategy_name"] for row in pool["selected"]],
            ["best_correlated", "uncorrelated"],
        )
        self.assertEqual(pool["diversity_report"]["selected_near_duplicate_pair_count"], 0)
        self.assertEqual(pool["diversity_report"]["selected_correlation_group_count"], 2)

    def test_target_frequency_pool_rejects_near_duplicate_signal_pairs(self) -> None:
        similarity_report = {
            "status": "near_duplicates_found",
            "threshold": 0.8,
            "near_duplicate_pair_count": 1,
            "pairs": [
                {
                    "left_trial": "exp_a",
                    "right_trial": "exp_b",
                    "similarity": 0.95,
                    "near_duplicate": True,
                }
            ],
        }
        records = [
            {
                "experiment_id": "exp_a",
                "module_id": "module_a",
                "strategy_name": "best_duplicate_signal",
                "passed": True,
                "trades_per_day": 1.0,
                "proxy_win_rate": 0.7,
                "signal_similarity_report": similarity_report,
            },
            {
                "experiment_id": "exp_b",
                "module_id": "module_b",
                "strategy_name": "near_duplicate_signal",
                "passed": True,
                "trades_per_day": 1.0,
                "proxy_win_rate": 0.69,
                "signal_similarity_report": similarity_report,
            },
            {
                "experiment_id": "exp_c",
                "module_id": "module_c",
                "strategy_name": "distinct_signal",
                "passed": True,
                "trades_per_day": 1.1,
                "proxy_win_rate": 0.6,
                "signal_similarity_report": similarity_report,
            },
        ]

        pool = build_target_frequency_pool(records)

        self.assertEqual(pool["status"], "target_met")
        self.assertEqual(
            [row["strategy_name"] for row in pool["selected"]],
            ["best_duplicate_signal", "distinct_signal"],
        )
        self.assertEqual(pool["diversity_report"]["selected_near_duplicate_pair_count"], 0)
        self.assertEqual(pool["candidate_quality_order"][0]["near_duplicate_experiment_ids"], ["exp_b"])

    def test_module_registry_promotes_and_audits_memory_summary(self) -> None:
        registry = build_module_registry(
            [
                {
                    "experiment_id": "exp_a",
                    "module_id": "opening_range_breakout",
                    "timeframe": "5m",
                    "trade_count": 140,
                    "expectancy": 8.0,
                    "passed": True,
                    "robustness_score": 0.7,
                    "rejection_reasons": [],
                    "parameter_stability_status": "stable",
                }
            ]
        )
        module = next(row for row in registry["modules"] if row["module_id"] == "opening_range_breakout")

        self.assertEqual(module["module_version"], "1.0.0")
        self.assertEqual(module["status"], "freeze_confirmed")
        self.assertTrue(module["promotion_gates"]["passed"])
        self.assertTrue(module_can_enter_runtime(module))
        self.assertEqual(module["audit_events"][0]["event_type"], "memory_summary_applied")
        self.assertTrue(module["next_retest_due"])

    def test_module_registry_retires_persistent_failures(self) -> None:
        records = [
            {
                "experiment_id": f"exp_{index}",
                "module_id": "trend_pullback",
                "timeframe": "5m",
                "trade_count": 10,
                "expectancy": -5.0,
                "passed": False,
                "robustness_score": None,
                "rejection_reasons": ["event_window_risk"],
                "parameter_stability_status": "unstable",
            }
            for index in range(3)
        ]

        module = next(row for row in build_module_registry(records)["modules"] if row["module_id"] == "trend_pullback")

        self.assertEqual(module["status"], "retired")
        self.assertIn("persistent_out_of_sample_failure", module["retirement_reasons"])
        self.assertIn("event_window_risk", module["retirement_reasons"])
        self.assertFalse(module_can_enter_runtime(module))

    def test_module_status_transition_rejects_invalid_jump(self) -> None:
        entry = {
            "module_id": "opening_range_breakout",
            "module_version": "1.0.0",
            "family": "opening_range_breakout",
            "status": "testing",
            "retirement_reasons": [],
            "audit_events": [],
        }

        candidate = transition_module_status(entry, "candidate", reason="passed_initial_gate")

        self.assertEqual(candidate["status"], "candidate")
        self.assertEqual(candidate["audit_events"][0]["details"]["from_status"], "testing")
        with self.assertRaisesRegex(ValueError, "Invalid module status transition"):
            transition_module_status(entry, "paper_shadow", reason="skip_required_gates")

    def test_module_memory_roundtrip_and_cli_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            memory_path = root / "exp_a" / "module_performance.jsonl"
            write_module_performance_memory(
                memory_path,
                [
                    {
                        "experiment_id": "exp_a",
                        "module_id": "trend_pullback",
                        "timeframe": "5m",
                        "trade_count": 80,
                        "expectancy": 3.0,
                        "passed": False,
                        "robustness_score": None,
                        "rejection_reasons": ["net_pnl_final_holdout"],
                    }
                ],
            )

            loaded = load_module_performance_memory([memory_path])
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "modules",
                        "summary",
                        "--experiments-root",
                        str(root),
                    ]
                )
            payload = json.loads(stdout.getvalue())

        self.assertEqual(len(loaded), 1)
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["module_count"], 1)
        self.assertEqual(payload["modules"][0]["module_id"], "trend_pullback")


if __name__ == "__main__":
    unittest.main()
