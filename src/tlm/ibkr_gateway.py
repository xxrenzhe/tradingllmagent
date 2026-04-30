from __future__ import annotations

import importlib.util
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4


IBKR_PROTOCOL_VERSION = "ibkr-paper.v1"


class IbkrGatewayAdapter(Protocol):
    def connect(self, host: str, port: int, client_id: int) -> dict[str, Any]: ...

    def disconnect(self) -> dict[str, Any]: ...

    def account_summary(self) -> dict[str, Any]: ...

    def request_contract_details(self, contract: dict[str, Any]) -> dict[str, Any]: ...

    def request_market_data(self, contract: dict[str, Any], timeout_seconds: int = 5) -> dict[str, Any]: ...

    def request_positions(self) -> list[dict[str, Any]]: ...

    def request_account_snapshot(self, account_id: str | None = None) -> dict[str, Any]: ...

    def submit_bracket_order(self, contract: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]: ...

    def cancel_order(self, order_id: int) -> dict[str, Any]: ...

    def drain_runtime_events(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class IbkrContractSpec:
    symbol: str = "MNQ"
    sec_type: str = "FUT"
    exchange: str = "CME"
    currency: str = "USD"
    quantity: int = 1
    last_trade_date_or_contract_month: str | None = None
    local_symbol: str | None = None
    trading_class: str | None = "MNQ"
    expected_tick_size: float = 0.25
    expected_point_value: float = 2.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "secType": self.sec_type,
            "exchange": self.exchange,
            "currency": self.currency,
            "quantity": self.quantity,
            "lastTradeDateOrContractMonth": self.last_trade_date_or_contract_month,
            "localSymbol": self.local_symbol,
            "tradingClass": self.trading_class,
            "expected_tick_size": self.expected_tick_size,
            "expected_point_value": self.expected_point_value,
        }


@dataclass(frozen=True)
class IbkrContractDetails:
    symbol: str
    tick_size: float
    point_value: float
    exchange: str
    currency: str
    last_trade_date_or_contract_month: str | None = None
    local_symbol: str | None = None
    trading_class: str | None = None

    def validate_against(self, spec: IbkrContractSpec) -> list[str]:
        errors = []
        if self.symbol != spec.symbol:
            errors.append(f"contract_symbol_mismatch:{self.symbol}:{spec.symbol}")
        if self.exchange != spec.exchange:
            errors.append(f"contract_exchange_mismatch:{self.exchange}:{spec.exchange}")
        if self.currency != spec.currency:
            errors.append(f"contract_currency_mismatch:{self.currency}:{spec.currency}")
        if abs(self.tick_size - spec.expected_tick_size) > 1e-9:
            errors.append(f"tick_size_mismatch:{self.tick_size}:{spec.expected_tick_size}")
        if abs(self.point_value - spec.expected_point_value) > 1e-9:
            errors.append(f"point_value_mismatch:{self.point_value}:{spec.expected_point_value}")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "tick_size": self.tick_size,
            "point_value": self.point_value,
            "exchange": self.exchange,
            "currency": self.currency,
            "last_trade_date_or_contract_month": self.last_trade_date_or_contract_month,
            "local_symbol": self.local_symbol,
            "trading_class": self.trading_class,
        }


@dataclass(frozen=True)
class IbkrMarketDataSnapshot:
    symbol: str
    bid: float | None
    ask: float | None
    last: float | None
    market_data_type: str
    snapshot_time: datetime
    error_code: int | None = None
    error_message: str | None = None

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def real_time(self) -> bool:
        return self.market_data_type == "real_time"

    @property
    def order_ready(self) -> bool:
        return self.market_data_type in {"real_time", "delayed", "delayed_frozen"}

    def age_seconds(self, now: datetime | None = None) -> float:
        current = now or datetime.now(UTC)
        return max((current - self.snapshot_time).total_seconds(), 0.0)

    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bid": self.bid,
            "ask": self.ask,
            "last": self.last,
            "spread": self.spread,
            "market_data_type": self.market_data_type,
            "real_time": self.real_time,
            "order_ready": self.order_ready,
            "snapshot_time": self.snapshot_time.isoformat(),
            "age_seconds": self.age_seconds(now),
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class IbkrBracketOrderDraft:
    bracket_id: str
    symbol: str
    action: str
    quantity: int
    entry_order_type: str
    entry_price: float | None
    stop_price: float
    take_profit_price: float
    max_holding_minutes: int
    parent_order_id: int
    stop_order_id: int
    take_profit_order_id: int
    account_id_hash: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "bracket_id": self.bracket_id,
            "symbol": self.symbol,
            "action": self.action,
            "quantity": self.quantity,
            "entry_order_type": self.entry_order_type,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "take_profit_price": self.take_profit_price,
            "max_holding_minutes": self.max_holding_minutes,
            "parent_order_id": self.parent_order_id,
            "stop_order_id": self.stop_order_id,
            "take_profit_order_id": self.take_profit_order_id,
            "account_id_hash": self.account_id_hash,
            "created_at": self.created_at,
            "transmit_sequence": [
                {"order_id": self.parent_order_id, "role": "entry", "transmit": False},
                {"order_id": self.stop_order_id, "role": "stop_loss", "transmit": False},
                {"order_id": self.take_profit_order_id, "role": "take_profit", "transmit": True},
            ],
        }


@dataclass(frozen=True)
class IbkrExecutionFill:
    execution_id: str
    order_id: int
    symbol: str
    side: str
    quantity: int
    fill_price: float
    commission: float
    realized_pnl: float | None
    filled_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "fill_price": self.fill_price,
            "commission": self.commission,
            "realized_pnl": self.realized_pnl,
            "filled_at": self.filled_at.isoformat(),
        }


@dataclass(frozen=True)
class IbkrPositionSnapshot:
    symbol: str
    quantity: int
    average_cost: float | None
    unrealized_pnl: float | None
    realized_pnl: float | None
    recorded_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "average_cost": self.average_cost,
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "recorded_at": self.recorded_at.isoformat(),
        }


@dataclass(frozen=True)
class IbkrAccountSnapshot:
    net_liquidation: float | None
    daily_pnl: float | None
    realized_pnl: float | None
    unrealized_pnl: float | None
    drawdown_usage: float | None
    recorded_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "net_liquidation": self.net_liquidation,
            "daily_pnl": self.daily_pnl,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "drawdown_usage": self.drawdown_usage,
            "recorded_at": self.recorded_at.isoformat(),
        }


@dataclass(frozen=True)
class IbkrConnectionConfig:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 11

    def to_dict(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port, "client_id": self.client_id}


@dataclass(frozen=True)
class IbkrPaperAccount:
    account_id: str
    account_type: str

    @property
    def is_paper(self) -> bool:
        return self.account_type.lower() == "paper" or self.account_id.upper().startswith("DU")

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id_hash": _redacted_hash(self.account_id),
            "account_type": self.account_type,
            "paper": self.is_paper,
        }


@dataclass
class IbkrPaperGateway:
    adapter: IbkrGatewayAdapter | None = None
    config: IbkrConnectionConfig = field(default_factory=IbkrConnectionConfig)
    account: IbkrPaperAccount | None = None
    connected: bool = False
    read_only: bool = True
    safe_mode: bool = False
    live_trading_enabled: bool = False
    sequence: int = 0
    connection_events: list[dict[str, Any]] = field(default_factory=list)
    incident_events: list[dict[str, Any]] = field(default_factory=list)
    contract_specs: dict[str, IbkrContractSpec] = field(default_factory=lambda: {"MNQ": IbkrContractSpec()})
    contract_details: dict[str, IbkrContractDetails] = field(default_factory=dict)
    market_data: dict[str, IbkrMarketDataSnapshot] = field(default_factory=dict)
    bracket_orders: dict[str, IbkrBracketOrderDraft] = field(default_factory=dict)
    submitted_bracket_order_ids: set[str] = field(default_factory=set)
    order_events: list[dict[str, Any]] = field(default_factory=list)
    next_order_id: int = 1
    paper_position_quantity: int = 0
    order_status_events: list[dict[str, Any]] = field(default_factory=list)
    executions: list[IbkrExecutionFill] = field(default_factory=list)
    positions: dict[str, IbkrPositionSnapshot] = field(default_factory=dict)
    account_snapshots: list[IbkrAccountSnapshot] = field(default_factory=list)

    def health(self) -> dict[str, Any]:
        return {
            "status": self._status(),
            "mode": "ibkr_paper",
            "protocol_version": IBKR_PROTOCOL_VERSION,
            "ibapi_available": ibapi_available(),
            "adapter_configured": self.adapter is not None,
            "connected": self.connected,
            "read_only": self.read_only,
            "safe_mode": self.safe_mode,
            "live_trading_enabled": self.live_trading_enabled,
            "paper_account_verified": bool(self.account and self.account.is_paper),
            "account": self.account.to_dict() if self.account else None,
            "sequence": self.sequence,
            "checked_at": _now(),
        }

    def readiness(self, symbol: str = "MNQ", max_stale_seconds: int = 5) -> dict[str, Any]:
        missing = []
        if not ibapi_available() and self.adapter is None:
            missing.append("ibapi_not_installed")
        if not self.connected:
            missing.append("not_connected")
        if not self.account:
            missing.append("account_not_loaded")
        elif not self.account.is_paper:
            missing.append("non_paper_account_blocked")
        if self.live_trading_enabled:
            missing.append("live_trading_enabled_blocked")
        if self.safe_mode:
            missing.append("gateway_safe_mode")
        contract_report = self.contract_readiness(symbol)
        market_report = self.market_data_readiness(symbol, max_stale_seconds=max_stale_seconds)
        missing.extend(f"contract:{item}" for item in contract_report["missing_requirements"])
        missing.extend(f"market_data:{item}" for item in market_report["missing_requirements"])
        return {
            "status": "ready" if not missing else "blocked",
            "mode": "ibkr_paper",
            "paper_only": True,
            "missing_requirements": missing,
            "contract": contract_report,
            "market_data": market_report,
            "checked_at": _now(),
        }

    def connect(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        if payload.get("live_trading_enabled") is True:
            self.enter_safe_mode("live_trading_requested")
            return self._event("connection_rejected", {"errors": ["live_trading_disabled"]})
        self.config = IbkrConnectionConfig(
            host=str(payload.get("host", self.config.host)),
            port=int(payload.get("port", self.config.port)),
            client_id=int(payload.get("client_id", self.config.client_id)),
        )
        if self.adapter is None:
            return self._event("connection_rejected", {"errors": ["adapter_not_configured"]})
        try:
            connection = self.adapter.connect(self.config.host, self.config.port, self.config.client_id)
            summary = self.adapter.account_summary()
        except Exception as exc:
            self.enter_safe_mode("adapter_connect_failed")
            return self._event("connection_failed", {"errors": [str(exc)]})
        self.connected = bool(connection.get("connected", True))
        self.account = IbkrPaperAccount(
            account_id=str(summary.get("account_id", "")),
            account_type=str(summary.get("account_type", "")),
        )
        if not self.account.is_paper:
            self.connected = False
            self.enter_safe_mode("non_paper_account_blocked")
            return self._event(
                "connection_rejected",
                {"errors": ["non_paper_account_blocked"], "account": self.account.to_dict()},
            )
        self.read_only = False
        self.safe_mode = False
        return self._event("connected", {"config": self.config.to_dict(), "account": self.account.to_dict()})

    def disconnect(self, reason: str = "manual") -> dict[str, Any]:
        if self.adapter is not None:
            self.adapter.disconnect()
        self.connected = False
        self.read_only = True
        return self._event("disconnected", {"reason": reason})

    def register_contract_spec(self, spec: IbkrContractSpec) -> dict[str, Any]:
        self.contract_specs[spec.symbol] = spec
        return self._event("contract_spec_registered", {"contract": spec.to_dict()})

    def sync_contract_details(self, symbol: str = "MNQ") -> dict[str, Any]:
        if self.adapter is None:
            return self._event("contract_sync_rejected", {"errors": ["adapter_not_configured"]})
        spec = self.contract_specs.get(symbol)
        if spec is None:
            return self._event("contract_sync_rejected", {"errors": [f"unknown_symbol:{symbol}"]})
        try:
            payload = self.adapter.request_contract_details(spec.to_dict())
        except Exception as exc:
            self.enter_safe_mode("adapter_contract_sync_failed")
            return self._event("contract_sync_failed", {"errors": [str(exc)], "symbol": symbol})
        event = self.record_contract_details(payload)
        return self._event("contract_sync_completed", {"symbol": symbol, "recorded_event": event})

    def record_contract_details(self, payload: dict[str, Any]) -> dict[str, Any]:
        details = IbkrContractDetails(
            symbol=str(payload.get("symbol", "MNQ")),
            tick_size=float(payload["tick_size"]),
            point_value=float(payload["point_value"]),
            exchange=str(payload.get("exchange", "CME")),
            currency=str(payload.get("currency", "USD")),
            last_trade_date_or_contract_month=_none_if_blank(payload.get("last_trade_date_or_contract_month")),
            local_symbol=payload.get("local_symbol"),
            trading_class=payload.get("trading_class"),
        )
        self.contract_details[details.symbol] = details
        spec = self.contract_specs.get(details.symbol, IbkrContractSpec(symbol=details.symbol))
        errors = details.validate_against(spec)
        if errors:
            self.enter_safe_mode("contract_details_mismatch")
        return self._event(
            "contract_details_recorded",
            {"contract": spec.to_dict(), "details": details.to_dict(), "errors": errors},
        )

    def contract_readiness(self, symbol: str = "MNQ") -> dict[str, Any]:
        spec = self.contract_specs.get(symbol)
        details = self.contract_details.get(symbol)
        missing = []
        errors = []
        if spec is None:
            missing.append("contract_spec_missing")
        if details is None:
            missing.append("contract_details_missing")
        if spec is not None and details is not None:
            errors = details.validate_against(spec)
        return {
            "status": "ready" if not missing and not errors else "blocked",
            "symbol": symbol,
            "contract": spec.to_dict() if spec else None,
            "details": details.to_dict() if details else None,
            "missing_requirements": [*missing, *errors],
        }

    def record_market_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        symbol = str(payload.get("symbol", "MNQ"))
        snapshot = IbkrMarketDataSnapshot(
            symbol=symbol,
            bid=_optional_float(payload.get("bid")),
            ask=_optional_float(payload.get("ask")),
            last=_optional_float(payload.get("last")),
            market_data_type=str(payload.get("market_data_type", "unknown")),
            snapshot_time=_parse_datetime(payload.get("snapshot_time")),
            error_code=_optional_int(payload.get("error_code")),
            error_message=_none_if_blank(payload.get("error_message")),
        )
        self.market_data[symbol] = snapshot
        return self._event("market_data_recorded", {"snapshot": snapshot.to_dict()})

    def sync_market_data(self, symbol: str = "MNQ", timeout_seconds: int = 5) -> dict[str, Any]:
        if self.adapter is None:
            return self._event("market_data_sync_rejected", {"errors": ["adapter_not_configured"]})
        resolved_contract = self.resolved_contract(symbol)
        if resolved_contract is None:
            return self._event("market_data_sync_rejected", {"errors": [f"unknown_symbol:{symbol}"]})
        try:
            payload = self.adapter.request_market_data(resolved_contract, timeout_seconds=timeout_seconds)
        except Exception as exc:
            self.enter_safe_mode("adapter_market_data_sync_failed")
            return self._event("market_data_sync_failed", {"errors": [str(exc)], "symbol": symbol})
        event = self.record_market_data(payload)
        return self._event("market_data_sync_completed", {"symbol": symbol, "recorded_event": event})

    def market_data_readiness(
        self,
        symbol: str = "MNQ",
        *,
        max_stale_seconds: int = 5,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        snapshot = self.market_data.get(symbol)
        missing = []
        if snapshot is None:
            missing.append("market_data_missing")
            return {
                "status": "blocked",
                "symbol": symbol,
                "snapshot": None,
                "missing_requirements": missing,
            }
        if not snapshot.order_ready:
            missing.append(f"market_data_not_order_ready:{snapshot.market_data_type}")
        if snapshot.error_code is not None:
            missing.append(f"market_data_error:{snapshot.error_code}")
        if snapshot.bid is None:
            missing.append("bid_missing")
        if snapshot.ask is None:
            missing.append("ask_missing")
        if snapshot.last is None:
            missing.append("last_missing")
        if snapshot.spread is None:
            missing.append("spread_unavailable")
        elif snapshot.spread < 0:
            missing.append("negative_spread")
        if snapshot.age_seconds(now) > max_stale_seconds:
            missing.append("market_data_stale")
        return {
            "status": "ready" if not missing else "blocked",
            "symbol": symbol,
            "snapshot": snapshot.to_dict(now),
            "max_stale_seconds": max_stale_seconds,
            "missing_requirements": missing,
        }

    def build_bracket_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        symbol = str(payload.get("symbol", "MNQ"))
        readiness = self.readiness(symbol=symbol, max_stale_seconds=int(payload.get("max_stale_seconds", 5)))
        if readiness["status"] != "ready":
            return self._order_event(
                "bracket_order_rejected",
                {"errors": readiness["missing_requirements"], "readiness": readiness},
            )
        errors = self._bracket_payload_errors(payload, symbol)
        if errors:
            return self._order_event("bracket_order_rejected", {"errors": errors})
        action = str(payload["action"]).upper()
        quantity = _positive_int(payload.get("quantity", 1))
        entry_order_type = str(payload.get("entry_order_type", "MKT")).upper()
        entry_price = _optional_float(payload.get("entry_price"))
        stop_price = float(payload["stop_price"])
        take_profit_price = float(payload["take_profit_price"])
        max_holding_minutes = _positive_int(payload["max_holding_minutes"])
        parent_order_id = self._next_order_id()
        draft = IbkrBracketOrderDraft(
            bracket_id=f"ibkr_bracket_{uuid4().hex}",
            symbol=symbol,
            action=action,
            quantity=quantity,
            entry_order_type=entry_order_type,
            entry_price=entry_price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            max_holding_minutes=max_holding_minutes,
            parent_order_id=parent_order_id,
            stop_order_id=self._next_order_id(),
            take_profit_order_id=self._next_order_id(),
            account_id_hash=self.account.to_dict()["account_id_hash"] if self.account else "",
            created_at=_now(),
        )
        self.bracket_orders[draft.bracket_id] = draft
        return self._order_event("bracket_order_built", {"bracket_order": draft.to_dict()})

    def cancel_open_orders(self, reason: str = "manual") -> dict[str, Any]:
        cancelled = [draft.to_dict() for draft in self.bracket_orders.values()]
        broker_cancellations = []
        if self.adapter is not None:
            for bracket_id, draft in list(self.bracket_orders.items()):
                if bracket_id not in self.submitted_bracket_order_ids:
                    continue
                for order_id in (draft.parent_order_id, draft.stop_order_id, draft.take_profit_order_id):
                    try:
                        broker_cancellations.append(self.adapter.cancel_order(order_id))
                    except Exception as exc:
                        broker_cancellations.append({"order_id": order_id, "cancelled": False, "error": str(exc)})
                self.submitted_bracket_order_ids.discard(bracket_id)
        self.bracket_orders.clear()
        return self._order_event(
            "open_orders_cancelled",
            {"reason": reason, "orders": cancelled, "broker_cancellations": broker_cancellations},
        )

    def flatten_paper_position(self, reason: str = "manual") -> dict[str, Any]:
        previous = self.paper_position_quantity
        self.paper_position_quantity = 0
        return self._order_event("paper_position_flattened", {"reason": reason, "previous_quantity": previous})

    def kill_switch(self, reason: str = "manual") -> dict[str, Any]:
        cancel_event = self.cancel_open_orders(reason)
        flatten_event = self.flatten_paper_position(reason)
        incident = self.enter_safe_mode(f"kill_switch:{reason}")
        return {
            "event_type": "kill_switch_triggered",
            "mode": "ibkr_paper",
            "reason": reason,
            "cancel_event": cancel_event,
            "flatten_event": flatten_event,
            "incident": incident,
            "created_at": _now(),
        }

    def record_order_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = self._order_event(
            "order_status_recorded",
            {
                "order_id": int(payload["order_id"]),
                "status": str(payload["status"]),
                "filled": _optional_float(payload.get("filled")),
                "remaining": _optional_float(payload.get("remaining")),
                "average_fill_price": _optional_float(payload.get("average_fill_price")),
                "why_held": payload.get("why_held"),
            },
        )
        self.order_status_events.append(event)
        return event

    def record_execution_fill(self, payload: dict[str, Any]) -> dict[str, Any]:
        fill = IbkrExecutionFill(
            execution_id=str(payload.get("execution_id") or f"exec_{uuid4().hex}"),
            order_id=int(payload["order_id"]),
            symbol=str(payload.get("symbol", "MNQ")),
            side=str(payload["side"]).upper(),
            quantity=_positive_int(payload["quantity"]),
            fill_price=float(payload["fill_price"]),
            commission=float(payload.get("commission", 0.0)),
            realized_pnl=_optional_float(payload.get("realized_pnl")),
            filled_at=_parse_datetime(payload.get("filled_at")),
        )
        if fill.side not in {"BUY", "SELL"}:
            return self._order_event("execution_fill_rejected", {"errors": [f"unsupported_side:{fill.side}"]})
        signed = fill.quantity if fill.side == "BUY" else -fill.quantity
        if fill.symbol == "MNQ":
            self.paper_position_quantity += signed
        self.executions.append(fill)
        return self._order_event("execution_fill_recorded", {"fill": fill.to_dict()})

    def record_position_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = IbkrPositionSnapshot(
            symbol=str(payload.get("symbol", "MNQ")),
            quantity=int(payload.get("quantity", 0)),
            average_cost=_optional_float(payload.get("average_cost")),
            unrealized_pnl=_optional_float(payload.get("unrealized_pnl")),
            realized_pnl=_optional_float(payload.get("realized_pnl")),
            recorded_at=_parse_datetime(payload.get("recorded_at")),
        )
        self.positions[snapshot.symbol] = snapshot
        if snapshot.symbol == "MNQ":
            self.paper_position_quantity = snapshot.quantity
        return self._order_event("position_snapshot_recorded", {"position": snapshot.to_dict()})

    def record_account_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = IbkrAccountSnapshot(
            net_liquidation=_optional_float(payload.get("net_liquidation")),
            daily_pnl=_optional_float(payload.get("daily_pnl")),
            realized_pnl=_optional_float(payload.get("realized_pnl")),
            unrealized_pnl=_optional_float(payload.get("unrealized_pnl")),
            drawdown_usage=_optional_float(payload.get("drawdown_usage")),
            recorded_at=_parse_datetime(payload.get("recorded_at")),
        )
        self.account_snapshots.append(snapshot)
        return self._order_event("account_snapshot_recorded", {"account_snapshot": snapshot.to_dict()})

    def sync_positions(self) -> dict[str, Any]:
        if self.adapter is None:
            return self._order_event("positions_sync_rejected", {"errors": ["adapter_not_configured"]})
        try:
            payloads = self.adapter.request_positions()
        except Exception as exc:
            self.enter_safe_mode("adapter_positions_sync_failed")
            return self._order_event("positions_sync_failed", {"errors": [str(exc)]})
        events = [self.record_position_snapshot(payload) for payload in payloads]
        return self._order_event("positions_sync_completed", {"count": len(events), "positions": payloads})

    def sync_account_snapshot(self) -> dict[str, Any]:
        if self.adapter is None:
            return self._order_event("account_snapshot_sync_rejected", {"errors": ["adapter_not_configured"]})
        try:
            payload = self.adapter.request_account_snapshot(
                self.account.account_id if self.account is not None else None
            )
        except Exception as exc:
            self.enter_safe_mode("adapter_account_snapshot_sync_failed")
            return self._order_event("account_snapshot_sync_failed", {"errors": [str(exc)]})
        event = self.record_account_snapshot(payload)
        return self._order_event("account_snapshot_sync_completed", {"recorded_event": event})

    def sync_runtime_events(self) -> dict[str, Any]:
        if self.adapter is None:
            return self._order_event("runtime_event_sync_rejected", {"errors": ["adapter_not_configured"]})
        try:
            payload = self.adapter.drain_runtime_events()
        except Exception as exc:
            self.enter_safe_mode("adapter_runtime_event_sync_failed")
            return self._order_event("runtime_event_sync_failed", {"errors": [str(exc)]})
        order_status_events = [self.record_order_status(item) for item in payload.get("order_status", [])]
        execution_events = [self.record_execution_fill(item) for item in payload.get("executions", [])]
        return self._order_event(
            "runtime_event_sync_completed",
            {
                "order_status_count": len(order_status_events),
                "execution_count": len(execution_events),
            },
        )

    def execution_ledger(self) -> dict[str, Any]:
        latest_account = self.account_snapshots[-1].to_dict() if self.account_snapshots else None
        return {
            "mode": "ibkr_paper",
            "fill_count": len(self.executions),
            "order_status_count": len(self.order_status_events),
            "position_count": len(self.positions),
            "account_snapshot_count": len(self.account_snapshots),
            "net_realized_pnl": sum(fill.realized_pnl or 0.0 for fill in self.executions),
            "total_commission": sum(fill.commission for fill in self.executions),
            "positions": [snapshot.to_dict() for snapshot in self.positions.values()],
            "latest_account_snapshot": latest_account,
            "fills": [fill.to_dict() for fill in self.executions],
        }

    def orders_report(self) -> dict[str, Any]:
        return {
            "mode": "ibkr_paper",
            "open_orders": [draft.to_dict() for draft in self.bracket_orders.values()],
            "order_status_events": self.order_status_events,
            "order_events": self.order_events,
            "open_order_count": len(self.bracket_orders),
            "order_status_count": len(self.order_status_events),
        }

    def executions_report(self) -> dict[str, Any]:
        return {
            "mode": "ibkr_paper",
            "executions": [fill.to_dict() for fill in self.executions],
            "count": len(self.executions),
            "total_commission": sum(fill.commission for fill in self.executions),
            "net_realized_pnl": sum(fill.realized_pnl or 0.0 for fill in self.executions),
        }

    def positions_report(self) -> dict[str, Any]:
        return {
            "mode": "ibkr_paper",
            "positions": [snapshot.to_dict() for snapshot in self.positions.values()],
            "count": len(self.positions),
            "tracked_mnq_quantity": self.paper_position_quantity,
        }

    def account_snapshots_report(self) -> dict[str, Any]:
        return {
            "mode": "ibkr_paper",
            "account_snapshots": [snapshot.to_dict() for snapshot in self.account_snapshots],
            "count": len(self.account_snapshots),
            "latest_account_snapshot": self.account_snapshots[-1].to_dict() if self.account_snapshots else None,
        }

    def bracket_order_report(self) -> dict[str, Any]:
        return {
            "mode": "ibkr_paper",
            "open_bracket_order_count": len(self.bracket_orders),
            "open_bracket_orders": [
                {**draft.to_dict(), "submitted": draft.bracket_id in self.submitted_bracket_order_ids}
                for draft in self.bracket_orders.values()
            ],
            "order_event_count": len(self.order_events),
            "recent_order_events": self.order_events[-20:],
        }

    def submit_bracket_order(self, bracket_id: str) -> dict[str, Any]:
        if self.adapter is None:
            return self._order_event("bracket_order_submit_rejected", {"errors": ["adapter_not_configured"]})
        draft = self.bracket_orders.get(bracket_id)
        if draft is None:
            return self._order_event("bracket_order_submit_rejected", {"errors": [f"unknown_bracket_id:{bracket_id}"]})
        readiness = self.readiness(symbol=draft.symbol)
        if readiness["status"] != "ready":
            return self._order_event(
                "bracket_order_submit_rejected",
                {"errors": readiness["missing_requirements"], "readiness": readiness},
            )
        resolved_contract = self.resolved_contract(draft.symbol, require_concrete=True)
        if resolved_contract is None:
            return self._order_event("bracket_order_submit_rejected", {"errors": [f"unknown_symbol:{draft.symbol}"]})
        if not resolved_contract.get("lastTradeDateOrContractMonth") and not resolved_contract.get("localSymbol"):
            return self._order_event(
                "bracket_order_submit_rejected",
                {"errors": ["contract_month_or_local_symbol_required_for_submit"]},
            )
        try:
            submission = self.adapter.submit_bracket_order(resolved_contract, draft.to_dict())
        except Exception as exc:
            self.enter_safe_mode("adapter_bracket_submit_failed")
            return self._order_event("bracket_order_submit_failed", {"errors": [str(exc)], "bracket_id": bracket_id})
        self.submitted_bracket_order_ids.add(bracket_id)
        return self._order_event(
            "bracket_order_submitted",
            {"bracket_id": bracket_id, "bracket_order": draft.to_dict(), "broker_submission": submission},
        )

    def resolved_contract(self, symbol: str = "MNQ", *, require_concrete: bool = False) -> dict[str, Any] | None:
        spec = self.contract_specs.get(symbol)
        if spec is None:
            return None
        payload = spec.to_dict()
        details = self.contract_details.get(symbol)
        if details is not None:
            if not payload.get("lastTradeDateOrContractMonth") and details.last_trade_date_or_contract_month:
                payload["lastTradeDateOrContractMonth"] = details.last_trade_date_or_contract_month
            if not payload.get("localSymbol") and details.local_symbol:
                payload["localSymbol"] = details.local_symbol
            if not payload.get("tradingClass") and details.trading_class:
                payload["tradingClass"] = details.trading_class
        if require_concrete and not (payload.get("lastTradeDateOrContractMonth") or payload.get("localSymbol")):
            return None
        return payload

    def reconcile_position(self, symbol: str = "MNQ", expected_quantity: int | None = None) -> dict[str, Any]:
        expected = self.paper_position_quantity if expected_quantity is None else expected_quantity
        actual = self.positions.get(symbol).quantity if symbol in self.positions else self.paper_position_quantity
        drift = actual != expected
        if drift:
            self.enter_safe_mode("ibkr_position_reconciliation_drift")
        return {
            "status": "drift" if drift else "ok",
            "mode": "ibkr_paper",
            "symbol": symbol,
            "expected_quantity": expected,
            "actual_quantity": actual,
            "safe_mode": self.safe_mode,
            "checked_at": _now(),
        }

    def set_read_only(self, enabled: bool, reason: str = "manual") -> dict[str, Any]:
        self.read_only = enabled
        return self._incident("read_only_enabled" if enabled else "read_only_disabled", reason)

    def enter_safe_mode(self, reason: str) -> dict[str, Any]:
        self.safe_mode = True
        self.read_only = True
        return self._incident("safe_mode_entered", reason)

    def exit_safe_mode(self, reason: str) -> dict[str, Any]:
        if not self.account or not self.account.is_paper:
            return self._incident("safe_mode_exit_blocked", "paper_account_not_verified")
        self.safe_mode = False
        return self._incident("safe_mode_exited", reason)

    def _status(self) -> str:
        if self.safe_mode:
            return "safe_mode"
        if not self.connected:
            return "offline"
        if self.read_only:
            return "connected_readonly"
        if self.account and self.account.is_paper:
            return "paper_connected"
        return "blocked"

    def _event(self, event_type: str, details: dict[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        event = {
            "event_id": f"ibkr_event_{uuid4().hex}",
            "event_type": event_type,
            "mode": "ibkr_paper",
            "protocol_version": IBKR_PROTOCOL_VERSION,
            "sequence": self.sequence,
            "connected": self.connected,
            "read_only": self.read_only,
            "safe_mode": self.safe_mode,
            "live_trading_enabled": self.live_trading_enabled,
            "details": details,
            "created_at": _now(),
        }
        self.connection_events.append(event)
        return event

    def _order_event(self, event_type: str, details: dict[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        event = {
            "event_id": f"ibkr_order_event_{uuid4().hex}",
            "event_type": event_type,
            "mode": "ibkr_paper",
            "protocol_version": IBKR_PROTOCOL_VERSION,
            "sequence": self.sequence,
            "connected": self.connected,
            "read_only": self.read_only,
            "safe_mode": self.safe_mode,
            "paper_account_verified": bool(self.account and self.account.is_paper),
            "details": details,
            "created_at": _now(),
        }
        self.order_events.append(event)
        return event

    def _next_order_id(self) -> int:
        order_id = self.next_order_id
        self.next_order_id += 1
        return order_id

    def _bracket_payload_errors(self, payload: dict[str, Any], symbol: str) -> list[str]:
        errors = []
        if symbol != "MNQ":
            errors.append(f"unsupported_symbol:{symbol}")
        action = str(payload.get("action", "")).upper()
        if action not in {"BUY", "SELL"}:
            errors.append(f"unsupported_action:{action}")
        try:
            quantity = _positive_int(payload.get("quantity", 1))
            if quantity != 1:
                errors.append("quantity_must_be_one_mnq")
        except (TypeError, ValueError):
            errors.append("quantity_must_be_positive")
        entry_order_type = str(payload.get("entry_order_type", "MKT")).upper()
        if entry_order_type not in {"MKT", "LMT"}:
            errors.append(f"unsupported_entry_order_type:{entry_order_type}")
        if entry_order_type == "LMT" and payload.get("entry_price") is None:
            errors.append("limit_entry_price_required")
        if payload.get("stop_price") is None or payload.get("take_profit_price") is None:
            errors.append("stop_and_take_profit_required")
            return errors
        try:
            stop_price = float(payload["stop_price"])
            take_profit_price = float(payload["take_profit_price"])
            reference = _optional_float(payload.get("entry_price")) or self.market_data[symbol].last
            if reference is None:
                errors.append("reference_price_unavailable")
            elif action == "BUY" and not (stop_price < reference < take_profit_price):
                errors.append("buy_bracket_prices_must_wrap_reference")
            elif action == "SELL" and not (take_profit_price < reference < stop_price):
                errors.append("sell_bracket_prices_must_wrap_reference")
        except (KeyError, TypeError, ValueError):
            errors.append("invalid_bracket_prices")
        try:
            _positive_int(payload.get("max_holding_minutes"))
        except (TypeError, ValueError):
            errors.append("max_holding_minutes_required")
        return errors

    def _incident(self, event_type: str, reason: str) -> dict[str, Any]:
        self.sequence += 1
        event = {
            "incident_id": f"ibkr_incident_{uuid4().hex}",
            "event_type": event_type,
            "reason": reason,
            "mode": "ibkr_paper",
            "protocol_version": IBKR_PROTOCOL_VERSION,
            "sequence": self.sequence,
            "safe_mode": self.safe_mode,
            "read_only": self.read_only,
            "created_at": _now(),
        }
        self.incident_events.append(event)
        return event


def ibapi_available() -> bool:
    return importlib.util.find_spec("ibapi") is not None


def _redacted_hash(value: str) -> str:
    if not value:
        return ""
    return f"acct_{hashlib.sha256(value.encode()).hexdigest()[:12]}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _none_if_blank(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _positive_int(value: Any) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("value_must_be_positive")
    return parsed


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    else:
        parsed = datetime.now(UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
