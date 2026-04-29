from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Sequence


SCHEMA_VERSION = 1
PROTOCOL_VERSION = "nt8-gateway.v1"
SUPPORTED_TRANSPORTS = ["local_http", "websocket", "named_pipe"]
SUPPORTED_COMMANDS = [
    "marketOrder",
    "marketBatch",
    "cancelOrders",
    "flatten",
    "flattenBatch",
    "closeQty",
    "bracket",
]
APPEND_ONLY_EVENT_TYPES = [
    "heartbeat",
    "account_snapshot",
    "instrument_snapshot",
    "order_update",
    "external_intervention",
    "incident",
]


def gateway_protocol_manifest() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "transports": SUPPORTED_TRANSPORTS,
        "commands": SUPPORTED_COMMANDS,
        "append_only_event_types": APPEND_ONLY_EVENT_TYPES,
        "required_command_fields": ["type", "correlation_id", "idempotency_key"],
        "safety_modes": ["sim_only", "read_only", "safe_mode"],
        "live_readiness": "blocked_until_external_validation",
    }


def validate_gateway_command(command: dict[str, Any]) -> dict[str, Any]:
    errors = []
    command_type = command.get("type")
    if command_type not in SUPPORTED_COMMANDS:
        errors.append(f"unsupported_command:{command_type}")
    for field in ["correlation_id", "idempotency_key"]:
        if not command.get(field):
            errors.append(f"missing_{field}")
    schema_version = int(command.get("schema_version", SCHEMA_VERSION))
    if schema_version != SCHEMA_VERSION:
        errors.append(f"unsupported_schema_version:{schema_version}")
    protocol_version = command.get("protocol_version", PROTOCOL_VERSION)
    if protocol_version not in {PROTOCOL_VERSION, "nt8-sim.v1"}:
        errors.append(f"unsupported_protocol_version:{protocol_version}")
    account = command.get("account")
    if account and not is_sim_account(str(account)):
        errors.append("non_sim_account_blocked")
    return {"valid": not errors, "errors": errors}


def is_sim_account(account: str) -> bool:
    normalized = account.strip().lower()
    return normalized.startswith("sim") or normalized.startswith("simulation")


def build_heartbeat(
    *,
    sequence: int,
    accounts: Sequence[str],
    instruments: Sequence[str],
    active_mode: str = "sim",
    read_only: bool = False,
    safe_mode: bool = False,
) -> dict[str, Any]:
    return gateway_event(
        "heartbeat",
        sequence,
        {
            "active_mode": active_mode,
            "read_only": read_only,
            "safe_mode": safe_mode,
            "accounts": list(accounts),
            "instruments": list(instruments),
        },
    )


def build_account_snapshot(sequence: int, positions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return gateway_event("account_snapshot", sequence, {"positions": list(positions)})


def build_order_update(
    *,
    sequence: int,
    order_id: str,
    status: str,
    account: str,
    instrument: str,
    command_id: str | None = None,
    source: str = "gateway",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_type = "external_intervention" if source == "manual" else "order_update"
    return gateway_event(
        event_type,
        sequence,
        {
            "order_id": order_id,
            "status": status,
            "account": account,
            "instrument": instrument,
            "command_id": command_id,
            "source": source,
            "details": details or {},
        },
    )


def gateway_event(event_type: str, sequence: int, payload: dict[str, Any]) -> dict[str, Any]:
    if event_type not in APPEND_ONLY_EVENT_TYPES:
        raise ValueError(f"unsupported_gateway_event:{event_type}")
    if sequence <= 0:
        raise ValueError("sequence_must_be_positive")
    return {
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "event_type": event_type,
        "sequence": sequence,
        "payload": payload,
        "created_at": datetime.now(UTC).isoformat(),
    }


def append_gateway_event(log: list[dict[str, Any]], event: dict[str, Any]) -> list[dict[str, Any]]:
    if log and int(event["sequence"]) <= int(log[-1]["sequence"]):
        raise ValueError("gateway_event_sequence_must_increase")
    return [*log, deepcopy(event)]


def detect_external_intervention(
    order_update: dict[str, Any],
    known_command_ids: Sequence[str],
) -> bool:
    payload = order_update.get("payload", order_update)
    if payload.get("source") == "manual":
        return True
    command_id = payload.get("command_id")
    return bool(command_id and command_id not in set(known_command_ids))
