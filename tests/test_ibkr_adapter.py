from __future__ import annotations

from types import SimpleNamespace
from types import ModuleType
import sys
import threading
from datetime import UTC, datetime
import unittest

from tlm.ibkr_adapter import OfficialIbkrAdapter, _ibkr_time_to_iso, _optional_broker_float


class FakeEWrapper:
    pass


class FakeEClient:
    def __init__(self, wrapper) -> None:
        self.wrapper = wrapper
        self.connected = False
        self.connect_calls: list[dict] = []
        self.market_data_error_once = False
        self.positions_error_once = False
        self.account_summary_error_once = False
        self.account_summary_timeout = False
        self.contract_details_requests: list[dict] = []
        self.market_data_types: list[int] = []
        self.market_data_requests: list[dict] = []
        self.historical_data_requests: list[dict] = []
        self.cancelled_requests: list[int] = []
        self.placed_orders: list[dict] = []
        self.position_requests = 0
        self.account_summary_requests = 0

    def isConnected(self) -> bool:  # noqa: N802
        return self.connected

    def connect(self, host: str, port: int, client_id: int) -> None:
        self.connected = True
        self.connect_calls.append({"host": host, "port": port, "client_id": client_id})
        self.wrapper.nextValidId(100 + len(self.connect_calls))
        self.wrapper.managedAccounts("DU1234567")

    def disconnect(self) -> None:
        self.connected = False

    def run(self) -> None:
        return

    def reqMarketDataType(self, market_data_type: int) -> None:  # noqa: N802
        self.market_data_types.append(market_data_type)

    def reqMktData(self, req_id: int, contract, generic_ticks: str, snapshot: bool, regulatory_snapshot: bool, options) -> None:  # noqa: N802
        self.market_data_requests.append(
            {
                "req_id": req_id,
                "symbol": getattr(contract, "symbol", None),
                "generic_ticks": generic_ticks,
                "snapshot": snapshot,
            }
        )
        if self.market_data_error_once:
            self.market_data_error_once = False
            self.connected = False
            self.wrapper.error(req_id, 504, "Not connected")
            return
        self.wrapper.marketDataType(req_id, 3)
        self.wrapper.tickPrice(req_id, 66, 19000.0, None)
        self.wrapper.tickPrice(req_id, 67, 19000.25, None)
        self.wrapper.tickPrice(req_id, 68, 19000.25, None)

    def cancelMktData(self, req_id: int) -> None:  # noqa: N802
        self.cancelled_requests.append(req_id)

    def reqHistoricalData(  # noqa: N802
        self,
        req_id: int,
        contract,
        end_datetime: str,
        duration: str,
        bar_size: str,
        what_to_show: str,
        use_rth: int,
        format_date: int,
        keep_up_to_date: bool,
        options,
    ) -> None:
        self.historical_data_requests.append(
            {
                "req_id": req_id,
                "symbol": getattr(contract, "symbol", None),
                "duration": duration,
                "bar_size": bar_size,
                "what_to_show": what_to_show,
                "use_rth": use_rth,
            }
        )
        self.wrapper.historicalData(
            req_id,
            SimpleNamespace(
                date="20260430  09:30:00",
                open=19000.0,
                high=19005.0,
                low=18998.0,
                close=19004.0,
                barCount=12,
                volume=120,
            ),
        )
        self.wrapper.historicalData(
            req_id,
            SimpleNamespace(
                date="20260430  09:31:00",
                open=19004.0,
                high=19008.0,
                low=19003.0,
                close=19007.0,
                barCount=10,
                volume=100,
            ),
        )
        self.wrapper.historicalDataEnd(req_id, "20260430 09:30:00", "20260430 09:31:00")

    def cancelHistoricalData(self, req_id: int) -> None:  # noqa: N802
        self.cancelled_requests.append(req_id)

    def cancelOrder(self, order_id: int) -> None:  # noqa: N802
        return

    def placeOrder(self, order_id: int, contract, order) -> None:  # noqa: N802
        self.placed_orders.append(
            {
                "order_id": order_id,
                "contract": contract,
                "order": order,
            }
        )

    def reqPositions(self) -> None:  # noqa: N802
        self.position_requests += 1
        if self.positions_error_once:
            self.positions_error_once = False
            self.connected = False
            raise RuntimeError("Not connected")
        self.wrapper.position("DU1234567", SimpleNamespace(symbol="MNQ"), 0, 0.0)
        self.wrapper.positionEnd()

    def cancelPositions(self) -> None:  # noqa: N802
        return

    def reqAccountSummary(self, req_id: int, group_name: str, tags: str) -> None:  # noqa: N802
        self.account_summary_requests += 1
        if self.account_summary_error_once:
            self.account_summary_error_once = False
            self.connected = False
            self.wrapper.error(req_id, 504, "Not connected")
            return
        if self.account_summary_timeout:
            return
        for tag, value in {
            "AccountType": "INDIVIDUAL",
            "NetLiquidation": "100000.0",
            "RealizedPnL": "10.0",
            "UnrealizedPnL": "2.0",
        }.items():
            self.wrapper.accountSummary(req_id, "DU1234567", tag, value, "USD")
        self.wrapper.accountSummaryEnd(req_id)

    def cancelAccountSummary(self, req_id: int) -> None:  # noqa: N802
        return

    def reqContractDetails(self, req_id: int, contract) -> None:  # noqa: N802
        self.contract_details_requests.append(
            {
                "req_id": req_id,
                "symbol": getattr(contract, "symbol", None),
                "last_trade_date_or_contract_month": getattr(contract, "lastTradeDateOrContractMonth", None),
                "local_symbol": getattr(contract, "localSymbol", None),
            }
        )
        contract.multiplier = "2"
        self.wrapper.contractDetails(req_id, SimpleNamespace(contract=contract, minTick=0.25))
        self.wrapper.contractDetailsEnd(req_id)

    def reqPnL(self, req_id: int, account: str, model_code: str) -> None:  # noqa: N802
        self.wrapper.pnl(req_id, 12.0, 2.0, 10.0)

    def cancelPnL(self, req_id: int) -> None:  # noqa: N802
        return


class FakeContract:
    symbol = ""
    secType = ""
    exchange = ""
    currency = ""
    lastTradeDateOrContractMonth = ""
    localSymbol = ""
    tradingClass = ""


class FakeOrder:
    def __init__(self) -> None:
        self.eTradeOnly = True
        self.firmQuoteOnly = True


class OfficialIbkrAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_modules = dict(sys.modules)
        client_module = ModuleType("ibapi.client")
        client_module.EClient = FakeEClient
        wrapper_module = ModuleType("ibapi.wrapper")
        wrapper_module.EWrapper = FakeEWrapper
        contract_module = ModuleType("ibapi.contract")
        contract_module.Contract = FakeContract
        order_module = ModuleType("ibapi.order")
        order_module.Order = FakeOrder
        ibapi_module = ModuleType("ibapi")
        sys.modules["ibapi"] = ibapi_module
        sys.modules["ibapi.client"] = client_module
        sys.modules["ibapi.wrapper"] = wrapper_module
        sys.modules["ibapi.contract"] = contract_module
        sys.modules["ibapi.order"] = order_module

    def tearDown(self) -> None:
        sys.modules.clear()
        sys.modules.update(self.original_modules)

    def test_request_market_data_uses_delayed_market_data_type(self) -> None:
        adapter = OfficialIbkrAdapter(socket_preflight_enabled=False)
        adapter.connect("127.0.0.1", 7497, 11)

        payload = adapter.request_market_data({"symbol": "MNQ"}, timeout_seconds=1)

        self.assertEqual(adapter.client.market_data_types, [3])
        self.assertEqual(adapter.client.market_data_requests[0]["symbol"], "MNQ")
        self.assertEqual(payload["market_data_type"], "delayed")

    def test_request_market_data_reconnects_once_after_504(self) -> None:
        adapter = OfficialIbkrAdapter(socket_preflight_enabled=False)
        adapter.connect("127.0.0.1", 7497, 11)
        adapter.client.market_data_error_once = True

        payload = adapter.request_market_data({"symbol": "MNQ"}, timeout_seconds=1)

        self.assertEqual(len(adapter.client.connect_calls), 2)
        self.assertEqual(len(adapter.client.market_data_requests), 2)
        self.assertEqual(payload["market_data_type"], "delayed")
        self.assertEqual(payload["bid"], 19000.0)

    def test_request_historical_bars_returns_one_minute_rows(self) -> None:
        adapter = OfficialIbkrAdapter(socket_preflight_enabled=False)
        adapter.connect("127.0.0.1", 7497, 11)

        rows = adapter.request_historical_bars({"symbol": "MNQ"}, duration="2 H", timeout_seconds=1)

        self.assertEqual(len(adapter.client.historical_data_requests), 1)
        self.assertEqual(adapter.client.historical_data_requests[0]["duration"], "7200 S")
        self.assertEqual(rows[0]["symbol"], "MNQ")
        expected_time = (
            datetime.strptime("20260430  09:30:00", "%Y%m%d  %H:%M:%S")
            .replace(tzinfo=datetime.now().astimezone().tzinfo)
            .astimezone(UTC)
            .isoformat()
        )
        self.assertEqual(rows[0]["bar_time"], expected_time)
        self.assertEqual(rows[1]["tick_count"], 10)

    def test_request_positions_reconnects_after_not_connected_exception(self) -> None:
        adapter = OfficialIbkrAdapter(socket_preflight_enabled=False)
        adapter.connect("127.0.0.1", 7497, 11)
        adapter.client.positions_error_once = True

        positions = adapter.request_positions()

        self.assertEqual(len(adapter.client.connect_calls), 2)
        self.assertEqual(adapter.client.position_requests, 2)
        self.assertEqual(positions[0]["symbol"], "MNQ")

    def test_account_snapshot_reconnects_after_account_summary_504(self) -> None:
        adapter = OfficialIbkrAdapter(socket_preflight_enabled=False)
        adapter.connect("127.0.0.1", 7497, 11)
        adapter.client.account_summary_error_once = True

        snapshot = adapter.request_account_snapshot()

        self.assertEqual(len(adapter.client.connect_calls), 2)
        self.assertEqual(adapter.client.account_summary_requests, 2)
        self.assertEqual(snapshot["net_liquidation"], 100000.0)
        self.assertEqual(snapshot["daily_pnl"], 12.0)

    def test_account_summary_falls_back_to_managed_paper_account_on_timeout(self) -> None:
        adapter = OfficialIbkrAdapter(socket_preflight_enabled=False)
        adapter.connect("127.0.0.1", 7497, 11)
        adapter.client.account_summary_timeout = True

        summary = adapter.account_summary()

        self.assertEqual(summary["account_id"], "DU1234567")
        self.assertEqual(summary["account_type"], "paper")
        self.assertIsNone(summary["net_liquidation"])

    def test_contract_details_reports_secdef_farm_unavailable(self) -> None:
        adapter = OfficialIbkrAdapter(socket_preflight_enabled=False)
        adapter.connect("127.0.0.1", 7497, 11)
        adapter.client.wrapper.error(-1, 2157, "Sec-def data farm connection is broken:secdefhk")

        with self.assertRaisesRegex(RuntimeError, "ibkr_secdef_farm_unavailable"):
            adapter.request_contract_details({"symbol": "MNQ", "lastTradeDateOrContractMonth": "202606"})

    def test_market_data_farm_ok_message_is_not_recorded_as_error(self) -> None:
        adapter = OfficialIbkrAdapter(socket_preflight_enabled=False)
        adapter.connect("127.0.0.1", 7497, 11)
        adapter.client.wrapper.error(-1, 2104, "Market data farm connection is OK:usfarm")

        payload = adapter.request_market_data({"symbol": "MNQ"}, timeout_seconds=1)

        self.assertEqual(payload["market_data_type"], "delayed")
        self.assertIsNone(payload["error_code"])
        self.assertIsNone(payload["error_message"])

    def test_delayed_market_data_notice_does_not_complete_snapshot_before_ticks(self) -> None:
        adapter = OfficialIbkrAdapter()
        event = threading.Event()
        adapter._market_data[1] = {"symbol": "MNQ"}
        adapter._market_data_events[1] = event

        adapter.client.error(1, 10167, "Requested market data is not subscribed. Displaying delayed market data.")

        self.assertFalse(event.is_set())
        self.assertNotIn("error_code", adapter._market_data[1])

    def test_market_data_waits_for_bid_ask_and_last_before_completion(self) -> None:
        adapter = OfficialIbkrAdapter()
        event = threading.Event()
        adapter._market_data[1] = {"symbol": "MNQ"}
        adapter._market_data_events[1] = event

        adapter.client.tickPrice(1, 9, 999.0, None)
        adapter.client.tickPrice(1, 66, 27592.5, None)
        adapter.client.tickPrice(1, 67, 27593.0, None)

        self.assertFalse(event.is_set())

        adapter.client.tickPrice(1, 68, 27592.25, None)

        self.assertTrue(event.is_set())

    def test_secdef_farm_ok_message_is_not_recorded_as_error(self) -> None:
        adapter = OfficialIbkrAdapter(socket_preflight_enabled=False)
        adapter.connect("127.0.0.1", 7497, 11)
        adapter.client.wrapper.error(-1, 2158, "Sec-def data farm connection is OK:secdefhk")

        payload = adapter.request_contract_details({"symbol": "MNQ", "lastTradeDateOrContractMonth": "202606"})

        self.assertEqual(payload["symbol"], "MNQ")

    def test_delayed_tick_types_populate_bid_ask_and_last(self) -> None:
        adapter = OfficialIbkrAdapter()
        adapter._market_data[1] = {"symbol": "MNQ"}
        adapter._market_data_events[1] = threading.Event()

        adapter.client.tickPrice(1, 66, 27592.5, None)
        adapter.client.tickPrice(1, 67, 27593.0, None)
        adapter.client.tickPrice(1, 68, 27592.25, None)

        self.assertEqual(adapter._market_data[1]["bid"], 27592.5)
        self.assertEqual(adapter._market_data[1]["ask"], 27593.0)
        self.assertEqual(adapter._market_data[1]["last"], 27592.25)

    def test_submit_flatten_order_sets_compatible_flags_and_transmits(self) -> None:
        adapter = OfficialIbkrAdapter()
        adapter.next_order_id = 77

        result = adapter.submit_flatten_order(
            {
                "symbol": "MNQ",
                "exchange": "CME",
                "currency": "USD",
                "lastTradeDateOrContractMonth": "202506",
                "localSymbol": "MNQM6",
                "tradingClass": "MNQ",
            },
            action="SELL",
            quantity=1,
        )

        self.assertEqual(result["order_id"], 77)
        self.assertEqual(len(adapter.client.placed_orders), 1)
        placed = adapter.client.placed_orders[0]["order"]
        self.assertEqual(placed.action, "SELL")
        self.assertEqual(placed.orderType, "MKT")
        self.assertEqual(placed.totalQuantity, 1)
        self.assertTrue(placed.transmit)
        self.assertFalse(placed.eTradeOnly)
        self.assertFalse(placed.firmQuoteOnly)

    def test_optional_broker_float_drops_ibkr_sentinel_value(self) -> None:
        self.assertIsNone(_optional_broker_float(""))
        self.assertIsNone(_optional_broker_float(1.7976931348623157e308))
        self.assertEqual(_optional_broker_float("12.5"), 12.5)

    def test_ibkr_execution_time_interprets_naive_value_as_local_time(self) -> None:
        local_tz = datetime.now().astimezone().tzinfo
        expected = datetime(2026, 5, 1, 21, 30, 4).replace(tzinfo=local_tz).astimezone(UTC).isoformat()

        self.assertEqual(_ibkr_time_to_iso("20260501  21:30:04"), expected)

    def test_drain_runtime_events_merges_out_of_order_commission_reports(self) -> None:
        adapter = OfficialIbkrAdapter()

        adapter.client.commissionReport(
            SimpleNamespace(
                execId="exec-1",
                commission=0.62,
                realizedPNL=1.5,
            )
        )
        adapter.client.execDetails(
            1,
            SimpleNamespace(symbol="MNQ"),
            SimpleNamespace(
                execId="exec-1",
                orderId=91,
                side="BOT",
                shares=1,
                price=19000.25,
                time="20260430  10:00:00",
            ),
        )

        first = adapter.drain_runtime_events()
        second = adapter.drain_runtime_events()

        self.assertEqual(len(first["executions"]), 1)
        self.assertEqual(first["executions"][0]["commission"], 0.62)
        self.assertEqual(first["executions"][0]["realized_pnl"], 1.5)
        self.assertEqual(second["executions"], [])

    def test_connect_reports_api_socket_not_listening_before_ibapi_wait(self) -> None:
        adapter = OfficialIbkrAdapter()

        with self.assertRaisesRegex(RuntimeError, "ibkr_api_socket_not_listening:127.0.0.1:1"):
            adapter.connect("127.0.0.1", 1, 11)


if __name__ == "__main__":
    unittest.main()
