from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests.test_smc_backtest import bar, long_setup_rows, smc_spec_payload, symbol_config
from tlm.config import CostModelConfig
from tlm.smc_validation import build_smc_validation_report, write_smc_validation_outputs
from tlm.storage import bar_path, write_bars_parquet
from tlm.strategy import parse_strategy_spec


def write_rows(data_root: Path, rows: list[dict]) -> None:
    parquet_rows = [
        (
            row["symbol"],
            row["timestamp"],
            row["open"],
            row["high"],
            row["low"],
            row["close"],
            row["bid_close"],
            row["ask_close"],
            row["tick_count"],
            1.0,
            1.0,
            row["avg_spread"],
        )
        for row in rows
    ]
    output = bar_path(data_root, "NQmain", "1m", rows[0]["timestamp"].date())
    write_bars_parquet(output, parquet_rows)


class SmcValidationReportTests(unittest.TestCase):
    def test_smc_validation_report_outputs_cost_stress_and_walk_forward(self) -> None:
        rows = long_setup_rows() + [
            bar(12, 109, 110, 107, 109),
            bar(13, 109, 115, 108, 114.75),
        ]
        cost_model = CostModelConfig(
            name="test_costs",
            tick_size=0.25,
            point_value=20,
            tick_value=5,
            slippage_ticks_per_side=1,
            round_trip_fees_usd=5,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_rows(root, rows)

            report = build_smc_validation_report(
                spec=parse_strategy_spec(smc_spec_payload()),
                symbol_config=symbol_config(),
                data_root=root,
                date_from=date(2025, 1, 1),
                date_to=date(2025, 1, 4),
                cost_model=cost_model,
                train_days=1,
                validation_days=1,
                test_days=1,
                step_days=1,
                embargo_days=0,
                final_holdout_days=1,
                min_trade_count=1,
                sample_trade_count=1,
            )
            outputs = write_smc_validation_outputs(report, root / "reports", "smc_validation_test")

            self.assertEqual(report["artifact"], "smc_lqem_ce_validation_report")
            self.assertEqual(report["full_history"]["metrics"]["trade_count"], 1)
            self.assertEqual(len(report["cost_stress"]), 3)
            self.assertTrue(report["objective_gates"])
            self.assertTrue(next(row for row in report["objective_gates"] if row["name"] == "minimum_trade_count")["passed"])
            self.assertTrue(next(row for row in report["objective_gates"] if row["name"] == "full_history_win_rate_ge_target")["passed"])
            self.assertFalse(next(row for row in report["objective_gates"] if row["name"] == "walk_forward_test_win_rate_ge_target")["passed"])
            self.assertFalse(next(row for row in report["objective_gates"] if row["name"] == "final_holdout_win_rate_ge_target")["passed"])
            self.assertEqual(report["walk_forward"]["status"], "ok")
            self.assertEqual(report["final_holdout"]["status"], "ok")
            self.assertEqual(report["full_history"]["audited_samples"]["top_winners"][0]["audit"]["direction"], "long")
            self.assertTrue(Path(outputs["markdown"]).exists())
            self.assertIn("## Objective Gates", Path(outputs["markdown"]).read_text(encoding="utf-8"))
            self.assertEqual(json.loads(Path(outputs["json"]).read_text())["strategy_family"], "smc_lqem_ce")


if __name__ == "__main__":
    unittest.main()
