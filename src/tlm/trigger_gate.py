from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
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
DEFAULT_FORWARD_TEST_WINDOWS = (("48h", 2), ("7d", 7), ("30d", 30), ("90d", 90))


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


def run_trigger_gate_simulation(
    *,
    target_frequency_pool: dict[str, Any],
    output_dir: Path,
    replay_start: date | datetime | str,
    replay_end: date | datetime | str,
    enable_llm: bool = False,
    step_minutes: int = 15,
    model: str = "local-trigger-gate",
    daily_token_budget: int | None = None,
) -> dict[str, Any]:
    start_at = _coerce_datetime(replay_start, end_of_day=False)
    end_at = _coerce_datetime(replay_end, end_of_day=True)
    if end_at <= start_at:
        raise ValueError("replay_end must be after replay_start")
    output_dir.mkdir(parents=True, exist_ok=True)
    selected_pool = list(target_frequency_pool.get("selected", []))
    triggers = generate_trigger_events(
        selected_pool,
        replay_start=start_at,
        replay_end=end_at,
    )
    evidence_records = []
    decision_records = []
    remaining_budget = daily_token_budget
    for trigger in triggers:
        evidence = build_trigger_evidence_record(
            strategy_spec_hash=str(trigger.get("strategy_spec_hash") or trigger["trigger_id"]),
            module_id=str(trigger.get("module_id") or "unknown"),
            strategy_name=trigger.get("strategy_name"),
            timeframe=trigger.get("timeframe"),
            signal_time=trigger["signal_time"],
            signal_features=trigger["signal_features"],
            market_snapshot=trigger["market_snapshot"],
            event_context=trigger["event_context"],
            risk_pre_gate=trigger["risk_pre_gate"],
            pool_version=str(target_frequency_pool.get("pool_version") or "target_frequency_pool"),
            trigger_reason="strategy_signal",
        )
        evidence_records.append(evidence)
        if enable_llm and evidence["risk_pre_gate"].get("passed"):
            decision, remaining_budget = build_deterministic_trigger_gate_decision(
                evidence,
                model=model,
                remaining_token_budget=remaining_budget,
            )
            decision_records.append(decision)

    for evidence in evidence_records:
        append_trigger_gate_memory(output_dir, evidence=evidence)
    for decision in decision_records:
        append_trigger_gate_memory(output_dir, decision=decision)

    duration_days = _duration_days(start_at, end_at)
    token_budget = build_token_budget_report(decision_records, daily_token_budget=daily_token_budget)
    manifest = {
        "schema_version": 1,
        "simulation_id": stable_hash(
            {
                "pool": target_frequency_pool,
                "replay_start": start_at.isoformat(),
                "replay_end": end_at.isoformat(),
                "enable_llm": enable_llm,
            }
        ),
        "mode": "llm_enabled" if enable_llm else "frequency_only",
        "replay_start": start_at.isoformat(),
        "replay_end": end_at.isoformat(),
        "duration_days": duration_days,
        "step_minutes": step_minutes,
        "event_count": int(duration_days * 24 * 60 / max(step_minutes, 1)),
        "trigger_count": len(evidence_records),
        "trigger_per_day": len(evidence_records) / duration_days if duration_days else 0,
        "llm_call_count": len(decision_records),
        "token_budget": token_budget,
        "target_frequency_pool": target_frequency_pool,
        "selected_strategy_pool": selected_pool,
        "artifacts": {
            "manifest": "manifest.json",
            "evidence": TRIGGER_GATE_MEMORY_FILES["evidence"],
            "decisions": TRIGGER_GATE_MEMORY_FILES["decisions"],
            "outcomes": TRIGGER_GATE_MEMORY_FILES["outcomes"],
            "forward_test_report": "forward_test_report.json",
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    forward_test_report = build_forward_test_report(
        manifest,
        {
            "evidence": evidence_records,
            "decisions": decision_records,
            "outcomes": [],
        },
    )
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    (output_dir / "forward_test_report.json").write_text(
        json.dumps(forward_test_report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return manifest


def generate_trigger_events(
    selected_pool: Sequence[dict[str, Any]],
    *,
    replay_start: datetime,
    replay_end: datetime,
) -> list[dict[str, Any]]:
    duration_days = _duration_days(replay_start, replay_end)
    events = []
    for pool_index, strategy in enumerate(selected_pool):
        daily_rate = float(strategy.get("trades_per_day") or 0)
        trigger_count = int(round(daily_rate * duration_days))
        if trigger_count <= 0 and daily_rate > 0 and duration_days >= 1:
            trigger_count = 1
        for trigger_index in range(trigger_count):
            offset_fraction = (trigger_index + 1) / (trigger_count + 1)
            offset_seconds = int((replay_end - replay_start).total_seconds() * offset_fraction)
            signal_time = replay_start + timedelta(seconds=offset_seconds + pool_index * 60)
            signal_time = min(signal_time, replay_end)
            events.append(_build_synthetic_trigger_event(strategy, signal_time, trigger_index))
    return sorted(events, key=lambda item: item["signal_time"])


def build_deterministic_trigger_gate_decision(
    evidence: dict[str, Any],
    *,
    model: str,
    remaining_token_budget: int | None,
) -> tuple[dict[str, Any], int | None]:
    prompt_payload = {
        "task": "trigger_gate_review",
        "evidence": evidence,
        "allowed_decisions": sorted(TRIGGER_GATE_DECISIONS),
        "forbidden_outputs": ["live_gateway_command", "freeform_order", "broker_order"],
    }
    estimated_input_tokens = max(len(json.dumps(prompt_payload, sort_keys=True, default=str)) // 4, 1)
    estimated_output_tokens = 96
    estimated_total_tokens = estimated_input_tokens + estimated_output_tokens
    if remaining_token_budget is not None and estimated_total_tokens > remaining_token_budget:
        response_payload = {
            "decision": "observe",
            "risk_level": "medium",
            "confidence": 0.5,
            "reasons": ["token_budget_exhausted"],
            "invalidation": [],
            "required_follow_up": ["increase_budget_or_reduce_pool"],
            "token_budget_note": "deterministic fallback without LLM call",
        }
        decision = build_trigger_decision_record(
            evidence=evidence,
            model="deterministic-token-budget-fallback",
            prompt_payload=prompt_payload,
            response_payload=response_payload,
            input_tokens=0,
            output_tokens=0,
        )
        return decision, remaining_token_budget
    confidence = min(max(float(evidence.get("signal_features", {}).get("proxy_win_rate", 0.53)), 0.0), 1.0)
    response_payload = {
        "decision": "allow" if confidence >= 0.57 else "observe",
        "risk_level": "low" if confidence >= 0.6 else "medium",
        "confidence": confidence,
        "reasons": ["strategy_signal_confirmed", "deterministic_pre_gate_passed"],
        "invalidation": ["risk_pre_gate_turns_false", "spread_or_event_risk_expands"],
        "required_follow_up": [],
        "token_budget_note": "within budget",
    }
    decision = build_trigger_decision_record(
        evidence=evidence,
        model=model,
        prompt_payload=prompt_payload,
        response_payload=response_payload,
        input_tokens=estimated_input_tokens,
        output_tokens=estimated_output_tokens,
    )
    if remaining_token_budget is None:
        return decision, None
    return decision, max(remaining_token_budget - estimated_total_tokens, 0)


def load_trigger_gate_forward_report(
    output_dir: Path,
    *,
    previous_pool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"trigger gate manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return build_forward_test_report(
        manifest,
        load_trigger_gate_memory(output_dir),
        previous_pool=previous_pool,
    )


def build_forward_test_report(
    manifest: dict[str, Any],
    memory: dict[str, list[dict[str, Any]]],
    *,
    previous_pool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = memory.get("evidence", [])
    decisions = memory.get("decisions", [])
    outcomes = memory.get("outcomes", [])
    selected_pool = list(manifest.get("selected_strategy_pool", []))
    token_budget = build_token_budget_report(decisions)
    outcome_rows = [row for row in outcomes if row.get("net_pnl") is not None]
    actual_paper_win_rate = (
        sum(1 for row in outcome_rows if float(row.get("net_pnl") or 0) > 0) / len(outcome_rows)
        if outcome_rows
        else None
    )
    proxy_win_rate = _pool_weighted_proxy_win_rate(manifest.get("target_frequency_pool", {}), selected_pool)
    return {
        "schema_version": 1,
        "lookback_days": manifest.get("duration_days"),
        "forward_days": manifest.get("duration_days"),
        "selected_strategy_pool": selected_pool,
        "trigger_per_day": manifest.get("trigger_per_day"),
        "trigger_count": len(evidence),
        "proxy_win_rate": proxy_win_rate,
        "actual_paper_win_rate": actual_paper_win_rate,
        "proxy_outcome_drift": build_proxy_outcome_drift_report(
            selected_pool=selected_pool,
            decisions=decisions,
            outcomes=outcomes,
            pool_proxy_win_rate=proxy_win_rate,
        ),
        "allow_rate": token_budget["allow_rate"],
        "block_rate": token_budget["block_rate"],
        "observe_rate": token_budget["observe_rate"],
        "token_total": token_budget["token_total"],
        "token_per_trigger": token_budget["token_total"] / len(evidence) if evidence else None,
        "decision_outcome_confusion": build_decision_outcome_confusion(decisions, outcomes),
        "block_opportunity_cost": build_block_opportunity_cost_report(decisions, outcomes),
        "strategy_pool_changes": build_strategy_pool_change_report(previous_pool, manifest.get("target_frequency_pool")),
        "llm_call_count": len(decisions),
        "llm_calls_match_triggers": len(decisions) == len(evidence) if manifest.get("mode") == "llm_enabled" else None,
        "live_gateway_command_count": 0,
    }


def build_block_opportunity_cost_report(
    decisions: Sequence[dict[str, Any]],
    outcomes: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    decisions_by_id = {str(row.get("decision_id")): row for row in decisions if row.get("decision_id")}
    block_decisions = [row for row in decisions if row.get("decision") == "block"]
    block_outcomes = [
        (decisions_by_id[str(outcome.get("decision_id"))], outcome)
        for outcome in outcomes
        if outcome.get("decision_id")
        and str(outcome.get("decision_id")) in decisions_by_id
        and decisions_by_id[str(outcome.get("decision_id"))].get("decision") == "block"
        and outcome.get("net_pnl") is not None
    ]
    missed_winners = [(decision, outcome) for decision, outcome in block_outcomes if float(outcome.get("net_pnl") or 0) > 0]
    avoided_losers = [(decision, outcome) for decision, outcome in block_outcomes if float(outcome.get("net_pnl") or 0) <= 0]
    opportunity_cost = sum(float(outcome.get("net_pnl") or 0) for _, outcome in missed_winners)
    avoided_loss = abs(sum(float(outcome.get("net_pnl") or 0) for _, outcome in avoided_losers))
    net_blocked_pnl = sum(float(outcome.get("net_pnl") or 0) for _, outcome in block_outcomes)
    by_strategy: dict[str, dict[str, Any]] = {}
    for decision, outcome in block_outcomes:
        strategy_hash = str(decision.get("strategy_spec_hash") or "unknown")
        row = by_strategy.setdefault(
            strategy_hash,
            {
                "strategy_spec_hash": strategy_hash,
                "strategy_name": decision.get("strategy_name"),
                "module_id": decision.get("module_id"),
                "blocked_outcome_count": 0,
                "missed_winner_count": 0,
                "avoided_loser_count": 0,
                "opportunity_cost": 0.0,
                "avoided_loss": 0.0,
                "net_blocked_pnl": 0.0,
            },
        )
        net_pnl = float(outcome.get("net_pnl") or 0)
        row["blocked_outcome_count"] += 1
        row["net_blocked_pnl"] += net_pnl
        if net_pnl > 0:
            row["missed_winner_count"] += 1
            row["opportunity_cost"] += net_pnl
        else:
            row["avoided_loser_count"] += 1
            row["avoided_loss"] += abs(net_pnl)
    return {
        "schema_version": 1,
        "status": _block_opportunity_status(block_decisions, block_outcomes, opportunity_cost),
        "block_decision_count": len(block_decisions),
        "blocked_outcome_count": len(block_outcomes),
        "pending_block_outcome_count": max(len(block_decisions) - len(block_outcomes), 0),
        "missed_winner_count": len(missed_winners),
        "avoided_loser_count": len(avoided_losers),
        "missed_winner_rate": len(missed_winners) / len(block_outcomes) if block_outcomes else None,
        "opportunity_cost": opportunity_cost,
        "avoided_loss": avoided_loss,
        "net_blocked_pnl": net_blocked_pnl,
        "opportunity_cost_per_block": opportunity_cost / len(block_decisions) if block_decisions else None,
        "strategy_rows": sorted(by_strategy.values(), key=lambda row: (-row["opportunity_cost"], row["strategy_spec_hash"])),
    }


def _block_opportunity_status(
    block_decisions: Sequence[dict[str, Any]],
    block_outcomes: Sequence[tuple[dict[str, Any], dict[str, Any]]],
    opportunity_cost: float,
) -> str:
    if not block_decisions:
        return "no_blocks"
    if not block_outcomes:
        return "pending_outcomes"
    if opportunity_cost > 0:
        return "missed_winners_found"
    return "blocks_avoided_losses"


def build_proxy_outcome_drift_report(
    *,
    selected_pool: Sequence[dict[str, Any]],
    decisions: Sequence[dict[str, Any]],
    outcomes: Sequence[dict[str, Any]],
    pool_proxy_win_rate: float | None,
    tolerance: float = 0.05,
    minimum_sample_size: int = 10,
) -> dict[str, Any]:
    outcomes_by_decision = {str(row.get("decision_id")): row for row in outcomes if row.get("decision_id")}
    matched = [
        (decision, outcomes_by_decision[str(decision.get("decision_id"))])
        for decision in decisions
        if decision.get("decision_id") and str(decision.get("decision_id")) in outcomes_by_decision
    ]
    actual_win_rate = _win_rate_from_outcomes([outcome for _, outcome in matched])
    drift = (
        actual_win_rate - float(pool_proxy_win_rate)
        if actual_win_rate is not None and pool_proxy_win_rate is not None
        else None
    )
    sample_size = len(matched)
    strategy_rows = []
    for strategy in selected_pool:
        strategy_hash = strategy.get("strategy_spec_hash")
        strategy_decisions = [
            (decision, outcome)
            for decision, outcome in matched
            if strategy_hash and decision.get("strategy_spec_hash") == strategy_hash
        ]
        actual = _win_rate_from_outcomes([outcome for _, outcome in strategy_decisions])
        proxy = strategy.get("proxy_win_rate")
        row_drift = actual - float(proxy) if actual is not None and proxy is not None else None
        strategy_rows.append(
            {
                "strategy_spec_hash": strategy_hash,
                "strategy_name": strategy.get("strategy_name"),
                "module_id": strategy.get("module_id"),
                "proxy_win_rate": proxy,
                "actual_paper_win_rate": actual,
                "drift": row_drift,
                "outcome_count": len(strategy_decisions),
                "status": _drift_status(row_drift, tolerance, has_outcomes=bool(strategy_decisions)),
            }
        )
    return {
        "schema_version": 1,
        "status": _drift_status(drift, tolerance, has_outcomes=bool(matched)),
        "sample_status": "sufficient" if sample_size >= minimum_sample_size else "insufficient",
        "minimum_sample_size": minimum_sample_size,
        "outcome_count": sample_size,
        "proxy_win_rate": pool_proxy_win_rate,
        "actual_paper_win_rate": actual_win_rate,
        "drift": drift,
        "tolerance": tolerance,
        "strategy_rows": strategy_rows,
    }


def _pool_weighted_proxy_win_rate(
    target_frequency_pool: dict[str, Any],
    selected_pool: Sequence[dict[str, Any]],
) -> float | None:
    if target_frequency_pool.get("weighted_proxy_win_rate") is not None:
        return float(target_frequency_pool["weighted_proxy_win_rate"])
    weighted_rows = [
        (float(row.get("trades_per_day") or 0), row.get("proxy_win_rate"))
        for row in selected_pool
        if row.get("proxy_win_rate") is not None
    ]
    total_rate = sum(rate for rate, _ in weighted_rows)
    if total_rate <= 0:
        return None
    return sum(rate * float(proxy) for rate, proxy in weighted_rows) / total_rate


def _win_rate_from_outcomes(outcomes: Sequence[dict[str, Any]]) -> float | None:
    scored = [row for row in outcomes if row.get("net_pnl") is not None]
    if not scored:
        return None
    return sum(1 for row in scored if float(row.get("net_pnl") or 0) > 0) / len(scored)


def _drift_status(drift: float | None, tolerance: float, *, has_outcomes: bool) -> str:
    if not has_outcomes:
        return "no_outcomes"
    if drift is None:
        return "missing_proxy"
    if drift < -abs(tolerance):
        return "proxy_overstates_outcomes"
    if drift > abs(tolerance):
        return "proxy_understates_outcomes"
    return "within_tolerance"


def build_decision_outcome_confusion(
    decisions: Sequence[dict[str, Any]],
    outcomes: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    outcomes_by_decision = {row.get("decision_id"): row for row in outcomes}
    rows: dict[str, dict[str, int]] = {}
    for decision in decisions:
        decision_name = str(decision.get("decision") or "unknown")
        outcome = outcomes_by_decision.get(decision.get("decision_id"), {})
        label = str(outcome.get("final_label") or "pending")
        bucket = rows.setdefault(decision_name, {})
        bucket[label] = bucket.get(label, 0) + 1
    return {
        "rows": rows,
        "decision_count": len(decisions),
        "outcome_count": len(outcomes),
        "pending_outcome_count": max(len(decisions) - len(outcomes_by_decision), 0),
    }


def build_strategy_pool_change_report(
    previous_pool: dict[str, Any] | None,
    current_pool: dict[str, Any] | None,
) -> dict[str, Any]:
    previous_hashes = _pool_strategy_hashes(previous_pool or {})
    current_hashes = _pool_strategy_hashes(current_pool or {})
    return {
        "previous_count": len(previous_hashes),
        "current_count": len(current_hashes),
        "added_strategy_hashes": sorted(current_hashes - previous_hashes),
        "removed_strategy_hashes": sorted(previous_hashes - current_hashes),
        "unchanged_strategy_hashes": sorted(previous_hashes.intersection(current_hashes)),
    }


def build_forward_test_schedule(
    *,
    target_frequency_pool: dict[str, Any],
    as_of: date | datetime | str,
    output_root: Path,
    enable_llm: bool = False,
    daily_token_budget: int | None = None,
    windows: Sequence[tuple[str, int]] = DEFAULT_FORWARD_TEST_WINDOWS,
) -> dict[str, Any]:
    as_of_date = _coerce_datetime(as_of, end_of_day=True).date()
    runs = []
    for label, days in windows:
        date_to = as_of_date
        date_from = as_of_date - timedelta(days=max(days, 1) - 1)
        output_dir = output_root / label
        runs.append(
            {
                "label": label,
                "days": days,
                "task_type": "trigger_gate.simulate",
                "payload": {
                    "target_frequency_pool": target_frequency_pool,
                    "from": date_from.isoformat(),
                    "to": date_to.isoformat(),
                    "output_dir": str(output_dir),
                    "enable_llm": enable_llm,
                    "daily_token_budget": daily_token_budget,
                },
                "report_payload": {
                    "output_dir": str(output_dir),
                },
            }
        )
    return {
        "schema_version": 1,
        "schedule_id": stable_hash(
            {
                "target_frequency_pool": target_frequency_pool,
                "as_of": as_of_date.isoformat(),
                "windows": list(windows),
                "enable_llm": enable_llm,
            }
        ),
        "as_of": as_of_date.isoformat(),
        "run_count": len(runs),
        "runs": runs,
        "created_at": datetime.now(UTC).isoformat(),
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


def _build_synthetic_trigger_event(
    strategy: dict[str, Any],
    signal_time: datetime,
    trigger_index: int,
) -> dict[str, Any]:
    proxy_win_rate = float(strategy.get("proxy_win_rate") or 0)
    spread = float(strategy.get("simulated_spread") or 0.5)
    risk_pre_gate = {
        "passed": spread <= float(strategy.get("max_spread", 2.0)),
        "reasons": [] if spread <= float(strategy.get("max_spread", 2.0)) else ["spread_above_limit"],
        "hard_blocks": {
            "event_blackout": False,
            "data_stale": False,
            "spread_above_limit": spread > float(strategy.get("max_spread", 2.0)),
            "strategy_not_in_pool": False,
            "live_gateway_forbidden": True,
        },
    }
    return {
        "trigger_id": stable_hash(
            {
                "strategy_spec_hash": strategy.get("strategy_spec_hash"),
                "module_id": strategy.get("module_id"),
                "signal_time": signal_time.isoformat(),
                "trigger_index": trigger_index,
            }
        ),
        "strategy_spec_hash": strategy.get("strategy_spec_hash"),
        "module_id": strategy.get("module_id"),
        "strategy_name": strategy.get("strategy_name"),
        "timeframe": strategy.get("timeframe"),
        "signal_time": signal_time,
        "signal_features": {
            "entry_signal": True,
            "proxy_win_rate": proxy_win_rate,
            "trades_per_day": float(strategy.get("trades_per_day") or 0),
            "trigger_index": trigger_index,
        },
        "market_snapshot": {
            "source": "synthetic_trigger_replay",
            "snapshot_time": signal_time.isoformat(),
            "last_price": float(strategy.get("simulated_last_price") or 19000 + trigger_index),
            "spread": spread,
        },
        "event_context": {
            "event_state": "normal",
            "active_event_ids": [],
            "max_importance": None,
        },
        "risk_pre_gate": risk_pre_gate,
    }


def _coerce_datetime(value: date | datetime | str, *, end_of_day: bool) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.max if end_of_day else time.min, tzinfo=UTC)
    parsed_date = date.fromisoformat(str(value))
    return datetime.combine(parsed_date, time.max if end_of_day else time.min, tzinfo=UTC)


def _duration_days(start_at: datetime, end_at: datetime) -> float:
    return max((end_at - start_at).total_seconds() / 86_400, 1 / 86_400)


def _pool_strategy_hashes(pool: dict[str, Any]) -> set[str]:
    return {
        str(row.get("strategy_spec_hash"))
        for row in pool.get("selected", [])
        if row.get("strategy_spec_hash")
    }
