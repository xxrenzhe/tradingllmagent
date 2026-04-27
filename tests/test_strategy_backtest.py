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
from tlm.variants import parameter_grid_metadata


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


def trend_pullback_spec() -> dict:
    payload = base_spec()
    payload["name"] = "test_trend_pullback"
    payload["strategy_family"] = "trend_pullback"
    payload["market_hypothesis"] = (
        "Trend pullbacks can resume after price reclaims a fast EMA while the fast EMA "
        "stays above the slow EMA."
    )
    payload["direction"] = "long"
    payload["indicators"] = {
        "ema_fast": {"type": "ema", "window": 2},
        "ema_slow": {"type": "ema", "window": 4},
    }
    payload["entry"] = {
        "long": {
            "all": [
                {"left": "ema_fast", "op": ">", "right": "ema_slow"},
                {"left": "close", "op": ">=", "right": "ema_fast"},
            ]
        }
    }
    payload["parameters"] = {"ema_fast_window": {"values": [2]}, "ema_slow_window": {"values": [4]}}
    return payload


def mean_reversion_spec() -> dict:
    payload = base_spec()
    payload["name"] = "test_mean_reversion"
    payload["strategy_family"] = "regime_filtered_mean_reversion"
    payload["market_hypothesis"] = (
        "In range-bound intraday regimes, extreme negative z-score deviations can revert "
        "toward the recent session mean."
    )
    payload["direction"] = "long"
    payload["indicators"] = {"z_close": {"type": "z_score", "window": 3, "entry_z": 1.0}}
    payload["entry"] = {
        "long": {"all": [{"left": "z_close", "op": "<=", "right": "-1.0"}]}
    }
    payload["parameters"] = {"mean_reversion_entry_z": {"values": [1.0]}}
    return payload


def family_spec(strategy_family: str, indicators: dict, parameters: dict, direction: str = "long") -> dict:
    payload = base_spec()
    payload["name"] = f"test_{strategy_family}"
    payload["strategy_family"] = strategy_family
    payload["market_hypothesis"] = (
        f"{strategy_family} is tested as a bounded deterministic intraday NQ strategy family."
    )
    payload["direction"] = direction
    payload["indicators"] = indicators
    payload["parameters"] = parameters
    payload["entry"] = {"long": {"all": [{"left": "close", "op": ">", "right": "open"}]}}
    if direction in {"short", "long_short"}:
        payload["entry"]["short"] = {"all": [{"left": "close", "op": "<", "right": "open"}]}
    return payload


def controlled_grid_spec() -> dict:
    payload = family_spec(
        "controlled_grid",
        {"z_close": {"type": "z_score", "window": 20, "entry_z": 1.5}},
        {"mean_reversion_entry_z": {"values": [1.5]}},
        direction="long_short",
    )
    payload["market_hypothesis"] = (
        "A tightly bounded grid can only be tested in range-regime NQ conditions with "
        "finite inventory, hard stops, and daily loss limits."
    )
    payload["anti_martingale_constraints"]["max_grid_levels"] = 1
    payload["risk"]["max_position_contracts"] = 2
    payload["risk"]["max_daily_loss_r"] = 2
    return payload


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


def build_bar_rows(start: datetime, prices: list[tuple[float, float, float, float]]) -> list[dict]:
    rows = []
    for index, (open_, high, low, close) in enumerate(prices):
        rows.append(
            {
                "symbol": "NQmain",
                "timestamp": start + timedelta(minutes=index),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "bid_close": close - 0.1,
                "ask_close": close + 0.1,
                "tick_count": 10,
                "avg_spread": 0.2,
            }
        )
    return rows


def run_bar_result_for_rows(spec_payload: dict, rows: list[dict]):
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
        return run_bar_backtest(parse_strategy_spec(spec_payload), symbol_config(), [output])


def run_bar_result_for_rows_with_events(spec_payload: dict, rows: list[dict], contexts: list[dict]):
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
        return run_bar_backtest(
            parse_strategy_spec(spec_payload),
            symbol_config(),
            [output],
            event_contexts=contexts,
        )


class StrategyValidationTests(unittest.TestCase):
    def test_valid_example_spec_loads(self) -> None:
        expected = {
            "example_opening_range_breakout.yaml": "opening_range_breakout",
            "example_trend_pullback.yaml": "trend_pullback",
            "example_regime_filtered_mean_reversion.yaml": "regime_filtered_mean_reversion",
            "example_volatility_expansion.yaml": "volatility_expansion",
            "example_intraday_momentum.yaml": "intraday_momentum",
            "example_time_of_day_edge.yaml": "time_of_day_edge",
            "example_gap_fade_or_continuation.yaml": "gap_fade_or_continuation",
        }
        for filename, family in expected.items():
            spec = load_strategy_spec(Path("strategies") / filename)
            self.assertEqual(spec.strategy_family, family)
            self.assertEqual(spec.symbol, "NQmain")

    def test_all_strategy_seed_files_are_valid(self) -> None:
        paths = sorted(Path("strategies").glob("*.yaml"))
        self.assertGreaterEqual(len(paths), 50)
        for path in paths:
            with self.subTest(path=str(path)):
                spec = load_strategy_spec(path)
                metadata = parameter_grid_metadata(spec, max_trials=1)
                self.assertEqual(spec.symbol, "NQmain")
                self.assertEqual(spec.timeframe, "1m")
                self.assertFalse(metadata.high_risk_budget)

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

    def test_unsupported_indicator_type_rejected(self) -> None:
        payload = base_spec()
        payload["indicators"]["custom_alpha"] = {"type": "python_function", "window": 5}
        with self.assertRaises(StrategySpecError):
            parse_strategy_spec(payload)

    def test_executable_payload_rejected(self) -> None:
        payload = base_spec()
        payload["market_hypothesis"] = "Opening range breakout. import os; os.system('rm -rf /')"
        with self.assertRaises(StrategySpecError):
            parse_strategy_spec(payload)

    def test_unsupported_exit_and_position_sizing_rejected(self) -> None:
        payload = base_spec()
        payload["exit"]["stop_loss"] = {"type": "python_callback", "value": 2}
        with self.assertRaises(StrategySpecError):
            parse_strategy_spec(payload)

        payload = base_spec()
        payload["risk"]["position_sizing"] = {"type": "martingale", "contracts": 1}
        with self.assertRaises(StrategySpecError):
            parse_strategy_spec(payload)

    def test_controlled_grid_requires_range_regime_and_bounded_inventory(self) -> None:
        valid = parse_strategy_spec(controlled_grid_spec())
        self.assertEqual(valid.strategy_family, "controlled_grid")

        payload = controlled_grid_spec()
        payload["indicators"] = {"ema_fast": {"type": "ema", "window": 9}}
        with self.assertRaisesRegex(StrategySpecError, "range-regime"):
            parse_strategy_spec(payload)

        payload = controlled_grid_spec()
        payload["anti_martingale_constraints"]["max_grid_levels"] = 3
        payload["risk"]["max_position_contracts"] = 2
        with self.assertRaisesRegex(StrategySpecError, "cannot exceed"):
            parse_strategy_spec(payload)

        payload = controlled_grid_spec()
        payload["exit"]["stop_loss"]["value"] = 0
        with self.assertRaisesRegex(StrategySpecError, "hard stop"):
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
        self.assertEqual(trade.entry_reason, "close_above_opening_range_high")
        self.assertEqual(trade.exit_reason, "take_profit")
        self.assertAlmostEqual(trade.gross_pnl, 60.0)
        self.assertAlmostEqual(trade.net_pnl, 45.0)
        self.assertEqual(result.metrics.trade_count, 1)
        self.assertAlmostEqual(result.metrics.net_pnl, 45.0)
        self.assertTrue(result.data_version_hash)
        self.assertEqual(result.to_dict()["data_version_hash"], result.data_version_hash)
        self.assertEqual(
            result.to_dict()["trades"][0]["entry_reason"],
            "close_above_opening_range_high",
        )

    def test_trend_pullback_produces_deterministic_trade(self) -> None:
        start = datetime(2025, 3, 19, 13, 30)
        closes = [100.0, 101.0, 102.0, 101.0, 103.0, 106.4]
        rows = []
        for index, close in enumerate(closes):
            timestamp = start + timedelta(minutes=index)
            rows.append(
                (
                    "NQmain",
                    timestamp,
                    close,
                    close + 0.4,
                    close - 0.4,
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
            output = bar_path(Path(temp_dir), "NQmain", "1m", start.date())
            write_bars_parquet(output, rows)
            result = run_bar_backtest(parse_strategy_spec(trend_pullback_spec()), symbol_config(), [output])

        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.side, "long")
        self.assertEqual(trade.entry_reason, "close_pullback_reclaim_ema_fast_above_ema_slow")
        self.assertEqual(trade.exit_reason, "take_profit")
        self.assertAlmostEqual(trade.net_pnl, 45.0)

    def test_regime_filtered_mean_reversion_produces_deterministic_trade(self) -> None:
        start = datetime(2025, 3, 19, 13, 30)
        closes = [100.0, 100.5, 99.0, 102.3]
        rows = []
        for index, close in enumerate(closes):
            timestamp = start + timedelta(minutes=index)
            rows.append(
                (
                    "NQmain",
                    timestamp,
                    close,
                    close + 0.4,
                    close - 0.4,
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
            output = bar_path(Path(temp_dir), "NQmain", "1m", start.date())
            write_bars_parquet(output, rows)
            result = run_bar_backtest(parse_strategy_spec(mean_reversion_spec()), symbol_config(), [output])

        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.side, "long")
        self.assertEqual(trade.entry_reason, "z_close_below_negative_1")
        self.assertEqual(trade.exit_reason, "take_profit")
        self.assertAlmostEqual(trade.net_pnl, 45.0)

    def test_volatility_expansion_produces_deterministic_trade(self) -> None:
        spec = family_spec(
            "volatility_expansion",
            {"realized_volatility": {"type": "realized_volatility", "window": 3, "expansion_multiple": 1.5}},
            {"volatility_window": {"values": [3]}, "volatility_expansion_multiple": {"values": [1.5]}},
        )
        rows = build_bar_rows(
            datetime(2025, 3, 19, 13, 30),
            [(100, 100.2, 99.9, 100.0), (100, 100.2, 99.9, 100.1), (100, 100.2, 99.9, 100.2), (100.2, 104.5, 100.0, 104.0), (104.0, 107.5, 103.8, 107.0)],
        )
        result = run_bar_result_for_rows(spec, rows)

        self.assertEqual(result.trades[0].entry_reason, "range_expansion_1.5_close_above_prior_high")
        self.assertEqual(result.trades[0].exit_reason, "take_profit")

    def test_intraday_momentum_produces_deterministic_trade(self) -> None:
        spec = family_spec(
            "intraday_momentum",
            {"momentum": {"type": "momentum", "lookback_minutes": 3, "threshold_points": 2}},
            {"momentum_lookback_minutes": {"values": [3]}, "momentum_threshold_points": {"values": [2]}},
        )
        rows = build_bar_rows(
            datetime(2025, 3, 19, 13, 30),
            [(100, 100.3, 99.8, 100.0), (100, 100.8, 99.8, 100.5), (100.5, 101.2, 100.3, 101.0), (101.0, 103.4, 100.8, 103.0), (103.0, 106.5, 102.8, 106.2)],
        )
        result = run_bar_result_for_rows(spec, rows)

        self.assertEqual(result.trades[0].entry_reason, "momentum_3m_above_2")
        self.assertEqual(result.trades[0].exit_reason, "take_profit")

    def test_time_of_day_edge_produces_deterministic_trade(self) -> None:
        spec = family_spec(
            "time_of_day_edge",
            {"time_of_day": {"type": "time_of_day", "entry_time": "13:32", "entry_side": "long"}},
            {"entry_time": {"values": ["13:32"]}, "entry_side": {"values": ["long"]}},
        )
        rows = build_bar_rows(
            datetime(2025, 3, 19, 13, 30),
            [(100, 100.3, 99.8, 100.0), (100, 100.3, 99.8, 100.0), (100, 100.4, 99.8, 100.1), (100.1, 103.5, 100.0, 103.1)],
        )
        result = run_bar_result_for_rows(spec, rows)

        self.assertEqual(result.trades[0].entry_reason, "time_of_day_13:32:00_long")
        self.assertEqual(result.trades[0].exit_reason, "take_profit")

    def test_gap_fade_produces_deterministic_trade(self) -> None:
        spec = family_spec(
            "gap_fade_or_continuation",
            {"gap": {"type": "gap", "threshold_points": 1, "mode": "fade"}},
            {"gap_threshold_points": {"values": [1]}, "gap_mode": {"values": ["fade"]}},
            direction="short",
        )
        rows = build_bar_rows(
            datetime(2025, 3, 18, 20, 54),
            [(100, 100.2, 99.8, 100.0), (103.0, 103.4, 102.8, 103.0), (103.0, 103.2, 99.5, 100.0)],
        )
        rows[1] = {**rows[1], "timestamp": datetime(2025, 3, 19, 13, 30)}
        rows[2] = {**rows[2], "timestamp": datetime(2025, 3, 19, 13, 31)}
        result = run_bar_result_for_rows(spec, rows)

        self.assertEqual(result.trades[0].side, "short")
        self.assertEqual(result.trades[0].entry_reason, "gap_fade_up")
        self.assertEqual(result.trades[0].exit_reason, "take_profit")

    def test_event_policy_blocks_release_window_entries(self) -> None:
        start = datetime(2025, 1, 1, 13, 30)
        rows = build_bar_rows(
            start,
            [
                (100, 100.5, 99.5, 100),
                (100, 100.5, 99.5, 100.1),
                (100.1, 100.5, 99.5, 100.2),
                (100.2, 104.5, 100.2, 104.0),
                (104.0, 108.0, 104.0, 107.5),
            ],
        )
        contexts = [
            {
                "timestamp": rows[3]["timestamp"],
                "event_state": "release_window",
                "active_event_ids": ["cpi_test"],
                "max_importance": "high",
                "policy_ref": "high_impact_macro_v1",
            }
        ]

        result = run_bar_result_for_rows_with_events(base_spec(), rows, contexts)

        self.assertEqual(result.trades, [])
        self.assertEqual(result.metrics.trade_count, 0)
        self.assertTrue(result.event_attribution["event_context_applied"])
        self.assertEqual(result.event_attribution["blocked_trade_count"], 1)
        self.assertEqual(result.event_attribution["event_dependency_ratio"], 1.0)
        self.assertIsNone(result.event_attribution["non_event_sharpe"])
        self.assertEqual(result.event_attribution["event_window_drawdown"], 0.0)
        self.assertEqual(result.event_attribution["event_window_metrics"]["trade_count"], 0)
        self.assertEqual(result.event_attribution["non_event_metrics"]["trade_count"], 0)
        blocked = result.event_attribution["blocked_trades"][0]
        self.assertEqual(blocked["event_state_at_entry"], "release_window")
        self.assertEqual(blocked["active_event_ids_at_entry"], ["cpi_test"])
        self.assertEqual(blocked["event_policy_action"], "block")
        self.assertEqual(blocked["blocked_or_delayed_reason"], "high_impact_release_window")

    def test_event_attribution_splits_event_and_non_event_metrics(self) -> None:
        start = datetime(2025, 1, 1, 13, 30)
        spec = family_spec(
            "time_of_day_edge",
            {"time_of_day": {"type": "time_of_day", "entry_time": "13:32", "entry_side": "long"}},
            {"entry_time": {"values": ["13:32"]}, "entry_side": {"values": ["long"]}},
        )
        spec["risk"]["max_trades_per_day"] = 2
        rows = build_bar_rows(
            start,
            [
                (100, 100.3, 99.8, 100.0),
                (100, 100.3, 99.8, 100.0),
                (100, 100.4, 99.8, 100.1),
                (100.1, 103.5, 100.0, 103.1),
                (103.1, 103.3, 102.0, 102.5),
                (102.5, 102.7, 100.0, 100.4),
                (100.4, 103.9, 100.2, 103.6),
            ],
        )
        contexts = [
            {
                "timestamp": rows[2]["timestamp"],
                "event_state": "pre_event",
                "active_event_ids": ["fomc_test"],
                "max_importance": "medium",
            },
            {
                "timestamp": rows[3]["timestamp"],
                "event_state": "pre_event",
                "active_event_ids": ["fomc_test"],
                "max_importance": "medium",
            },
        ]

        result = run_bar_result_for_rows_with_events(spec, rows, contexts)

        self.assertEqual(len(result.trades), 2)
        self.assertEqual(result.event_attribution["event_window_metrics"]["trade_count"], 1)
        self.assertEqual(result.event_attribution["non_event_metrics"]["trade_count"], 1)
        self.assertEqual(result.event_attribution["event_dependency_ratio"], 0.5)
        self.assertEqual(
            result.event_attribution["event_window_metrics"]["net_pnl"],
            result.trades[0].net_pnl,
        )
        self.assertEqual(
            result.event_attribution["non_event_metrics"]["net_pnl"],
            result.trades[1].net_pnl,
        )


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
        self.assertEqual(trade.entry_reason, "mid_above_opening_range_high")
        self.assertEqual(trade.exit_reason, "take_profit")
        self.assertEqual(trade.entry_time, start + timedelta(minutes=3, seconds=1))
        self.assertAlmostEqual(trade.entry_price, 104.5)
        self.assertAlmostEqual(trade.exit_price, 107.5)
        self.assertAlmostEqual(trade.gross_pnl, 60.0)
        self.assertAlmostEqual(trade.net_pnl, 45.0)
        self.assertTrue(result.data_version_hash)

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
        self.assertEqual(trade.entry_reason, "mid_below_opening_range_low")
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
        self.assertTrue(result.data_version_hash)

    def test_tick_replay_supports_trend_pullback_family(self) -> None:
        start = datetime(2025, 3, 19, 13, 30)
        mids = [100.0, 101.0, 102.0, 101.0, 103.0, 106.4]
        ticks = [
            Tick(
                start + timedelta(minutes=index),
                bid=mid - 0.1,
                ask=mid + 0.1,
                bid_size=1,
                ask_size=1,
            )
            for index, mid in enumerate(mids)
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            tick_path = normalized_tick_path(Path(temp_dir), "NQmain", start.date())
            write_ticks_parquet(tick_path, "NQmain", ticks)
            result = run_tick_backtest(parse_strategy_spec(trend_pullback_spec()), symbol_config(), [tick_path])

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].entry_reason, "close_pullback_reclaim_ema_fast_above_ema_slow")


if __name__ == "__main__":
    unittest.main()
