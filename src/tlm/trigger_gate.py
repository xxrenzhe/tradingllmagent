from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from .variants import stable_hash


TRIGGER_GATE_DECISIONS = {"allow", "block", "observe"}
TRIGGER_GATE_RISK_LEVELS = {"low", "medium", "high"}
TRIGGER_GATE_MEMORY_FILES = {
    "evidence": "memory_evidence_records.jsonl",
    "decisions": "memory_decisions.jsonl",
    "outcomes": "memory_outcomes.jsonl",
}


def build_trigger_evidence_record(
    *,
    strategy_spec_hash: str,
    module_id: str,
    signal_time: str | datetime,
    signal_features: dict[str, Any],
    market_snapshot: dict[str, Any],
    event_context: dict[str, Any] | None,
    risk_pre_gate: dict[str, Any],
    pool_version: str,
    trigger_reason: str,
    strategy_name: str | None = None,
    timeframe: str | None = None,
) -> dict[str, Any]:
    signal_time_value = _isoformat(signal_time)
    snapshot_hash = stable_hash(market_snapshot)
    payload = {
        "schema_version": 1,
        "strategy_spec_hash": strategy_spec_hash,
        "strategy_name": strategy_name,
        "module_id": module_id,
        "timeframe": timeframe,
        "snapshot_hash": snapshot_hash,
        "signal_time": signal_time_value,
        "signal_features": signal_features,
        "market_snapshot": market_snapshot,
        "event_context": event_context or {},
        "risk_pre_gate": risk_pre_gate,
        "pool_version": pool_version,
        "trigger_reason": trigger_reason,
        "created_at": datetime.now(UTC).isoformat(),
    }
    payload["evidence_id"] = stable_hash(
        {
            "strategy_spec_hash": strategy_spec_hash,
            "module_id": module_id,
            "snapshot_hash": snapshot_hash,
            "signal_time": signal_time_value,
            "trigger_reason": trigger_reason,
        }
    )
    return payload


def build_trigger_decision_record(
    *,
    evidence: dict[str, Any],
    model: str,
    prompt_payload: dict[str, Any],
    response_payload: dict[str, Any],
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> dict[str, Any]:
    validate_trigger_gate_response(response_payload)
    evidence_id = str(evidence["evidence_id"])
    prompt_hash = stable_hash(prompt_payload)
    response_hash = stable_hash(response_payload)
    input_token_count = max(int(input_tokens), 0)
    output_token_count = max(int(output_tokens), 0)
    payload = {
        "schema_version": 1,
        "decision_id": stable_hash(
            {
                "evidence_id": evidence_id,
                "model": model,
                "prompt_hash": prompt_hash,
                "response_hash": response_hash,
            }
        ),
        "evidence_id": evidence_id,
        "strategy_spec_hash": evidence.get("strategy_spec_hash"),
        "module_id": evidence.get("module_id"),
        "model": model,
        "prompt_hash": prompt_hash,
        "response_hash": response_hash,
        "decision": response_payload["decision"],
        "risk_level": response_payload["risk_level"],
        "confidence": float(response_payload.get("confidence", 0)),
        "reasons": list(response_payload.get("reasons", [])),
        "invalidation": list(response_payload.get("invalidation", [])),
        "required_follow_up": list(response_payload.get("required_follow_up", [])),
        "token_budget_note": str(response_payload.get("token_budget_note", "")),
        "input_tokens": input_token_count,
        "output_tokens": output_token_count,
        "total_tokens": input_token_count + output_token_count,
        "created_at": datetime.now(UTC).isoformat(),
    }
    if payload["decision"] == "allow":
        payload["runtime_action"] = "paper_intent"
    elif payload["decision"] == "block":
        payload["runtime_action"] = "no_intent"
    else:
        payload["runtime_action"] = "observe_only"
    return payload


def build_trigger_outcome_record(
    *,
    decision: dict[str, Any],
    outcome_window: str,
    net_pnl: float | None = None,
    mfe: float | None = None,
    mae: float | None = None,
    paper_fill_id: str | None = None,
    would_have_hit_target: bool | None = None,
    would_have_hit_stop: bool | None = None,
    final_label: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "decision_id": decision["decision_id"],
        "evidence_id": decision["evidence_id"],
        "paper_fill_id": paper_fill_id,
        "outcome_window": outcome_window,
        "mfe": mfe,
        "mae": mae,
        "net_pnl": net_pnl,
        "would_have_hit_target": would_have_hit_target,
        "would_have_hit_stop": would_have_hit_stop,
        "final_label": final_label or _default_outcome_label(decision, net_pnl),
        "notes": notes,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    payload["outcome_id"] = stable_hash(
        {
            "decision_id": payload["decision_id"],
            "outcome_window": outcome_window,
            "net_pnl": net_pnl,
            "final_label": payload["final_label"],
        }
    )
    return payload


def validate_trigger_gate_response(payload: dict[str, Any]) -> None:
    if payload.get("decision") not in TRIGGER_GATE_DECISIONS:
        raise ValueError("trigger gate decision must be allow, block, or observe")
    if payload.get("risk_level") not in TRIGGER_GATE_RISK_LEVELS:
        raise ValueError("trigger gate risk_level must be low, medium, or high")
    confidence = float(payload.get("confidence", 0))
    if confidence < 0 or confidence > 1:
        raise ValueError("trigger gate confidence must be between 0 and 1")
    forbidden_keys = {"live_gateway_command", "freeform_order", "broker_order"}
    if forbidden_keys.intersection(payload):
        raise ValueError("trigger gate response must not contain live execution commands")


def append_trigger_gate_memory(
    root: Path,
    *,
    evidence: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
) -> dict[str, Path]:
    paths = {name: root / filename for name, filename in TRIGGER_GATE_MEMORY_FILES.items()}
    if evidence is not None:
        append_jsonl(paths["evidence"], evidence)
    if decision is not None:
        append_jsonl(paths["decisions"], decision)
    if outcome is not None:
        append_jsonl(paths["outcomes"], outcome)
    return paths


def load_trigger_gate_memory(root: Path) -> dict[str, list[dict[str, Any]]]:
    return {
        name: load_jsonl(root / filename)
        for name, filename in TRIGGER_GATE_MEMORY_FILES.items()
    }


def build_token_budget_report(
    decisions: Sequence[dict[str, Any]],
    *,
    daily_token_budget: int | None = None,
) -> dict[str, Any]:
    total_tokens = sum(int(row.get("total_tokens") or 0) for row in decisions)
    decision_counts = {decision: 0 for decision in sorted(TRIGGER_GATE_DECISIONS)}
    for row in decisions:
        decision = str(row.get("decision", ""))
        if decision in decision_counts:
            decision_counts[decision] += 1
    remaining = None if daily_token_budget is None else max(int(daily_token_budget) - total_tokens, 0)
    return {
        "schema_version": 1,
        "decision_count": len(decisions),
        "llm_call_count": len(decisions),
        "token_total": total_tokens,
        "input_tokens": sum(int(row.get("input_tokens") or 0) for row in decisions),
        "output_tokens": sum(int(row.get("output_tokens") or 0) for row in decisions),
        "token_per_decision": total_tokens / len(decisions) if decisions else None,
        "daily_token_budget": daily_token_budget,
        "remaining_token_budget": remaining,
        "over_budget": bool(daily_token_budget is not None and total_tokens > int(daily_token_budget)),
        "decision_counts": decision_counts,
        "allow_rate": _decision_rate(decision_counts, "allow"),
        "block_rate": _decision_rate(decision_counts, "block"),
        "observe_rate": _decision_rate(decision_counts, "observe"),
    }


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid trigger gate JSONL at {path}:{line_number}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Trigger gate JSONL row must be an object at {path}:{line_number}")
        rows.append(payload)
    return rows


def _decision_rate(counts: dict[str, int], decision: str) -> float | None:
    total = sum(counts.values())
    return counts.get(decision, 0) / total if total else None


def _default_outcome_label(decision: dict[str, Any], net_pnl: float | None) -> str:
    if net_pnl is None:
        return "pending"
    if decision.get("decision") == "block":
        return "blocked_would_have_won" if net_pnl > 0 else "blocked_correctly"
    if decision.get("decision") == "allow":
        return "allowed_winner" if net_pnl > 0 else "allowed_loser"
    return "observed_winner" if net_pnl > 0 else "observed_loser"


def _isoformat(value: str | datetime) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
