from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from tlm.ibkr_gateway import IbkrContractSpec, IbkrPaperGateway
from tlm.config import get_cost_model, get_symbol


class FakeIbkrAdapter:
    def __init__(self, *, account_id: str = "DU1234567", account_type: str = "paper") -> None:
        self.account_id = account_id
        self.account_type = account_type
        self.connected = False
        self.disconnect_count = 0
        self.submitted_orders: list[dict] = []
        self.market_data_requests: list[dict] = []

    def connect(self, host: str, port: int, client_id: int) -> dict:
        self.connected = True
        return {
            "connected": True,
            "host": host,
            "port": port,
            "client_id": client_id,
            "next_valid_order_id": 9001,
        }

    def disconnect(self) -> dict:
        self.connected = False
        self.disconnect_count += 1
        return {"connected": False}

    def account_summary(self) -> dict:
        return {"account_id": self.account_id, "account_type": self.account_type}

    def request_contract_details(self, contract: dict) -> dict:
        return {
            "symbol": contract["symbol"],
            "tick_size": 0.25,
            "point_value": 2.0,
            "exchange": contract["exchange"],
            "currency": contract["currency"],
            "last_trade_date_or_contract_month": contract.get("lastTradeDateOrContractMonth") or "202506",
            "local_symbol": contract.get("localSymbol") or "MNQM6",
            "trading_class": contract.get("tradingClass") or "MNQ",
        }

    def request_market_data(self, contract: dict, timeout_seconds: int = 5) -> dict:
        self.market_data_requests.append(dict(contract))
        return {
            "symbol": contract["symbol"],
            "bid": 19000.0,
            "ask": 19000.25,
            "last": 19000.25,
            "market_data_type": "real_time",
            "snapshot_time": datetime.now(UTC).isoformat(),
        }

    def request_positions(self) -> list[dict]:
        return [{"symbol": "MNQ", "quantity": 1, "average_cost": 19000.25, "recorded_at": datetime.now(UTC).isoformat()}]

    def request_account_snapshot(self, account_id: str | None = None) -> dict:
        return {
            "net_liquidation": 100020.0,
            "daily_pnl": 20.0,
            "realized_pnl": 12.5,
            "unrealized_pnl": 8.0,
            "drawdown_usage": 0.02,
            "recorded_at": datetime.now(UTC).isoformat(),
        }

    def submit_bracket_order(self, contract: dict, order: dict) -> dict:
        payload = {
            "symbol": contract["symbol"],
            "last_trade_date_or_contract_month": contract.get("lastTradeDateOrContractMonth"),
            "local_symbol": contract.get("localSymbol"),
            "parent_order_id": 9001,
            "stop_order_id": 9002,
            "take_profit_order_id": 9003,
            "submitted": True,
        }
        self.submitted_orders.append(payload)
        return payload

    def drain_runtime_events(self) -> dict:
        return {
            "order_status": [
                {
                    "order_id": 9001,
                    "status": "Filled",
                    "filled": 1,
                    "remaining": 0,
                    "average_fill_price": 19000.25,
                }
            ],
            "executions": [
                {
                    "execution_id": "exec-live-1",
                    "order_id": 9001,
                    "symbol": "MNQ",
                    "side": "BUY",
                    "quantity": 1,
                    "fill_price": 19000.25,
                    "commission": 0.47,
                    "realized_pnl": 12.5,
                    "filled_at": datetime.now(UTC).isoformat(),
                }
            ],
        }


class IbkrPaperGatewayTests(unittest.TestCase):
    def ready_gateway(self) -> IbkrPaperGateway:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())
        gateway.connect()
        gateway.record_contract_details(
            {
                "symbol": "MNQ",
                "tick_size": 0.25,
                "point_value": 2.0,
                "exchange": "CME",
                "currency": "USD",
                "last_trade_date_or_contract_month": "202506",
                "local_symbol": "MNQM6",
                "trading_class": "MNQ",
            }
        )
        gateway.record_market_data(
            {
                "symbol": "MNQ",
                "bid": 19000.0,
                "ask": 19000.25,
                "last": 19000.25,
                "market_data_type": "real_time",
                "snapshot_time": datetime.now(UTC).isoformat(),
            }
        )
        return gateway

    def test_gateway_starts_blocked_without_adapter_or_connection(self) -> None:
        gateway = IbkrPaperGateway()

        health = gateway.health()
        readiness = gateway.readiness()
        connect = gateway.connect()

        self.assertEqual(health["mode"], "ibkr_paper")
        self.assertEqual(health["protocol_version"], "ibkr-paper.v1")
        self.assertFalse(health["live_trading_enabled"])
        self.assertEqual(readiness["status"], "blocked")
        self.assertIn("not_connected", readiness["missing_requirements"])
        self.assertEqual(connect["event_type"], "connection_rejected")
        self.assertIn("adapter_not_configured", connect["details"]["errors"])

    def test_connects_only_to_paper_account(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())

        event = gateway.connect({"host": "127.0.0.1", "port": 7497, "client_id": 21})
        health = gateway.health()
        readiness = gateway.readiness()

        self.assertEqual(event["event_type"], "connected")
        self.assertEqual(health["status"], "paper_connected")
        self.assertTrue(health["paper_account_verified"])
        self.assertEqual(health["account"]["account_type"], "paper")
        self.assertEqual(readiness["status"], "blocked")
        self.assertIn("contract:contract_details_missing", readiness["missing_requirements"])
        self.assertIn("market_data:market_data_missing", readiness["missing_requirements"])

    def test_du_account_prefix_is_treated_as_paper_even_without_literal_paper_type(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter(account_id="DU7654321", account_type="individual"))

        event = gateway.connect()

        self.assertEqual(event["event_type"], "connected")
        self.assertTrue(gateway.account is not None and gateway.account.is_paper)

    def test_contract_and_real_time_market_data_complete_readiness(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())

        gateway.connect()
        contract_event = gateway.record_contract_details(
            {
                "symbol": "MNQ",
                "tick_size": 0.25,
                "point_value": 2.0,
                "exchange": "CME",
                "currency": "USD",
                "last_trade_date_or_contract_month": "202506",
                "local_symbol": "MNQM6",
                "trading_class": "MNQ",
            }
        )
        market_event = gateway.record_market_data(
            {
                "symbol": "MNQ",
                "bid": 19000.0,
                "ask": 19000.25,
                "last": 19000.25,
                "market_data_type": "real_time",
                "snapshot_time": datetime.now(UTC).isoformat(),
            }
        )
        readiness = gateway.readiness(max_stale_seconds=30)

        self.assertEqual(contract_event["event_type"], "contract_details_recorded")
        self.assertEqual(contract_event["details"]["errors"], [])
        self.assertEqual(market_event["event_type"], "market_data_recorded")
        self.assertEqual(readiness["status"], "ready")
        self.assertEqual(readiness["contract"]["status"], "ready")
        self.assertEqual(readiness["market_data"]["status"], "ready")

    def test_market_data_readiness_blocks_delayed_stale_or_incomplete_quotes(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())
        old_time = datetime.now(UTC) - timedelta(seconds=60)

        gateway.record_market_data(
            {
                "symbol": "MNQ",
                "bid": 19000.0,
                "ask": None,
                "last": 19000.0,
                "market_data_type": "delayed",
                "snapshot_time": old_time.isoformat(),
            }
        )
        readiness = gateway.market_data_readiness("MNQ", max_stale_seconds=5)

        self.assertEqual(readiness["status"], "blocked")
        self.assertIn("market_data_not_real_time:delayed", readiness["missing_requirements"])
        self.assertIn("ask_missing", readiness["missing_requirements"])
        self.assertIn("spread_unavailable", readiness["missing_requirements"])
        self.assertIn("market_data_stale", readiness["missing_requirements"])

    def test_market_data_readiness_surfaces_ibkr_error_code(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())

        gateway.record_market_data(
            {
                "symbol": "MNQ",
                "market_data_type": "unknown",
                "snapshot_time": datetime.now(UTC).isoformat(),
                "error_code": 10168,
                "error_message": "Requested market data is not subscribed. Delayed market data is not enabled.",
            }
        )
        readiness = gateway.market_data_readiness("MNQ", max_stale_seconds=5)

        self.assertEqual(readiness["snapshot"]["error_code"], 10168)
        self.assertIn("market_data_error:10168", readiness["missing_requirements"])

    def test_contract_readiness_blocks_wrong_tick_or_point_value(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())

        event = gateway.record_contract_details(
            {
                "symbol": "MNQ",
                "tick_size": 0.5,
                "point_value": 20.0,
                "exchange": "CME",
                "currency": "USD",
            }
        )
        readiness = gateway.contract_readiness("MNQ")

        self.assertEqual(event["event_type"], "contract_details_recorded")
        self.assertTrue(gateway.safe_mode)
        self.assertIn("tick_size_mismatch:0.5:0.25", readiness["missing_requirements"])
        self.assertIn("point_value_mismatch:20.0:2.0", readiness["missing_requirements"])

    def test_rejects_live_trading_request_before_connecting(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())

        event = gateway.connect({"live_trading_enabled": True})

        self.assertEqual(event["event_type"], "connection_rejected")
        self.assertIn("live_trading_disabled", event["details"]["errors"])
        self.assertTrue(gateway.safe_mode)
        self.assertTrue(gateway.read_only)
        self.assertEqual(gateway.readiness()["status"], "blocked")

    def test_rejects_non_paper_account_and_enters_safe_mode(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter(account_id="U1234567", account_type="live"))

        event = gateway.connect()
        health = gateway.health()
        readiness = gateway.readiness()

        self.assertEqual(event["event_type"], "connection_rejected")
        self.assertIn("non_paper_account_blocked", event["details"]["errors"])
        self.assertFalse(health["connected"])
        self.assertTrue(health["safe_mode"])
        self.assertIn("non_paper_account_blocked", readiness["missing_requirements"])

    def test_disconnect_returns_to_readonly_offline_state(self) -> None:
        adapter = FakeIbkrAdapter()
        gateway = IbkrPaperGateway(adapter=adapter)

        gateway.connect()
        event = gateway.disconnect("test_complete")

        self.assertEqual(event["event_type"], "disconnected")
        self.assertFalse(gateway.connected)
        self.assertTrue(gateway.read_only)
        self.assertEqual(adapter.disconnect_count, 1)

    def test_sync_methods_use_adapter_snapshots(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())
        gateway.connect()

        contract = gateway.sync_contract_details("MNQ")
        market = gateway.sync_market_data("MNQ")
        positions = gateway.sync_positions()
        account = gateway.sync_account_snapshot()

        self.assertEqual(contract["event_type"], "contract_sync_completed")
        self.assertEqual(market["event_type"], "market_data_sync_completed")
        self.assertEqual(positions["event_type"], "positions_sync_completed")
        self.assertEqual(account["event_type"], "account_snapshot_sync_completed")
        self.assertEqual(gateway.contract_readiness("MNQ")["status"], "ready")
        self.assertEqual(gateway.market_data_readiness("MNQ")["status"], "ready")
        self.assertEqual(gateway.positions_report()["count"], 1)
        self.assertEqual(gateway.account_snapshots_report()["count"], 1)

    def test_sync_market_data_uses_resolved_front_month_contract(self) -> None:
        adapter = FakeIbkrAdapter()
        gateway = IbkrPaperGateway(adapter=adapter)
        gateway.connect()

        gateway.sync_contract_details("MNQ")
        gateway.sync_market_data("MNQ")

        self.assertEqual(adapter.market_data_requests[-1]["localSymbol"], "MNQM6")
        self.assertEqual(adapter.market_data_requests[-1]["lastTradeDateOrContractMonth"], "202506")

    def test_sync_runtime_events_updates_local_execution_ledger(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())
        gateway.connect()

        event = gateway.sync_runtime_events()

        self.assertEqual(event["event_type"], "runtime_event_sync_completed")
        self.assertEqual(gateway.executions_report()["count"], 1)
        self.assertEqual(gateway.execution_ledger()["net_realized_pnl"], 12.5)

    def test_default_mnq_config_and_cost_model_are_available(self) -> None:
        symbol = get_symbol("MNQ_IBKR")
        cost_model = get_cost_model("mnq_futures_v1")

        self.assertEqual(symbol.provider, "ibkr")
        self.assertEqual(symbol.instrument, "MNQ")
        self.assertEqual(symbol.tick_size, 0.25)
        self.assertEqual(symbol.point_value, 2)
        self.assertEqual(cost_model.tick_value, 0.5)

    def test_bracket_order_requires_full_readiness(self) -> None:
        gateway = IbkrPaperGateway(adapter=FakeIbkrAdapter())
        gateway.connect()

        event = gateway.build_bracket_order(
            {
                "symbol": "MNQ",
                "action": "BUY",
                "quantity": 1,
                "stop_price": 18995.0,
                "take_profit_price": 19010.0,
                "max_holding_minutes": 20,
            }
        )

        self.assertEqual(event["event_type"], "bracket_order_rejected")
        self.assertIn("contract:contract_details_missing", event["details"]["errors"])
        self.assertIn("market_data:market_data_missing", event["details"]["errors"])

    def test_builds_mnq_bracket_order_draft_after_readiness(self) -> None:
        gateway = self.ready_gateway()

        event = gateway.build_bracket_order(
            {
                "symbol": "MNQ",
                "action": "BUY",
                "quantity": 1,
                "entry_order_type": "MKT",
                "stop_price": 18995.0,
                "take_profit_price": 19010.0,
                "max_holding_minutes": 20,
            }
        )
        draft = event["details"]["bracket_order"]

        self.assertEqual(event["event_type"], "bracket_order_built")
        self.assertEqual(draft["symbol"], "MNQ")
        self.assertEqual(draft["action"], "BUY")
        self.assertEqual(draft["quantity"], 1)
        self.assertEqual(draft["parent_order_id"], 1)
        self.assertEqual(draft["stop_order_id"], 2)
        self.assertEqual(draft["take_profit_order_id"], 3)
        self.assertEqual(draft["transmit_sequence"][-1]["transmit"], True)
        report = gateway.bracket_order_report()
        self.assertEqual(report["open_bracket_order_count"], 1)
        self.assertEqual(report["open_bracket_orders"][0]["bracket_id"], draft["bracket_id"])
        self.assertEqual(report["order_event_count"], 1)

    def test_submits_bracket_order_through_adapter_after_readiness(self) -> None:
        gateway = self.ready_gateway()
        built = gateway.build_bracket_order(
            {
                "symbol": "MNQ",
                "action": "BUY",
                "quantity": 1,
                "entry_order_type": "MKT",
                "stop_price": 18995.0,
                "take_profit_price": 19010.0,
                "max_holding_minutes": 20,
            }
        )

        submitted = gateway.submit_bracket_order(built["details"]["bracket_order"]["bracket_id"])

        self.assertEqual(submitted["event_type"], "bracket_order_submitted")
        self.assertTrue(submitted["details"]["broker_submission"]["submitted"])
        self.assertEqual(submitted["details"]["broker_submission"]["local_symbol"], "MNQM6")

    def test_rejects_risk_increasing_or_invalid_bracket_payloads(self) -> None:
        gateway = self.ready_gateway()

        too_large = gateway.build_bracket_order(
            {
                "symbol": "MNQ",
                "action": "BUY",
                "quantity": 2,
                "stop_price": 18995.0,
                "take_profit_price": 19010.0,
                "max_holding_minutes": 20,
            }
        )
        bad_prices = gateway.build_bracket_order(
            {
                "symbol": "MNQ",
                "action": "BUY",
                "quantity": 1,
                "stop_price": 19005.0,
                "take_profit_price": 19010.0,
                "max_holding_minutes": 20,
            }
        )

        self.assertEqual(too_large["event_type"], "bracket_order_rejected")
        self.assertIn("quantity_must_be_one_mnq", too_large["details"]["errors"])
        self.assertEqual(bad_prices["event_type"], "bracket_order_rejected")
        self.assertIn("buy_bracket_prices_must_wrap_reference", bad_prices["details"]["errors"])

    def test_kill_switch_cancels_bracket_drafts_flattens_and_enters_safe_mode(self) -> None:
        gateway = self.ready_gateway()
        gateway.paper_position_quantity = 1
        gateway.build_bracket_order(
            {
                "symbol": "MNQ",
                "action": "BUY",
                "quantity": 1,
                "stop_price": 18995.0,
                "take_profit_price": 19010.0,
                "max_holding_minutes": 20,
            }
        )

        event = gateway.kill_switch("test")

        self.assertEqual(event["event_type"], "kill_switch_triggered")
        self.assertEqual(gateway.bracket_orders, {})
        self.assertEqual(gateway.paper_position_quantity, 0)
        self.assertTrue(gateway.safe_mode)

    def test_records_order_status_fills_positions_and_account_pnl(self) -> None:
        gateway = self.ready_gateway()

        status_event = gateway.record_order_status(
            {
                "order_id": 1,
                "status": "Filled",
                "filled": 1,
                "remaining": 0,
                "average_fill_price": 19000.25,
            }
        )
        fill_event = gateway.record_execution_fill(
            {
                "execution_id": "exec-1",
                "order_id": 1,
                "symbol": "MNQ",
                "side": "BUY",
                "quantity": 1,
                "fill_price": 19000.25,
                "commission": 0.47,
                "realized_pnl": 12.5,
                "filled_at": datetime.now(UTC).isoformat(),
            }
        )
        position_event = gateway.record_position_snapshot(
            {
                "symbol": "MNQ",
                "quantity": 1,
                "average_cost": 19000.25,
                "unrealized_pnl": 8.0,
                "realized_pnl": 12.5,
            }
        )
        account_event = gateway.record_account_snapshot(
            {
                "net_liquidation": 100020.0,
                "daily_pnl": 20.0,
                "realized_pnl": 12.5,
                "unrealized_pnl": 8.0,
                "drawdown_usage": 0.02,
            }
        )
        ledger = gateway.execution_ledger()

        self.assertEqual(status_event["event_type"], "order_status_recorded")
        self.assertEqual(fill_event["event_type"], "execution_fill_recorded")
        self.assertEqual(position_event["event_type"], "position_snapshot_recorded")
        self.assertEqual(account_event["event_type"], "account_snapshot_recorded")
        self.assertEqual(ledger["fill_count"], 1)
        self.assertEqual(ledger["net_realized_pnl"], 12.5)
        self.assertEqual(ledger["total_commission"], 0.47)
        self.assertEqual(ledger["positions"][0]["quantity"], 1)
        self.assertEqual(ledger["latest_account_snapshot"]["daily_pnl"], 20.0)

    def test_position_reconciliation_enters_safe_mode_on_drift(self) -> None:
        gateway = self.ready_gateway()
        gateway.record_position_snapshot({"symbol": "MNQ", "quantity": 1})

        report = gateway.reconcile_position("MNQ", expected_quantity=0)

        self.assertEqual(report["status"], "drift")
        self.assertEqual(report["actual_quantity"], 1)
        self.assertEqual(report["expected_quantity"], 0)
        self.assertTrue(gateway.safe_mode)


if __name__ == "__main__":
    unittest.main()
