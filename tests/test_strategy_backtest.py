from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tlm.backtest import run_bar_backtest
from tlm.config import SymbolConfig
from tlm.storage import bar_path, write_bars_parquet
from tlm.strategy import StrategySpecError, load_strategy_spec, parse_strategy_spec


def base_spec() -> dict:
    return {
        "schema_version": 0,
        "name": "test_orb",
        "strategy_family": "opening_range_breakout",
        "market_hypothesis": "Opening range breakouts can persist during active intraday sessions.",
        "symbol": "NQmain",
        "timeframe": "1m",
        "direction": "long_short",
        "session": {"timezone": "UTC", "trade": "13:30-20:45", "flatten": "20:55"},
        "regime_filter": {},
        "indicators": {"opening_range": {"type": "opening_range", "minutes": 3}},
        "entry": {
            "long": {"all": [{"left": "close", "op": ">", "right": "opening_range.high"}]},
            "short": {"all": [{"left": "close", "op": "<", "right": "opening_range.low"}]},
        },
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
        default_trade_session="13:30-20:45",
        default_flatten_time="20:55",
    )


class StrategyValidationTests(unittest.TestCase):
    def test_valid_example_spec_loads(self) -> None:
        spec = load_strategy_spec(Path("strategies/example_opening_range_breakout.yaml"))
        self.assertEqual(spec.strategy_family, "opening_range_breakout")
        self.assertEqual(spec.symbol, "NQmain")

    def test_banned_family_rejected(self) -> None:
        payload = base_spec()
        payload["strategy_family"] = "martingale"
        with self.assertRaises(StrategySpecError):
            parse_strategy_spec(payload)

    def test_missing_market_hypothesis_rejected(self) -> None:
        payload = base_spec()
        del payload["market_hypothesis"]
        with self.assertRaises(StrategySpecError):
            parse_strategy_spec(payload)

    def test_future_reference_rejected(self) -> None:
        payload = base_spec()
        payload["entry"]["long"]["all"][0]["right"] = "future.close"
        with self.assertRaises(StrategySpecError):
            parse_strategy_spec(payload)


class BarBacktestTests(unittest.TestCase):
    def test_opening_range_breakout_produces_deterministic_trade(self) -> None:
        start = datetime(2025, 3, 19, 13, 30)
        rows = []
        prices = [
            (100.0, 100.5, 99.5, 100.0),
            (100.0, 100.4, 99.7, 100.1),
            (100.1, 100.6, 99.8, 100.2),
            (100.2, 104.4, 100.2, 104.0),
            (104.0, 107.2, 103.8, 106.8),
        ]
        for index, (open_, high, low, close) in enumerate(prices):
            timestamp = start + timedelta(minutes=index)
            rows.append(
                (
                    "NQmain",
                    timestamp,
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

        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            output = bar_path(data_root, "NQmain", "1m", start.date())
            write_bars_parquet(output, rows)

            result = run_bar_backtest(parse_strategy_spec(base_spec()), symbol_config(), [output])

        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.side, "long")
        self.assertEqual(trade.exit_reason, "take_profit")
        self.assertAlmostEqual(trade.gross_pnl, 60.0)
        self.assertAlmostEqual(trade.net_pnl, 45.0)
        self.assertEqual(result.metrics.trade_count, 1)
        self.assertAlmostEqual(result.metrics.net_pnl, 45.0)


if __name__ == "__main__":
    unittest.main()
