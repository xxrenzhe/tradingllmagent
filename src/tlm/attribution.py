from __future__ import annotations

from collections import Counter, defaultdict
from typing import Sequence


def build_search_attribution_report(results: Sequence[object]) -> dict:
    by_family: dict[str, dict] = {}
    by_feature: dict[str, dict] = {}
    by_mutation: dict[str, dict] = {}
    failure_modes = Counter()
    pre_screen_reasons = Counter()
    for result in results:
        payload = result.to_dict() if hasattr(result, "to_dict") else result
        family = _family(payload)
        _update_group(by_family, family, payload)
        mutation_type = _mutation_type(payload)
        _update_group(by_mutation, mutation_type, payload)
        for feature in _features(payload):
            _update_group(by_feature, feature, payload)
        reasons = list(payload.get("gates", {}).get("reasons", []))
        pre_screen = payload.get("pre_screen_report") or {}
        pre_reasons = list(pre_screen.get("reasons", []))
        pre_screen_reasons.update(pre_reasons)
        failure_modes.update(_failure_modes(payload, reasons, pre_reasons))
    return {
        "artifact": "strategy_search_attribution_report",
        "result_count": len(results),
        "passed_count": sum(1 for result in results if _payload(result).get("gates", {}).get("passed")),
        "by_family": _finalize_groups(by_family),
        "by_feature": _finalize_groups(by_feature),
        "by_mutation": _finalize_groups(by_mutation),
        "failure_modes": dict(failure_modes.most_common()),
        "pre_screen_reasons": dict(pre_screen_reasons.most_common()),
    }


def _payload(result: object) -> dict:
    return result.to_dict() if hasattr(result, "to_dict") else result


def _family(payload: dict) -> str:
    return (
        payload.get("module_id")
        or payload.get("strategy_card", {}).get("module_id")
        or payload.get("strategy_spec", {}).get("strategy_family")
        or "unknown"
    )


def _mutation_type(payload: dict) -> str:
    return payload.get("strategy_spec", {}).get("mutation", {}).get("type", "original")


def _features(payload: dict) -> list[str]:
    spec = payload.get("strategy_spec", {})
    names = []
    for feature in spec.get("feature_set", []):
        if isinstance(feature, dict) and feature.get("name"):
            names.append(str(feature["name"]))
        elif isinstance(feature, str):
            names.append(feature)
    grammar = spec.get("signal_grammar", {})
    names.extend(_predicate_features(grammar))
    return sorted(set(names))


def _predicate_features(node) -> list[str]:
    if isinstance(node, list):
        return [name for item in node for name in _predicate_features(item)]
    if not isinstance(node, dict):
        return []
    names = []
    if "feature" in node:
        names.append(str(node["feature"]))
    if "left" in node:
        names.append(str(node["left"]))
    for key in ("all", "any"):
        if key in node:
            names.extend(_predicate_features(node[key]))
    if "not" in node:
        names.extend(_predicate_features(node["not"]))
    for key in ("entry", "filters"):
        if key in node:
            names.extend(_predicate_features(node[key]))
    return names


def _update_group(groups: dict[str, dict], key: str, payload: dict) -> None:
    group = groups.setdefault(
        key,
        {
            "count": 0,
            "passed": 0,
            "reason_counts": Counter(),
            "pre_screen_reason_counts": Counter(),
            "net_pnls": [],
            "sharpes": [],
            "annual_trades": [],
        },
    )
    group["count"] += 1
    if payload.get("gates", {}).get("passed"):
        group["passed"] += 1
    group["reason_counts"].update(payload.get("gates", {}).get("reasons", []))
    group["pre_screen_reason_counts"].update((payload.get("pre_screen_report") or {}).get("reasons", []))
    metrics = payload.get("aggregate_test_metrics", {})
    for source_key, target_key in (
        ("net_pnl", "net_pnls"),
        ("sharpe", "sharpes"),
        ("annual_trades", "annual_trades"),
    ):
        value = metrics.get(source_key)
        if value is not None:
            group[target_key].append(value)


def _finalize_groups(groups: dict[str, dict]) -> dict[str, dict]:
    finalized = {}
    for key, group in groups.items():
        finalized[key] = {
            "count": group["count"],
            "passed": group["passed"],
            "best_net_pnl": max(group["net_pnls"], default=None),
            "best_sharpe": max(group["sharpes"], default=None),
            "best_annual_trades": max(group["annual_trades"], default=None),
            "reason_counts": dict(group["reason_counts"].most_common()),
            "pre_screen_reason_counts": dict(group["pre_screen_reason_counts"].most_common()),
        }
    return dict(sorted(finalized.items()))


def _failure_modes(payload: dict, gate_reasons: list[str], pre_screen_reasons: list[str]) -> list[str]:
    metrics = payload.get("aggregate_test_metrics", {})
    modes = []
    if (metrics.get("trade_count") or 0) == 0:
        modes.append("no_trade")
    if "negative_gross_edge" in pre_screen_reasons:
        modes.append("negative_gross_edge")
    if "insufficient_cost_coverage" in pre_screen_reasons and "negative_gross_edge" not in pre_screen_reasons:
        modes.append("cost_fragile")
    if "inverse_signal_better" in pre_screen_reasons:
        modes.append("wrong_direction")
    if (metrics.get("annual_trades") or 0) > 1000 and (metrics.get("avg_trade_net_pnl") or 0) <= 0:
        modes.append("overtrading")
    if "final_holdout_sharpe_decay" in gate_reasons or "test_to_holdout_sharpe_decay" in gate_reasons:
        modes.append("holdout_decay")
    if "positive_test_fold_ratio" in gate_reasons or "positive_year_ratio" in gate_reasons:
        modes.append("regime_specific")
    return modes or ["unclassified"]
