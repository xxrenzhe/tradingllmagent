from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tlm.cli import main
from tlm.paper import export_ninjatrader_signals, load_backtest_result, replay_trades


def sample_result() -> dict:
    return {
        "strategy_name": "test_orb",
        "symbol": "NQmain",
        "trades": [
            {
                "symbol": "NQmain",
                "side": "long",
                "entry_time": "2025-03-19T13:33:00",
                "exit_time": "2025-03-19T13:34:00",
                "entry_price": 104.2,
                "exit_price": 107.2,
                "contracts": 1,
                "gross_pnl": 60.0,
                "fees": 5.0,
                "slippage_cost": 10.0,
                "net_pnl": 45.0,
                "entry_reason": "close_above_opening_range_high",
                "exit_reason": "take_profit",
            }
        ],
        "metrics": {},
    }


class PaperReplayTests(unittest.TestCase):
    def test_replay_trades_builds_paper_fills_and_equity(self) -> None:
        replay = replay_trades(sample_result()["trades"], starting_equity=100_000)

        self.assertEqual(replay.trade_count, 1)
        self.assertEqual(replay.realized_pnl, 45.0)
        self.assertEqual(replay.ending_equity, 100_045.0)
        self.assertEqual(replay.fills[0]["entry_reason"], "close_above_opening_range_high")

    def test_ninjatrader_csv_and_oif_exports_are_offline_signals(self) -> None:
        trades = sample_result()["trades"]
        csv_output = export_ninjatrader_signals(
            trades,
            export_format="csv",
            account="Sim101",
            instrument="NQ 06-26",
        )
        rows = list(csv.DictReader(csv_output.splitlines()))
        oif_output = export_ninjatrader_signals(
            trades,
            export_format="oif",
            account="Sim101",
            instrument="NQ 06-26",
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["action"], "BUY")
        self.assertEqual(rows[1]["action"], "SELL")
        self.assertIn("PLACE;Sim101;NQ 06-26;BUY;1;MARKET", oif_output)
        self.assertIn("PLACE;Sim101;NQ 06-26;SELL;1;MARKET", oif_output)

    def test_cli_paper_replay_and_nt_export_write_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            result_path = temp_path / "backtest.json"
            replay_path = temp_path / "paper.json"
            signal_path = temp_path / "signals.csv"
            result_path.write_text(json.dumps(sample_result()), encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                replay_code = main(
                    [
                        "paper",
                        "replay",
                        "--strategy-id",
                        str(result_path),
                        "--output",
                        str(replay_path),
                    ]
                )
                export_code = main(
                    [
                        "nt",
                        "export-signal",
                        "--strategy-id",
                        str(result_path),
                        "--format",
                        "csv",
                        "--account",
                        "Sim101",
                        "--instrument",
                        "NQ 06-26",
                        "--output",
                        str(signal_path),
                    ]
                )
            replay_payload = json.loads(replay_path.read_text(encoding="utf-8"))
            loaded = load_backtest_result(result_path)
            signal_output = signal_path.read_text(encoding="utf-8")

        self.assertEqual(replay_code, 0)
        self.assertEqual(export_code, 0)
        self.assertEqual(replay_payload["ending_equity"], 100_045.0)
        self.assertEqual(loaded["symbol"], "NQmain")
        self.assertIn("close_above_opening_range_high", signal_output)


if __name__ == "__main__":
    unittest.main()
