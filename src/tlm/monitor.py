from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from .backtest import load_bar_rows
from .events import MacroEvent, context_for_timestamp


MONITOR_REVIEW_ACTIONS = {
    "no_action",
    "observe",
    "paper_allow",
    "paper_block",
    "research_candidate",
}


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    timeframe: str
    snapshot_time: datetime | None
    last_price: float | None
    session_high: float | None
    session_low: float | None
    opening_range_high: float | None
    opening_range_low: float | None
    vwap: float | None
    spread: float | None
    bar_count: int
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "snapshot_time": self.snapshot_time.isoformat() if self.snapshot_time else None,
            "last_price": self.last_price,
            "session_high": self.session_high,
            "session_low": self.session_low,
            "opening_range_high": self.opening_range_high,
            "opening_range_low": self.opening_range_low,
            "vwap": self.vwap,
            "spread": self.spread,
            "bar_count": self.bar_count,
            "source": self.source,
        }


def build_market_snapshot(
    symbol: str,
    timeframe: str,
    bars: Sequence[dict[str, Any]],
    opening_range_bars: int = 3,
    source: str = "local_bars",
) -> MarketSnapshot:
    if not bars:
        return MarketSnapshot(symbol, timeframe, None, None, None, None, None, None, None, None, 0, source)
    ordered = sorted(bars, key=lambda row: row["timestamp"])
    latest = ordered[-1]
    opening = ordered[: max(opening_range_bars, 1)]
    total_weight = sum(max(int(row.get("tick_count", 1)), 1) for row in ordered)
    vwap = (
        sum(float(row["close"]) * max(int(row.get("tick_count", 1)), 1) for row in ordered)
        / total_weight
        if total_weight
        else None
    )
    return MarketSnapshot(
        symbol=symbol,
        timeframe=timeframe,
        snapshot_time=latest["timestamp"],
        last_price=float(latest["close"]),
        session_high=max(float(row["high"]) for row in ordered),
        session_low=min(float(row["low"]) for row in ordered),
        opening_range_high=max(float(row["high"]) for row in opening),
        opening_range_low=min(float(row["low"]) for row in opening),
        vwap=vwap,
        spread=float(latest.get("avg_spread") or 0),
        bar_count=len(ordered),
        source=source,
    )


def scan_key_levels(
    snapshot: MarketSnapshot,
    proximity_points: float = 2.0,
) -> list[dict[str, Any]]:
    if snapshot.last_price is None:
        return []
    levels = {
        "session_high": snapshot.session_high,
        "session_low": snapshot.session_low,
        "opening_range_high": snapshot.opening_range_high,
        "opening_range_low": snapshot.opening_range_low,
        "vwap": snapshot.vwap,
    }
    signals = []
    for name, level in levels.items():
        if level is None:
            continue
        distance = float(snapshot.last_price - level)
        abs_distance = abs(distance)
        if abs_distance <= proximity_points:
            signals.append(
                {
                    "level": name,
                    "level_price": level,
                    "distance_points": distance,
                    "abs_distance_points": abs_distance,
                    "proximity_score": max(0.0, 1.0 - abs_distance / max(proximity_points, 1e-9)),
                }
            )
    return sorted(signals, key=lambda row: row["abs_distance_points"])


def score_monitor_signal(
    snapshot: MarketSnapshot,
    key_level_signals: Sequence[dict[str, Any]],
    event_state: str = "normal",
    max_importance: str | None = None,
) -> dict[str, Any]:
    if snapshot.last_price is None:
        return {"strength": 0.0, "bucket": "none", "signal_class": "none", "reasons": ["no_bars"]}
    level_score = max((float(signal["proximity_score"]) for signal in key_level_signals), default=0.0)
    event_bonus = 0.15 if event_state in {"pre_event", "release_window", "event_release", "post_event"} else 0.0
    strength = min(1.0, level_score + event_bonus)
    if event_state in {"release_window", "event_release"} and max_importance == "high":
        bucket = "blocked"
    elif strength >= 0.78:
        bucket = "strong_review"
    elif strength >= 0.60:
        bucket = "medium_watch"
    elif strength >= 0.35:
        bucket = "weak_notice"
    else:
        bucket = "none"
    reasons = [f"near_{signal['level']}" for signal in key_level_signals[:3]]
    if event_bonus:
        reasons.append(f"event_state_{event_state}")
    if bucket == "blocked":
        reasons.append("blocked_by_high_impact_release_window")
    return {"strength": strength, "bucket": bucket, "signal_class": bucket, "reasons": reasons}


def build_monitor_report(
    *,
    symbol: str,
    timeframe: str,
    bar_files: Sequence[Path],
    events: Sequence[MacroEvent] = (),
    proximity_points: float = 2.0,
    target_frequency_pool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bars = load_bar_rows(bar_files)
    snapshot = build_market_snapshot(symbol, timeframe, bars)
    event_context = (
        context_for_timestamp(symbol, snapshot.snapshot_time, events).to_dict()
        if snapshot.snapshot_time and events
        else {
            "symbol": symbol,
            "timestamp": snapshot.snapshot_time.isoformat() if snapshot.snapshot_time else None,
            "event_state": "normal",
            "active_event_ids": [],
            "max_importance": None,
            "policy_ref": None,
            "minutes_to_event": None,
            "minutes_since_event": None,
        }
    )
    key_levels = scan_key_levels(snapshot, proximity_points=proximity_points)
    signal = score_monitor_signal(
        snapshot,
        key_levels,
        event_state=event_context["event_state"],
        max_importance=event_context.get("max_importance"),
    )
    report = {
        "monitor_run_id": monitor_run_id(symbol, timeframe, snapshot.snapshot_time),
        "snapshot": snapshot.to_dict(),
        "key_levels": key_levels,
        "event_context": event_context,
        "signal": signal,
    }
    if target_frequency_pool is not None:
        report["trigger_gate"] = build_monitor_trigger_gate(report, target_frequency_pool)
    return report


def build_monitor_trigger_gate(
    monitor_report: dict[str, Any],
    target_frequency_pool: dict[str, Any],
    *,
    max_spread: float = 2.0,
) -> dict[str, Any]:
    signal = monitor_report.get("signal", {})
    selected = list(target_frequency_pool.get("selected", []))
    candidates = []
    for strategy in selected:
        risk_pre_gate = monitor_trigger_pre_gate(
            monitor_report,
            strategy,
            max_spread=max_spread,
        )
        candidates.append(
            {
                "strategy_spec_hash": strategy.get("strategy_spec_hash"),
                "strategy_name": strategy.get("strategy_name"),
                "module_id": strategy.get("module_id"),
                "timeframe": strategy.get("timeframe"),
                "trigger_reason": "monitor_signal",
                "signal_features": {
                    "monitor_bucket": signal.get("bucket"),
                    "monitor_strength": signal.get("strength"),
                    "proxy_win_rate": strategy.get("proxy_win_rate"),
                    "trades_per_day": strategy.get("trades_per_day"),
                },
                "risk_pre_gate": risk_pre_gate,
            }
        )
    eligible = [
        candidate
        for candidate in candidates
        if candidate["risk_pre_gate"]["passed"]
        and signal.get("bucket") in {"strong_review", "medium_watch"}
    ]
    return {
        "schema_version": 1,
        "pool_version": target_frequency_pool.get("pool_version"),
        "selected_count": len(selected),
        "candidate_count": len(candidates),
        "eligible_candidate_count": len(eligible),
        "llm_trigger_required": bool(eligible),
        "candidates": candidates,
        "forbidden_outputs": ["live_gateway_command", "freeform_order", "broker_order"],
    }


def monitor_trigger_pre_gate(
    monitor_report: dict[str, Any],
    strategy: dict[str, Any],
    *,
    max_spread: float = 2.0,
) -> dict[str, Any]:
    snapshot = monitor_report.get("snapshot", {})
    event_context = monitor_report.get("event_context", {})
    reasons = []
    hard_blocks = {
        "event_blackout": False,
        "data_stale": False,
        "spread_above_limit": False,
        "strategy_not_in_pool": False,
        "live_gateway_forbidden": True,
    }
    if not strategy.get("strategy_spec_hash"):
        hard_blocks["strategy_not_in_pool"] = True
        reasons.append("missing_strategy_spec_hash")
    if not snapshot.get("snapshot_time"):
        hard_blocks["data_stale"] = True
        reasons.append("missing_snapshot_time")
    if (
        event_context.get("event_state") in {"release_window", "event_release"}
        and event_context.get("max_importance") == "high"
    ):
        hard_blocks["event_blackout"] = True
        reasons.append("high_impact_event_blackout")
    spread = snapshot.get("spread")
    if spread is not None and float(spread) > max_spread:
        hard_blocks["spread_above_limit"] = True
        reasons.append("spread_above_limit")
    return {
        "passed": not any(value for key, value in hard_blocks.items() if key != "live_gateway_forbidden"),
        "reasons": reasons,
        "hard_blocks": hard_blocks,
    }


def monitor_run_id(symbol: str, timeframe: str, timestamp: datetime | None) -> str:
    suffix = timestamp.strftime("%Y%m%dT%H%M%S") if timestamp else "empty"
    return f"{symbol}_{timeframe}_{suffix}"


def render_monitor_report(payload: dict[str, Any]) -> str:
    snapshot = payload["snapshot"]
    signal = payload["signal"]
    levels = payload["key_levels"]
    lines = [
        f"# Runtime Monitor {payload['monitor_run_id']}",
        "",
        f"- symbol: {snapshot['symbol']}",
        f"- timeframe: {snapshot['timeframe']}",
        f"- snapshot_time: {snapshot['snapshot_time']}",
        f"- last_price: {snapshot['last_price']}",
        f"- signal_bucket: {signal['bucket']}",
        f"- signal_strength: {signal['strength']:.2f}",
        f"- reasons: {', '.join(signal['reasons']) if signal['reasons'] else 'none'}",
        "",
        "## Key Levels",
    ]
    if levels:
        lines.extend(
            f"- {level['level']}: {level['level_price']} ({level['distance_points']:+.2f})"
            for level in levels
        )
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def write_monitor_outputs(output_dir: Path, payload: dict[str, Any]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = payload["monitor_run_id"]
    json_path = output_dir / f"{run_id}.json"
    report_path = output_dir / f"{run_id}.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.write_text(render_monitor_report(payload), encoding="utf-8")
    return {"json": str(json_path), "report": str(report_path)}


def build_monitor_review_request(
    monitor_report: dict[str, Any],
    *,
    model: str = "local-deterministic-reviewer",
    temperature: float = 0.0,
) -> dict[str, Any]:
    snapshot_hash = stable_hash(monitor_report.get("snapshot", {}))
    prompt_payload = {
        "monitor_run_id": monitor_report.get("monitor_run_id"),
        "snapshot_hash": snapshot_hash,
        "signal": monitor_report.get("signal", {}),
        "event_context": monitor_report.get("event_context", {}),
        "key_levels": monitor_report.get("key_levels", []),
    }
    return {
        "schema_version": 1,
        "monitor_run_id": monitor_report.get("monitor_run_id"),
        "snapshot_hash": snapshot_hash,
        "model": model,
        "temperature": temperature,
        "prompt_hash": stable_hash(prompt_payload),
        "prompt_payload": prompt_payload,
    }


def build_structured_monitor_review(
    monitor_report: dict[str, Any],
    *,
    model: str = "local-deterministic-reviewer",
    temperature: float = 0.0,
) -> dict[str, Any]:
    request = build_monitor_review_request(
        monitor_report,
        model=model,
        temperature=temperature,
    )
    signal = monitor_report.get("signal", {})
    event_context = monitor_report.get("event_context", {})
    action = monitor_review_action(signal, event_context)
    result = {
        "schema_version": 1,
        "monitor_run_id": monitor_report.get("monitor_run_id"),
        "snapshot_hash": request["snapshot_hash"],
        "model": model,
        "temperature": temperature,
        "prompt_hash": request["prompt_hash"],
        "response_hash": "",
        "action": action,
        "bull_case": review_case("bull", monitor_report),
        "bear_case": review_case("bear", monitor_report),
        "risk_review": risk_review(monitor_report),
        "decision_summarizer": decision_summary(action, monitor_report),
        "invalidation": invalidation_plan(monitor_report),
        "created_at": datetime.now(UTC).isoformat(),
    }
    validate_monitor_review_result(result, event_context)
    result["response_hash"] = stable_hash({key: value for key, value in result.items() if key != "response_hash"})
    return result


def monitor_review_action(signal: dict[str, Any], event_context: dict[str, Any]) -> str:
    if event_context.get("event_state") in {"release_window", "event_release"} and event_context.get("max_importance") == "high":
        return "paper_block"
    bucket = signal.get("bucket")
    if bucket == "strong_review":
        return "research_candidate"
    if bucket == "medium_watch":
        return "observe"
    if bucket == "blocked":
        return "paper_block"
    return "no_action"


def validate_monitor_review_result(result: dict[str, Any], event_context: dict[str, Any] | None = None) -> None:
    missing = [
        key
        for key in ["bull_case", "bear_case", "risk_review", "decision_summarizer", "invalidation"]
        if not result.get(key)
    ]
    if missing:
        raise ValueError(f"Monitor review missing required sections: {', '.join(missing)}")
    action = result.get("action")
    if action not in MONITOR_REVIEW_ACTIONS:
        raise ValueError(f"Unsupported monitor review action: {action}")
    event_context = event_context or {}
    if (
        action == "paper_allow"
        and event_context.get("event_state") in {"release_window", "event_release"}
        and event_context.get("max_importance") == "high"
    ):
        raise ValueError("paper_allow is forbidden during high-impact release windows")


def review_case(side: str, monitor_report: dict[str, Any]) -> dict[str, Any]:
    levels = monitor_report.get("key_levels", [])
    signal = monitor_report.get("signal", {})
    prefix = "Continuation" if side == "bull" else "Failure"
    return {
        "summary": f"{prefix} case from {signal.get('bucket', 'none')} signal.",
        "evidence": [f"near_{level['level']}" for level in levels[:3]],
        "confidence": min(1.0, float(signal.get("strength", 0.0))),
    }


def risk_review(monitor_report: dict[str, Any]) -> dict[str, Any]:
    event_context = monitor_report.get("event_context", {})
    reasons = list(monitor_report.get("signal", {}).get("reasons", []))
    if event_context.get("event_state") != "normal":
        reasons.append(f"event_state_{event_context.get('event_state')}")
    return {
        "allowed_modes": ["research", "paper_shadow"],
        "forbidden_outputs": ["live_gateway_command", "freeform_order"],
        "risk_flags": reasons,
        "event_state": event_context.get("event_state", "normal"),
    }


def decision_summary(action: str, monitor_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": action,
        "direct_execution_allowed": False,
        "research_candidate_requires_phase_1": action == "research_candidate",
        "signal_bucket": monitor_report.get("signal", {}).get("bucket"),
    }


def invalidation_plan(monitor_report: dict[str, Any]) -> dict[str, Any]:
    snapshot = monitor_report.get("snapshot", {})
    return {
        "last_price": snapshot.get("last_price"),
        "invalid_if_signal_bucket_changes": True,
        "invalid_if_event_state_worsens": True,
    }


def stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
