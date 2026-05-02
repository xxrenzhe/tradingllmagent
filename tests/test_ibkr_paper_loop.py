from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from tlm.api import _ibkr_default_control_state, _ibkr_default_strategy, _ibkr_load_warm_start_bars, _ibkr_maybe_backfill_warm_start_bars, _ibkr_warm_start_rows_usable, run_ibkr_decision_cycle
from tlm.ibkr_gateway import IbkrPaperGateway
from tlm.ibkr_optimizer import apply_fast_path_control_diff
from tlm.ibkr_paper import build_ibkr_paper_report, create_ibkr_paper_run_artifacts, load_ibkr_paper_report
from tlm.ibkr_review import build_five_minute_review_request, deterministic_fallback_review
from tlm.ibkr_signals import OneMinuteBar, build_one_minute_bars, build_signal_candidate, merge_one_minute_bars


class FakeIbkrAdapter:
    def __init__(self, *, historical_bars: list[dict] | None = None) -> None:
        self.connected = False
        self.historical_bars = list(historical_bars or [])

    def connect(self, host: str, port: int, client_id: int) -> dict:
        self.connected = True
        return {"connected": True, "host": host, "port": port, "client_id": client_id}

    def disconnect(self) -> dict:
        self.connected = False
        return {"connected": False}

    def account_summary(self) -> dict:
        return {"account_id": "DU1234567", "account_type": "paper"}

    def request_historical_bars(
        self,
        contract: dict,
        *,
        duration: str = "4 H",
        bar_size: str = "1 min",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
        timeout_seconds: int = 20,
    ) -> list[dict]:
        symbol = str(contract.get("symbol", "MNQ"))
        return [{**row, "symbol": str(row.get("symbol", symbol) or symbol)} for row in self.historical_bars]


class IbkrPaperLoopTests(unittest.TestCase):
    def test_default_ibkr_strategy_uses_expanded_high_edge_cap24_balanced_risk(self) -> None:
        strategy = _ibkr_default_strategy("MNQ")

        self.assertEqual(strategy["family"], "expanded_high_edge")
        self.assertEqual(strategy["preset"], "expanded_high_edge_cap24_balanced_risk")
        self.assertEqual(strategy["module_id"], "expanded_high_edge")
        self.assertEqual(strategy["max_concurrent_positions"], 18)
        self.assertEqual(strategy["max_holding_minutes"], 300)

    def test_default_expanded_high_edge_control_uses_four_tick_spread_cap(self) -> None:
        strategy = _ibkr_default_strategy("MNQ")
        control_state = _ibkr_default_control_state(strategy)

        self.assertEqual(control_state["max_spread_ticks"], 4.0)

    def test_ibkr_runtime_caps_can_be_overridden_for_paper_soak(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TLM_IBKR_MAX_CONCURRENT_POSITIONS": "1",
                "TLM_IBKR_DAILY_TRADE_CAP": "3",
            },
        ):
            strategy = _ibkr_default_strategy("MNQ")
            control_state = _ibkr_default_control_state(strategy)

        self.assertEqual(strategy["max_concurrent_positions"], 1)
        self.assertEqual(control_state["daily_trade_cap"], 3)

    def test_expanded_high_edge_signal_candidate_matches_prior_day_breakout(self) -> None:
        strategy = _ibkr_default_strategy("MNQ")
        signal = build_signal_candidate(
            strategy,
            _expanded_prior_day_breakout_bars(),
            tick_size=0.25,
            max_spread_ticks=2.0,
        )

        self.assertEqual(signal["signal_class"], "strong_review")
        self.assertEqual(signal["family"], "expanded_high_edge")
        self.assertEqual(signal["side"], "BUY")
        self.assertIn("expanded_high_edge:prior_day_breakout", signal["trigger_reasons"])
        self.assertEqual(signal["risk_context"]["preset"], "expanded_high_edge_cap24_balanced_risk")
        self.assertEqual(signal["risk_context"]["max_concurrent_positions"], 18)
        self.assertEqual(signal["risk_context"]["session_bucket"], "ny_0930_1159")
        self.assertEqual(signal["risk_context"]["dow"], 1)

    def test_expanded_high_edge_signal_candidate_allows_three_tick_spread(self) -> None:
        strategy = _ibkr_default_strategy("MNQ")
        bars = _expanded_prior_day_breakout_bars()
        current = bars[-1]
        bars[-1] = replace(current, bid=current.close - 0.75, ask=current.close)

        signal = build_signal_candidate(
            strategy,
            bars,
            tick_size=0.25,
            max_spread_ticks=4.0,
        )

        self.assertEqual(signal["signal_class"], "strong_review")
        self.assertEqual(signal["risk_context"]["spread_ticks"], 3.0)
        self.assertEqual(signal["risk_context"]["max_spread_ticks"], 4.0)

    def test_low_r_signal_candidate_matches_simple_robust_opening_range_edge(self) -> None:
        strategy = _low_r_strategy()
        signal = build_signal_candidate(
            strategy,
            _low_r_opening_range_bars(),
            tick_size=0.25,
            max_spread_ticks=2.0,
        )

        self.assertEqual(signal["signal_class"], "strong_review")
        self.assertEqual(signal["family"], "low_r_regime_basket")
        self.assertEqual(signal["side"], "BUY")
        self.assertIn("low_r:opening_range_breakout", signal["trigger_reasons"])
        self.assertEqual(signal["risk_context"]["preset"], "simple_robust_low_r")
        self.assertEqual(signal["risk_context"]["edge_index"], 0)
        self.assertGreaterEqual(signal["risk_context"]["stop_loss_ticks"], 32)

    def test_load_warm_start_bars_uses_latest_artifact_report(self) -> None:
        bars = [bar.to_dict() for bar in _low_r_opening_range_bars()[:60]]
        report = build_ibkr_paper_report(
            run_id="warm-start-test",
            health={"status": "paper_armed"},
            readiness={"status": "ready", "missing_requirements": []},
            contracts={},
            market_data={},
            bracket_orders={},
            execution_ledger={},
            incidents={"incidents": [], "count": 0},
            one_minute_bars=bars,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            create_ibkr_paper_run_artifacts(
                run_id="warm-start-test",
                root=Path(temp_dir),
                report=report,
            )
            loaded, source = _ibkr_load_warm_start_bars(Path(temp_dir), "MNQ", 40)

        self.assertEqual(len(loaded), 40)
        self.assertTrue(source is not None and source.endswith("daily_report.json"))
        self.assertEqual(loaded[-1]["bar_time"], bars[59]["bar_time"])

    def test_decision_cycle_uses_warm_start_bars_for_low_r_history(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())
        gateway.connect()
        gateway.record_contract_details(
            {
                "symbol": "MNQ",
                "tick_size": 0.25,
                "point_value": 2.0,
                "exchange": "CME",
                "currency": "USD",
            }
        )
        bars = _low_r_opening_range_bars()
        for snapshot in _snapshots_from_bar(bars[-1]):
            gateway.record_market_data(snapshot)

        review_history: list[dict] = []
        optimizer_history: list[dict] = []
        state = {
            "review_interval_seconds": 0,
            "market_data_history_limit": 500,
            "readiness_max_stale_seconds": 600,
            "strategy": _low_r_strategy(),
            "control_state": {
                "mode": "paper",
                "min_confidence": 0.55,
                "max_spread_ticks": 2.0,
                "daily_trade_cap": 6,
                "strategies": {},
                "trade_session": {"start": "09:30", "end": "15:55"},
                "safe_mode": False,
                "kill_switch": False,
            },
            "warm_start_bars": [bar.to_dict() for bar in bars[:-1]],
        }

        decision = run_ibkr_decision_cycle(
            gateway,
            symbol="MNQ",
            review_history=review_history,
            optimizer_history=optimizer_history,
            state=state,
        )

        self.assertEqual(decision["status"], "review_completed")
        self.assertEqual(state["latest_signal"]["signal_class"], "strong_review")
        self.assertEqual(state["latest_signal"]["family"], "low_r_regime_basket")
        self.assertNotIn("insufficient_low_r_history", state["latest_signal"].get("reasons", []))
        self.assertGreaterEqual(len(merge_one_minute_bars(state["warm_start_bars"], [])), 50)

    def test_decision_cycle_persists_warm_start_cache(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())
        gateway.connect()
        gateway.record_contract_details(
            {
                "symbol": "MNQ",
                "tick_size": 0.25,
                "point_value": 2.0,
                "exchange": "CME",
                "currency": "USD",
            }
        )
        bars = _low_r_opening_range_bars()
        for snapshot in _snapshots_from_bar(bars[-1]):
            gateway.record_market_data(snapshot)

        with tempfile.TemporaryDirectory() as temp_dir:
            state = {
                "review_interval_seconds": 0,
                "market_data_history_limit": 500,
                "readiness_max_stale_seconds": 600,
                "strategy": _low_r_strategy(),
                "control_state": {
                    "mode": "paper",
                    "min_confidence": 0.55,
                    "max_spread_ticks": 2.0,
                    "daily_trade_cap": 6,
                    "strategies": {},
                    "trade_session": {"start": "09:30", "end": "15:55"},
                    "safe_mode": False,
                    "kill_switch": False,
                },
                "warm_start_root": temp_dir,
                "warm_start_bars": [bar.to_dict() for bar in bars[:-1]],
            }
            run_ibkr_decision_cycle(
                gateway,
                symbol="MNQ",
                review_history=[],
                optimizer_history=[],
                state=state,
            )
            loaded, source = _ibkr_load_warm_start_bars(Path(temp_dir), "MNQ", 240)

        self.assertTrue(source is not None and source.endswith("warm_start_mnq.json"))
        self.assertGreaterEqual(len(loaded), 50)

    def test_backfill_warm_start_bars_uses_ibkr_historical_data_when_cache_missing(self) -> None:
        historical_bars = [bar.to_dict() for bar in _low_r_opening_range_bars()[:60]]
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter(historical_bars=historical_bars))
        gateway.connect()

        with tempfile.TemporaryDirectory() as temp_dir:
            state = {
                "symbol": "MNQ",
                "warm_start_root": temp_dir,
                "warm_start_bars": [],
                "warm_start_bar_limit": 240,
                "warm_start_min_bars": 50,
                "warm_start_backfill_duration": "4 H",
                "warm_start_backfill_timeout_seconds": 1,
            }
            count = _ibkr_maybe_backfill_warm_start_bars(gateway, state)
            loaded, source = _ibkr_load_warm_start_bars(Path(temp_dir), "MNQ", 240)

        self.assertEqual(count, 60)
        self.assertTrue(state["warm_start_backfill_attempted"])
        self.assertEqual(state["warm_start_source"], "ibkr_historical:MNQ")
        self.assertIsNone(state["warm_start_backfill_error"])
        self.assertTrue(source is not None and source.endswith("warm_start_mnq.json"))
        self.assertEqual(len(loaded), 60)

    def test_warm_start_rows_usable_rejects_future_or_stale_cache(self) -> None:
        now = datetime(2026, 5, 1, 1, 30, tzinfo=UTC)
        valid_rows = [{"bar_time": "2026-05-01T01:20:00+00:00"}]
        future_rows = [{"bar_time": "2026-05-01T02:20:00+00:00"}]
        stale_rows = [{"bar_time": "2026-04-30T20:00:00+00:00"}]

        self.assertTrue(_ibkr_warm_start_rows_usable(valid_rows, now=now))
        self.assertFalse(_ibkr_warm_start_rows_usable(future_rows, now=now))
        self.assertFalse(_ibkr_warm_start_rows_usable(stale_rows, now=now))

    def test_decision_cycle_runs_review_and_builds_bracket_draft(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())
        gateway.connect()
        gateway.record_contract_details(
            {
                "symbol": "MNQ",
                "tick_size": 0.25,
                "point_value": 2.0,
                "exchange": "CME",
                "currency": "USD",
            }
        )
        snapshots = _breakout_snapshots()
        for snapshot in snapshots:
            gateway.record_market_data(snapshot)

        review_history: list[dict] = []
        optimizer_history: list[dict] = []
        state = {
            "review_interval_seconds": 0,
            "market_data_history_limit": 100,
            "readiness_max_stale_seconds": 600,
            "strategy": {
                "strategy_id": "mnq_1m_breakout",
                "strategy_spec_hash": "strategy-hash",
                "module_id": "range_breakout",
                "family": "range_breakout",
                "symbol": "MNQ",
                "timeframe": "1m",
                "lookback_bars": 5,
                "breakout_ticks": 1,
                "enabled": True,
                "tick_size": 0.25,
                "stop_loss_ticks": 20,
                "take_profit_ticks": 40,
                "max_holding_minutes": 20,
            },
            "control_state": {
                "mode": "paper",
                "min_confidence": 0.55,
                "max_spread_ticks": 2.0,
                "daily_trade_cap": 6,
                "strategies": {},
                "trade_session": {"start": "09:30", "end": "15:55"},
                "safe_mode": False,
                "kill_switch": False,
            },
        }

        decision = run_ibkr_decision_cycle(
            gateway,
            symbol="MNQ",
            review_history=review_history,
            optimizer_history=optimizer_history,
            state=state,
        )

        self.assertEqual(decision["status"], "review_completed")
        self.assertEqual(decision["review_result_action"], "paper_allow")
        self.assertEqual(decision["bracket_event_type"], "bracket_order_built")
        self.assertEqual(len(review_history), 1)
        self.assertEqual(state["review_cycle_count"], 1)
        self.assertEqual(gateway.bracket_order_report()["open_bracket_order_count"], 1)
        self.assertGreater(len(state["latest_bars"]), 0)

    def test_decision_cycle_does_not_build_bracket_when_auto_submit_is_explicitly_disabled(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())
        gateway.connect()
        gateway.record_contract_details(
            {
                "symbol": "MNQ",
                "tick_size": 0.25,
                "point_value": 2.0,
                "exchange": "CME",
                "currency": "USD",
            }
        )
        for snapshot in _breakout_snapshots():
            gateway.record_market_data(snapshot)

        state = {
            "auto_submit": False,
            "review_interval_seconds": 0,
            "market_data_history_limit": 100,
            "readiness_max_stale_seconds": 600,
            "strategy": {
                "strategy_id": "mnq_1m_breakout",
                "strategy_spec_hash": "strategy-hash",
                "module_id": "range_breakout",
                "family": "range_breakout",
                "symbol": "MNQ",
                "timeframe": "1m",
                "lookback_bars": 5,
                "breakout_ticks": 1,
                "enabled": True,
                "tick_size": 0.25,
                "stop_loss_ticks": 20,
                "take_profit_ticks": 40,
                "max_holding_minutes": 20,
            },
            "control_state": {
                "mode": "paper",
                "min_confidence": 0.55,
                "max_spread_ticks": 2.0,
                "daily_trade_cap": 6,
                "strategies": {},
                "trade_session": {"start": "09:30", "end": "15:55"},
                "safe_mode": False,
                "kill_switch": False,
            },
        }

        decision = run_ibkr_decision_cycle(
            gateway,
            symbol="MNQ",
            review_history=[],
            optimizer_history=[],
            state=state,
        )

        self.assertEqual(decision["review_result_action"], "paper_allow")
        self.assertIsNone(decision["bracket_event_type"])
        self.assertEqual(decision["bracket_build_skipped_reason"], "auto_submit_disabled")
        self.assertEqual(gateway.bracket_order_report()["open_bracket_order_count"], 0)

    def test_decision_cycle_allows_additional_paper_bracket_under_concurrency_cap(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())
        gateway.connect()
        gateway.record_contract_details(
            {
                "symbol": "MNQ",
                "tick_size": 0.25,
                "point_value": 2.0,
                "exchange": "CME",
                "currency": "USD",
            }
        )
        for snapshot in _breakout_snapshots():
            gateway.record_market_data(snapshot)
        gateway.build_bracket_order(
            {
                "symbol": "MNQ",
                "action": "BUY",
                "quantity": 1,
                "entry_order_type": "MKT",
                "reference_price": 100.0,
                "stop_price": 95.0,
                "take_profit_price": 110.0,
                "max_holding_minutes": 20,
            }
        )

        state = {
            "review_interval_seconds": 0,
            "market_data_history_limit": 100,
            "readiness_max_stale_seconds": 600,
            "strategy": {
                "strategy_id": "mnq_1m_breakout",
                "strategy_spec_hash": "strategy-hash",
                "module_id": "range_breakout",
                "family": "range_breakout",
                "symbol": "MNQ",
                "timeframe": "1m",
                "lookback_bars": 5,
                "breakout_ticks": 1,
                "enabled": True,
                "tick_size": 0.25,
                "stop_loss_ticks": 20,
                "take_profit_ticks": 40,
                "max_holding_minutes": 20,
                "max_concurrent_positions": 2,
            },
            "control_state": {
                "mode": "paper",
                "min_confidence": 0.55,
                "max_spread_ticks": 2.0,
                "daily_trade_cap": 6,
                "strategies": {},
                "trade_session": {"start": "09:30", "end": "15:55"},
                "safe_mode": False,
                "kill_switch": False,
            },
        }

        decision = run_ibkr_decision_cycle(
            gateway,
            symbol="MNQ",
            review_history=[],
            optimizer_history=[],
            state=state,
        )

        self.assertEqual(decision["bracket_event_type"], "bracket_order_built")
        self.assertEqual(gateway.bracket_order_report()["open_bracket_order_count"], 2)

    def test_decision_cycle_blocks_repeated_signal_regime_inside_cooldown(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())
        gateway.connect()
        gateway.record_contract_details(
            {
                "symbol": "MNQ",
                "tick_size": 0.25,
                "point_value": 2.0,
                "exchange": "CME",
                "currency": "USD",
            }
        )
        snapshots = _breakout_snapshots()
        for snapshot in snapshots:
            gateway.record_market_data(snapshot)
        first_now = datetime.fromisoformat(snapshots[-1]["snapshot_time"]) + timedelta(seconds=1)

        state = {
            "review_interval_seconds": 0,
            "market_data_history_limit": 100,
            "readiness_max_stale_seconds": 600,
            "signal_regime_cooldown_seconds": 300,
            "strategy": {
                "strategy_id": "mnq_1m_breakout",
                "strategy_spec_hash": "strategy-hash",
                "module_id": "range_breakout",
                "family": "range_breakout",
                "symbol": "MNQ",
                "timeframe": "1m",
                "lookback_bars": 5,
                "breakout_ticks": 1,
                "enabled": True,
                "tick_size": 0.25,
                "stop_loss_ticks": 20,
                "take_profit_ticks": 40,
                "max_holding_minutes": 20,
                "max_concurrent_positions": 4,
            },
            "control_state": {
                "mode": "paper",
                "min_confidence": 0.55,
                "max_spread_ticks": 2.0,
                "daily_trade_cap": 6,
                "strategies": {},
                "trade_session": {"start": "09:30", "end": "15:55"},
                "safe_mode": False,
                "kill_switch": False,
            },
        }

        first_decision = run_ibkr_decision_cycle(
            gateway,
            symbol="MNQ",
            review_history=[],
            optimizer_history=[],
            state=state,
            now=first_now,
        )
        first_signal_hash = state["last_planned_signal_hash"]
        first_regime_key = state["last_planned_signal_regime_key"]
        next_snapshot_time = first_now + timedelta(seconds=60)
        gateway.record_market_data(
            {
                "symbol": "MNQ",
                "bid": 106.75,
                "ask": 107.0,
                "last": 107.0,
                "market_data_type": "real_time",
                "snapshot_time": next_snapshot_time.isoformat(),
            }
        )

        second_decision = run_ibkr_decision_cycle(
            gateway,
            symbol="MNQ",
            review_history=[],
            optimizer_history=[],
            state=state,
            now=next_snapshot_time + timedelta(seconds=1),
        )

        self.assertEqual(first_decision["bracket_event_type"], "bracket_order_built")
        self.assertEqual(second_decision["review_result_action"], "paper_block")
        self.assertIsNone(second_decision["bracket_event_type"])
        self.assertTrue(second_decision["signal_regime_cooldown_active"])
        self.assertEqual(second_decision["signal_regime_key"], first_regime_key)
        self.assertNotEqual(state["latest_signal"]["signal_hash"], first_signal_hash)
        self.assertIn(
            "signal_regime_cooldown_active",
            state["latest_review"]["review_result"]["risk_review"]["blocked_reasons"],
        )
        self.assertEqual(gateway.bracket_order_report()["open_bracket_order_count"], 1)

    def test_decision_cycle_blocks_new_bracket_when_daily_trade_cap_is_reached(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())
        gateway.connect()
        gateway.record_contract_details(
            {
                "symbol": "MNQ",
                "tick_size": 0.25,
                "point_value": 2.0,
                "exchange": "CME",
                "currency": "USD",
            }
        )
        for snapshot in _breakout_snapshots():
            gateway.record_market_data(snapshot)
        gateway.build_bracket_order(
            {
                "symbol": "MNQ",
                "action": "BUY",
                "quantity": 1,
                "entry_order_type": "MKT",
                "reference_price": 100.0,
                "stop_price": 95.0,
                "take_profit_price": 110.0,
                "max_holding_minutes": 20,
            }
        )

        state = {
            "review_interval_seconds": 0,
            "market_data_history_limit": 100,
            "readiness_max_stale_seconds": 600,
            "strategy": {
                "strategy_id": "mnq_1m_breakout",
                "strategy_spec_hash": "strategy-hash",
                "module_id": "range_breakout",
                "family": "range_breakout",
                "symbol": "MNQ",
                "timeframe": "1m",
                "lookback_bars": 5,
                "breakout_ticks": 1,
                "enabled": True,
                "tick_size": 0.25,
                "stop_loss_ticks": 20,
                "take_profit_ticks": 40,
                "max_holding_minutes": 20,
                "max_concurrent_positions": 2,
            },
            "control_state": {
                "mode": "paper",
                "min_confidence": 0.55,
                "max_spread_ticks": 2.0,
                "daily_trade_cap": 1,
                "strategies": {},
                "trade_session": {"start": "09:30", "end": "15:55"},
                "safe_mode": False,
                "kill_switch": False,
            },
        }

        decision = run_ibkr_decision_cycle(
            gateway,
            symbol="MNQ",
            review_history=[],
            optimizer_history=[],
            state=state,
        )

        self.assertEqual(decision["review_result_action"], "paper_block")
        self.assertIsNone(decision["bracket_event_type"])
        self.assertEqual(gateway.bracket_order_report()["open_bracket_order_count"], 1)

    def test_signal_review_bracket_fill_pnl_and_fast_path_risk_reduction(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())
        gateway.connect()
        gateway.record_contract_details(
            {
                "symbol": "MNQ",
                "tick_size": 0.25,
                "point_value": 2.0,
                "exchange": "CME",
                "currency": "USD",
            }
        )
        snapshots = _breakout_snapshots()
        for snapshot in snapshots:
            gateway.record_market_data(snapshot)

        bars = build_one_minute_bars(snapshots)
        signal = build_signal_candidate(
            {
                "strategy_id": "mnq_1m_breakout",
                "strategy_spec_hash": "strategy-hash",
                "module_id": "range_breakout",
                "symbol": "MNQ",
                "timeframe": "1m",
                "lookback_bars": 5,
                "breakout_ticks": 1,
            },
            bars,
            max_spread_ticks=2,
        )
        review_request = build_five_minute_review_request(
            bars_1m=[bar.to_dict() for bar in bars[-5:]],
            signals=[signal],
            execution_ledger=gateway.execution_ledger(),
            strategy_state={"tick_size": 0.25, "stop_loss_ticks": 20, "take_profit_ticks": 40},
        )
        review = deterministic_fallback_review(review_request)
        bracket = gateway.build_bracket_order(review["paper_plan"])

        fill = gateway.record_execution_fill(
            {
                "order_id": bracket["details"]["bracket_order"]["parent_order_id"],
                "symbol": "MNQ",
                "side": "BUY",
                "quantity": 1,
                "fill_price": 106.0,
                "commission": 0.47,
                "realized_pnl": 8.5,
            }
        )
        gateway.record_position_snapshot({"symbol": "MNQ", "quantity": 0, "realized_pnl": 8.5})
        gateway.record_account_snapshot({"net_liquidation": 100008.03, "daily_pnl": 8.03, "realized_pnl": 8.5})

        loss_review_request = build_five_minute_review_request(
            bars_1m=[bar.to_dict() for bar in bars[-5:]],
            signals=[],
            execution_ledger=gateway.execution_ledger(),
            risk_context={"daily_loss_limit_hit": True},
        )
        loss_review = deterministic_fallback_review(loss_review_request)
        optimizer = apply_fast_path_control_diff({"mode": "paper", "safe_mode": False}, loss_review)

        self.assertEqual(signal["signal_class"], "strong_review")
        self.assertEqual(review["action"], "paper_allow")
        self.assertEqual(bracket["event_type"], "bracket_order_built")
        self.assertEqual(fill["event_type"], "execution_fill_recorded")
        self.assertEqual(gateway.execution_ledger()["net_realized_pnl"], 8.5)
        self.assertEqual(loss_review["action"], "paper_block")
        self.assertEqual(optimizer["status"], "applied")
        self.assertTrue(optimizer["control_state"]["safe_mode"])

    def test_ibkr_paper_run_artifacts_wrap_runtime_payloads(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())
        gateway.connect()
        report = build_ibkr_paper_report(
            run_id="paper-run-test",
            health=gateway.health(),
            readiness=gateway.readiness(),
            contracts=gateway.contract_readiness(),
            market_data=gateway.market_data_readiness(),
            bracket_orders=gateway.bracket_order_report(),
            execution_ledger=gateway.execution_ledger(),
            incidents={"incidents": gateway.incident_events, "count": len(gateway.incident_events)},
            one_minute_bars=[{"symbol": "MNQ", "bar_time": datetime.now(UTC).isoformat(), "close": 19000.0}],
            signals=[{"symbol": "MNQ", "signal_class": "strong_review", "side": "BUY"}],
            strategy_state={"strategy_id": "mnq_1m_breakout"},
            control_state={"mode": "paper"},
            poller={"review_cycle_count": 1},
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = create_ibkr_paper_run_artifacts(
                run_id="paper-run-test",
                root=Path(temp_dir),
                report=report,
            )
            loaded = load_ibkr_paper_report(Path(temp_dir), "paper-run-test")
            parquet_path = Path(manifest["artifacts"]["one_minute_bars"])
            self.assertTrue(parquet_path.exists())
            rows = duckdb.connect().execute(f"SELECT COUNT(*) FROM read_parquet('{str(parquet_path)}')").fetchone()[0]
            self.assertGreaterEqual(rows, 1)

        self.assertEqual(manifest["status"], "created")
        self.assertIn("daily_report", manifest["artifacts"])
        self.assertIn("one_minute_bars", manifest["artifacts"])
        self.assertEqual(loaded["execution_environment"], "ibkr_paper")
        self.assertFalse(loaded["live_execution_claim"])
        self.assertIn("contract:contract_details_missing", loaded["promotion_blockers"])

    def test_ibkr_paper_report_exposes_acceptance_evidence_gates(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())
        gateway.connect()
        acceptance_days = [
            "2026-04-21T14:30:00+00:00",
            "2026-04-22T14:30:00+00:00",
            "2026-04-23T14:30:00+00:00",
            "2026-04-24T14:30:00+00:00",
            "2026-04-25T14:30:00+00:00",
        ]
        fills = [
            {
                "execution_id": f"exec-{index}",
                "order_id": index,
                "symbol": "MNQ",
                "side": "BUY",
                "quantity": 1,
                "fill_price": 19000.0 + index,
                "commission": 0.47,
                "realized_pnl": 10.0,
                "filled_at": filled_at,
            }
            for index, filled_at in enumerate(acceptance_days, start=1)
        ]
        report = build_ibkr_paper_report(
            run_id="paper-run-acceptance",
            health=gateway.health(),
            readiness={"status": "ready", "missing_requirements": []},
            contracts=gateway.contract_readiness(),
            market_data={"status": "ready", "missing_requirements": []},
            bracket_orders={"order_event_count": 20, "recent_order_events": []},
            execution_ledger={
                "fills": fills,
                "latest_account_snapshot": {"recorded_at": acceptance_days[-1]},
                "net_realized_pnl": 50.0,
                "total_commission": 2.35,
            },
            incidents={"incidents": [], "count": 0},
            reviews=[],
            one_minute_bars=[],
            poller={
                "readiness_check_count": 100,
                "review_cycle_count": 30,
                "live_order_attempt_count": 0,
                "duplicate_order_event_count": 0,
            },
        )

        evidence = report["acceptance_evidence"]

        self.assertEqual(evidence["status"], "ready")
        self.assertEqual(evidence["trading_day_count"], 5)
        self.assertEqual(evidence["readiness_check_count"], 100)
        self.assertEqual(evidence["review_cycle_count"], 30)
        self.assertEqual(evidence["paper_order_lifecycle_event_count"], 20)
        self.assertEqual(evidence["gates"]["live_order_attempts"], "ready")
        self.assertEqual(evidence["gates"]["unexplained_duplicate_orders"], "ready")
        self.assertEqual(evidence["missing_requirements"], [])

    def test_decision_cycle_enters_safe_mode_when_market_data_is_stale(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())
        gateway.connect()
        gateway.record_contract_details(
            {
                "symbol": "MNQ",
                "tick_size": 0.25,
                "point_value": 2.0,
                "exchange": "CME",
                "currency": "USD",
            }
        )
        stale_time = datetime.now(UTC) - timedelta(minutes=6)
        for close in (100.0, 101.0, 102.0, 103.0, 104.0, 106.0):
            gateway.record_market_data(
                {
                    "symbol": "MNQ",
                    "bid": close - 0.25,
                    "ask": close,
                    "last": close,
                    "market_data_type": "real_time",
                    "snapshot_time": stale_time.isoformat(),
                }
            )

        review_history: list[dict] = []
        optimizer_history: list[dict] = []
        state = {
            "review_interval_seconds": 0,
            "market_data_history_limit": 100,
            "readiness_max_stale_seconds": 5,
            "strategy": {
                "strategy_id": "mnq_1m_breakout",
                "strategy_spec_hash": "strategy-hash",
                "module_id": "range_breakout",
                "family": "range_breakout",
                "symbol": "MNQ",
                "timeframe": "1m",
                "lookback_bars": 5,
                "breakout_ticks": 1,
                "enabled": True,
                "tick_size": 0.25,
                "stop_loss_ticks": 20,
                "take_profit_ticks": 40,
                "max_holding_minutes": 20,
            },
            "control_state": {
                "mode": "paper",
                "min_confidence": 0.55,
                "max_spread_ticks": 2.0,
                "daily_trade_cap": 6,
                "strategies": {},
                "trade_session": {"start": "09:30", "end": "15:55"},
                "safe_mode": False,
                "kill_switch": False,
            },
        }

        decision = run_ibkr_decision_cycle(
            gateway,
            symbol="MNQ",
            review_history=review_history,
            optimizer_history=optimizer_history,
            state=state,
        )

        self.assertEqual(decision["review_result_action"], "paper_block")
        self.assertTrue(gateway.safe_mode)
        self.assertEqual(optimizer_history[-1]["control_state"]["safe_mode"], True)


def _breakout_snapshots() -> list[dict]:
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 106.0]
    start = datetime.now(UTC) - timedelta(minutes=len(closes) - 1)
    snapshots = []
    for minute_offset, close in enumerate(closes):
        snapshots.append(
            {
                "symbol": "MNQ",
                "bid": close - 0.25,
                "ask": close,
                "last": close,
                "market_data_type": "real_time",
                "snapshot_time": (start + timedelta(minutes=minute_offset)).isoformat(),
            }
        )
    return snapshots


def _low_r_strategy() -> dict:
    return {
        "strategy_id": "mnq_1m_simple_robust_low_r",
        "strategy_spec_hash": "ibkr-paper-low-r:simple_robust_low_r",
        "module_id": "low_r_regime_basket",
        "family": "low_r_regime_basket",
        "symbol": "MNQ",
        "timeframe": "1m",
        "preset": "simple_robust_low_r",
        "enabled": True,
        "tick_size": 0.25,
        "stop_loss_ticks": 32,
        "take_profit_ticks": 40,
        "stop_range_multiple": 10.0,
        "min_stop_points": 8.0,
        "max_stop_points": 90.0,
        "max_holding_minutes": 300,
    }


def _expanded_prior_day_breakout_bars() -> list[OneMinuteBar]:
    bars: list[OneMinuteBar] = []
    prior_start = datetime(2026, 4, 24, 13, 30, tzinfo=UTC)
    for minute_offset in range(100):
        bar_time = prior_start + timedelta(minutes=minute_offset)
        close = 100.0 + minute_offset * 0.01
        bars.append(
            OneMinuteBar(
                symbol="MNQ",
                bar_time=bar_time,
                open=close - 0.05,
                high=close + 0.4,
                low=close - 0.4,
                close=close,
                bid=close - 0.25,
                ask=close,
                tick_count=10,
            )
        )
    current_start = datetime(2026, 4, 27, 13, 30, tzinfo=UTC)
    for minute_offset in range(120):
        bar_time = current_start + timedelta(minutes=minute_offset)
        close = 100.0 + minute_offset * 0.02
        bars.append(
            OneMinuteBar(
                symbol="MNQ",
                bar_time=bar_time,
                open=close - 0.05,
                high=close + 0.5,
                low=close - 0.5,
                close=close,
                bid=close - 0.25,
                ask=close,
                tick_count=10,
            )
        )
    final_close = 103.0
    bars.append(
        OneMinuteBar(
            symbol="MNQ",
            bar_time=current_start + timedelta(minutes=120),
            open=final_close - 0.1,
            high=final_close + 0.2,
            low=final_close - 0.3,
            close=final_close,
            bid=final_close - 0.25,
            ask=final_close,
            tick_count=10,
        )
    )
    return bars


def _low_r_opening_range_bars() -> list[OneMinuteBar]:
    start = datetime(2026, 4, 29, 13, 30, tzinfo=UTC)
    bars: list[OneMinuteBar] = []
    for minute_offset in range(216):
        bar_time = start + timedelta(minutes=minute_offset)
        base = 100.0 + minute_offset * 0.02
        bar_range = 0.2
        high = base + 0.1
        low = base - 0.1
        close = base
        tick_count = 10
        if minute_offset == 215:
            high = base + 0.1
            low = base - 0.05
            close = base + 0.08
        bars.append(
            OneMinuteBar(
                symbol="MNQ",
                bar_time=bar_time,
                open=base - 0.05,
                high=high,
                low=low,
                close=close,
                bid=close - 0.25,
                ask=close,
                tick_count=tick_count,
            )
        )
    return bars


def _snapshots_from_bar(bar: OneMinuteBar) -> list[dict]:
    tick_count = max(bar.tick_count, 4)
    prices = [bar.open, bar.high, bar.low, bar.close]
    if tick_count > 4:
        prices.extend([bar.close] * (tick_count - 4))
    snapshots = []
    for index, price in enumerate(prices[:tick_count]):
        snapshot_time = bar.bar_time + timedelta(seconds=min(index, 59))
        snapshots.append(
            {
                "symbol": bar.symbol,
                "bid": (bar.bid if bar.bid is not None else price - 0.25),
                "ask": (bar.ask if bar.ask is not None else price),
                "last": price,
                "market_data_type": "delayed",
                "snapshot_time": snapshot_time.isoformat(),
            }
        )
    return snapshots


if __name__ == "__main__":
    unittest.main()
