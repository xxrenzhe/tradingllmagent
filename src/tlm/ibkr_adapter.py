from __future__ import annotations

import threading
import time
import socket
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
    connection_host: str | None = None
    connection_port: int | None = None
    connection_client_id: int | None = None
    socket_preflight_enabled: bool = True
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
    _historical_data: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    _historical_data_events: dict[int, threading.Event] = field(default_factory=dict)
    _pnl: dict[int, dict[str, Any]] = field(default_factory=dict)
    _pnl_events: dict[int, threading.Event] = field(default_factory=dict)
    _positions: list[dict[str, Any]] = field(default_factory=list)
    _runtime_order_status: list[dict[str, Any]] = field(default_factory=list)
    _executions_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    _undrained_execution_ids: set[str] = field(default_factory=set)
    _commission_reports: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._init_client()

    def connect(self, host: str, port: int, client_id: int) -> dict[str, Any]:
        if self.client is None:
            self._init_client()
        self.connection_host = host
        self.connection_port = port
        self.connection_client_id = client_id
        if self.client.isConnected():
            return {
                "connected": True,
                "host": host,
                "port": port,
                "client_id": client_id,
                "next_valid_order_id": self.next_order_id,
                "managed_accounts": list(self.managed_accounts),
            }
        self.connected_event.clear()
        self.next_valid_id_event.clear()
        self._check_socket_available(host, port)
        self.client.connect(host, port, client_id)
        if self.thread is None or not self.thread.is_alive():
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
        self.thread = None
        return {"connected": False}

    def account_summary(self) -> dict[str, Any]:
        self._ensure_connected()
        req_id = self._next_request_id()
        event = threading.Event()
        self._account_summary_events[req_id] = event
        self._account_summary[req_id] = {}
        timed_out = False
        try:
            self.client.reqAccountSummary(req_id, "All", "AccountType,NetLiquidation,RealizedPnL,UnrealizedPnL")
            try:
                self._wait(event, 10.0, "timed_out_waiting_for_account_summary")
            except RuntimeError as exc:
                if str(exc) != "timed_out_waiting_for_account_summary":
                    raise
                timed_out = True
            self.client.cancelAccountSummary(req_id)
            request_error = self._request_error(req_id)
            if request_error is not None and int(request_error.get("error_code") or 0) == 504:
                raise RuntimeError("not connected")
            payload = self._account_summary.pop(req_id, {})
        finally:
            self._account_summary_events.pop(req_id, None)
        account_id = str(payload.get("account_id") or (self.managed_accounts[0] if self.managed_accounts else ""))
        if timed_out and not payload and not account_id:
            raise RuntimeError("timed_out_waiting_for_account_summary")
        return {
            "account_id": account_id,
            "account_type": str(payload.get("AccountType") or ("paper" if account_id.upper().startswith("DU") else "unknown")),
            "net_liquidation": _optional_float(payload.get("NetLiquidation")),
            "realized_pnl": _optional_float(payload.get("RealizedPnL")),
            "unrealized_pnl": _optional_float(payload.get("UnrealizedPnL")),
        }

    def request_contract_details(self, contract: dict[str, Any]) -> dict[str, Any]:
        self._ensure_connected()
        req_id = self._next_request_id()
        event = threading.Event()
        self._contract_detail_events[req_id] = event
        self._contract_details[req_id] = []
        try:
            self.client.reqContractDetails(req_id, self._build_contract(contract))
            diagnostic = self._global_request_error("contract_details")
            if diagnostic is not None:
                raise RuntimeError(str(diagnostic["message"]))
            try:
                self._wait(event, 10.0, "timed_out_waiting_for_contract_details")
            except RuntimeError as exc:
                diagnostic = self._global_request_error("contract_details")
                if diagnostic is not None:
                    raise RuntimeError(str(diagnostic["message"])) from exc
                raise
            rows = self._contract_details.pop(req_id, [])
        finally:
            self._contract_details.pop(req_id, None)
            self._contract_detail_events.pop(req_id, None)
        if not rows:
            raise RuntimeError("no_contract_details_returned")
        return rows[0]

    def request_market_data(self, contract: dict[str, Any], timeout_seconds: int = 5) -> dict[str, Any]:
        self._ensure_connected()
        payload = self._request_market_data_once(contract, timeout_seconds=timeout_seconds)
        if int(payload.get("error_code") or 0) == 504:
            self._reconnect()
            payload = self._request_market_data_once(contract, timeout_seconds=timeout_seconds)
        return payload

    def request_historical_bars(
        self,
        contract: dict[str, Any],
        *,
        duration: str = "14400 S",
        bar_size: str = "1 min",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
        timeout_seconds: int = 20,
    ) -> list[dict[str, Any]]:
        return self._retry_after_not_connected(
            lambda: self._request_historical_bars_once(
                contract,
                duration=duration,
                bar_size=bar_size,
                what_to_show=what_to_show,
                use_rth=use_rth,
                timeout_seconds=timeout_seconds,
            )
        )

    def _request_market_data_once(self, contract: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
        req_id = self._next_request_id()
        event = threading.Event()
        self._market_data_events[req_id] = event
        self._market_data[req_id] = {
            "symbol": str(contract.get("symbol", "MNQ")),
            "market_data_type": "unknown",
            "snapshot_time": _now(),
            "error_code": None,
            "error_message": None,
        }
        self.client.reqMarketDataType(3)
        self.client.reqMktData(req_id, self._build_contract(contract), "", False, False, [])
        diagnostic = self._global_request_error("market_data")
        if diagnostic is not None:
            payload = self._market_data.setdefault(req_id, {})
            payload["error_code"] = diagnostic["error_code"]
            payload["error_message"] = diagnostic["message"]
        self._wait(event, float(timeout_seconds), "timed_out_waiting_for_market_data", raise_on_timeout=False)
        self.client.cancelMktData(req_id)
        payload = self._market_data.pop(req_id, {})
        self._market_data_events.pop(req_id, None)
        return payload

    def _request_historical_bars_once(
        self,
        contract: dict[str, Any],
        *,
        duration: str,
        bar_size: str,
        what_to_show: str,
        use_rth: bool,
        timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        self._ensure_connected()
        req_id = self._next_request_id()
        event = threading.Event()
        self._historical_data_events[req_id] = event
        self._historical_data[req_id] = []
        try:
            self.client.reqHistoricalData(
                req_id,
                self._build_contract(contract),
                "",
                _normalize_historical_duration(duration),
                bar_size,
                what_to_show,
                1 if use_rth else 0,
                1,
                False,
                [],
            )
            self._wait(event, float(timeout_seconds), "timed_out_waiting_for_historical_bars")
            request_error = self._request_error(req_id)
            rows = list(self._historical_data.pop(req_id, []))
            if request_error is not None:
                if int(request_error.get("error_code") or 0) == 504:
                    raise RuntimeError("not connected")
                if not rows:
                    raise RuntimeError(str(request_error.get("error") or "historical_data_request_failed"))
            symbol = str(contract.get("symbol", "MNQ"))
            for row in rows:
                row["symbol"] = str(row.get("symbol", symbol) or symbol)
            return rows
        finally:
            try:
                self.client.cancelHistoricalData(req_id)
            except Exception:
                pass
            self._historical_data.pop(req_id, None)
            self._historical_data_events.pop(req_id, None)

    def request_positions(self) -> list[dict[str, Any]]:
        return self._retry_after_not_connected(self._request_positions_once)

    def _request_positions_once(self) -> list[dict[str, Any]]:
        self._ensure_connected()
        self.positions_event.clear()
        self._positions = []
        self.client.reqPositions()
        try:
            self._wait(self.positions_event, 10.0, "timed_out_waiting_for_positions")
        except RuntimeError as exc:
            diagnostic = self._global_request_error("positions")
            if diagnostic is not None:
                raise RuntimeError(str(diagnostic["message"])) from exc
            raise
        self.client.cancelPositions()
        return list(self._positions)

    def request_account_snapshot(self, account_id: str | None = None) -> dict[str, Any]:
        return self._retry_after_not_connected(lambda: self._request_account_snapshot_once(account_id))

    def _request_account_snapshot_once(self, account_id: str | None = None) -> dict[str, Any]:
        self._ensure_connected()
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

    def cancel_order(self, order_id: int) -> dict[str, Any]:
        self.client.cancelOrder(order_id)
        return {"order_id": order_id, "cancelled": True, "cancelled_at": _now()}

    def submit_flatten_order(self, contract: dict[str, Any], action: str, quantity: int) -> dict[str, Any]:
        order_id = self._reserve_order_id()
        ib_contract = self._build_contract(contract)
        order = self._build_parent_order(
            order_id,
            {
                "action": action,
                "entry_order_type": "MKT",
                "quantity": quantity,
            },
        )
        order.transmit = True
        self.client.placeOrder(order_id, ib_contract, order)
        return {
            "submitted": True,
            "order_id": order_id,
            "action": action,
            "quantity": quantity,
            "submitted_at": _now(),
        }

    def drain_runtime_events(self) -> dict[str, Any]:
        with self.lock:
            executions = []
            for execution_id in sorted(self._undrained_execution_ids):
                payload = self._executions_by_id.get(execution_id)
                if payload is None:
                    continue
                commission = self._commission_reports.get(execution_id, {})
                executions.append(
                    {
                        **payload,
                        "commission": _optional_float(commission.get("commission")) or 0.0,
                        "realized_pnl": _optional_float(commission.get("realized_pnl")),
                    }
                )
                if execution_id in self._commission_reports:
                    self._commission_reports.pop(execution_id, None)
            drained = {
                "order_status": list(self._runtime_order_status),
                "executions": executions,
            }
            self._runtime_order_status.clear()
            self._undrained_execution_ids.clear()
            return drained

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
                        "last_trade_date_or_contract_month": getattr(
                            contractDetails.contract, "lastTradeDateOrContractMonth", None
                        ),
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
                if tickType in {1, 66}:
                    payload["bid"] = price
                elif tickType in {2, 67}:
                    payload["ask"] = price
                elif tickType in {4, 68}:
                    payload["last"] = price
                else:
                    return
                payload["snapshot_time"] = _now()
                event = adapter._market_data_events.get(reqId)
                if event is not None and all(payload.get(key) is not None for key in ("bid", "ask", "last")):
                    event.set()

            def historicalData(self, reqId: int, bar: Any) -> None:  # noqa: N802
                adapter._historical_data.setdefault(reqId, []).append(
                    {
                        "symbol": None,
                        "bar_time": _ibkr_bar_time_to_iso(getattr(bar, "date", "")),
                        "open": float(getattr(bar, "open", 0.0)),
                        "high": float(getattr(bar, "high", 0.0)),
                        "low": float(getattr(bar, "low", 0.0)),
                        "close": float(getattr(bar, "close", 0.0)),
                        "bid": None,
                        "ask": None,
                        "tick_count": int(getattr(bar, "barCount", 0) or getattr(bar, "volume", 0) or 0),
                    }
                )

            def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:  # noqa: N802
                event = adapter._historical_data_events.get(reqId)
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

            def orderStatus(  # noqa: N802
                self,
                orderId: int,
                status: str,
                filled: float,
                remaining: float,
                avgFillPrice: float,
                permId: int,
                parentId: int,
                lastFillPrice: float,
                clientId: int,
                whyHeld: str,
                mktCapPrice: float,
            ) -> None:
                with adapter.lock:
                    adapter._runtime_order_status.append(
                        {
                            "order_id": orderId,
                            "status": status,
                            "filled": filled,
                            "remaining": remaining,
                            "average_fill_price": avgFillPrice,
                            "why_held": whyHeld or None,
                        }
                    )

            def execDetails(self, reqId: int, contract: Any, execution: Any) -> None:  # noqa: N802
                with adapter.lock:
                    execution_id = str(execution.execId)
                    adapter._executions_by_id[execution_id] = {
                        "execution_id": str(execution.execId),
                        "order_id": int(execution.orderId),
                        "symbol": contract.symbol,
                        "side": str(execution.side).upper(),
                        "quantity": int(execution.shares),
                        "fill_price": float(execution.price),
                        "filled_at": _ibkr_time_to_iso(str(execution.time)),
                    }
                    adapter._undrained_execution_ids.add(execution_id)

            def commissionReport(self, commissionReport: Any) -> None:  # noqa: N802
                with adapter.lock:
                    execution_id = str(commissionReport.execId)
                    adapter._commission_reports[execution_id] = {
                        "commission": float(commissionReport.commission),
                        "realized_pnl": _optional_broker_float(commissionReport.realizedPNL),
                    }
                    if execution_id in adapter._executions_by_id:
                        adapter._undrained_execution_ids.add(execution_id)

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
                if errorCode == 504:
                    adapter.connected_event.clear()
                if reqId in adapter._account_summary_events:
                    adapter._account_summary_events[reqId].set()
                if reqId in adapter._contract_detail_events:
                    adapter._contract_detail_events[reqId].set()
                if reqId in adapter._market_data_events:
                    if _is_delayed_market_data_notice(errorCode, errorString):
                        return
                    payload = adapter._market_data.setdefault(reqId, {})
                    payload["error_code"] = errorCode
                    payload["error_message"] = errorString
                    adapter._market_data_events[reqId].set()
                if reqId in adapter._historical_data_events:
                    adapter._historical_data_events[reqId].set()
                if reqId in adapter._pnl_events:
                    adapter._pnl_events[reqId].set()

        self.client = Client()

    def _ensure_connected(self) -> None:
        if self.client is None:
            self._init_client()
        if self.client.isConnected():
            return
        self._reconnect()

    def _reconnect(self) -> None:
        if self.connection_host is None or self.connection_port is None or self.connection_client_id is None:
            raise RuntimeError("adapter_not_connected")
        if self.client is None:
            self._init_client()
        try:
            if self.client.isConnected():
                self.client.disconnect()
        except Exception:
            pass
        self.connected_event.clear()
        self.next_valid_id_event.clear()
        self._check_socket_available(self.connection_host, self.connection_port)
        self.client.connect(self.connection_host, self.connection_port, self.connection_client_id)
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.client.run, name="ibkr-api", daemon=True)
            self.thread.start()
        self._wait(self.next_valid_id_event, 10.0, "timed_out_waiting_for_next_valid_id")
        self.connected_event.set()

    def _retry_after_not_connected(self, operation):
        try:
            return operation()
        except Exception as exc:
            if not _is_not_connected_error(exc):
                raise
            self._reconnect()
            return operation()

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

    def _request_error(self, req_id: int) -> dict[str, Any] | None:
        for error in reversed(self.errors):
            if int(error.get("req_id") or -1) == req_id:
                return error
        return None

    def _global_request_error(self, context: str) -> dict[str, Any] | None:
        for error in reversed(self.errors):
            if int(error.get("req_id") or -1) != -1:
                continue
            if not _is_recent_error(error):
                continue
            code = int(error.get("error_code") or 0)
            message = str(error.get("error") or "")
            lowered = message.lower()
            if _is_connectivity_restored_message(code, lowered):
                continue
            if context == "contract_details" and (
                code == 2157 or _is_broken_farm_message(lowered, "secdef") or _is_broken_farm_message(lowered, "sec-def")
            ):
                return {"error_code": code, "message": f"ibkr_secdef_farm_unavailable:{message}"}
            if context == "market_data" and (code == 2103 or _is_broken_farm_message(lowered, "market data farm")):
                return {"error_code": code, "message": f"ibkr_market_data_farm_unavailable:{message}"}
            if code == 2110:
                return {"error_code": code, "message": f"ibkr_connectivity_broken:{message}"}
        return None

    def _check_socket_available(self, host: str, port: int) -> None:
        if not self.socket_preflight_enabled:
            return
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return
        except OSError as exc:
            raise RuntimeError(f"ibkr_api_socket_not_listening:{host}:{port}") from exc

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
        return self._apply_compatible_order_flags(order)

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
        return self._apply_compatible_order_flags(order)

    def _apply_compatible_order_flags(self, order: Any) -> Any:
        if hasattr(order, "eTradeOnly"):
            order.eTradeOnly = False
        if hasattr(order, "firmQuoteOnly"):
            order.firmQuoteOnly = False
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


def _optional_broker_float(value: Any) -> float | None:
    parsed = _optional_float(value)
    if parsed is None:
        return None
    if abs(parsed) >= 1e307:
        return None
    return parsed


def _is_not_connected_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "not connected" in message or "error 504" in message or message.strip() == "504"


def _is_connectivity_restored_message(error_code: int, message: str) -> bool:
    if error_code in {2104, 2106, 2158}:
        return True
    return "connection is ok" in message or "connection is restored" in message


def _is_delayed_market_data_notice(error_code: int, message: str) -> bool:
    return error_code == 10167 and "displaying delayed market data" in message.lower()


def _is_broken_farm_message(message: str, farm_name: str) -> bool:
    return farm_name in message and ("broken" in message or "inactive" in message or "disconnected" in message)


def _is_recent_error(error: dict[str, Any], *, max_age_seconds: float = 120.0) -> bool:
    created_at = error.get("created_at")
    if not created_at:
        return True
    try:
        parsed = datetime.fromisoformat(str(created_at))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (datetime.now(UTC) - parsed).total_seconds() <= max_age_seconds


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ibkr_time_to_iso(value: str) -> str:
    cleaned = value.strip()
    for pattern in ("%Y%m%d  %H:%M:%S", "%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(cleaned, pattern).replace(tzinfo=UTC).isoformat()
        except ValueError:
            continue
    return _now()


def _ibkr_bar_time_to_iso(value: Any) -> str:
    if value is None:
        return _now()
    cleaned = str(value).strip()
    if cleaned.isdigit():
        try:
            return datetime.fromtimestamp(int(cleaned), tz=UTC).isoformat()
        except ValueError:
            return _now()
    local_tz = datetime.now().astimezone().tzinfo
    for pattern in ("%Y%m%d  %H:%M:%S", "%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(cleaned, pattern)
            if local_tz is not None:
                parsed = parsed.replace(tzinfo=local_tz).astimezone(UTC)
            else:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.isoformat()
        except ValueError:
            continue
    return _ibkr_time_to_iso(cleaned)


def _normalize_historical_duration(value: str) -> str:
    cleaned = str(value).strip().upper()
    if not cleaned:
        return "14400 S"
    parts = cleaned.split()
    if len(parts) == 2 and parts[0].isdigit():
        quantity = int(parts[0])
        unit = parts[1]
        if unit in {"S", "D", "W", "M", "Y"}:
            return f"{quantity} {unit}"
        if unit == "H":
            return f"{quantity * 3600} S"
        if unit == "MIN":
            return f"{quantity * 60} S"
    return cleaned
