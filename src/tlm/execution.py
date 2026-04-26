from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


ALLOWED_ACTIONS = {"buy", "sell", "sell_short", "buy_to_cover"}
ALLOWED_MODES = {"offline_export", "nt8_sim", "paper_shadow", "micro_live", "live"}
ENTRY_ACTIONS = {"buy", "sell_short"}


@dataclass(frozen=True)
class ExecutionIntent:
    intent_id: str
    mode: str
    account: str
    instrument: str
    action: str
    quantity: int
    order_type: str
    bracket: dict[str, Any]
    source_strategy: dict[str, Any]
    reason: str
    idempotency_key: str
    correlation_id: str
    schema_version: int
    protocol_version: str
    created_at: str
    expires_at: str | None = None
    account_snapshot_id: str | None = None
    market_snapshot_id: str | None = None
    expected_max_slippage_ticks: float | None = None
    expected_spread_ticks: float | None = None
    status: str = "created"

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "mode": self.mode,
            "account": self.account,
            "instrument": self.instrument,
            "action": self.action,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "bracket": self.bracket,
            "source_strategy": self.source_strategy,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "correlation_id": self.correlation_id,
            "schema_version": self.schema_version,
            "protocol_version": self.protocol_version,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "account_snapshot_id": self.account_snapshot_id,
            "market_snapshot_id": self.market_snapshot_id,
            "expected_max_slippage_ticks": self.expected_max_slippage_ticks,
            "expected_spread_ticks": self.expected_spread_ticks,
            "status": self.status,
        }


def create_execution_intent(payload: dict[str, Any]) -> ExecutionIntent:
    mode = str(payload.get("mode", "paper_shadow"))
    if mode not in ALLOWED_MODES:
        raise ValueError(f"Unsupported execution mode: {mode}")
    action = str(payload.get("action", "")).lower()
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"Unsupported execution action: {action}")
    quantity = int(payload.get("quantity", payload.get("qty", 0)))
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    account = str(payload.get("account", "")).strip()
    instrument = str(payload.get("instrument", "")).strip()
    if not account:
        raise ValueError("account is required")
    if not instrument:
        raise ValueError("instrument is required")
    return ExecutionIntent(
        intent_id=str(payload.get("intent_id") or f"intent_{uuid4().hex}"),
        mode=mode,
        account=account,
        instrument=instrument,
        action=action,
        quantity=quantity,
        order_type=str(payload.get("order_type", "market")).lower(),
        bracket=dict(payload.get("bracket") or {}),
        source_strategy=dict(payload.get("source_strategy") or {}),
        reason=str(payload.get("reason", "")),
        idempotency_key=str(payload.get("idempotency_key") or uuid4().hex),
        correlation_id=str(payload.get("correlation_id") or f"corr_{uuid4().hex}"),
        schema_version=int(payload.get("schema_version", 1)),
        protocol_version=str(payload.get("protocol_version", "execution.v1")),
        created_at=str(payload.get("created_at") or datetime.now(UTC).isoformat()),
        expires_at=payload.get("expires_at"),
        account_snapshot_id=payload.get("account_snapshot_id"),
        market_snapshot_id=payload.get("market_snapshot_id"),
        expected_max_slippage_ticks=_optional_float(payload.get("expected_max_slippage_ticks")),
        expected_spread_ticks=_optional_float(payload.get("expected_spread_ticks")),
    )


def evaluate_risk(intent: ExecutionIntent, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = profile or {}
    reasons = []
    allowed_accounts = set(profile.get("allowed_accounts", []))
    allowed_instruments = set(profile.get("allowed_instruments", []))
    max_quantity = int(profile.get("max_quantity", 1))
    live_enabled = bool(profile.get("live_enabled", False))
    require_bracket = bool(profile.get("require_bracket", True))
    kill_switch = bool(profile.get("kill_switch", False))
    data_stale = bool(profile.get("data_stale", False))
    event_blackout = bool(profile.get("event_blackout", False))
    max_spread_ticks = profile.get("max_spread_ticks")
    max_slippage_ticks = profile.get("max_slippage_ticks")
    current_position = int(profile.get("current_position", 0))
    max_position_after_fill = int(profile.get("max_position_after_fill", max_quantity))
    daily_loss_limit_reached = bool(profile.get("daily_loss_limit_reached", False))

    if allowed_accounts and intent.account not in allowed_accounts:
        reasons.append("account_not_allowed")
    if allowed_instruments and intent.instrument not in allowed_instruments:
        reasons.append("instrument_not_allowed")
    if intent.quantity > max_quantity:
        reasons.append("quantity_exceeds_limit")
    if kill_switch:
        reasons.append("kill_switch_enabled")
    if data_stale:
        reasons.append("data_stale")
    if event_blackout:
        reasons.append("event_blackout")
    if daily_loss_limit_reached:
        reasons.append("daily_loss_limit_reached")
    if intent.mode in {"micro_live", "live"} and not live_enabled:
        reasons.append("live_profile_disabled")
    if intent.mode in {"micro_live", "live"} and not intent.source_strategy.get("strategy_freeze_id"):
        reasons.append("missing_strategy_freeze_id")
    if intent.mode == "live" and intent.quantity > max_quantity:
        reasons.append("live_quantity_above_profile_limit")
    if require_bracket and intent.action in ENTRY_ACTIONS:
        if not intent.bracket.get("stop") or not intent.bracket.get("limit"):
            reasons.append("bracket_required")
    if max_spread_ticks is not None and intent.expected_spread_ticks is not None:
        if intent.expected_spread_ticks > float(max_spread_ticks):
            reasons.append("spread_exceeds_limit")
    if max_slippage_ticks is not None and intent.expected_max_slippage_ticks is not None:
        if intent.expected_max_slippage_ticks > float(max_slippage_ticks):
            reasons.append("slippage_exceeds_limit")
    signed_quantity = intent.quantity if intent.action in {"buy", "buy_to_cover"} else -intent.quantity
    if abs(current_position + signed_quantity) > max_position_after_fill:
        reasons.append("position_after_fill_exceeds_limit")
    if not intent.source_strategy.get("strategy_spec_hash"):
        reasons.append("missing_strategy_spec_hash")
    decision = "risk_rejected" if reasons else "risk_approved"
    return {
        "risk_decision_id": f"risk_{uuid4().hex}",
        "intent_id": intent.intent_id,
        "decision": decision,
        "passed": not reasons,
        "reasons": reasons,
        "checked_at": datetime.now(UTC).isoformat(),
        "profile": {
            "max_quantity": max_quantity,
            "live_enabled": live_enabled,
            "require_bracket": require_bracket,
            "kill_switch": kill_switch,
            "data_stale": data_stale,
            "event_blackout": event_blackout,
            "daily_loss_limit_reached": daily_loss_limit_reached,
            "max_spread_ticks": max_spread_ticks,
            "max_slippage_ticks": max_slippage_ticks,
            "max_position_after_fill": max_position_after_fill,
        },
    }


def build_gateway_command(intent: ExecutionIntent, risk: dict[str, Any]) -> dict[str, Any]:
    if risk.get("decision") != "risk_approved":
        raise ValueError("Only risk_approved intents can be converted to gateway commands")
    command_type = "marketOrder" if intent.order_type == "market" else intent.order_type
    return {
        "command_id": f"cmd_{uuid4().hex}",
        "type": command_type,
        "correlation_id": intent.correlation_id,
        "idempotency_key": intent.idempotency_key,
        "schema_version": intent.schema_version,
        "protocol_version": intent.protocol_version,
        "mode": intent.mode,
        "account": intent.account,
        "instrument": intent.instrument,
        "action": intent.action,
        "qty": intent.quantity,
        "bracket": intent.bracket,
        "strategy_freeze_id": intent.source_strategy.get("strategy_freeze_id"),
        "strategy_spec_hash": intent.source_strategy.get("strategy_spec_hash"),
        "risk_decision_id": risk["risk_decision_id"],
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": intent.expires_at,
    }


def build_execution_intent_response(payload: dict[str, Any]) -> dict[str, Any]:
    intent = create_execution_intent(payload)
    risk = evaluate_risk(intent, payload.get("risk_profile"))
    status = "risk_approved" if risk["passed"] else "risk_rejected"
    intent_payload = intent.to_dict()
    intent_payload["status"] = status
    return {"intent": intent_payload, "risk": risk}


def append_execution_audit(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")


def submit_paper_shadow(payload: dict[str, Any], audit_path: Path) -> dict[str, Any]:
    response = build_execution_intent_response(payload)
    event = {
        "event_type": "paper_shadow_intent",
        "intent": response["intent"],
        "risk": response["risk"],
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    append_execution_audit(audit_path, event)
    return {**response, "audit_path": str(audit_path)}


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)
