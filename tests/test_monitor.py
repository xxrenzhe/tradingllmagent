from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

from tlm.cli import main
from tlm.monitor import build_market_snapshot, build_monitor_report, scan_key_levels
from tlm.storage import bar_path, write_bars_parquet
from tlm.tasks import create_task
from tlm.worker import run_task


def write_monitor_bars(data_root: Path, day: datetime) -> None:
    rows = []
    prices = [
        (100.0, 101.0, 99.0, 100.5),
        (100.5, 102.0, 100.0, 101.5),
        (101.5, 103.0, 101.0, 102.5),
        (102.5, 104.0, 102.0, 103.8),
    ]
    for index, (open_, high, low, close) in enumerate(prices):
        rows.append(
            (
                "NQmain",
                day + timedelta(minutes=5 * index),
                open_,
                high,
                low,
                close,
                close - 0.1,
                close + 0.1,
                10,
                1.0,
                1.0,
                0.2,
            )
        )
    write_bars_parquet(bar_path(data_root, "NQmain", "5m", day.date()), rows)


class RuntimeMonitorTests(unittest.TestCase):
    def test_snapshot_and_key_level_scanner_identify_strong_review(self) -> None:
        bars = [
            {"symbol": "NQmain", "timestamp": datetime(2026, 4, 27, 13, 30), "open": 100, "high": 101, "low": 99, "close": 100, "bid_close": 99.9, "ask_close": 100.1, "tick_count": 10, "avg_spread": 0.2},
            {"symbol": "NQmain", "timestamp": datetime(2026, 4, 27, 13, 35), "open": 100, "high": 104, "low": 100, "close": 103.8, "bid_close": 103.7, "ask_close": 103.9, "tick_count": 10, "avg_spread": 0.2},
        ]

        snapshot = build_market_snapshot("NQmain", "5m", bars, opening_range_bars=1)
        levels = scan_key_levels(snapshot, proximity_points=0.5)
        report = build_monitor_report(symbol="NQmain", timeframe="5m", bar_files=[])

        self.assertEqual(snapshot.last_price, 103.8)
        self.assertEqual(levels[0]["level"], "session_high")
        self.assertAlmostEqual(levels[0]["abs_distance_points"], 0.2)
        self.assertEqual(report["signal"]["bucket"], "none")

    def test_cli_monitor_once_writes_json_and_markdown(self) -> None:
        day = datetime(2026, 4, 27, 13, 30)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            output_dir = root / "reports"
            write_monitor_bars(data_root, day)

            with redirect_stdout(io.StringIO()) as stdout:
                code = main(
                    [
                        "--data-root",
                        str(data_root),
                        "monitor",
                        "once",
                        "--symbol",
                        "NQmain",
                        "--date",
                        day.date().isoformat(),
                        "--timeframe",
                        "5m",
                        "--output-dir",
                        str(output_dir),
                    ]
                )
            payload = json.loads(stdout.getvalue())
            json_path = Path(payload["outputs"]["json"])
            report_path = Path(payload["outputs"]["report"])
            json_exists = json_path.exists()
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(code, 0)
        self.assertEqual(payload["snapshot"]["bar_count"], 4)
        self.assertEqual(payload["signal"]["bucket"], "strong_review")
        self.assertTrue(json_exists)
        self.assertIn("Runtime Monitor", report_text)

    def test_worker_executes_monitor_once_task(self) -> None:
        day = datetime(2026, 4, 27, 13, 30)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            output_dir = root / "reports"
            db_path = root / "tasks.sqlite3"
            write_monitor_bars(data_root, day)
            create_task(
                db_path,
                "monitor.once",
                {
                    "symbol": "NQmain",
                    "date": day.date().isoformat(),
                    "timeframe": "5m",
                    "data_root": str(data_root),
                    "output_dir": str(output_dir),
                },
                task_id="monitor_once",
            )

            completed = run_task(db_path, "monitor_once")

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["signal"]["bucket"], "strong_review")


if __name__ == "__main__":
    unittest.main()
