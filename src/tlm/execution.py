from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


ALLOWED_ACTIONS = {"buy", "sell", "sell_short", "buy_to_cover"}
ALLOWED_MODES = {"offline_export", "nt8_sim", "paper_shadow", "micro_live", "live"}
ENTRY_ACTIONS = {"buy", "sell_short"}
READINESS_STAGES = {"paper_shadow", "nt8_sim", "micro_live", "controlled_live"}
EXECUTION_INTENT_STATUSES = {
    "created",
    "risk_rejected",
    "risk_approved",
    "pending_human_approval",
    "human_rejected",
    "approved",
    "submitted",
    "accepted",
    "partially_filled",
    "filled",
    "cancel_requested",
    "cancelled",
    "failed",
    "reconciled",
}
TERMINAL_INTENT_STATUSES = {"risk_rejected", "human_rejected", "filled", "cancelled", "failed", "reconciled"}
EXECUTION_STATUS_TRANSITIONS = {
    "created": {"risk_rejected", "risk_approved"},
    "risk_rejected": set(),
    "risk_approved": {"pending_human_approval", "approved"},
    "pending_human_approval": {"human_rejected", "approved"},
    "human_rejected": set(),
    "approved": {"submitted"},
    "submitted": {"accepted", "failed"},
    "accepted": {"partially_filled", "filled", "cancel_requested", "failed"},
    "partially_filled": {"filled", "cancel_requested", "failed"},
    "filled": {"reconciled"},
    "cancel_requested": {"cancelled", "failed"},
    "cancelled": {"reconciled"},
    "failed": set(),
    "reconciled": set(),
}
GATEWAY_DRIVEN_STATUSES = {"accepted", "partially_filled", "filled", "cancelled", "failed", "reconciled"}
GATEWAY_SOURCE_EVENTS = {"gateway_update", "order_update", "reconciliation"}

EXECUTION_STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_intents (
    intent_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    risk_decision_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS human_approvals (
    approval_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    approver TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gateway_commands (
    command_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_updates (
    update_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


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
    response = build_paper_shadow_run(payload)
    event = {
        "event_type": "paper_shadow_intent",
        "intent": response["intent"],
        "risk": response["risk"],
        "paper_shadow_run": response["paper_shadow_run"],
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    append_execution_audit(audit_path, event)
    return {**response, "audit_path": str(audit_path)}


def build_paper_shadow_run(payload: dict[str, Any]) -> dict[str, Any]:
    response = build_execution_intent_response(payload)
    intent = response["intent"]
    risk = response["risk"]
    market_snapshot = dict(payload.get("market_snapshot") or {})
    slippage_model = dict(payload.get("slippage_model") or {})
    backtest_costs = dict(payload.get("backtest_costs") or {})
    blocked = risk["decision"] != "risk_approved"
    paper_shadow_run = {
        "schema_version": 1,
        "paper_shadow_run_id": str(payload.get("paper_shadow_run_id") or f"ps_{uuid4().hex}"),
        "intent_id": intent["intent_id"],
        "strategy_spec_hash": intent["source_strategy"].get("strategy_spec_hash"),
        "strategy_freeze_id": intent["source_strategy"].get("strategy_freeze_id"),
        "module_id": intent["source_strategy"].get("module_id"),
        "module_version": intent["source_strategy"].get("module_version"),
        "snapshot_hash": snapshot_hash(market_snapshot),
        "replay_key": paper_shadow_replay_key(intent, market_snapshot),
        "risk_decision_id": risk["risk_decision_id"],
        "risk_decision": risk["decision"],
        "blocked": blocked,
        "blocked_reasons": list(risk.get("reasons", [])),
        "hypothetical_fill": None if blocked else hypothetical_fill(intent, market_snapshot, slippage_model),
        "drift_report": drift_report(backtest_costs, market_snapshot, slippage_model),
        "live_gateway_command_created": False,
        "created_at": datetime.now(UTC).isoformat(),
    }
    return {**response, "paper_shadow_run": paper_shadow_run}


def snapshot_hash(snapshot: dict[str, Any]) -> str:
    if snapshot.get("snapshot_hash"):
        return str(snapshot["snapshot_hash"])
    return stable_hash(snapshot)


def paper_shadow_replay_key(intent: dict[str, Any], market_snapshot: dict[str, Any]) -> str:
    payload = {
        "strategy_spec_hash": intent.get("source_strategy", {}).get("strategy_spec_hash"),
        "module_id": intent.get("source_strategy", {}).get("module_id"),
        "instrument": intent.get("instrument"),
        "action": intent.get("action"),
        "quantity": intent.get("quantity"),
        "order_type": intent.get("order_type"),
        "snapshot_hash": snapshot_hash(market_snapshot),
    }
    return stable_hash(payload)


def hypothetical_fill(
    intent: dict[str, Any],
    market_snapshot: dict[str, Any],
    slippage_model: dict[str, Any],
) -> dict[str, Any]:
    bid = _optional_float(market_snapshot.get("bid"))
    ask = _optional_float(market_snapshot.get("ask"))
    last_price = _optional_float(market_snapshot.get("last_price"))
    tick_size = float(slippage_model.get("tick_size", market_snapshot.get("tick_size", 0.25)))
    slippage_ticks = float(slippage_model.get("slippage_ticks", 0))
    action = intent["action"]
    if action in {"buy", "buy_to_cover"}:
        reference_price = ask if ask is not None else last_price
        fill_price = None if reference_price is None else reference_price + slippage_ticks * tick_size
    else:
        reference_price = bid if bid is not None else last_price
        fill_price = None if reference_price is None else reference_price - slippage_ticks * tick_size
    return {
        "instrument": intent["instrument"],
        "action": action,
        "quantity": intent["quantity"],
        "reference_price": reference_price,
        "fill_price": fill_price,
        "slippage_ticks": slippage_ticks,
        "tick_size": tick_size,
        "bracket": intent.get("bracket", {}),
    }


def drift_report(
    backtest_costs: dict[str, Any],
    market_snapshot: dict[str, Any],
    slippage_model: dict[str, Any],
) -> dict[str, Any]:
    expected_spread = _optional_float(backtest_costs.get("expected_spread_ticks"))
    observed_spread = _optional_float(market_snapshot.get("spread_ticks"))
    expected_slippage = _optional_float(backtest_costs.get("expected_slippage_ticks"))
    observed_slippage = _optional_float(slippage_model.get("slippage_ticks"))
    return {
        "schema_version": 1,
        "expected_spread_ticks": expected_spread,
        "observed_spread_ticks": observed_spread,
        "spread_drift_ticks": _delta(observed_spread, expected_spread),
        "expected_slippage_ticks": expected_slippage,
        "observed_slippage_ticks": observed_slippage,
        "slippage_drift_ticks": _delta(observed_slippage, expected_slippage),
    }


def stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _delta(observed: float | None, expected: float | None) -> float | None:
    if observed is None or expected is None:
        return None
    return observed - expected


def evaluate_live_readiness(stage: str, evidence: dict[str, Any]) -> dict[str, Any]:
    if stage not in READINESS_STAGES:
        raise ValueError(f"Unsupported readiness stage: {stage}")
    reasons: list[str] = []
    if stage == "paper_shadow":
        _min_gate(reasons, evidence, "trading_days", 10)
        _bool_gate(reasons, evidence, "replay_consistent")
        _max_gate(reasons, evidence, "p95_spread_slippage_drift", evidence.get("max_allowed_drift"))
    elif stage == "nt8_sim":
        _min_any_gate(reasons, evidence, [("trading_days", 5), ("sim_commands", 200)])
        _bool_gate(reasons, evidence, "disconnect_reconnect_validated")
        _bool_gate(reasons, evidence, "idempotency_validated")
        _bool_gate(reasons, evidence, "flatten_validated")
        _max_gate(reasons, evidence, "reconcile_drift_count", 0)
    elif stage == "micro_live":
        _bool_gate(reasons, evidence, "external_nt8_validated")
        _min_any_gate(reasons, evidence, [("trade_count", 20), ("trading_days", 10)])
        _bool_gate(reasons, evidence, "manual_approval_audited")
        _bool_gate(reasons, evidence, "broker_side_protection")
        _max_gate(reasons, evidence, "unresolved_incident_count", 0)
    elif stage == "controlled_live":
        _bool_gate(reasons, evidence, "external_broker_validated")
        _min_any_gate(reasons, evidence, [("sample_trades", 100), ("trading_days", 30)])
        _bool_gate(reasons, evidence, "strategy_profile_whitelisted")
        _bool_gate(reasons, evidence, "kill_switch_verified")
        _max_gate(reasons, evidence, "high_risk_drift_count", 0)
    return {
        "stage": stage,
        "passed": not reasons,
        "decision": "ready" if not reasons else "blocked",
        "reasons": reasons,
        "evidence": evidence,
        "checked_at": datetime.now(UTC).isoformat(),
    }


def connect_execution_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(EXECUTION_STORE_SCHEMA)
    return connection


def create_execution_state_record(
    path: Path,
    intent: ExecutionIntent,
    risk: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    status = "risk_approved" if risk.get("passed") else "risk_rejected"
    intent_payload = intent.to_dict()
    intent_payload["status"] = status
    with connect_execution_store(path) as connection:
        connection.execute(
            """
            INSERT INTO execution_intents (intent_id, status, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (intent.intent_id, status, json.dumps(intent_payload, sort_keys=True, default=str), now, now),
        )
        connection.execute(
            """
            INSERT INTO risk_decisions (risk_decision_id, intent_id, decision, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                risk["risk_decision_id"],
                intent.intent_id,
                risk["decision"],
                json.dumps(risk, sort_keys=True, default=str),
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO execution_audit_events (intent_id, event_type, from_status, to_status, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                intent.intent_id,
                "risk_decision",
                "created",
                status,
                json.dumps({"risk_decision_id": risk["risk_decision_id"]}, sort_keys=True),
                now,
            ),
        )
    return load_execution_state(path, intent.intent_id)


def load_execution_state(path: Path, intent_id: str) -> dict[str, Any]:
    with connect_execution_store(path) as connection:
        row = connection.execute(
            """
            SELECT intent_id, status, payload_json, created_at, updated_at
            FROM execution_intents WHERE intent_id = ?
            """,
            (intent_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown intent_id: {intent_id}")
        risk_rows = connection.execute(
            "SELECT payload_json FROM risk_decisions WHERE intent_id = ? ORDER BY created_at ASC",
            (intent_id,),
        ).fetchall()
        approval_rows = connection.execute(
            """
            SELECT approval_id, decision, approver, reason, created_at
            FROM human_approvals WHERE intent_id = ? ORDER BY created_at ASC
            """,
            (intent_id,),
        ).fetchall()
        audit_rows = connection.execute(
            """
            SELECT event_type, from_status, to_status, payload_json, created_at
            FROM execution_audit_events WHERE intent_id = ? ORDER BY id ASC
            """,
            (intent_id,),
        ).fetchall()
    return {
        "intent_id": row[0],
        "status": row[1],
        "intent": json.loads(row[2]),
        "created_at": row[3],
        "updated_at": row[4],
        "risk_decisions": [json.loads(item[0]) for item in risk_rows],
        "human_approvals": [
            {
                "approval_id": item[0],
                "decision": item[1],
                "approver": item[2],
                "reason": item[3],
                "created_at": item[4],
            }
            for item in approval_rows
        ],
        "audit_events": [
            {
                "event_type": item[0],
                "from_status": item[1],
                "to_status": item[2],
                "payload": json.loads(item[3]),
                "created_at": item[4],
            }
            for item in audit_rows
        ],
    }


def transition_execution_state(
    path: Path,
    intent_id: str,
    to_status: str,
    *,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = payload or {}
    state = load_execution_state(path, intent_id)
    from_status = state["status"]
    validate_execution_transition(from_status, to_status, event_type, payload)
    now = datetime.now(UTC).isoformat()
    with connect_execution_store(path) as connection:
        if to_status == "approved" and payload.get("approval_id"):
            connection.execute(
                """
                INSERT INTO human_approvals (approval_id, intent_id, decision, approver, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["approval_id"],
                    intent_id,
                    "approved",
                    str(payload.get("approver", "unknown")),
                    payload.get("reason"),
                    now,
                ),
            )
        if event_type == "gateway_command" and payload.get("command"):
            command = payload["command"]
            connection.execute(
                """
                INSERT INTO gateway_commands (command_id, intent_id, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    command["command_id"],
                    intent_id,
                    json.dumps(command, sort_keys=True, default=str),
                    now,
                ),
            )
        if event_type in GATEWAY_SOURCE_EVENTS:
            connection.execute(
                """
                INSERT INTO order_updates (update_id, intent_id, status, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(payload.get("update_id") or f"upd_{uuid4().hex}"),
                    intent_id,
                    to_status,
                    json.dumps(payload, sort_keys=True, default=str),
                    now,
                ),
            )
        connection.execute(
            "UPDATE execution_intents SET status = ?, updated_at = ? WHERE intent_id = ?",
            (to_status, now, intent_id),
        )
        connection.execute(
            """
            INSERT INTO execution_audit_events (intent_id, event_type, from_status, to_status, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                intent_id,
                event_type,
                from_status,
                to_status,
                json.dumps(payload, sort_keys=True, default=str),
                now,
            ),
        )
    return load_execution_state(path, intent_id)


def validate_execution_transition(
    from_status: str,
    to_status: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    if from_status not in EXECUTION_INTENT_STATUSES:
        raise ValueError(f"Unsupported execution status: {from_status}")
    if to_status not in EXECUTION_INTENT_STATUSES:
        raise ValueError(f"Unsupported execution status: {to_status}")
    if from_status in TERMINAL_INTENT_STATUSES and to_status != "reconciled":
        raise ValueError(f"Cannot transition terminal intent from {from_status} to {to_status}")
    if to_status not in EXECUTION_STATUS_TRANSITIONS[from_status]:
        raise ValueError(f"Invalid execution transition: {from_status} -> {to_status}")
    if to_status == "approved" and not (payload.get("approval_id") or payload.get("automation_profile_id")):
        raise ValueError("approved status requires human approval or automation profile")
    if to_status == "submitted" and event_type != "gateway_command":
        raise ValueError("submitted status requires gateway_command event")
    if to_status in GATEWAY_DRIVEN_STATUSES and event_type not in (GATEWAY_SOURCE_EVENTS | {"gateway_command"}):
        raise ValueError(f"{to_status} status requires gateway/order/reconciliation event")


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _bool_gate(reasons: list[str], evidence: dict[str, Any], key: str) -> None:
    if evidence.get(key) is not True:
        reasons.append(key)


def _min_gate(reasons: list[str], evidence: dict[str, Any], key: str, minimum: int | float) -> None:
    if float(evidence.get(key, 0)) < minimum:
        reasons.append(key)


def _max_gate(
    reasons: list[str],
    evidence: dict[str, Any],
    key: str,
    maximum: int | float | None,
) -> None:
    if maximum is None:
        reasons.append(f"{key}_threshold_missing")
    elif float(evidence.get(key, maximum + 1)) > float(maximum):
        reasons.append(key)


def _min_any_gate(
    reasons: list[str],
    evidence: dict[str, Any],
    gates: list[tuple[str, int | float]],
) -> None:
    if not any(float(evidence.get(key, 0)) >= minimum for key, minimum in gates):
        reasons.append("_or_".join(key for key, _ in gates))
