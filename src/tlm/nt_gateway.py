from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .nt8_protocol import build_order_update, detect_external_intervention


SUPPORTED_COMMANDS = {
    "marketOrder",
    "marketBatch",
    "cancelOrders",
    "flatten",
    "flattenBatch",
    "closeQty",
    "bracket",
}


@dataclass
class SimOrder:
    order_id: str
    account: str
    instrument: str
    action: str
    quantity: int
    status: str
    name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "account": self.account,
            "instrument": self.instrument,
            "action": self.action,
            "quantity": self.quantity,
            "status": self.status,
            "name": self.name,
        }


@dataclass
class Nt8SimGateway:
    accounts: list[str] = field(default_factory=lambda: ["Sim101"])
    instruments: list[str] = field(default_factory=lambda: ["NQ 06-26"])
    orders: dict[str, SimOrder] = field(default_factory=dict)
    positions: dict[tuple[str, str], int] = field(default_factory=dict)
    brackets: list[dict[str, Any]] = field(default_factory=list)
    command_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    incident_events: list[dict[str, Any]] = field(default_factory=list)
    order_update_events: list[dict[str, Any]] = field(default_factory=list)
    sequence: int = 0
    read_only: bool = False
    safe_mode: bool = False

    def health(self) -> dict[str, Any]:
        return {
            "status": "safe_mode" if self.safe_mode else "ok",
            "mode": "nt8_sim",
            "read_only": self.read_only,
            "safe_mode": self.safe_mode,
            "accounts": self.accounts,
            "instruments": self.instruments,
            "order_count": len(self.orders),
            "position_count": len([qty for qty in self.positions.values() if qty]),
            "sequence": self.sequence,
            "checked_at": datetime.now(UTC).isoformat(),
        }

    def order_updates(self) -> dict[str, Any]:
        return {
            "mode": "nt8_sim",
            "event_count": len(self.order_update_events),
            "events": list(self.order_update_events),
        }

    def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        idempotency_key = command.get("idempotency_key")
        if idempotency_key and idempotency_key in self.command_cache:
            cached = dict(self.command_cache[str(idempotency_key)])
            cached["duplicate"] = True
            return cached
        command_type = str(command.get("type", ""))
        if command_type not in SUPPORTED_COMMANDS:
            return self._ack(command, "rejected", [f"unsupported_command:{command_type}"])
        if self.read_only:
            return self._ack(command, "rejected", ["gateway_read_only"])
        if self.safe_mode and command_type not in {"cancelOrders", "flatten", "flattenBatch", "closeQty"}:
            return self._ack(command, "rejected", ["gateway_safe_mode"])
        handler = getattr(self, f"_handle_{command_type}")
        try:
            ack = handler(command)
        except ValueError as exc:
            ack = self._ack(command, "rejected", [str(exc)])
        if idempotency_key:
            self.command_cache[str(idempotency_key)] = ack
        return ack

    def set_read_only(self, enabled: bool, reason: str = "manual") -> dict[str, Any]:
        self.read_only = enabled
        return self._incident("read_only_enabled" if enabled else "read_only_disabled", reason)

    def enter_safe_mode(self, reason: str) -> dict[str, Any]:
        self.safe_mode = True
        return self._incident("safe_mode_entered", reason)

    def exit_safe_mode(self, reason: str) -> dict[str, Any]:
        self.safe_mode = False
        return self._incident("safe_mode_exited", reason)

    def reconciliation_report(
        self,
        expected_positions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        actual = {
            (account, instrument): quantity
            for (account, instrument), quantity in self.positions.items()
            if quantity
        }
        expected = {
            (str(row["account"]), str(row["instrument"])): int(row.get("quantity", 0))
            for row in expected_positions or []
            if int(row.get("quantity", 0))
        }
        drift = []
        for key in sorted(set(actual) | set(expected)):
            if actual.get(key, 0) != expected.get(key, 0):
                drift.append(
                    {
                        "account": key[0],
                        "instrument": key[1],
                        "expected_quantity": expected.get(key, 0),
                        "actual_quantity": actual.get(key, 0),
                    }
                )
        if drift:
            self.enter_safe_mode("reconciliation_drift")
        return {
            "status": "drift" if drift else "ok",
            "mode": "nt8_sim",
            "drift": drift,
            "safe_mode": self.safe_mode,
            "checked_at": datetime.now(UTC).isoformat(),
        }

    def record_external_intervention(
        self,
        *,
        account: str,
        instrument: str,
        order_id: str,
        status: str,
        reason: str,
    ) -> dict[str, Any]:
        self.sequence += 1
        event = build_order_update(
            sequence=self.sequence,
            order_id=order_id,
            status=status,
            account=account,
            instrument=instrument,
            source="manual",
            details={"reason": reason},
        )
        self.order_update_events.append(event)
        self.enter_safe_mode("external_intervention")
        return event

    def _handle_marketOrder(self, command: dict[str, Any]) -> dict[str, Any]:
        account = self._required_account(command)
        instrument = self._required_instrument(command)
        action = str(command.get("action"))
        qty = self._positive_qty(command.get("qty", command.get("quantity")))
        order = self._fill_order(account, instrument, action, qty, command.get("namePrefix"))
        return self._ack(command, "filled", [], orders=[order.to_dict()])

    def _handle_marketBatch(self, command: dict[str, Any]) -> dict[str, Any]:
        accounts = command.get("accounts") or []
        items = command.get("items") or []
        orders = []
        errors = []
        for account in accounts:
            if account not in self.accounts:
                errors.append(f"account_not_allowed:{account}")
                continue
            for item in items:
                try:
                    instrument = self._required_instrument(item)
                    action = str(item.get("action"))
                    qty = self._positive_qty(item.get("qty", item.get("quantity")))
                    orders.append(
                        self._fill_order(str(account), instrument, action, qty, item.get("namePrefix")).to_dict()
                    )
                except ValueError as exc:
                    errors.append(str(exc))
        return self._ack(command, "partial" if errors and orders else "filled", errors, orders=orders)

    def _handle_cancelOrders(self, command: dict[str, Any]) -> dict[str, Any]:
        order_id = command.get("orderId") or command.get("order_id")
        name_prefix = command.get("namePrefix") or command.get("name_prefix")
        cancelled = []
        for order in self.orders.values():
            if order.status == "cancelled":
                continue
            if order_id and order.order_id == order_id:
                order.status = "cancelled"
                cancelled.append(order.to_dict())
            elif name_prefix and order.name.startswith(str(name_prefix)):
                order.status = "cancelled"
                cancelled.append(order.to_dict())
        return self._ack(command, "cancelled", [], orders=cancelled)

    def _handle_flatten(self, command: dict[str, Any]) -> dict[str, Any]:
        account = self._required_account(command)
        instrument = self._required_instrument(command)
        self.positions[(account, instrument)] = 0
        for order in self.orders.values():
            if order.account == account and order.instrument == instrument and order.status != "filled":
                order.status = "cancelled"
        return self._ack(command, "flattened", [])

    def _handle_flattenBatch(self, command: dict[str, Any]) -> dict[str, Any]:
        accounts = command.get("accounts") or []
        for account in accounts:
            for key in list(self.positions):
                if key[0] == account:
                    self.positions[key] = 0
        return self._ack(command, "flattened", [])

    def _handle_closeQty(self, command: dict[str, Any]) -> dict[str, Any]:
        account = self._required_account(command)
        instrument = self._required_instrument(command)
        qty = self._positive_qty(command.get("qty", command.get("quantity")))
        key = (account, instrument)
        current = self.positions.get(key, 0)
        if current > 0:
            self.positions[key] = max(0, current - qty)
        elif current < 0:
            self.positions[key] = min(0, current + qty)
        return self._ack(command, "closed", [])

    def _handle_bracket(self, command: dict[str, Any]) -> dict[str, Any]:
        account = self._required_account(command)
        instrument = self._required_instrument(command)
        if command.get("stop") is None or command.get("limit") is None:
            return self._ack(command, "rejected", ["stop_and_limit_required"])
        bracket = {
            "bracket_id": f"bracket_{uuid4().hex}",
            "account": account,
            "instrument": instrument,
            "stop": float(command["stop"]),
            "limit": float(command["limit"]),
            "oco": True,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self.brackets.append(bracket)
        return self._ack(command, "accepted", [], brackets=[bracket])

    def _fill_order(
        self,
        account: str,
        instrument: str,
        action: str,
        qty: int,
        name_prefix: Any = None,
    ) -> SimOrder:
        if action not in {"buy", "sell", "sell_short", "buy_to_cover", "BUY", "SELL", "SELLSHORT", "BUYTOCOVER"}:
            raise ValueError(f"unsupported_action:{action}")
        signed = qty if action.lower() in {"buy", "buytocover"} else -qty
        if action.lower() == "sell":
            signed = -qty
        if action.lower() == "sell_short":
            signed = -qty
        key = (account, instrument)
        self.positions[key] = self.positions.get(key, 0) + signed
        order = SimOrder(
            order_id=f"order_{uuid4().hex}",
            account=account,
            instrument=instrument,
            action=action,
            quantity=qty,
            status="filled",
            name=f"{name_prefix or 'sim'}_{len(self.orders) + 1}",
        )
        self.orders[order.order_id] = order
        return order

    def _required_account(self, payload: dict[str, Any]) -> str:
        account = str(payload.get("account", "")).strip()
        if account not in self.accounts:
            raise ValueError(f"account_not_allowed:{account}")
        return account

    def _required_instrument(self, payload: dict[str, Any]) -> str:
        instrument = str(payload.get("instrument", "")).strip()
        if instrument not in self.instruments:
            raise ValueError(f"instrument_not_allowed:{instrument}")
        return instrument

    def _positive_qty(self, value: Any) -> int:
        qty = int(value or 0)
        if qty <= 0:
            raise ValueError("qty_must_be_positive")
        return qty

    def _ack(
        self,
        command: dict[str, Any],
        status: str,
        errors: list[str],
        *,
        orders: list[dict[str, Any]] | None = None,
        brackets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.sequence += 1
        payload = {
            "command_id": command.get("command_id") or f"cmd_{uuid4().hex}",
            "correlation_id": command.get("correlation_id"),
            "idempotency_key": command.get("idempotency_key"),
            "schema_version": command.get("schema_version", 1),
            "protocol_version": command.get("protocol_version", "nt8-sim.v1"),
            "sequence": self.sequence,
            "status": status,
            "errors": errors,
            "orders": orders or [],
            "brackets": brackets or [],
            "positions": [
                {"account": account, "instrument": instrument, "quantity": quantity}
                for (account, instrument), quantity in sorted(self.positions.items())
            ],
            "acknowledged_at": datetime.now(UTC).isoformat(),
        }
        for order in payload["orders"]:
            update = build_order_update(
                sequence=self.sequence,
                order_id=order["order_id"],
                status=order["status"],
                account=order["account"],
                instrument=order["instrument"],
                command_id=payload["command_id"],
                source="gateway",
            )
            if detect_external_intervention(update, [payload["command_id"]]):
                self.enter_safe_mode("external_intervention")
            self.order_update_events.append(update)
        return payload

    def _incident(self, event_type: str, reason: str) -> dict[str, Any]:
        event = {
            "incident_id": f"incident_{uuid4().hex}",
            "event_type": event_type,
            "reason": reason,
            "mode": "nt8_sim",
            "safe_mode": self.safe_mode,
            "read_only": self.read_only,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self.incident_events.append(event)
        return event
