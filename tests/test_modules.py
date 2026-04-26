from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tlm.cli import main
from tlm.modules import (
    load_module_performance_memory,
    strategy_module_catalog,
    summarize_module_performance,
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
