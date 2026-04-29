from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import get_cost_model, get_symbol
from .execution import evaluate_live_readiness
from .research_config import PRIMARY_COST_MODEL, PRIMARY_RESEARCH_SYMBOL
from .snapshot import cost_model_hash


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


def build_primary_nq_external_readiness(
    *,
    data_root: Path,
    experiments_root: Path,
    config_dir: Path = Path("configs"),
    symbol: str = PRIMARY_RESEARCH_SYMBOL,
    cost_model_name: str = PRIMARY_COST_MODEL,
    require_mbp1: bool = False,
    institutional_acceptance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    symbol_config = get_symbol(symbol, config_dir)
    cost_model = get_cost_model(cost_model_name, config_dir)
    quote_files = sorted((data_root / "normalized" / "quotes" / symbol).glob("date=*/*.parquet"))
    mbp1_files = sorted((data_root / "normalized" / "mbp1" / symbol).glob("date=*/*.parquet"))
    freeze_status = _final_holdout_freeze_status(experiments_root)
    acceptance = institutional_acceptance or {}
    missing = []
    if symbol_config.provider != "databento" or symbol_config.instrument != "NQ":
        missing.append("nq_cme_databento_symbol_config")
    if not quote_files:
        missing.append("representative_nq_cme_quote_data")
    if require_mbp1 and not mbp1_files:
        missing.append("mbp1_depth_data")
    if not freeze_status["freeze_confirmed"]:
        missing.append("final_holdout_freeze_confirmed_strategy")
    if acceptance.get("reduced_candidate_volume_accepted") is not True:
        missing.append("institutional_acceptance_reduced_candidate_volume")
    payload = {
        "schema_version": 1,
        "artifact": "primary_nq_external_readiness",
        "symbol": {
            "alias": symbol_config.alias,
            "provider": symbol_config.provider,
            "instrument": symbol_config.instrument,
            "tick_size": symbol_config.tick_size,
            "point_value": symbol_config.point_value,
        },
        "quote_coverage": {
            "status": "ready" if quote_files else "missing",
            "file_count": len(quote_files),
            "dates": _partition_dates(quote_files),
            "files": [str(path) for path in quote_files],
            "required": True,
        },
        "mbp1_coverage": {
            "status": "ready" if mbp1_files else "missing",
            "file_count": len(mbp1_files),
            "dates": _partition_dates(mbp1_files),
            "files": [str(path) for path in mbp1_files],
            "required": require_mbp1,
        },
        "cost_model": {
            "name": cost_model.name,
            "hash": cost_model_hash(cost_model),
            "tick_size": cost_model.tick_size,
            "point_value": cost_model.point_value,
            "tick_value": cost_model.tick_value,
            "slippage_ticks_per_side": cost_model.slippage_ticks_per_side,
            "round_trip_fees_usd": cost_model.round_trip_fees_usd,
            "status": "frozen_conservative",
        },
        "final_holdout_freeze": freeze_status,
        "institutional_acceptance": {
            "reduced_candidate_volume_accepted": acceptance.get("reduced_candidate_volume_accepted") is True,
            "accepted_by": acceptance.get("accepted_by"),
            "accepted_at": acceptance.get("accepted_at"),
            "notes": acceptance.get("notes"),
        },
        "missing_external_blockers": sorted(set(missing)),
        "decision": "ready" if not missing else "blocked",
        "checked_at": datetime.now(UTC).isoformat(),
    }
    payload["artifact_hash"] = stable_hash({key: value for key, value in payload.items() if key != "artifact_hash"})
    return payload


def write_primary_nq_external_readiness_artifact(path: Path, artifact: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _final_holdout_freeze_status(experiments_root: Path) -> dict[str, Any]:
    try:
        from .research import load_leaderboard_report

        report = load_leaderboard_report(experiments_root)
    except Exception as exc:
        return {
            "status": "blocked",
            "freeze_confirmed": False,
            "freeze_confirmed_count": 0,
            "error": str(exc),
        }
    freeze_rows = report.get("freeze_confirmed_leaderboard", [])
    return {
        "status": "ready" if freeze_rows else "missing",
        "freeze_confirmed": bool(freeze_rows),
        "freeze_confirmed_count": len(freeze_rows),
        "experiment_ids": [str(row.get("experiment_id")) for row in freeze_rows],
        "summary": report.get("summary", {}),
    }


def _partition_dates(paths: list[Path]) -> list[str]:
    dates = []
    for path in paths:
        for parent in path.parents:
            if parent.name.startswith("date="):
                dates.append(parent.name.removeprefix("date="))
                break
    return sorted(set(dates))


def stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
