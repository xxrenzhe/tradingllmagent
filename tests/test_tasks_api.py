from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from tlm.api import build_nt_export_signal_response, build_paper_replay_response
from tlm.tasks import (
    append_task_log,
    cancel_task,
    create_task,
    get_task,
    get_task_logs,
    list_tasks,
    update_task,
)
from test_paper_nt import sample_result


class TaskStoreTests(unittest.TestCase):
    def test_task_lifecycle_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.sqlite3"
            task = create_task(
                db_path,
                "research.run",
                {"symbol": "NQmain", "max_trials": 2},
                task_id="task_1",
            )
            append_task_log(db_path, "task_1", "queued for worker")
            running = update_task(db_path, "task_1", "running")
            completed = update_task(db_path, "task_1", "completed", result={"trials": 2})
            logs = get_task_logs(db_path, "task_1")
            tasks = list_tasks(db_path)

        self.assertEqual(task["status"], "queued")
        self.assertEqual(running["status"], "running")
        self.assertEqual(completed["result"], {"trials": 2})
        self.assertEqual(len(tasks), 1)
        self.assertTrue(any(log["message"] == "queued for worker" for log in logs))

    def test_cancel_is_idempotent_for_terminal_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.sqlite3"
            create_task(db_path, "data.download", {"symbol": "NQmain"}, task_id="task_2")
            cancelled = cancel_task(db_path, "task_2")
            cancelled_again = cancel_task(db_path, "task_2")

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled_again["status"], "cancelled")

    def test_get_task_rejects_unknown_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(KeyError):
                get_task(Path(temp_dir) / "tasks.sqlite3", "missing")


class APIImportTests(unittest.TestCase):
    def test_api_module_imports_without_fastapi_installed(self) -> None:
        import tlm.api as api

        self.assertTrue(hasattr(api, "create_app"))
        self.assertTrue(hasattr(api, "app"))

    def test_paper_api_helpers_replay_and_export_without_fastapi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "backtest.json"
            result_path.write_text(json.dumps(sample_result()), encoding="utf-8")
            replay = build_paper_replay_response({"strategy_id": str(result_path)})
            exported = build_nt_export_signal_response(
                {
                    "strategy_id": str(result_path),
                    "format": "oif",
                    "account": "Sim101",
                    "instrument": "NQ 06-26",
                }
            )

        self.assertEqual(replay["ending_equity"], 100_045.0)
        self.assertEqual(exported["format"], "oif")
        self.assertEqual(exported["line_count"], 2)
        self.assertIn("PLACE;Sim101;NQ 06-26;BUY;1;MARKET", exported["content"])


if __name__ == "__main__":
    unittest.main()
