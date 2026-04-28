from __future__ import annotations

import lzma
import io
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import duckdb

from tlm.bars import (
    build_minute_bars_from_parquet,
    build_minute_bars_from_ticks,
    build_timeframe_bars_from_1m_parquet,
)
from tlm.cli import main
from tlm.config import get_symbol
from tlm.dukascopy import (
    DownloadResult,
    TICK_STRUCT,
    download_hour,
    dukascopy_url,
    parse_bi5_ticks,
    raw_tick_path,
)
from tlm.quality import build_quality_report
from tlm.storage import normalized_tick_path, write_ticks_parquet


def make_bi5(records: list[tuple[int, int, int, float, float]]) -> bytes:
    payload = b"".join(TICK_STRUCT.pack(*record) for record in records)
    return lzma.compress(payload)


class DukascopyParsingTests(unittest.TestCase):
    def test_url_uses_zero_based_month(self) -> None:
        hour = datetime(2025, 3, 19, 13, tzinfo=UTC)
        self.assertEqual(
            dukascopy_url("USATECHIDXUSD", hour),
            "https://datafeed.dukascopy.com/datafeed/USATECHIDXUSD/2025/02/19/13h_ticks.bi5",
        )

    def test_parse_bi5_ticks_scales_prices_and_timestamps(self) -> None:
        hour = datetime(2025, 3, 19, 13, tzinfo=UTC)
        payload = make_bi5(
            [
                (100, 19551625, 19548125, 9.0, 12.0),
                (60_100, 19552625, 19550125, 10.0, 13.0),
            ]
        )

        ticks = parse_bi5_ticks(payload, hour, price_scale=1000)

        self.assertEqual(len(ticks), 2)
        self.assertEqual(ticks[0].timestamp, hour + timedelta(milliseconds=100))
        self.assertAlmostEqual(ticks[0].ask, 19551.625)
        self.assertAlmostEqual(ticks[0].bid, 19548.125)
        self.assertAlmostEqual(ticks[0].spread, 3.5)
        self.assertAlmostEqual(ticks[1].mid, (19552.625 + 19550.125) / 2)

    def test_parse_rejects_invalid_payload_size(self) -> None:
        payload = lzma.compress(struct.pack(">I", 1))
        with self.assertRaises(ValueError):
            parse_bi5_ticks(payload, datetime(2025, 1, 1, tzinfo=UTC), price_scale=1000)

    def test_download_hour_reuses_zero_byte_cached_file(self) -> None:
        symbol = get_symbol("NQmain")
        hour = datetime(2025, 3, 22, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            cached_path = raw_tick_path(data_root, symbol.instrument, hour)
            cached_path.parent.mkdir(parents=True)
            cached_path.write_bytes(b"")

            with patch("tlm.dukascopy.urlopen") as urlopen:
                result = download_hour(symbol, hour, data_root)

        urlopen.assert_not_called()
        self.assertEqual(result.status, "cached")
        self.assertEqual(result.bytes_written, 0)

    def test_download_hour_times_out_when_lock_is_held(self) -> None:
        symbol = get_symbol("NQmain")
        hour = datetime(2025, 3, 22, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            target = raw_tick_path(data_root, symbol.instrument, hour)
            target.parent.mkdir(parents=True)
            target.with_suffix(f"{target.suffix}.lock").write_text("pid=other\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "Timed out waiting for download lock"):
                download_hour(symbol, hour, data_root, lock_wait_seconds=0)


class BarAndQualityTests(unittest.TestCase):
    def test_build_minute_bars_from_ticks(self) -> None:
        hour = datetime(2025, 3, 19, 13, tzinfo=UTC)
        ticks = parse_bi5_ticks(
            make_bi5(
                [
                    (100, 100_200, 100_000, 1.0, 2.0),
                    (1_000, 100_400, 100_100, 3.0, 4.0),
                    (60_100, 100_800, 100_500, 5.0, 6.0),
                ]
            ),
            hour,
            price_scale=1000,
        )

        bars = build_minute_bars_from_ticks("NQmain", ticks)

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0][0], "NQmain")
        self.assertEqual(bars[0][8], 2)
        self.assertAlmostEqual(bars[0][2], 100.1)
        self.assertAlmostEqual(bars[0][5], 100.25)
        self.assertAlmostEqual(bars[1][5], 100.65)

    def test_write_ticks_build_bars_and_quality_report(self) -> None:
        hour = datetime(2025, 3, 19, 13, tzinfo=UTC)
        ticks = parse_bi5_ticks(
            make_bi5(
                [
                    (100, 100_200, 100_000, 1.0, 2.0),
                    (1_000, 100_400, 100_100, 3.0, 4.0),
                    (1_000, 100_500, 100_200, 5.0, 6.0),
                    (60_100, 100_800, 100_500, 7.0, 8.0),
                ]
            ),
            hour,
            price_scale=1000,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            tick_path = normalized_tick_path(data_root, "NQmain", hour.date())
            write_ticks_parquet(tick_path, "NQmain", ticks)
            self.assertTrue(tick_path.exists())
            empty_path = normalized_tick_path(data_root, "NQmain", (hour + timedelta(days=1)).date())
            missing_path = normalized_tick_path(data_root, "NQmain", (hour + timedelta(days=2)).date())
            write_ticks_parquet(empty_path, "NQmain", [])

            bar_path = data_root / "bars" / "1m" / "NQmain" / "date=2025-03-19" / "part-000.parquet"
            bar_count = build_minute_bars_from_parquet([tick_path], bar_path)
            self.assertEqual(bar_count, 2)

            con = duckdb.connect(":memory:")
            try:
                rows = con.execute("SELECT count(*) FROM read_parquet(?)", [str(bar_path)]).fetchone()[0]
            finally:
                con.close()
            self.assertEqual(rows, 2)

            report = build_quality_report(
                "NQmain",
                [tick_path, empty_path, missing_path],
                max_normal_spread=0.25,
                max_normal_price_jump=0.2,
            )
            self.assertEqual(report.rows, 4)
            self.assertTrue(report.data_version_hash)
            self.assertIn("data_version_hash", report.to_dict())
            self.assertEqual(report.expected_files, 3)
            self.assertEqual(report.files, 2)
            self.assertAlmostEqual(report.coverage_ratio, 2 / 3)
            self.assertEqual(report.status, "gaps_or_anomalies")
            self.assertIn("missing_partitions", report.quality_flags)
            self.assertIn("empty_partitions", report.quality_flags)
            self.assertIn("large_spread", report.quality_flags)
            self.assertIn("price_jumps", report.quality_flags)
            self.assertEqual(report.missing_files, [str(missing_path)])
            self.assertEqual(report.zero_row_files, [str(empty_path)])
            self.assertEqual(report.duplicate_timestamps, 1)
            self.assertGreater(report.max_spread or 0, 0)
            self.assertGreater(report.large_spread_rows, 0)
            self.assertGreater(report.price_jump_rows, 0)

    def test_data_discover_filters_provider(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["data", "discover", "--provider", "dukascopy", "--query", "NQmain"])
        self.assertEqual(code, 0)
        self.assertIn("NQmain\tdukascopy", output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["data", "discover", "--provider", "twelvedata", "--query", "NQmain"])
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "")

    def test_build_higher_timeframe_bars_from_1m_bars(self) -> None:
        hour = datetime(2025, 3, 19, 13, tzinfo=UTC)
        ticks = parse_bi5_ticks(
            make_bi5(
                [
                    (100, 100_200, 100_000, 1.0, 2.0),
                    (60_100, 100_800, 100_500, 3.0, 4.0),
                    (300_100, 101_400, 101_000, 5.0, 6.0),
                ]
            ),
            hour,
            price_scale=1000,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            tick_path = normalized_tick_path(data_root, "NQmain", hour.date())
            write_ticks_parquet(tick_path, "NQmain", ticks)
            one_minute_path = data_root / "bars" / "1m" / "NQmain" / "date=2025-03-19" / "part-000.parquet"
            five_minute_path = data_root / "bars" / "5m" / "NQmain" / "date=2025-03-19" / "part-000.parquet"
            build_minute_bars_from_parquet([tick_path], one_minute_path)
            bar_count = build_timeframe_bars_from_1m_parquet(
                [one_minute_path],
                five_minute_path,
                "5m",
            )

            con = duckdb.connect(":memory:")
            try:
                rows = con.execute(
                    "SELECT open, high, low, close, tick_count FROM read_parquet(?) ORDER BY timestamp",
                    [str(five_minute_path)],
                ).fetchall()
            finally:
                con.close()

        self.assertEqual(bar_count, 2)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0][0], 100.1)
        self.assertAlmostEqual(rows[0][1], 100.65)
        self.assertAlmostEqual(rows[0][2], 100.1)
        self.assertAlmostEqual(rows[0][3], 100.65)
        self.assertEqual(rows[0][4], 2)
        self.assertAlmostEqual(rows[1][3], 101.2)

    def test_cli_build_bars_reports_data_version_hash(self) -> None:
        hour = datetime(2025, 3, 19, 13, tzinfo=UTC)
        ticks = parse_bi5_ticks(
            make_bi5(
                [
                    (100, 100_200, 100_000, 1.0, 2.0),
                    (60_100, 100_800, 100_500, 3.0, 4.0),
                ]
            ),
            hour,
            price_scale=1000,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            tick_path = normalized_tick_path(data_root, "NQmain", hour.date())
            write_ticks_parquet(tick_path, "NQmain", ticks)
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "--data-root",
                        str(data_root),
                        "data",
                        "build-bars",
                        "--symbol",
                        "NQmain",
                        "--from",
                        "2025-03-19",
                        "--to",
                        "2025-03-19",
                        "--timeframe",
                        "1m",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertIn("data_version_hash=", output.getvalue())

    def test_cli_download_passes_hour_retry_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            calls = []

            def fake_download(symbol, hour, target_root, retries=3, timeout_seconds=30):
                calls.append((symbol.instrument, hour, target_root, retries, timeout_seconds))
                return DownloadResult(
                    url=f"https://example.test/{hour.hour:02d}",
                    path=data_root / "raw" / f"{hour.hour:02d}.bi5",
                    status="empty_hour",
                    bytes_written=0,
                )

            output = io.StringIO()
            with patch("tlm.cli.download_hour", side_effect=fake_download):
                with redirect_stdout(output):
                    code = main(
                        [
                            "--data-root",
                            str(data_root),
                            "data",
                            "download",
                            "--symbol",
                            "NQmain",
                            "--from",
                            "2025-03-19",
                            "--to",
                            "2025-03-19",
                            "--hour-retries",
                            "1",
                            "--hour-timeout-seconds",
                            "7",
                        ]
                    )

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 24)
        self.assertTrue(all(call[0] == "USATECHIDXUSD" for call in calls))
        self.assertTrue(all(call[2] == data_root for call in calls))
        self.assertTrue(all(call[3] == 1 for call in calls))
        self.assertTrue(all(call[4] == 7 for call in calls))

    def test_cli_download_skips_existing_normalized_day(self) -> None:
        hour = datetime(2025, 3, 19, 13, tzinfo=UTC)
        ticks = parse_bi5_ticks(make_bi5([(100, 100_200, 100_000, 1.0, 2.0)]), hour, 1000)
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            tick_path = normalized_tick_path(data_root, "NQmain", hour.date())
            write_ticks_parquet(tick_path, "NQmain", ticks)

            output = io.StringIO()
            with patch("tlm.cli.download_hour") as download:
                with redirect_stdout(output):
                    code = main(
                        [
                            "--data-root",
                            str(data_root),
                            "data",
                            "download",
                            "--symbol",
                            "NQmain",
                            "--from",
                            "2025-03-19",
                            "--to",
                            "2025-03-19",
                        ]
                    )

        self.assertEqual(code, 0)
        download.assert_not_called()
        self.assertIn("skipped_existing_day", output.getvalue())

    def test_cli_download_continues_after_hour_failure_before_failing_day(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            calls = []

            def fake_download(symbol, hour, target_root, retries=3, timeout_seconds=30):
                calls.append(hour.hour)
                if hour.hour == 3:
                    raise RuntimeError("temporary provider failure")
                return DownloadResult(
                    url=f"https://example.test/{hour.hour:02d}",
                    path=data_root / "raw" / f"{hour.hour:02d}.bi5",
                    status="empty_hour",
                    bytes_written=0,
                )

            output = io.StringIO()
            with patch("tlm.cli.download_hour", side_effect=fake_download):
                with self.assertRaises(RuntimeError):
                    with redirect_stdout(output):
                        main(
                            [
                                "--data-root",
                                str(data_root),
                                "data",
                                "download",
                                "--symbol",
                                "NQmain",
                                "--from",
                                "2025-03-19",
                                "--to",
                                "2025-03-19",
                                "--hour-retries",
                                "0",
                                "--hour-timeout-seconds",
                                "7",
                            ]
                        )

        self.assertEqual(calls, list(range(24)))
        self.assertIn("2025-03-19T03:00:00+00:00\tfailed\t0\ttemporary provider failure", output.getvalue())
        self.assertFalse(normalized_tick_path(data_root, "NQmain", datetime(2025, 3, 19).date()).exists())


if __name__ == "__main__":
    unittest.main()
