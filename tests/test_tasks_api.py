from __future__ import annotations

import tempfile
import unittest
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from tlm.api import build_nt_export_signal_response, build_paper_replay_response
from tlm.dukascopy import Tick
from tlm.storage import normalized_tick_path, write_ticks_parquet
from tlm.tasks import (
    append_task_log,
    claim_queued_task,
    cancel_task,
    create_task,
    encode_sse_event,
    get_task,
    get_task_logs,
    list_tasks,
    next_queued_task,
    task_event_snapshot,
    task_snapshot_sse,
    update_task,
)
from tlm.worker import run_next_queued_task, run_task
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

    def test_task_sse_snapshot_includes_task_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.sqlite3"
            create_task(db_path, "research.run", {"symbol": "NQmain"}, task_id="task_sse")
            append_task_log(db_path, "task_sse", "worker started")
            update_task(db_path, "task_sse", "completed", result={"trials": 1})
            snapshot = task_event_snapshot(db_path, "task_sse")
            sse = task_snapshot_sse(db_path, "task_sse")
            encoded = encode_sse_event("task", snapshot["task"])

        self.assertEqual(snapshot["task"]["status"], "completed")
        self.assertTrue(any(log["message"] == "worker started" for log in snapshot["logs"]))
        self.assertIn("event: task", sse)
        self.assertIn("event: log", sse)
        self.assertIn('"status": "completed"', encoded)

    def test_run_task_executes_strategy_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.sqlite3"
            create_task(
                db_path,
                "strategy.validate",
                {"spec": "strategies/example_opening_range_breakout.yaml"},
                task_id="task_validate",
            )
            completed = run_task(db_path, "task_validate")
            logs = get_task_logs(db_path, "task_validate")

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["strategy_family"], "opening_range_breakout")
        self.assertTrue(any(log["message"] == "Completed strategy.validate" for log in logs))

    def test_run_next_queued_task_claims_and_executes_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.sqlite3"
            create_task(
                db_path,
                "strategy.validate",
                {"spec": "strategies/example_opening_range_breakout.yaml"},
                task_id="task_next",
            )
            queued = next_queued_task(db_path)
            claimed = claim_queued_task(db_path, "task_next")
            second_claim = claim_queued_task(db_path, "task_next")
            completed = run_task(db_path, "task_next")
            no_work = run_next_queued_task(db_path)

        self.assertEqual(queued["task_id"], "task_next")
        self.assertEqual(claimed["status"], "running")
        self.assertIsNone(second_claim)
        self.assertEqual(completed["status"], "completed")
        self.assertIsNone(no_work)

    def test_run_task_marks_unknown_task_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.sqlite3"
            create_task(db_path, "unknown.task", {}, task_id="task_unknown")
            failed = run_task(db_path, "task_unknown")
            logs = get_task_logs(db_path, "task_unknown")

        self.assertEqual(failed["status"], "failed")
        self.assertIn("Unsupported task_type", failed["error"])
        self.assertTrue(any(log["level"] == "error" for log in logs))

    def test_run_task_builds_bars_from_local_tick_parquet(self) -> None:
        day = datetime(2025, 3, 19, 13, tzinfo=UTC)
        ticks = [
            Tick(day + timedelta(milliseconds=100), 100.00, 100.20, 2.0, 1.0),
            Tick(day + timedelta(seconds=1), 100.10, 100.30, 4.0, 3.0),
            Tick(day + timedelta(minutes=1, milliseconds=100), 100.50, 100.80, 8.0, 7.0),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            db_path = root / "tasks.sqlite3"
            tick_path = normalized_tick_path(data_root, "NQmain", day.date())
            write_ticks_parquet(tick_path, "NQmain", ticks)
            create_task(
                db_path,
                "data.build_bars",
                {
                    "symbol": "NQmain",
                    "date_from": day.date().isoformat(),
                    "date_to": day.date().isoformat(),
                    "timeframe": "1m",
                    "data_root": str(data_root),
                },
                task_id="task_build_bars",
            )

            completed = run_task(db_path, "task_build_bars")
            output_path = Path(completed["result"]["outputs"][0]["path"])
            con = duckdb.connect(":memory:")
            try:
                rows = con.execute(
                    "SELECT count(*), min(open), max(close) FROM read_parquet(?)",
                    [str(output_path)],
                ).fetchone()
            finally:
                con.close()
            output_exists = output_path.exists()

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["rows"], 2)
        self.assertTrue(output_exists)
        self.assertEqual(rows[0], 2)
        self.assertAlmostEqual(rows[1], 100.1)
        self.assertAlmostEqual(rows[2], 100.65)

    def test_run_task_rejects_non_tick_data_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.sqlite3"
            create_task(
                db_path,
                "data.download",
                {
                    "symbol": "NQmain",
                    "from": "2025-03-19",
                    "to": "2025-03-19",
                    "granularity": "1m",
                },
                task_id="task_download_invalid",
            )
            failed = run_task(db_path, "task_download_invalid")

        self.assertEqual(failed["status"], "failed")
        self.assertIn("Only granularity=tick", failed["error"])


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
