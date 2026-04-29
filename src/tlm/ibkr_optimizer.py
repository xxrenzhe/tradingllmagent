from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from .ibkr_review import validate_review_result


def apply_fast_path_control_diff(
    control_state: dict[str, Any],
    review_result: dict[str, Any],
) -> dict[str, Any]:
    validate_review_result(review_result)
    next_state = copy.deepcopy(control_state)
    fast_diff = review_result.get("fast_path_control_diff") or {}
    if not fast_diff.get("auto_apply"):
        return {
            "status": "no_change",
            "applied": [],
            "rejected": [],
            "control_state": next_state,
        }
    applied = []
    rejected = []
    for change in fast_diff.get("changes") or []:
        result = _apply_change(next_state, change)
        if result["applied"]:
            applied.append(result)
        else:
            rejected.append(result)
    return {
        "status": "applied" if applied and not rejected else "partial" if applied else "rejected",
        "applied": applied,
        "rejected": rejected,
        "control_state": next_state,
    }


def _apply_change(state: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    name = str(change.get("change"))
    try:
        if name == "pause_strategy":
            strategy_id = _strategy_id(change)
            strategy = _strategy_state(state, strategy_id)
            before = strategy.get("enabled", True)
            strategy["enabled"] = False
            return _applied(name, {"strategy_id": strategy_id, "before": before, "after": False})
        if name == "demote_strategy":
            strategy_id = _strategy_id(change)
            strategy = _strategy_state(state, strategy_id)
            before = float(strategy.get("weight", 1.0))
            after = float(change["weight"])
            if after > before:
                return _rejected(name, "weight_increase_not_allowed")
            strategy["weight"] = max(after, 0.0)
            return _applied(name, {"strategy_id": strategy_id, "before": before, "after": strategy["weight"]})
        if name == "raise_min_confidence":
            before = float(state.get("min_confidence", 0.0))
            after = float(change["value"])
            if after < before:
                return _rejected(name, "confidence_decrease_not_allowed")
            state["min_confidence"] = min(after, 1.0)
            return _applied(name, {"before": before, "after": state["min_confidence"]})
        if name == "tighten_max_spread_ticks":
            before = float(state.get("max_spread_ticks", float("inf")))
            after = float(change["value"])
            if after > before:
                return _rejected(name, "spread_limit_increase_not_allowed")
            state["max_spread_ticks"] = max(after, 0.0)
            return _applied(name, {"before": before, "after": state["max_spread_ticks"]})
        if name == "reduce_daily_trade_cap":
            before = int(state.get("daily_trade_cap", 0))
            after = int(change["value"])
            if before and after > before:
                return _rejected(name, "daily_trade_cap_increase_not_allowed")
            state["daily_trade_cap"] = max(after, 0)
            return _applied(name, {"before": before, "after": state["daily_trade_cap"]})
        if name == "narrow_session":
            before = state.get("trade_session", {"start": "00:00", "end": "23:59"})
            after = {"start": str(change["start"]), "end": str(change["end"])}
            if not _session_is_narrower(before, after):
                return _rejected(name, "session_expansion_not_allowed")
            state["trade_session"] = after
            return _applied(name, {"before": before, "after": after})
        if name == "observe_only":
            before = state.get("mode", "paper")
            state["mode"] = "observe_only"
            return _applied(name, {"before": before, "after": "observe_only"})
        if name == "safe_mode":
            before = bool(state.get("safe_mode", False))
            state["safe_mode"] = True
            return _applied(name, {"before": before, "after": True})
        if name == "kill_switch":
            before = bool(state.get("kill_switch", False))
            state["kill_switch"] = True
            state["safe_mode"] = True
            state["mode"] = "observe_only"
            return _applied(name, {"before": before, "after": True})
    except (KeyError, TypeError, ValueError) as exc:
        return _rejected(name, f"invalid_change_payload:{exc}")
    return _rejected(name, "unsupported_change")


def _strategy_id(change: dict[str, Any]) -> str:
    return str(change["strategy_id"])


def _strategy_state(state: dict[str, Any], strategy_id: str) -> dict[str, Any]:
    strategies = state.setdefault("strategies", {})
    strategy = strategies.setdefault(strategy_id, {})
    return strategy


def _session_is_narrower(before: dict[str, Any], after: dict[str, Any]) -> bool:
    before_start = _minutes(str(before.get("start", "00:00")))
    before_end = _minutes(str(before.get("end", "23:59")))
    after_start = _minutes(after["start"])
    after_end = _minutes(after["end"])
    return before_start <= after_start <= after_end <= before_end


def _minutes(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _applied(change: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"change": change, "applied": True, "details": details}


def _rejected(change: str, reason: str) -> dict[str, Any]:
    return {"change": change, "applied": False, "reason": reason}
