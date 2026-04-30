from __future__ import annotations

from types import SimpleNamespace
from types import ModuleType
import sys
import threading
import unittest

from tlm.ibkr_adapter import OfficialIbkrAdapter, _optional_broker_float


class FakeEWrapper:
    pass


class FakeEClient:
    def __init__(self, wrapper) -> None:
        self.wrapper = wrapper
        self.connected = False
        self.market_data_types: list[int] = []
        self.market_data_requests: list[dict] = []
        self.cancelled_requests: list[int] = []
        self.placed_orders: list[dict] = []

    def isConnected(self) -> bool:  # noqa: N802
        return self.connected

    def connect(self, host: str, port: int, client_id: int) -> None:
        self.connected = True

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

    def cancelMktData(self, req_id: int) -> None:  # noqa: N802
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
        adapter = OfficialIbkrAdapter()

        payload = adapter.request_market_data({"symbol": "MNQ"}, timeout_seconds=0)

        self.assertEqual(adapter.client.market_data_types, [3])
        self.assertEqual(adapter.client.market_data_requests[0]["symbol"], "MNQ")
        self.assertEqual(payload["market_data_type"], "unknown")

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


if __name__ == "__main__":
    unittest.main()
