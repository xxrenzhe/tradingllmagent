from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

import duckdb

from tlm.api import run_ibkr_decision_cycle
from tlm.ibkr_gateway import IbkrPaperGateway
from tlm.ibkr_optimizer import apply_fast_path_control_diff
from tlm.ibkr_paper import build_ibkr_paper_report, create_ibkr_paper_run_artifacts, load_ibkr_paper_report
from tlm.ibkr_review import build_five_minute_review_request, deterministic_fallback_review
from tlm.ibkr_signals import build_one_minute_bars, build_signal_candidate


class FakeIbkrAdapter:
    def __init__(self) -> None:
        self.connected = False

    def connect(self, host: str, port: int, client_id: int) -> dict:
        self.connected = True
        return {"connected": True, "host": host, "port": port, "client_id": client_id}

    def disconnect(self) -> dict:
        self.connected = False
        return {"connected": False}

    def account_summary(self) -> dict:
        return {"account_id": "DU1234567", "account_type": "paper"}


class IbkrPaperLoopTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
