from __future__ import annotations

import lzma
import struct
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from tlm.bars import (
    build_minute_bars_from_parquet,
    build_minute_bars_from_ticks,
    build_timeframe_bars_from_1m_parquet,
)
from tlm.dukascopy import TICK_STRUCT, dukascopy_url, parse_bi5_ticks
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
            self.assertEqual(report.files, 2)
            self.assertEqual(report.missing_files, [str(missing_path)])
            self.assertEqual(report.zero_row_files, [str(empty_path)])
            self.assertEqual(report.duplicate_timestamps, 1)
            self.assertGreater(report.max_spread or 0, 0)
            self.assertGreater(report.large_spread_rows, 0)
            self.assertGreater(report.price_jump_rows, 0)

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


if __name__ == "__main__":
    unittest.main()
