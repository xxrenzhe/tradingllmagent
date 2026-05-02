from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Sequence


ALLOWED_REVIEW_ACTIONS = {
    "no_action",
    "observe",
    "paper_allow",
    "paper_block",
    "research_candidate",
    "mutation_proposal",
}
ALLOWED_FAST_PATH_CHANGES = {
    "pause_strategy",
    "demote_strategy",
    "raise_min_confidence",
    "tighten_max_spread_ticks",
    "reduce_daily_trade_cap",
    "narrow_session",
    "observe_only",
    "safe_mode",
    "kill_switch",
}
ALLOWED_MUTATION_CHANGES = {
    "adjust_threshold_range",
    "add_filter",
    "remove_filter",
    "adjust_session_filter",
    "adjust_exit_rule",
    "retire_strategy",
}


def build_five_minute_review_request(
    *,
    bars_1m: Sequence[dict[str, Any]],
    signals: Sequence[dict[str, Any]],
    execution_ledger: dict[str, Any],
    risk_context: dict[str, Any] | None = None,
    strategy_state: dict[str, Any] | None = None,
    previous_reviews: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    strong_signals = [signal for signal in signals if signal.get("signal_class") == "strong_review"]
    blocked_signals = [signal for signal in signals if signal.get("signal_class") == "blocked"]
    request = {
        "schema_version": 1,
        "source": "ibkr_5m_review_loop",
        "review_window": "5m",
        "created_at": datetime.now(UTC).isoformat(),
        "bars_1m": list(bars_1m)[-5:],
        "signals": list(signals),
        "strong_signal_count": len(strong_signals),
        "blocked_signal_count": len(blocked_signals),
        "execution_ledger_summary": _ledger_summary(execution_ledger),
        "risk_context": risk_context or {},
        "strategy_state": strategy_state or {},
        "previous_review_summaries": [_review_summary(review) for review in list(previous_reviews)[-3:]],
        "allowed_actions": sorted(ALLOWED_REVIEW_ACTIONS),
        "allowed_fast_path_changes": sorted(ALLOWED_FAST_PATH_CHANGES),
        "allowed_mutation_changes": sorted(ALLOWED_MUTATION_CHANGES),
        "forbidden_outputs": ["ibkr_order_object", "live_order", "increase_risk_auto_apply"],
    }
    request["review_request_hash"] = _stable_hash(request)
    return request


def deterministic_fallback_review(request: dict[str, Any]) -> dict[str, Any]:
    risk_context = request.get("risk_context", {})
    ledger = request.get("execution_ledger_summary", {})
    blocked_reasons = []
    fast_changes = []
    strategy_state = request.get("strategy_state", {})
    max_concurrent_positions = max(
        int(strategy_state.get("max_concurrent_positions", risk_context.get("max_concurrent_positions", 1)) or 1),
        1,
    )
    open_position_quantity = abs(int(ledger.get("open_position_quantity") or 0))
    open_bracket_order_count = int(risk_context.get("open_bracket_order_count") or 0)
    if risk_context.get("safe_mode"):
        blocked_reasons.append("safe_mode")
        fast_changes.append({"change": "safe_mode", "reason": "safe_mode"})
    if risk_context.get("data_stale"):
        blocked_reasons.append("data_stale")
        fast_changes.append({"change": "safe_mode", "reason": "data_stale"})
    if risk_context.get("daily_loss_limit_hit"):
        blocked_reasons.append("daily_loss_limit_hit")
        fast_changes.append({"change": "safe_mode", "reason": "daily_loss_limit_hit"})
    if risk_context.get("daily_trade_cap_reached"):
        blocked_reasons.append("daily_trade_cap_reached")
    if risk_context.get("signal_regime_cooldown_active"):
        blocked_reasons.append("signal_regime_cooldown_active")
    if (
        request.get("strong_signal_count", 0)
        and open_position_quantity + open_bracket_order_count >= max_concurrent_positions
    ):
        blocked_reasons.append("max_concurrent_positions_reached")
    if blocked_reasons:
        action = "paper_block"
    elif request.get("strong_signal_count", 0) > 0:
        action = "paper_allow"
    elif request.get("blocked_signal_count", 0) > 0:
        action = "observe"
    else:
        action = "no_action"
    result = {
        "schema_version": 1,
        "action": action,
        "confidence": "low" if action == "paper_allow" else "medium",
        "bull_case": {"evidence": _strong_signal_reasons(request), "invalidation": []},
        "bear_case": {"evidence": blocked_reasons, "invalidation": []},
        "risk_review": {"key_risks": list(blocked_reasons), "blocked_reasons": blocked_reasons},
        "paper_plan": _paper_plan(request) if action == "paper_allow" else None,
        "fast_path_control_diff": {
            "auto_apply": bool(fast_changes),
            "changes": fast_changes,
        },
        "mutation_proposal": {
            "create": False,
            "allowed_change": None,
            "rationale": None,
            "validation_required": [],
        },
        "final_summary": {
            "market_structure": "structured_fallback_review",
            "decision_reason": ";".join(blocked_reasons) if blocked_reasons else action,
            "next_check": "next_5m_close",
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    validate_review_result(result)
    result["review_result_hash"] = _stable_hash(result)
    return result


def validate_review_result(payload: dict[str, Any]) -> None:
    if payload.get("action") not in ALLOWED_REVIEW_ACTIONS:
        raise ValueError("review action is not allowed")
    confidence = payload.get("confidence")
    if confidence not in {"low", "medium", "high"}:
        raise ValueError("confidence must be low, medium, or high")
    fast_diff = payload.get("fast_path_control_diff") or {}
    changes = fast_diff.get("changes") or []
    for change in changes:
        name = change.get("change") if isinstance(change, dict) else change
        if name not in ALLOWED_FAST_PATH_CHANGES:
            raise ValueError(f"fast path change is not allowed: {name}")
    mutation = payload.get("mutation_proposal") or {}
    allowed_change = mutation.get("allowed_change")
    if allowed_change is not None and allowed_change not in ALLOWED_MUTATION_CHANGES:
        raise ValueError(f"mutation change is not allowed: {allowed_change}")
    if "ibkr_order_object" in payload:
        raise ValueError("LLM review must not contain IBKR order objects")


def _ledger_summary(ledger: dict[str, Any]) -> dict[str, Any]:
    positions = ledger.get("positions") or []
    open_qty = sum(int(position.get("quantity", 0)) for position in positions)
    return {
        "fill_count": int(ledger.get("fill_count", 0)),
        "net_realized_pnl": float(ledger.get("net_realized_pnl", 0.0)),
        "total_commission": float(ledger.get("total_commission", 0.0)),
        "open_position_quantity": open_qty,
        "latest_account_snapshot": ledger.get("latest_account_snapshot"),
    }


def _review_summary(review: dict[str, Any]) -> dict[str, Any]:
    request = review.get("review_request") or {}
    result = review.get("review_result") or {}
    final_summary = result.get("final_summary") or {}
    return {
        "created_at": result.get("created_at") or request.get("created_at"),
        "action": result.get("action"),
        "confidence": result.get("confidence"),
        "decision_reason": final_summary.get("decision_reason"),
        "next_check": final_summary.get("next_check"),
        "strong_signal_count": request.get("strong_signal_count", 0),
        "blocked_signal_count": request.get("blocked_signal_count", 0),
        "review_request_hash": request.get("review_request_hash"),
        "review_result_hash": result.get("review_result_hash"),
    }


def _paper_plan(request: dict[str, Any]) -> dict[str, Any]:
    signals = [signal for signal in request.get("signals", []) if signal.get("signal_class") == "strong_review"]
    signal = signals[0] if signals else {}
    side = signal.get("side", "BUY")
    bar = signal.get("bar_1m") or {}
    close = float(bar.get("close", 0.0))
    stop_ticks = int(request.get("strategy_state", {}).get("stop_loss_ticks", 20))
    take_ticks = int(request.get("strategy_state", {}).get("take_profit_ticks", 40))
    take_ticks_multiplier = max(float(request.get("strategy_state", {}).get("take_profit_ticks_multiplier", 1.0)), 0.0)
    effective_take_ticks = max(int(round(take_ticks * take_ticks_multiplier)), 1)
    tick_size = float(request.get("strategy_state", {}).get("tick_size", 0.25))
    if side == "SELL":
        stop_price = close + stop_ticks * tick_size
        take_profit_price = close - effective_take_ticks * tick_size
    else:
        stop_price = close - stop_ticks * tick_size
        take_profit_price = close + effective_take_ticks * tick_size
    return {
        "symbol": signal.get("symbol", "MNQ"),
        "action": side,
        "quantity": 1,
        "entry_order_type": "MKT",
        "reference_price": close,
        "stop_price": stop_price,
        "take_profit_price": take_profit_price,
        "take_profit_ticks": effective_take_ticks,
        "take_profit_ticks_multiplier": take_ticks_multiplier,
        "max_holding_minutes": int(request.get("strategy_state", {}).get("max_holding_minutes", 20)),
    }


def _strong_signal_reasons(request: dict[str, Any]) -> list[str]:
    reasons = []
    for signal in request.get("signals", []):
        if signal.get("signal_class") == "strong_review":
            reasons.extend(str(reason) for reason in signal.get("trigger_reasons", []))
    return reasons


def _stable_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
