from __future__ import annotations

import json
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
from tlm.firstrate import parse_firstrate_csv
from tlm.quality import build_quality_report
from tlm.quotes import parse_databento_quote_csv
from tlm.storage import (
    bar_path,
    normalized_quote_path,
    normalized_tick_path,
    write_bars_parquet,
    write_ticks_parquet,
)


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
    def test_cli_backtest_bar_honors_symbol_override(self) -> None:
        spec_payload = {
            "schema_version": 0,
            "name": "override_orb",
            "strategy_family": "opening_range_breakout",
            "market_hypothesis": "Opening range breakouts can persist during active intraday sessions.",
            "symbol": "NQmain",
            "timeframe": "1m",
            "direction": "long",
            "session": {"timezone": "UTC", "trade": "13:30-20:45", "flatten": "20:55"},
            "indicators": {"opening_range": {"type": "opening_range", "minutes": 3}},
            "entry": {"long": {"all": [{"left": "close", "op": ">", "right": "opening_range.high"}]}},
            "exit": {
                "stop_loss": {"type": "points", "value": 2},
                "take_profit": {"type": "points", "value": 3},
                "max_holding_minutes": 10,
            },
            "risk": {
                "position_sizing": {"type": "fixed_contracts", "contracts": 1},
                "max_position_contracts": 1,
                "max_trades_per_day": 5,
                "max_daily_loss_r": 3,
            },
            "anti_martingale_constraints": {
                "forbid_loss_doubling": True,
                "forbid_position_increase_when_unrealized_loss": True,
                "max_grid_levels": 0,
            },
            "parameters": {"opening_range_minutes": {"values": [3]}},
            "cost_model": "nq_conservative_v1",
        }
        day = datetime(2025, 3, 19).date()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            spec_path = root / "strategy.json"
            spec_path.write_text(json.dumps(spec_payload), encoding="utf-8")
            write_bars_parquet(
                bar_path(data_root, "NQ_1M", "1m", day),
                [
                    ("NQ_1M", datetime(2025, 3, 19, 13, 30), 100.0, 100.5, 99.5, 100.0, 99.9, 100.1, 10, 1.0, 1.0, 0.2),
                    ("NQ_1M", datetime(2025, 3, 19, 13, 31), 100.0, 100.4, 99.7, 100.1, 100.0, 100.2, 10, 1.0, 1.0, 0.2),
                    ("NQ_1M", datetime(2025, 3, 19, 13, 32), 100.1, 100.6, 99.8, 100.2, 100.1, 100.3, 10, 1.0, 1.0, 0.2),
                    ("NQ_1M", datetime(2025, 3, 19, 13, 33), 100.2, 104.4, 100.2, 104.0, 103.9, 104.1, 10, 1.0, 1.0, 0.2),
                    ("NQ_1M", datetime(2025, 3, 19, 13, 34), 104.0, 107.2, 103.8, 106.8, 106.7, 106.9, 10, 1.0, 1.0, 0.2),
                ],
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        "--data-root",
                        str(data_root),
                        "backtest",
                        "bar",
                        "--spec",
                        str(spec_path),
                        "--symbol",
                        "NQ_1M",
                        "--from",
                        day.isoformat(),
                        "--to",
                        day.isoformat(),
                    ]
                )
            payload = json.loads(stdout.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["symbol"], "NQ_1M")
        self.assertEqual(len(payload["trades"]), 1)

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

    def test_parse_firstrate_csv_header_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nq.csv"
            path.write_text(
                "timestamp,open,high,low,close,volume\n"
                "2025-03-19 13:30:00,20000.00,20002.25,19999.75,20001.50,123\n",
                encoding="utf-8",
            )

            bars = parse_firstrate_csv(path)

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].timestamp, datetime(2025, 3, 19, 13, 30))
        self.assertAlmostEqual(bars[0].close, 20001.5)
        self.assertAlmostEqual(bars[0].volume, 123)

    def test_cli_import_firstrate_writes_daily_bar_partitions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir) / "data"
            csv_path = Path(temp_dir) / "nq.csv"
            csv_path.write_text(
                "date,time,open,high,low,close,volume\n"
                "2025-03-19,13:30:00,20000.00,20002.25,19999.75,20001.50,123\n"
                "2025-03-20,13:30:00,20100.00,20102.25,20099.75,20101.50,456\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "--data-root",
                        str(data_root),
                        "data",
                        "import-firstrate",
                        "--symbol",
                        "NQ_1M",
                        "--input",
                        str(csv_path),
                    ]
                )

            first_day_path = data_root / "bars" / "1m" / "NQ_1M" / "date=2025-03-19" / "part-000.parquet"
            second_day_path = data_root / "bars" / "1m" / "NQ_1M" / "date=2025-03-20" / "part-000.parquet"
            con = duckdb.connect(":memory:")
            try:
                rows = con.execute(
                    "SELECT symbol, open, close, tick_count, avg_spread FROM read_parquet(?)",
                    [str(first_day_path)],
                ).fetchall()
            finally:
                con.close()

            self.assertEqual(code, 0)
            self.assertTrue(first_day_path.exists())
            self.assertTrue(second_day_path.exists())
            self.assertEqual(rows, [("NQ_1M", 20000.0, 20001.5, 123, None)])
            self.assertIn("written", output.getvalue())

    def test_cli_bar_quality_reports_bar_anomalies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            day = datetime(2025, 3, 19).date()
            output_path = bar_path(data_root, "NQ_1M", "1m", day)
            write_bars_parquet(
                output_path,
                [
                    ("NQ_1M", datetime(2025, 3, 19, 13, 30), 100.0, 101.0, 99.0, 100.5, 100.5, 100.5, 10, 10.0, 10.0, None),
                    ("NQ_1M", datetime(2025, 3, 19, 13, 30), 100.0, 101.0, 99.0, 100.5, 100.5, 100.5, 10, 10.0, 10.0, None),
                    ("NQ_1M", datetime(2025, 3, 19, 13, 35), 101.0, 100.0, 99.0, 150.0, 150.0, 150.0, 0, 0.0, 0.0, -0.25),
                ],
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        "--data-root",
                        str(data_root),
                        "data",
                        "bar-quality",
                        "--symbol",
                        "NQ_1M",
                        "--from",
                        "2025-03-19",
                        "--to",
                        "2025-03-20",
                        "--max-normal-price-jump",
                        "20",
                    ]
                )

            report_path = data_root / "quality" / "NQ_1M" / "2025-03-19_2025-03-20_1m_bars.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "gaps_or_anomalies")
        self.assertIn("missing_partitions", report["quality_flags"])
        self.assertIn("duplicate_timestamps", report["quality_flags"])
        self.assertIn("intraday_gaps", report["quality_flags"])
        self.assertIn("invalid_ohlc", report["quality_flags"])
        self.assertIn("non_positive_tick_count", report["quality_flags"])
        self.assertIn("negative_spread", report["quality_flags"])
        self.assertIn("price_jumps", report["quality_flags"])
        self.assertIn("data_version_hash", report)
        self.assertIn(str(report_path), stdout.getvalue())

    def test_cli_split_manifest_writes_holdout_isolation_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            output = data_root / "splits.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        "--data-root",
                        str(data_root),
                        "data",
                        "split-manifest",
                        "--symbol",
                        "NQ_1M",
                        "--from",
                        "2020-01-01",
                        "--to",
                        "2020-02-20",
                        "--train-days",
                        "5",
                        "--validation-days",
                        "5",
                        "--test-days",
                        "5",
                        "--step-days",
                        "5",
                        "--embargo-days",
                        "1",
                        "--final-holdout-days",
                        "5",
                        "--output",
                        str(output),
                    ]
                )

            manifest = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(manifest["artifact"], "dataset_split_manifest")
        self.assertEqual(manifest["symbol"], "NQ_1M")
        self.assertFalse(manifest["final_holdout_policy"]["llm_feedback_includes_final_holdout"])
        self.assertIn("final_holdout", manifest["final_holdout_policy"]["llm_hidden_splits"])
        self.assertGreaterEqual(len(manifest["plan"]["folds"]), 1)
        self.assertTrue(manifest["data_version_hash"])
        self.assertIn(str(output), stdout.getvalue())

    def test_parse_databento_quote_csv_accepts_tbbo_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tbbo.csv"
            path.write_text(
                "ts_event,bid_px_00,ask_px_00,bid_sz_00,ask_sz_00\n"
                "2025-03-19T13:30:00.000000000Z,100.00,100.25,7,9\n",
                encoding="utf-8",
            )

            quotes = parse_databento_quote_csv(path)

        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0].timestamp, datetime(2025, 3, 19, 13, 30))
        self.assertAlmostEqual(quotes[0].spread, 0.25)
        self.assertAlmostEqual(quotes[0].bid_size, 7)

    def test_cli_databento_quote_import_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            quotes_csv = root / "tbbo.csv"
            quotes_csv.write_text(
                "ts_event,bid_px_00,ask_px_00,bid_sz_00,ask_sz_00\n"
                "2025-03-19T13:30:00Z,100.00,100.25,7,9\n"
                "2025-03-19T13:35:00Z,101.00,101.25,8,10\n",
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()):
                import_code = main(
                    [
                        "--data-root",
                        str(data_root),
                        "data",
                        "import-databento-quotes",
                        "--symbol",
                        "NQ_CME",
                        "--input",
                        str(quotes_csv),
                    ]
                )

            quote_path = normalized_quote_path(data_root, "NQ_CME", datetime(2025, 3, 19).date())
            result_path = root / "backtest.json"
            result_path.write_text(
                json.dumps(
                    {
                        "trades": [
                            {
                                "symbol": "NQ_CME",
                                "side": "long",
                                "entry_time": "2025-03-19T13:30:00",
                                "exit_time": "2025-03-19T13:35:00",
                                "entry_price": 100.0,
                                "exit_price": 101.0,
                                "contracts": 1,
                                "gross_pnl": 20.0,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            report_path = root / "quote_replay.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                replay_code = main(
                    [
                        "--data-root",
                        str(data_root),
                        "data",
                        "quote-replay",
                        "--symbol",
                        "NQ_CME",
                        "--from",
                        "2025-03-19",
                        "--to",
                        "2025-03-19",
                        "--backtest-result",
                        str(result_path),
                        "--output",
                        str(report_path),
                    ]
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertEqual(import_code, 0)
            self.assertTrue(quote_path.exists())
            self.assertEqual(replay_code, 0)
            self.assertEqual(report["artifact"], "quote_execution_validation")
            self.assertEqual(report["trade_count"], 1)
            self.assertEqual(report["validated_trade_count"], 1)
            self.assertAlmostEqual(report["validations"][0]["quote_gross_pnl"], 15.0)
            self.assertAlmostEqual(report["avg_bid_ask_cost_usd"], 10.0)
            self.assertIn("quote_execution_validation", stdout.getvalue())

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
