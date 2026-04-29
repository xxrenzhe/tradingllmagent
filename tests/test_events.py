from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from tlm.cli import main
from tlm.events import context_for_timestamp, load_event_calendar, validate_event_calendar
from tlm.storage import bar_path, event_context_path, write_bars_parquet
from tlm.strategy import parse_strategy_spec
from test_strategy_backtest import base_spec


def write_calendar(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "calendar_id": "test_macro",
                "events": [
                    {
                        "event_id": "cpi_test",
                        "name": "CPI test",
                        "timestamp_utc": "2026-04-27T13:30:00Z",
                        "importance": "high",
                        "affected_symbols": ["NQmain"],
                        "pre_event_minutes": 15,
                        "release_window_minutes": 5,
                        "post_event_minutes": 20,
                        "policy_ref": "high_impact_macro_v1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


class MacroEventTests(unittest.TestCase):
    def test_event_calendar_validation_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            calendar_path = Path(temp_dir) / "macro_events.json"
            write_calendar(calendar_path)

            validation = validate_event_calendar(calendar_path)
            calendar = load_event_calendar(calendar_path)
            pre_context = context_for_timestamp(
                "NQmain",
                datetime(2026, 4, 27, 13, 20),
                calendar["events"],
            )
            normal_context = context_for_timestamp(
                "NQmain",
                datetime(2026, 4, 27, 12, 0),
                calendar["events"],
            )
            release_context = context_for_timestamp(
                "NQmain",
                datetime(2026, 4, 27, 13, 30),
                calendar["events"],
            )

        self.assertTrue(validation["valid"])
        self.assertEqual(validation["event_count"], 1)
        self.assertTrue(validation["event_calendar_hash"])
        self.assertEqual(pre_context.event_state, "pre_event")
        self.assertEqual(pre_context.active_event_ids, ["cpi_test"])
        self.assertEqual(pre_context.max_importance, "high")
        self.assertEqual(pre_context.minutes_to_event, 10)
        self.assertEqual(release_context.event_state, "release_window")
        self.assertEqual(normal_context.event_state, "normal")

    def test_event_actual_and_surprise_are_point_in_time_gated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            calendar_path = Path(temp_dir) / "macro_events.json"
            calendar_path.write_text(
                json.dumps(
                    {
                        "calendar_id": "test_macro",
                        "events": [
                            {
                                "event_id": "cpi_actual",
                                "name": "CPI actual",
                                "timestamp_utc": "2026-04-27T13:30:00Z",
                                "actual_available_at": "2026-04-27T13:31:00Z",
                                "importance": "high",
                                "affected_symbols": ["NQmain"],
                                "pre_event_minutes": 15,
                                "release_window_minutes": 5,
                                "post_event_minutes": 20,
                                "forecast": 3.1,
                                "previous": 3.0,
                                "actual": 3.4,
                                "surprise": 0.3,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            event = load_event_calendar(calendar_path)["events"][0]
            pre_context = context_for_timestamp("NQmain", datetime(2026, 4, 27, 13, 29), [event])
            post_context = context_for_timestamp("NQmain", datetime(2026, 4, 27, 13, 31), [event])

        pre_detail = pre_context.active_event_details[0]
        post_detail = post_context.active_event_details[0]
        self.assertEqual(pre_detail["forecast"], 3.1)
        self.assertEqual(pre_detail["previous"], 3.0)
        self.assertIsNone(pre_detail["actual"])
        self.assertIsNone(pre_detail["surprise"])
        self.assertFalse(pre_detail["actual_released"])
        self.assertEqual(post_detail["actual"], 3.4)
        self.assertEqual(post_detail["surprise"], 0.3)
        self.assertTrue(post_detail["actual_released"])

    def test_event_calendar_normalizes_aliases_and_calendar_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "configs"
            config_dir.mkdir()
            calendar_path = config_dir / "macro_events.yaml"
            calendar_path.write_text(
                json.dumps(
                    {
                        "calendar_id": "macro_events_v1",
                        "events": [
                            {
                                "event_id": "nfp_alias",
                                "name": "NFP alias",
                                "time": "2026-04-27T13:30:00Z",
                                "importance": "high",
                                "symbols": ["NQmain"],
                                "pre_window_minutes": 20,
                                "release_window_minutes": 10,
                                "post_window_minutes": 25,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()) as stdout:
                exit_code = main(
                    [
                        "--config-dir",
                        str(config_dir),
                        "events",
                        "validate",
                        "--calendar",
                        "macro_events_v1",
                    ]
                )
            payload = json.loads(stdout.getvalue())
            calendar = load_event_calendar(calendar_path)

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["valid"])
        self.assertEqual(calendar["events"][0].affected_symbols, ["NQmain"])
        self.assertEqual(calendar["events"][0].pre_event_minutes, 20)
        self.assertEqual(calendar["events"][0].release_window_minutes, 10)
        self.assertEqual(calendar["events"][0].post_event_minutes, 25)

    def test_cli_builds_event_context_from_bars(self) -> None:
        day = datetime(2026, 4, 27, 13, 10)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            calendar_path = root / "macro_events.json"
            data_root = root / "data"
            write_calendar(calendar_path)
            rows = []
            for index in range(4):
                timestamp = day + timedelta(minutes=5 * index)
                rows.append(
                    (
                        "NQmain",
                        timestamp,
                        100 + index,
                        101 + index,
                        99 + index,
                        100.5 + index,
                        100.4 + index,
                        100.6 + index,
                        10,
                        1.0,
                        1.0,
                        0.2,
                    )
                )
            write_bars_parquet(bar_path(data_root, "NQmain", "5m", day.date()), rows)

            with redirect_stdout(io.StringIO()) as stdout:
                code = main(
                    [
                        "--data-root",
                        str(data_root),
                        "events",
                        "build-context",
                        "--calendar",
                        str(calendar_path),
                        "--symbol",
                        "NQmain",
                        "--from",
                        day.date().isoformat(),
                        "--to",
                        day.date().isoformat(),
                        "--timeframe",
                        "5m",
                    ]
                )
            payload = json.loads(stdout.getvalue())
            output = event_context_path(data_root, "NQmain", day.date())
            output_exists = output.exists()
            con = duckdb.connect(":memory:")
            try:
                states = [
                    row[0]
                    for row in con.execute(
                        "SELECT event_state FROM read_parquet(?) ORDER BY timestamp",
                        [str(output)],
                    ).fetchall()
                ]
                event_details = [
                    row[0]
                    for row in con.execute(
                        "SELECT active_event_details FROM read_parquet(?) ORDER BY timestamp",
                        [str(output)],
                    ).fetchall()
                ]
            finally:
                con.close()

        self.assertEqual(code, 0)
        self.assertEqual(payload["rows"], 4)
        self.assertTrue(output_exists)
        self.assertEqual(states, ["normal", "pre_event", "pre_event", "pre_event"])
        self.assertEqual(json.loads(event_details[0]), [])

    def test_strategy_spec_accepts_event_policy(self) -> None:
        payload = base_spec()
        payload["event_policy"] = {
            "calendar_ref": "macro_events_v1",
            "policy_ref": "high_impact_macro_v1",
            "pre_event_blackout_minutes": 30,
            "post_event_blackout_minutes": 15,
        }

        spec = parse_strategy_spec(payload)

        self.assertEqual(spec.event_policy["calendar_ref"], "macro_events_v1")
        self.assertEqual(spec.event_policy["pre_event_blackout_minutes"], 30)


if __name__ == "__main__":
    unittest.main()
