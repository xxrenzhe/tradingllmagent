from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def build_ibkr_gateway_adapter() -> OfficialIbkrAdapter | None:
    try:
        return OfficialIbkrAdapter()
    except ModuleNotFoundError:
        return None


@dataclass
class OfficialIbkrAdapter:
    client: Any | None = None
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    connected_event: threading.Event = field(default_factory=threading.Event)
    next_valid_id_event: threading.Event = field(default_factory=threading.Event)
    positions_event: threading.Event = field(default_factory=threading.Event)
    next_order_id: int | None = None
    managed_accounts: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    _request_seq: int = 1000
    _account_summary: dict[int, dict[str, Any]] = field(default_factory=dict)
    _account_summary_events: dict[int, threading.Event] = field(default_factory=dict)
    _contract_details: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    _contract_detail_events: dict[int, threading.Event] = field(default_factory=dict)
    _market_data: dict[int, dict[str, Any]] = field(default_factory=dict)
    _market_data_events: dict[int, threading.Event] = field(default_factory=dict)
    _pnl: dict[int, dict[str, Any]] = field(default_factory=dict)
    _pnl_events: dict[int, threading.Event] = field(default_factory=dict)
    _positions: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._init_client()

    def connect(self, host: str, port: int, client_id: int) -> dict[str, Any]:
        if self.client is None:
            self._init_client()
        self.connected_event.clear()
        self.next_valid_id_event.clear()
        self.client.connect(host, port, client_id)
        self.thread = threading.Thread(target=self.client.run, name="ibkr-api", daemon=True)
        self.thread.start()
        self._wait(self.next_valid_id_event, 10.0, "timed_out_waiting_for_next_valid_id")
        self.connected_event.set()
        return {
            "connected": bool(self.client.isConnected()),
            "host": host,
            "port": port,
            "client_id": client_id,
            "next_valid_order_id": self.next_order_id,
            "managed_accounts": list(self.managed_accounts),
        }

    def disconnect(self) -> dict[str, Any]:
        if self.client is not None and self.client.isConnected():
            self.client.disconnect()
        self.connected_event.clear()
        return {"connected": False}

    def account_summary(self) -> dict[str, Any]:
        req_id = self._next_request_id()
        event = threading.Event()
        self._account_summary_events[req_id] = event
        self._account_summary[req_id] = {}
        self.client.reqAccountSummary(req_id, "All", "AccountType,NetLiquidation,RealizedPnL,UnrealizedPnL")
        self._wait(event, 10.0, "timed_out_waiting_for_account_summary")
        self.client.cancelAccountSummary(req_id)
        payload = self._account_summary.pop(req_id, {})
        self._account_summary_events.pop(req_id, None)
        account_id = str(payload.get("account_id") or (self.managed_accounts[0] if self.managed_accounts else ""))
        return {
            "account_id": account_id,
            "account_type": str(payload.get("AccountType") or "unknown"),
            "net_liquidation": _optional_float(payload.get("NetLiquidation")),
            "realized_pnl": _optional_float(payload.get("RealizedPnL")),
            "unrealized_pnl": _optional_float(payload.get("UnrealizedPnL")),
        }

    def request_contract_details(self, contract: dict[str, Any]) -> dict[str, Any]:
        req_id = self._next_request_id()
        event = threading.Event()
        self._contract_detail_events[req_id] = event
        self._contract_details[req_id] = []
        self.client.reqContractDetails(req_id, self._build_contract(contract))
        self._wait(event, 10.0, "timed_out_waiting_for_contract_details")
        rows = self._contract_details.pop(req_id, [])
        self._contract_detail_events.pop(req_id, None)
        if not rows:
            raise RuntimeError("no_contract_details_returned")
        return rows[0]

    def request_market_data(self, contract: dict[str, Any], timeout_seconds: int = 5) -> dict[str, Any]:
        req_id = self._next_request_id()
        event = threading.Event()
        self._market_data_events[req_id] = event
        self._market_data[req_id] = {
            "symbol": str(contract.get("symbol", "MNQ")),
            "market_data_type": "unknown",
            "snapshot_time": _now(),
        }
        self.client.reqMarketDataType(1)
        self.client.reqMktData(req_id, self._build_contract(contract), "", False, False, [])
        self._wait(event, float(timeout_seconds), "timed_out_waiting_for_market_data", raise_on_timeout=False)
        self.client.cancelMktData(req_id)
        payload = self._market_data.pop(req_id, {})
        self._market_data_events.pop(req_id, None)
        return payload

    def request_positions(self) -> list[dict[str, Any]]:
        self.positions_event.clear()
        self._positions = []
        self.client.reqPositions()
        self._wait(self.positions_event, 10.0, "timed_out_waiting_for_positions")
        self.client.cancelPositions()
        return list(self._positions)

    def request_account_snapshot(self, account_id: str | None = None) -> dict[str, Any]:
        summary = self.account_summary()
        resolved_account_id = account_id or str(summary.get("account_id") or "")
        req_id = self._next_request_id()
        event = threading.Event()
        self._pnl_events[req_id] = event
        self._pnl[req_id] = {}
        if resolved_account_id:
            self.client.reqPnL(req_id, resolved_account_id, "")
            self._wait(event, 5.0, "timed_out_waiting_for_account_pnl", raise_on_timeout=False)
            self.client.cancelPnL(req_id)
        pnl = self._pnl.pop(req_id, {})
        self._pnl_events.pop(req_id, None)
        return {
            "net_liquidation": summary.get("net_liquidation"),
            "daily_pnl": _optional_float(pnl.get("daily_pnl")),
            "realized_pnl": _optional_float(pnl.get("realized_pnl")) or summary.get("realized_pnl"),
            "unrealized_pnl": _optional_float(pnl.get("unrealized_pnl")) or summary.get("unrealized_pnl"),
            "drawdown_usage": None,
            "recorded_at": _now(),
        }

    def submit_bracket_order(self, contract: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
        parent_order_id = self._reserve_order_id()
        stop_order_id = self._reserve_order_id()
        take_profit_order_id = self._reserve_order_id()
        ib_contract = self._build_contract(contract)
        parent = self._build_parent_order(parent_order_id, order)
        stop = self._build_child_order(
            order_id=stop_order_id,
            parent_order_id=parent_order_id,
            action=_close_action(str(order.get("action", "BUY"))),
            order_type="STP",
            aux_price=float(order["stop_price"]),
            limit_price=None,
            quantity=int(order["quantity"]),
            transmit=False,
        )
        take_profit = self._build_child_order(
            order_id=take_profit_order_id,
            parent_order_id=parent_order_id,
            action=_close_action(str(order.get("action", "BUY"))),
            order_type="LMT",
            aux_price=None,
            limit_price=float(order["take_profit_price"]),
            quantity=int(order["quantity"]),
            transmit=True,
        )
        self.client.placeOrder(parent_order_id, ib_contract, parent)
        self.client.placeOrder(stop_order_id, ib_contract, stop)
        self.client.placeOrder(take_profit_order_id, ib_contract, take_profit)
        return {
            "submitted": True,
            "parent_order_id": parent_order_id,
            "stop_order_id": stop_order_id,
            "take_profit_order_id": take_profit_order_id,
            "submitted_at": _now(),
        }

    def _init_client(self) -> None:
        from ibapi.client import EClient
        from ibapi.wrapper import EWrapper

        adapter = self

        class Client(EWrapper, EClient):
            def __init__(self) -> None:
                EClient.__init__(self, self)

            def nextValidId(self, orderId: int) -> None:  # noqa: N802
                adapter.next_order_id = orderId
                adapter.next_valid_id_event.set()

            def managedAccounts(self, accountsList: str) -> None:  # noqa: N802
                adapter.managed_accounts = [item.strip() for item in accountsList.split(",") if item.strip()]

            def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str) -> None:  # noqa: N802
                payload = adapter._account_summary.setdefault(reqId, {})
                payload["account_id"] = account
                payload[tag] = value

            def accountSummaryEnd(self, reqId: int) -> None:  # noqa: N802
                event = adapter._account_summary_events.get(reqId)
                if event is not None:
                    event.set()

            def contractDetails(self, reqId: int, contractDetails: Any) -> None:  # noqa: N802
                adapter._contract_details.setdefault(reqId, []).append(
                    {
                        "symbol": contractDetails.contract.symbol,
                        "tick_size": float(contractDetails.minTick),
                        "point_value": float(contractDetails.contract.multiplier or 0.0),
                        "exchange": contractDetails.contract.exchange,
                        "currency": contractDetails.contract.currency,
                        "local_symbol": getattr(contractDetails.contract, "localSymbol", None),
                        "trading_class": getattr(contractDetails.contract, "tradingClass", None),
                    }
                )

            def contractDetailsEnd(self, reqId: int) -> None:  # noqa: N802
                event = adapter._contract_detail_events.get(reqId)
                if event is not None:
                    event.set()

            def marketDataType(self, reqId: int, marketDataType: int) -> None:  # noqa: N802
                payload = adapter._market_data.setdefault(reqId, {})
                payload["market_data_type"] = _market_data_type_name(marketDataType)

            def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any) -> None:  # noqa: N802
                payload = adapter._market_data.setdefault(reqId, {})
                if tickType == 1:
                    payload["bid"] = price
                elif tickType == 2:
                    payload["ask"] = price
                elif tickType == 4:
                    payload["last"] = price
                payload["snapshot_time"] = _now()
                event = adapter._market_data_events.get(reqId)
                if event is not None:
                    event.set()

            def position(self, account: str, contract: Any, pos: float, avgCost: float) -> None:  # noqa: N802
                adapter._positions.append(
                    {
                        "symbol": contract.symbol,
                        "quantity": int(pos),
                        "average_cost": avgCost,
                        "recorded_at": _now(),
                    }
                )

            def positionEnd(self) -> None:  # noqa: N802
                adapter.positions_event.set()

            def pnl(self, reqId: int, dailyPnL: float, unrealizedPnL: float, realizedPnL: float) -> None:  # noqa: N802
                adapter._pnl[reqId] = {
                    "daily_pnl": dailyPnL,
                    "unrealized_pnl": unrealizedPnL,
                    "realized_pnl": realizedPnL,
                }
                event = adapter._pnl_events.get(reqId)
                if event is not None:
                    event.set()

            def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:  # noqa: N802
                adapter.errors.append(
                    {
                        "req_id": reqId,
                        "error_code": errorCode,
                        "error": errorString,
                        "advanced_reject_json": advancedOrderRejectJson,
                        "created_at": _now(),
                    }
                )
                if reqId in adapter._account_summary_events:
                    adapter._account_summary_events[reqId].set()
                if reqId in adapter._contract_detail_events:
                    adapter._contract_detail_events[reqId].set()
                if reqId in adapter._market_data_events:
                    adapter._market_data_events[reqId].set()
                if reqId in adapter._pnl_events:
                    adapter._pnl_events[reqId].set()

        self.client = Client()

    def _next_request_id(self) -> int:
        with self.lock:
            self._request_seq += 1
            return self._request_seq

    def _reserve_order_id(self) -> int:
        with self.lock:
            if self.next_order_id is None:
                raise RuntimeError("next_valid_order_id_not_available")
            order_id = self.next_order_id
            self.next_order_id += 1
            return order_id

    def _wait(
        self,
        event: threading.Event,
        timeout_seconds: float,
        error_message: str,
        *,
        raise_on_timeout: bool = True,
    ) -> None:
        if event.wait(timeout_seconds):
            return
        if raise_on_timeout:
            raise RuntimeError(error_message)

    def _build_contract(self, payload: dict[str, Any]) -> Any:
        from ibapi.contract import Contract

        contract = Contract()
        contract.symbol = str(payload.get("symbol", "MNQ"))
        contract.secType = str(payload.get("secType") or payload.get("sec_type") or "FUT")
        contract.exchange = str(payload.get("exchange", "CME"))
        contract.currency = str(payload.get("currency", "USD"))
        if payload.get("lastTradeDateOrContractMonth"):
            contract.lastTradeDateOrContractMonth = str(payload["lastTradeDateOrContractMonth"])
        if payload.get("localSymbol"):
            contract.localSymbol = str(payload["localSymbol"])
        if payload.get("tradingClass"):
            contract.tradingClass = str(payload["tradingClass"])
        return contract

    def _build_parent_order(self, order_id: int, payload: dict[str, Any]) -> Any:
        from ibapi.order import Order

        order = Order()
        order.orderId = order_id
        if self.managed_accounts:
            order.account = self.managed_accounts[0]
        order.action = str(payload["action"]).upper()
        order.orderType = str(payload.get("entry_order_type", "MKT")).upper()
        order.totalQuantity = int(payload["quantity"])
        order.tif = "DAY"
        if order.orderType == "LMT":
            order.lmtPrice = float(payload["entry_price"])
        order.transmit = False
        return order

    def _build_child_order(
        self,
        *,
        order_id: int,
        parent_order_id: int,
        action: str,
        order_type: str,
        aux_price: float | None,
        limit_price: float | None,
        quantity: int,
        transmit: bool,
    ) -> Any:
        from ibapi.order import Order

        order = Order()
        order.orderId = order_id
        order.parentId = parent_order_id
        if self.managed_accounts:
            order.account = self.managed_accounts[0]
        order.action = action
        order.orderType = order_type
        order.totalQuantity = quantity
        order.tif = "DAY"
        if aux_price is not None:
            order.auxPrice = aux_price
        if limit_price is not None:
            order.lmtPrice = limit_price
        order.transmit = transmit
        return order


def _market_data_type_name(value: int) -> str:
    mapping = {
        1: "real_time",
        2: "frozen",
        3: "delayed",
        4: "delayed_frozen",
    }
    return mapping.get(value, f"unknown:{value}")


def _close_action(value: str) -> str:
    return "SELL" if value.upper() == "BUY" else "BUY"


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _now() -> str:
    return datetime.now(UTC).isoformat()
