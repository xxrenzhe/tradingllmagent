from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .execution import evaluate_live_readiness


REQUIRED_COVERAGE_WINDOWS = {"open", "midday", "close", "high_volatility", "high_impact_event"}

STAGE_EVIDENCE_REQUIREMENTS = {
    "paper_shadow": {
        "required_true": ["replay_consistent"],
        "min_values": {"trading_days": 10},
        "max_values": {"p95_spread_slippage_drift": "max_allowed_drift"},
        "coverage_windows": REQUIRED_COVERAGE_WINDOWS,
    },
    "nt8_sim": {
        "required_true": [
            "external_nt8_validated",
            "sim_account_only",
            "independent_gateway_process",
            "disconnect_reconnect_validated",
            "idempotency_validated",
            "flatten_validated",
        ],
        "min_any": {"trading_days": 5, "sim_commands": 200},
        "max_values": {"reconcile_drift_count": 0},
    },
    "micro_live": {
        "required_true": [
            "external_nt8_validated",
            "minimal_position",
            "manual_approval_audited",
            "broker_side_protection",
        ],
        "min_any": {"trade_count": 20, "trading_days": 10},
        "max_values": {"unresolved_incident_count": 0},
    },
    "controlled_live": {
        "required_true": [
            "external_broker_validated",
            "strategy_profile_whitelisted",
            "kill_switch_verified",
            "automation_profile_limited",
        ],
        "min_any": {"sample_trades": 100, "trading_days": 30},
        "max_values": {"high_risk_drift_count": 0},
    },
}


def evaluate_external_validation(stage: str, evidence: dict[str, Any]) -> dict[str, Any]:
    if stage not in STAGE_EVIDENCE_REQUIREMENTS:
        raise ValueError(f"Unsupported readiness stage: {stage}")
    base = evaluate_live_readiness(stage, evidence)
    missing = []
    requirements = STAGE_EVIDENCE_REQUIREMENTS[stage]
    for key in requirements.get("required_true", []):
        if evidence.get(key) is not True:
            missing.append(key)
    for key, minimum in requirements.get("min_values", {}).items():
        if float(evidence.get(key, 0)) < float(minimum):
            missing.append(key)
    if requirements.get("min_any"):
        min_any = requirements["min_any"]
        if not any(float(evidence.get(key, 0)) >= float(minimum) for key, minimum in min_any.items()):
            missing.append("_or_".join(min_any))
    for key, maximum in requirements.get("max_values", {}).items():
        resolved_maximum = evidence.get(maximum) if isinstance(maximum, str) else maximum
        if resolved_maximum is None or float(evidence.get(key, float(resolved_maximum) + 1)) > float(resolved_maximum):
            missing.append(key)
    required_windows = requirements.get("coverage_windows")
    if required_windows:
        covered = set(evidence.get("coverage_windows", []))
        missing_windows = sorted(required_windows - covered)
        missing.extend(f"coverage_window:{window}" for window in missing_windows)
    passed = base["passed"] and not missing
    return {
        "schema_version": 1,
        "stage": stage,
        "passed": passed,
        "decision": "ready" if passed else "blocked",
        "base_readiness": base,
        "missing_evidence": sorted(set(missing)),
        "evidence": evidence,
        "evidence_hash": stable_hash(evidence),
        "checked_at": datetime.now(UTC).isoformat(),
    }


def build_external_validation_artifact(stage: str, evidence: dict[str, Any]) -> dict[str, Any]:
    decision = evaluate_external_validation(stage, evidence)
    artifact = {
        "schema_version": 1,
        "artifact": "external_validation_evidence",
        "stage": stage,
        "decision": decision,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    artifact["artifact_hash"] = stable_hash({key: value for key, value in artifact.items() if key != "artifact_hash"})
    return artifact


def write_external_validation_artifact(path: Path, artifact: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
