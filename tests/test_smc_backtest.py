from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tlm.backtest import run_bar_backtest
from tlm.config import SymbolConfig
from tlm.storage import bar_path, write_bars_parquet
from tlm.strategy import load_strategy_spec, parse_strategy_spec


def symbol_config() -> SymbolConfig:
    return SymbolConfig(
        alias="NQmain",
        provider="dukascopy",
        instrument="USATECHIDXUSD",
        description="proxy",
        timezone="UTC",
        price_scale=1000,
        tick_size=0.25,
        point_value=20,
        default_session_timezone="UTC",
        default_trade_session="09:30-16:00",
        default_flatten_time="16:00",
    )


def smc_spec_payload() -> dict:
    return {
        "schema_version": 0,
        "name": "test_smc_lqem_ce",
        "strategy_family": "smc_lqem_ce",
        "market_hypothesis": "Synthetic SMC LQ-EM CE fixtures should trade only after confirmed pivots, sweep, and ChoCh.",
        "symbol": "NQmain",
        "timeframe": "1m",
        "direction": "long_short",
        "session": {"timezone": "UTC", "trade": "09:30-16:00", "flatten": "16:00"},
        "regime_filter": {},
        "indicators": {"smc_lqem_ce": {"type": "smc_lqem_ce"}},
        "entry": {
            "long": {"all": [{"left": "smc_lqem_ce", "op": "==", "right": "long_signal"}]},
            "short": {"all": [{"left": "smc_lqem_ce", "op": "==", "right": "short_signal"}]},
        },
        "exit": {
            "stop_loss": {"type": "points", "value": 4},
            "take_profit": {"type": "points", "value": 8},
            "max_holding_minutes": 20,
        },
        "risk": {
            "position_sizing": {"type": "fixed_contracts", "contracts": 1},
            "max_position_contracts": 1,
            "max_trades_per_day": 2,
            "max_daily_loss_r": 3,
        },
        "anti_martingale_constraints": {
            "forbid_loss_doubling": True,
            "forbid_position_increase_when_unrealized_loss": True,
            "max_grid_levels": 0,
        },
        "parameters": {
            "tick_size": {"values": [0.25]},
            "htf_minutes": {"values": [1]},
            "htf_swing_left": {"values": [1]},
            "htf_swing_right": {"values": [1]},
            "ltf_swing_left": {"values": [1]},
            "ltf_swing_right": {"values": [1]},
            "break_buffer_ticks": {"values": [0]},
            "min_htf_range_ticks": {"values": [4]},
            "min_ob_ticks": {"values": [1]},
            "max_ob_ticks": {"values": [80]},
            "min_micro_ob_ticks": {"values": [1]},
            "max_micro_ob_ticks": {"values": [80]},
            "pbl_clearance_ticks": {"values": [0]},
            "sweep_buffer_ticks": {"values": [0]},
            "max_reclaim_bars": {"values": [3]},
            "stop_buffer_ticks": {"values": [1]},
            "min_stop_ticks": {"values": [1]},
            "max_stop_ticks": {"values": [80]},
            "min_reward_r": {"values": [2.0]},
            "default_take_profit_r": {"values": [2.0]},
            "pending_ttl_bars": {"values": [2]},
            "max_spread_ticks": {"values": [4.0]},
            "cooldown_bars_after_cancel": {"values": [1]},
            "cooldown_bars_after_exit": {"values": [1]},
        },
        "cost_model": "nq_conservative_v1",
    }


def bar(index: int, open_price: float, high: float, low: float, close: float) -> dict:
    return {
        "symbol": "NQmain",
        "timestamp": datetime(2025, 1, 2, 9, 30) + timedelta(minutes=index),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "bid_close": close - 0.1,
        "ask_close": close + 0.1,
        "tick_count": 100 + index,
        "avg_spread": 0.5,
    }


def long_setup_rows() -> list[dict]:
    return [
        bar(0, 101, 102, 100.5, 101),
        bar(1, 100.5, 101, 100, 100.25),
        bar(2, 103, 105, 101, 104),
        bar(3, 104, 104, 99, 103),
        bar(4, 103, 106, 102, 106),
        bar(5, 106, 108, 106, 107),
        bar(6, 107, 108, 105, 106),
        bar(7, 106, 109, 106, 108),
        bar(8, 108, 108, 103.5, 105.5),
        bar(9, 105.5, 109, 105, 107),
        bar(10, 107, 107, 104.5, 105),
        bar(11, 105, 110, 105, 110),
    ]


def run_result(rows: list[dict]):
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
    with tempfile.TemporaryDirectory() as temp_dir:
        output = bar_path(Path(temp_dir), "NQmain", "1m", rows[0]["timestamp"].date())
        write_bars_parquet(output, parquet_rows)
        return run_bar_backtest(parse_strategy_spec(smc_spec_payload()), symbol_config(), [output])


def run_result_with_payload(rows: list[dict], payload: dict):
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
    with tempfile.TemporaryDirectory() as temp_dir:
        output = bar_path(Path(temp_dir), "NQmain", "1m", rows[0]["timestamp"].date())
        write_bars_parquet(output, parquet_rows)
        return run_bar_backtest(parse_strategy_spec(payload), symbol_config(), [output])


class SmcBacktestTests(unittest.TestCase):
    def test_smc_strategy_spec_file_loads(self) -> None:
        spec = load_strategy_spec(Path("strategies/nq_smc_lqem_ce_v1.yaml"))

        self.assertEqual(spec.strategy_family, "smc_lqem_ce")
        self.assertEqual(spec.symbol, "NQ_CME")

    def test_smc_lqem_ce_backtest_produces_limit_trade(self) -> None:
        rows = long_setup_rows() + [
            bar(12, 109, 110, 107, 109),
            bar(13, 109, 115, 108, 114.75),
        ]

        result = run_result(rows)

        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.side, "long")
        self.assertEqual(trade.entry_reason, "smc_lqem_ce")
        self.assertEqual(trade.exit_reason, "take_profit")
        self.assertEqual(trade.entry_time, rows[12]["timestamp"])
        self.assertEqual(trade.exit_time, rows[13]["timestamp"])
        self.assertAlmostEqual(trade.entry_price, 107)
        self.assertAlmostEqual(trade.exit_price, 114.5)
        self.assertAlmostEqual(trade.gross_pnl, 150.0)
        self.assertAlmostEqual(trade.net_pnl, 135.0)
        self.assertIn("smc_signal_audit", trade.feature_values_at_entry)

    def test_smc_lqem_ce_backtest_waits_for_right_side_confirmation(self) -> None:
        result = run_result(long_setup_rows()[:-1])

        self.assertEqual(result.trades, [])
        self.assertEqual(result.metrics.trade_count, 0)

    def test_smc_session_timezone_is_applied_to_utc_bars(self) -> None:
        payload = smc_spec_payload()
        payload["session"] = {"timezone": "America/New_York", "trade": "09:30-16:00", "flatten": "16:00"}
        rows = [
            {**row, "timestamp": row["timestamp"] + timedelta(hours=5)}
            for row in long_setup_rows()
            + [
                bar(12, 109, 110, 107, 109),
                bar(13, 109, 115, 108, 114.75),
            ]
        ]

        result = run_result_with_payload(rows, payload)

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].entry_time, datetime(2025, 1, 2, 14, 42))


if __name__ == "__main__":
    unittest.main()
