from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tlm.backtest import run_bar_backtest, run_tick_backtest
from tlm.config import CostModelConfig, SymbolConfig, get_cost_model
from tlm.dukascopy import Tick
from tlm.storage import bar_path, normalized_tick_path, write_bars_parquet, write_ticks_parquet
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

    def test_cost_model_loads_from_config(self) -> None:
        cost_model = get_cost_model("nq_conservative_v1", Path("configs"))

        self.assertEqual(cost_model.name, "nq_conservative_v1")
        self.assertAlmostEqual(cost_model.tick_size, 0.25)
        self.assertAlmostEqual(cost_model.point_value, 20)

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


class TickReplayBacktestTests(unittest.TestCase):
    def test_opening_range_breakout_uses_next_tick_bid_ask_execution(self) -> None:
        start = datetime(2025, 3, 19, 13, 30)
        ticks = [
            Tick(start + timedelta(seconds=0), bid=99.9, ask=100.1, bid_size=1, ask_size=1),
            Tick(start + timedelta(minutes=1), bid=100.0, ask=100.2, bid_size=1, ask_size=1),
            Tick(start + timedelta(minutes=2), bid=100.1, ask=100.3, bid_size=1, ask_size=1),
            Tick(start + timedelta(minutes=3), bid=104.0, ask=104.2, bid_size=1, ask_size=1),
            Tick(start + timedelta(minutes=3, seconds=1), bid=104.1, ask=104.3, bid_size=1, ask_size=1),
            Tick(start + timedelta(minutes=4), bid=107.6, ask=107.8, bid_size=1, ask_size=1),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            tick_path = normalized_tick_path(data_root, "NQmain", start.date())
            write_ticks_parquet(tick_path, "NQmain", ticks)

            result = run_tick_backtest(parse_strategy_spec(base_spec()), symbol_config(), [tick_path])

        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.side, "long")
        self.assertEqual(trade.exit_reason, "take_profit")
        self.assertEqual(trade.entry_time, start + timedelta(minutes=3, seconds=1))
        self.assertAlmostEqual(trade.entry_price, 104.5)
        self.assertAlmostEqual(trade.exit_price, 107.5)
        self.assertAlmostEqual(trade.gross_pnl, 60.0)
        self.assertAlmostEqual(trade.net_pnl, 45.0)

    def test_tick_replay_short_stop_uses_ask(self) -> None:
        start = datetime(2025, 3, 19, 13, 30)
        ticks = [
            Tick(start + timedelta(seconds=0), bid=99.9, ask=100.1, bid_size=1, ask_size=1),
            Tick(start + timedelta(minutes=1), bid=100.0, ask=100.2, bid_size=1, ask_size=1),
            Tick(start + timedelta(minutes=2), bid=100.1, ask=100.3, bid_size=1, ask_size=1),
            Tick(start + timedelta(minutes=3), bid=96.0, ask=96.2, bid_size=1, ask_size=1),
            Tick(start + timedelta(minutes=3, seconds=1), bid=95.9, ask=96.1, bid_size=1, ask_size=1),
            Tick(start + timedelta(minutes=4), bid=98.0, ask=98.3, bid_size=1, ask_size=1),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            tick_path = normalized_tick_path(data_root, "NQmain", start.date())
            write_ticks_parquet(tick_path, "NQmain", ticks)

            result = run_tick_backtest(parse_strategy_spec(base_spec()), symbol_config(), [tick_path])

        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.side, "short")
        self.assertEqual(trade.exit_reason, "stop_loss")
        self.assertAlmostEqual(trade.entry_price, 95.75)
        self.assertAlmostEqual(trade.exit_price, 98.5)
        self.assertAlmostEqual(trade.gross_pnl, -55.0)
        self.assertAlmostEqual(trade.net_pnl, -70.0)

    def test_tick_replay_uses_configurable_cost_model(self) -> None:
        start = datetime(2025, 3, 19, 13, 30)
        ticks = [
            Tick(start + timedelta(seconds=0), bid=99.9, ask=100.1, bid_size=1, ask_size=1),
            Tick(start + timedelta(minutes=1), bid=100.0, ask=100.2, bid_size=1, ask_size=1),
            Tick(start + timedelta(minutes=2), bid=100.1, ask=100.3, bid_size=1, ask_size=1),
            Tick(start + timedelta(minutes=3), bid=104.0, ask=104.2, bid_size=1, ask_size=1),
            Tick(start + timedelta(minutes=3, seconds=1), bid=104.1, ask=104.3, bid_size=1, ask_size=1),
            Tick(start + timedelta(minutes=4), bid=107.6, ask=107.8, bid_size=1, ask_size=1),
        ]
        expensive_costs = CostModelConfig(
            name="expensive_test",
            tick_size=0.25,
            point_value=20,
            tick_value=5,
            slippage_ticks_per_side=2,
            round_trip_fees_usd=20,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            tick_path = normalized_tick_path(data_root, "NQmain", start.date())
            write_ticks_parquet(tick_path, "NQmain", ticks)

            result = run_tick_backtest(
                parse_strategy_spec(base_spec()),
                symbol_config(),
                [tick_path],
                cost_model=expensive_costs,
            )

        trade = result.trades[0]
        self.assertAlmostEqual(trade.gross_pnl, 60.0)
        self.assertAlmostEqual(trade.fees, 20.0)
        self.assertAlmostEqual(trade.slippage_cost, 20.0)
        self.assertAlmostEqual(trade.net_pnl, 20.0)
        self.assertEqual(result.cost_model["name"], "expensive_test")


if __name__ == "__main__":
    unittest.main()
