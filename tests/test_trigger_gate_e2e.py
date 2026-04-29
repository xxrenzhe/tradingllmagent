from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tlm.monitor import build_monitor_report
from tlm.modules import build_target_frequency_pool, write_module_performance_memory
from tlm.storage import bar_path
from tlm.tasks import create_task
from tlm.trigger_gate import load_trigger_gate_memory
from tlm.worker import run_task
from test_monitor import write_monitor_bars


class TriggerGateEndToEndTests(unittest.TestCase):
    def test_module_pool_monitor_worker_report_flow_has_no_live_commands(self) -> None:
        day = datetime(2026, 4, 27, 13, 30)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            output_dir = root / "trigger_gate"
            db_path = root / "tasks.sqlite3"
            memory_path = root / "experiments" / "exp_a" / "module_performance.jsonl"
            write_module_performance_memory(
                memory_path,
                [
                    {
                        "experiment_id": "exp_a",
                        "module_id": "module_a",
                        "strategy_name": "strategy_a",
                        "strategy_spec_hash": "hash_a",
                        "timeframe": "15m",
                        "passed": True,
                        "trades_per_day": 1.1,
                        "proxy_win_rate": 0.61,
                        "expectancy": 5.0,
                        "robustness_score": 0.7,
                    },
                    {
                        "experiment_id": "exp_b",
                        "module_id": "module_b",
                        "strategy_name": "strategy_b",
                        "strategy_spec_hash": "hash_b",
                        "timeframe": "15m",
                        "passed": True,
                        "trades_per_day": 1.0,
                        "proxy_win_rate": 0.58,
                        "expectancy": 4.0,
                        "robustness_score": 0.6,
                    },
                ],
            )
            pool = build_target_frequency_pool(
                [
                    {
                        "experiment_id": "exp_a",
                        "module_id": "module_a",
                        "strategy_name": "strategy_a",
                        "strategy_spec_hash": "hash_a",
                        "timeframe": "15m",
                        "passed": True,
                        "trades_per_day": 1.1,
                        "proxy_win_rate": 0.61,
                        "expectancy": 5.0,
                        "robustness_score": 0.7,
                    },
                    {
                        "experiment_id": "exp_b",
                        "module_id": "module_b",
                        "strategy_name": "strategy_b",
                        "strategy_spec_hash": "hash_b",
                        "timeframe": "15m",
                        "passed": True,
                        "trades_per_day": 1.0,
                        "proxy_win_rate": 0.58,
                        "expectancy": 4.0,
                        "robustness_score": 0.6,
                    },
                ]
            )
            write_monitor_bars(data_root, day)
            monitor_report = build_monitor_report(
                symbol="NQmain",
                timeframe="5m",
                bar_files=[bar_path(data_root, "NQmain", "5m", day.date())],
                target_frequency_pool=pool,
            )
            create_task(
                db_path,
                "trigger_gate.simulate",
                {
                    "target_frequency_pool": pool,
                    "from": "2026-04-26",
                    "to": "2026-04-27",
                    "output_dir": str(output_dir),
                    "enable_llm": True,
                    "daily_token_budget": 30_000,
                },
                task_id="trigger_gate_sim",
            )
            simulation = run_task(db_path, "trigger_gate_sim")
            create_task(
                db_path,
                "trigger_gate.report",
                {"output_dir": str(output_dir)},
                task_id="trigger_gate_report",
            )
            report = run_task(db_path, "trigger_gate_report")
            memory = load_trigger_gate_memory(output_dir)

        self.assertEqual(pool["status"], "target_met")
        self.assertTrue(monitor_report["trigger_gate"]["llm_trigger_required"])
        self.assertEqual(simulation["status"], "completed")
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["result"]["live_gateway_command_count"], 0)
        self.assertEqual(len(memory["evidence"]), simulation["result"]["trigger_count"])
        self.assertTrue(all(row["runtime_action"] in {"paper_intent", "observe_only"} for row in memory["decisions"]))


if __name__ == "__main__":
    unittest.main()
