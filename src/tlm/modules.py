from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from .metrics import BacktestMetrics
from .strategy import StrategySpec


@dataclass(frozen=True)
class StrategyModule:
    module_id: str
    family: str
    description: str
    supported_timeframes: list[str]
    minimum_sample_size: int
    tags: list[str]
    known_failure_modes: list[str]
    module_version: str = "1.0.0"
    status: str = "testing"

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_id": self.module_id,
            "module_version": self.module_version,
            "status": self.status,
            "family": self.family,
            "description": self.description,
            "supported_timeframes": self.supported_timeframes,
            "minimum_sample_size": self.minimum_sample_size,
            "tags": self.tags,
            "known_failure_modes": self.known_failure_modes,
        }


DEFAULT_STRATEGY_MODULES = {
    "opening_range_breakout": StrategyModule(
        module_id="opening_range_breakout",
        family="opening_range_breakout",
        description="Opening range breakout with fixed risk and session flatten.",
        supported_timeframes=["1m", "5m", "15m"],
        minimum_sample_size=100,
        tags=["level_reaction", "breakout", "session_open"],
        known_failure_modes=["false_breakout", "event_window_slippage", "low_sample_count"],
    ),
    "trend_pullback": StrategyModule(
        module_id="trend_pullback",
        family="trend_pullback",
        description="Trend continuation after pullback into moving-average structure.",
        supported_timeframes=["5m", "15m", "30m"],
        minimum_sample_size=100,
        tags=["momentum", "pullback", "moving_average"],
        known_failure_modes=["late_trend_entry", "range_regime_chop", "parameter_instability"],
    ),
    "volatility_expansion": StrategyModule(
        module_id="volatility_expansion",
        family="volatility_expansion",
        description="Range and realized-volatility expansion continuation.",
        supported_timeframes=["5m", "15m"],
        minimum_sample_size=75,
        tags=["momentum", "volatility", "breakout"],
        known_failure_modes=["single_bar_exhaustion", "wide_spread", "news_shock"],
    ),
    "intraday_momentum": StrategyModule(
        module_id="intraday_momentum",
        family="intraday_momentum",
        description="Short horizon intraday momentum continuation.",
        supported_timeframes=["5m", "15m"],
        minimum_sample_size=100,
        tags=["momentum", "multi_bar_confirmation"],
        known_failure_modes=["overextension", "low_volatility_session", "chase_after_event"],
    ),
    "regime_filtered_mean_reversion": StrategyModule(
        module_id="regime_filtered_mean_reversion",
        family="regime_filtered_mean_reversion",
        description="Mean reversion gated by a range-regime or z-score filter.",
        supported_timeframes=["5m", "15m", "30m"],
        minimum_sample_size=100,
        tags=["mean_reversion", "z_score", "range_regime"],
        known_failure_modes=["trend_day", "volatility_expansion", "averaging_down_pressure"],
    ),
    "time_of_day_edge": StrategyModule(
        module_id="time_of_day_edge",
        family="time_of_day_edge",
        description="Time-window edge with fixed direction and bounded hold.",
        supported_timeframes=["5m", "15m", "30m"],
        minimum_sample_size=100,
        tags=["time_window", "session_seasonality"],
        known_failure_modes=["calendar_drift", "event_overlap", "thin_sample_window"],
    ),
    "gap_fade_or_continuation": StrategyModule(
        module_id="gap_fade_or_continuation",
        family="gap_fade_or_continuation",
        description="Gap fade or continuation module with explicit gap mode.",
        supported_timeframes=["5m", "15m"],
        minimum_sample_size=75,
        tags=["gap", "level_reaction", "session_open"],
        known_failure_modes=["event_driven_gap", "opening_spread", "mode_instability"],
    ),
}


MODULE_STATUSES = {
    "draft",
    "testing",
    "candidate",
    "freeze_confirmed",
    "paper_shadow",
    "retired",
}

ALLOWED_STATUS_TRANSITIONS = {
    "draft": {"testing", "retired"},
    "testing": {"candidate", "retired"},
    "candidate": {"freeze_confirmed", "testing", "retired"},
    "freeze_confirmed": {"paper_shadow", "retired"},
    "paper_shadow": {"freeze_confirmed", "retired"},
    "retired": set(),
}

RUNTIME_ALLOWED_STATUSES = {"candidate", "freeze_confirmed", "paper_shadow"}


@dataclass(frozen=True)
class ModuleRegistryEntry:
    module_id: str
    module_version: str
    family: str
    status: str
    promotion_gates: dict[str, Any]
    retirement_reasons: list[str]
    last_retest_at: str | None
    next_retest_due: str | None
    audit_events: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_id": self.module_id,
            "module_version": self.module_version,
            "family": self.family,
            "status": self.status,
            "promotion_gates": self.promotion_gates,
            "retirement_reasons": self.retirement_reasons,
            "last_retest_at": self.last_retest_at,
            "next_retest_due": self.next_retest_due,
            "audit_events": self.audit_events,
        }


def infer_module_id(spec: StrategySpec) -> str:
    return spec.module_id or spec.strategy_family


def module_for_spec(spec: StrategySpec) -> StrategyModule | None:
    return DEFAULT_STRATEGY_MODULES.get(infer_module_id(spec)) or DEFAULT_STRATEGY_MODULES.get(
        spec.strategy_family
    )


def module_summary_for_spec(spec: StrategySpec) -> dict[str, Any]:
    module = module_for_spec(spec)
    if module is None:
        return {
            "module_id": infer_module_id(spec),
            "family": spec.strategy_family,
            "description": None,
            "supported_timeframes": [],
            "minimum_sample_size": None,
            "tags": [],
            "known_failure_modes": [],
        }
    payload = module.to_dict()
    payload["module_id"] = infer_module_id(spec)
    payload["catalog_module_id"] = module.module_id
    return payload


def build_module_performance_record(
    *,
    experiment_id: str,
    spec: StrategySpec,
    execution_mode: str,
    strategy_spec_hash: str,
    variant_parameters: dict[str, Any],
    aggregate_test_metrics: BacktestMetrics,
    final_holdout_metrics: BacktestMetrics,
    gates: dict[str, Any],
    robustness_score: float | None,
    parameter_stability_report: dict[str, Any],
    win_rate: float | None = None,
) -> dict[str, Any]:
    annual_trades = aggregate_test_metrics.annual_trades
    return {
        "experiment_id": experiment_id,
        "module_id": infer_module_id(spec),
        "strategy_family": spec.strategy_family,
        "strategy_name": spec.name,
        "timeframe": spec.timeframe,
        "execution_mode": execution_mode,
        "strategy_spec_hash": strategy_spec_hash,
        "variant_parameters": variant_parameters,
        "trade_count": aggregate_test_metrics.trade_count,
        "annual_trades": annual_trades,
        "trades_per_day": annual_trades / 365,
        "win_rate": win_rate,
        "proxy_win_rate": win_rate,
        "expectancy": aggregate_test_metrics.avg_trade_net_pnl,
        "profit_factor": aggregate_test_metrics.profit_factor,
        "max_drawdown": aggregate_test_metrics.max_drawdown,
        "holdout_net_pnl": final_holdout_metrics.net_pnl,
        "passed": bool(gates.get("passed")),
        "rejection_reasons": list(gates.get("reasons", [])),
        "robustness_score": robustness_score,
        "parameter_stability_status": parameter_stability_report.get("status"),
    }


def write_module_performance_memory(path: Path, records: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True, default=str) for record in records)
        + ("\n" if records else ""),
        encoding="utf-8",
    )


def load_module_performance_memory(paths: Sequence[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid module memory JSONL at {path}:{line_number}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"Module memory row must be an object at {path}:{line_number}")
            records.append(payload)
    return records


def discover_module_memory_files(experiments_root: Path) -> list[Path]:
    return sorted(experiments_root.glob("*/module_performance.jsonl"))


def summarize_module_performance(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    modules: dict[str, dict[str, Any]] = {}
    for record in records:
        module_id = str(record.get("module_id") or "unknown")
        row = modules.setdefault(
            module_id,
            {
                "module_id": module_id,
                "catalog": _catalog_payload(module_id),
                "evaluated_records": 0,
                "passed_records": 0,
                "rejected_records": 0,
                "total_trade_count": 0,
                "best_robustness_score": None,
                "best_experiment_id": None,
                "positive_expectancy_records": 0,
                "timeframes": [],
                "rejection_reasons": {},
                "parameter_stability_statuses": {},
            },
        )
        row["evaluated_records"] += 1
        if record.get("passed"):
            row["passed_records"] += 1
        else:
            row["rejected_records"] += 1
        row["total_trade_count"] += int(record.get("trade_count") or 0)
        timeframe = record.get("timeframe")
        if timeframe and timeframe not in row["timeframes"]:
            row["timeframes"].append(timeframe)
        expectancy = record.get("expectancy")
        if expectancy is not None and float(expectancy) > 0:
            row["positive_expectancy_records"] += 1
        score = record.get("robustness_score")
        if score is not None and (
            row["best_robustness_score"] is None or float(score) > row["best_robustness_score"]
        ):
            row["best_robustness_score"] = float(score)
            row["best_experiment_id"] = record.get("experiment_id")
        for reason in record.get("rejection_reasons", []):
            reasons = row["rejection_reasons"]
            reasons[reason] = reasons.get(reason, 0) + 1
        status = record.get("parameter_stability_status")
        if status:
            statuses = row["parameter_stability_statuses"]
            statuses[status] = statuses.get(status, 0) + 1

    ordered = sorted(
        modules.values(),
        key=lambda item: (
            -item["passed_records"],
            -(item["best_robustness_score"] or -1),
            item["module_id"],
        ),
    )
    for row in ordered:
        row["timeframes"] = sorted(row["timeframes"])
        evaluated = max(row["evaluated_records"], 1)
        row["pass_rate"] = row["passed_records"] / evaluated
        row["positive_expectancy_ratio"] = row["positive_expectancy_records"] / evaluated
    return {
        "schema_version": 1,
        "evaluated_records": len(records),
        "module_count": len(ordered),
        "modules": ordered,
        "target_frequency_pool": build_target_frequency_pool(records),
    }


def build_target_frequency_pool(
    records: Sequence[dict[str, Any]],
    *,
    target_min_per_day: float = 2.0,
    target_max_per_day: float = 3.0,
    min_proxy_win_rate: float = 0.53,
    lookback_days: int = 90,
    require_passed: bool = True,
    max_pool_size: int = 12,
    max_per_module: int = 1,
    max_per_strategy_hash: int = 1,
) -> dict[str, Any]:
    candidates = []
    rejected = {
        "not_passed": 0,
        "missing_or_zero_rate": 0,
        "missing_proxy_win_rate": 0,
        "proxy_win_rate_below_threshold": 0,
    }
    for record in records:
        if require_passed and not record.get("passed"):
            rejected["not_passed"] += 1
            continue
        daily_rate, rate_source = _record_daily_signal_rate(record, lookback_days)
        if daily_rate is None or daily_rate <= 0:
            rejected["missing_or_zero_rate"] += 1
            continue
        proxy_win_rate = _record_proxy_win_rate(record)
        if proxy_win_rate is None:
            rejected["missing_proxy_win_rate"] += 1
            continue
        if proxy_win_rate < min_proxy_win_rate:
            rejected["proxy_win_rate_below_threshold"] += 1
            continue
        candidates.append(
            {
                "experiment_id": record.get("experiment_id"),
                "module_id": str(record.get("module_id") or "unknown"),
                "strategy_family": record.get("strategy_family"),
                "strategy_name": record.get("strategy_name"),
                "strategy_spec_hash": record.get("strategy_spec_hash"),
                "timeframe": record.get("timeframe"),
                "trades_per_day": daily_rate,
                "rate_source": rate_source,
                "proxy_win_rate": proxy_win_rate,
                "expectancy": _optional_float(record.get("expectancy")),
                "robustness_score": _optional_float(record.get("robustness_score")),
                "quality_rank_key": _target_pool_quality_rank(record, daily_rate, proxy_win_rate),
            }
        )

    ordered = sorted(
        candidates,
        key=lambda item: (
            -float(item["proxy_win_rate"]),
            -float(item["robustness_score"] or 0),
            -float(item["expectancy"] or 0),
            -float(item["trades_per_day"]),
            str(item.get("strategy_spec_hash") or item.get("experiment_id") or ""),
        ),
    )
    selected = _select_frequency_pool(
        ordered,
        target_min_per_day=target_min_per_day,
        target_max_per_day=target_max_per_day,
        max_pool_size=max_pool_size,
        max_per_module=max_per_module,
        max_per_strategy_hash=max_per_strategy_hash,
    )
    total_rate = sum(float(item["trades_per_day"]) for item in selected)
    weighted_proxy_win_rate = (
        sum(float(item["trades_per_day"]) * float(item["proxy_win_rate"]) for item in selected)
        / total_rate
        if total_rate > 0
        else None
    )
    if target_min_per_day <= total_rate <= target_max_per_day and weighted_proxy_win_rate is not None:
        status = "target_met"
    elif total_rate < target_min_per_day:
        status = "below_target"
    else:
        status = "above_target"
    diversity_report = _target_pool_diversity_report(ordered, selected)
    return {
        "schema_version": 2,
        "pool_version": "target_frequency_pool.v2",
        "status": status,
        "target_min_per_day": target_min_per_day,
        "target_max_per_day": target_max_per_day,
        "min_proxy_win_rate": min_proxy_win_rate,
        "lookback_days": lookback_days,
        "require_passed": require_passed,
        "max_per_module": max_per_module,
        "max_per_strategy_hash": max_per_strategy_hash,
        "candidate_count": len(ordered),
        "selected_count": len(selected),
        "selected_trades_per_day": total_rate,
        "weighted_proxy_win_rate": weighted_proxy_win_rate,
        "selected": selected,
        "candidate_quality_order": [
            {
                "experiment_id": item.get("experiment_id"),
                "module_id": item.get("module_id"),
                "strategy_name": item.get("strategy_name"),
                "strategy_spec_hash": item.get("strategy_spec_hash"),
                "trades_per_day": item.get("trades_per_day"),
                "proxy_win_rate": item.get("proxy_win_rate"),
            }
            for item in ordered
        ],
        "diversity_report": diversity_report,
        "rejected": rejected,
    }


def _select_frequency_pool(
    candidates: Sequence[dict[str, Any]],
    *,
    target_min_per_day: float,
    target_max_per_day: float,
    max_pool_size: int,
    max_per_module: int,
    max_per_strategy_hash: int,
) -> list[dict[str, Any]]:
    selected = []
    total_rate = 0.0
    for candidate in candidates:
        if len(selected) >= max_pool_size:
            break
        if _violates_pool_diversity_limits(
            selected,
            candidate,
            max_per_module=max_per_module,
            max_per_strategy_hash=max_per_strategy_hash,
        ):
            continue
        candidate_rate = float(candidate["trades_per_day"])
        if total_rate + candidate_rate <= target_max_per_day:
            selected.append(candidate)
            total_rate += candidate_rate
        if total_rate >= target_min_per_day:
            break
    if total_rate >= target_min_per_day:
        return selected
    overflow_candidates = [
        candidate for candidate in candidates if candidate not in selected and float(candidate["trades_per_day"]) > 0
    ]
    if not overflow_candidates or len(selected) >= max_pool_size:
        return selected
    best_overflow = min(
        (
            candidate
            for candidate in overflow_candidates
            if not _violates_pool_diversity_limits(
                selected,
                candidate,
                max_per_module=max_per_module,
                max_per_strategy_hash=max_per_strategy_hash,
            )
        ),
        key=lambda item: (
            abs((total_rate + float(item["trades_per_day"])) - target_min_per_day),
            -float(item["proxy_win_rate"]),
        ),
        default=None,
    )
    return selected + [best_overflow] if best_overflow else selected


def _violates_pool_diversity_limits(
    selected: Sequence[dict[str, Any]],
    candidate: dict[str, Any],
    *,
    max_per_module: int,
    max_per_strategy_hash: int,
) -> bool:
    if max_per_module > 0:
        module_id = candidate.get("module_id")
        selected_module_count = sum(1 for item in selected if item.get("module_id") == module_id)
        if selected_module_count >= max_per_module:
            return True
    if max_per_strategy_hash > 0 and candidate.get("strategy_spec_hash"):
        strategy_hash = candidate.get("strategy_spec_hash")
        selected_hash_count = sum(1 for item in selected if item.get("strategy_spec_hash") == strategy_hash)
        if selected_hash_count >= max_per_strategy_hash:
            return True
    return False


def _target_pool_diversity_report(
    candidates: Sequence[dict[str, Any]],
    selected: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    selected_modules = {str(item.get("module_id") or "unknown") for item in selected}
    selected_hashes = {
        str(item.get("strategy_spec_hash"))
        for item in selected
        if item.get("strategy_spec_hash")
    }
    return {
        "candidate_module_count": len({str(item.get("module_id") or "unknown") for item in candidates}),
        "selected_module_count": len(selected_modules),
        "selected_modules": sorted(selected_modules),
        "candidate_strategy_hash_count": len(
            {str(item.get("strategy_spec_hash")) for item in candidates if item.get("strategy_spec_hash")}
        ),
        "selected_strategy_hash_count": len(selected_hashes),
    }


def _target_pool_quality_rank(
    record: dict[str, Any],
    daily_rate: float,
    proxy_win_rate: float,
) -> dict[str, Any]:
    return {
        "proxy_win_rate": proxy_win_rate,
        "robustness_score": _optional_float(record.get("robustness_score")),
        "expectancy": _optional_float(record.get("expectancy")),
        "trades_per_day": daily_rate,
    }


def _record_daily_signal_rate(record: dict[str, Any], lookback_days: int) -> tuple[float | None, str | None]:
    for key in ("trades_per_day", "trigger_per_day", "daily_signal_rate", "signals_per_day"):
        value = _optional_float(record.get(key))
        if value is not None:
            return value, key
    for key in ("annual_trades", "annual_signal_rate"):
        value = _optional_float(record.get(key))
        if value is not None:
            return value / 365, key
    trade_count = _optional_float(record.get("trade_count"))
    if trade_count is not None and lookback_days > 0:
        return trade_count / lookback_days, "trade_count_per_lookback_day"
    return None, None


def _record_proxy_win_rate(record: dict[str, Any]) -> float | None:
    for key in ("proxy_win_rate", "recent90_win_rate", "weighted_win_rate", "win_rate"):
        value = _optional_float(record.get(key))
        if value is not None:
            return value / 100 if value > 1 else value
    return None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def strategy_module_catalog() -> list[dict[str, Any]]:
    return [module.to_dict() for module in DEFAULT_STRATEGY_MODULES.values()]


def build_module_registry(
    records: Sequence[dict[str, Any]],
    *,
    now: datetime | None = None,
    retest_days: int = 30,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    summary = summarize_module_performance(records)
    entries = []
    seen = set()
    for row in summary["modules"]:
        module_id = row["module_id"]
        seen.add(module_id)
        entries.append(module_registry_entry_from_summary(row, now=now, retest_days=retest_days))
    for module_id, module in DEFAULT_STRATEGY_MODULES.items():
        if module_id in seen:
            continue
        entries.append(
            ModuleRegistryEntry(
                module_id=module_id,
                module_version=module.module_version,
                family=module.family,
                status="testing",
                promotion_gates={"status": "not_evaluated"},
                retirement_reasons=[],
                last_retest_at=None,
                next_retest_due=None,
                audit_events=[
                    module_audit_event(
                        module_id,
                        "registry_initialized",
                        "testing",
                        {"reason": "catalog_module_without_memory"},
                        now,
                    )
                ],
            ).to_dict()
        )
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "module_count": len(entries),
        "modules": sorted(entries, key=lambda item: item["module_id"]),
    }


def module_registry_entry_from_summary(
    row: dict[str, Any],
    *,
    now: datetime,
    retest_days: int,
) -> dict[str, Any]:
    module_id = row["module_id"]
    catalog = row.get("catalog") or {}
    gates = module_promotion_gates(row)
    retirement_reasons = module_retirement_reasons(row)
    if retirement_reasons:
        status = "retired"
    elif gates["passed"]:
        status = "freeze_confirmed"
    elif row.get("passed_records", 0) > 0:
        status = "candidate"
    else:
        status = "testing"
    last_retest_at = now.isoformat()
    next_retest_due = (now + timedelta(days=retest_days)).isoformat() if status != "retired" else None
    return ModuleRegistryEntry(
        module_id=module_id,
        module_version=str(catalog.get("module_version") or "1.0.0"),
        family=str((catalog or {}).get("family") or module_id),
        status=status,
        promotion_gates=gates,
        retirement_reasons=retirement_reasons,
        last_retest_at=last_retest_at,
        next_retest_due=next_retest_due,
        audit_events=[
            module_audit_event(
                module_id,
                "memory_summary_applied",
                status,
                {
                    "evaluated_records": row["evaluated_records"],
                    "passed_records": row["passed_records"],
                    "pass_rate": row["pass_rate"],
                    "retirement_reasons": retirement_reasons,
                },
                now,
            )
        ],
    ).to_dict()


def module_promotion_gates(row: dict[str, Any]) -> dict[str, Any]:
    evaluated = int(row.get("evaluated_records") or 0)
    passed = int(row.get("passed_records") or 0)
    total_trade_count = int(row.get("total_trade_count") or 0)
    positive_expectancy_ratio = float(row.get("positive_expectancy_ratio") or 0)
    robustness_score = row.get("best_robustness_score")
    minimum_sample_size = int((row.get("catalog") or {}).get("minimum_sample_size") or 75)
    reasons = []
    if evaluated <= 0:
        reasons.append("no_evaluated_records")
    if passed <= 0:
        reasons.append("no_passing_records")
    if total_trade_count < minimum_sample_size:
        reasons.append("sample_size_below_module_minimum")
    if positive_expectancy_ratio < 0.5:
        reasons.append("positive_expectancy_ratio_below_threshold")
    if robustness_score is None or float(robustness_score) < 0.5:
        reasons.append("robustness_score_below_threshold")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "minimum_sample_size": minimum_sample_size,
        "total_trade_count": total_trade_count,
        "positive_expectancy_ratio": positive_expectancy_ratio,
        "best_robustness_score": robustness_score,
    }


def module_retirement_reasons(row: dict[str, Any]) -> list[str]:
    reasons = []
    rejection_reasons = row.get("rejection_reasons") or {}
    if rejection_reasons.get("event_window_drawdown") or rejection_reasons.get("event_window_risk"):
        reasons.append("event_window_risk")
    if rejection_reasons.get("slippage_sensitivity") or rejection_reasons.get("cost_stress"):
        reasons.append("slippage_sensitive")
    if row.get("evaluated_records", 0) >= 3 and row.get("passed_records", 0) == 0:
        reasons.append("persistent_out_of_sample_failure")
    if row.get("total_trade_count", 0) < int((row.get("catalog") or {}).get("minimum_sample_size") or 75):
        reasons.append("sample_size_insufficient")
    return sorted(set(reasons))


def transition_module_status(
    entry: dict[str, Any],
    new_status: str,
    *,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    current_status = str(entry.get("status", "draft"))
    if new_status not in MODULE_STATUSES:
        raise ValueError(f"Unsupported module status: {new_status}")
    if new_status != current_status and new_status not in ALLOWED_STATUS_TRANSITIONS[current_status]:
        raise ValueError(f"Invalid module status transition: {current_status} -> {new_status}")
    updated = dict(entry)
    updated["status"] = new_status
    if new_status == "retired" and reason not in updated.get("retirement_reasons", []):
        updated["retirement_reasons"] = list(updated.get("retirement_reasons", [])) + [reason]
    updated["audit_events"] = list(updated.get("audit_events", [])) + [
        module_audit_event(
            str(entry["module_id"]),
            "status_transition",
            new_status,
            {"from_status": current_status, "reason": reason},
            now,
        )
    ]
    return updated


def module_can_enter_runtime(entry: dict[str, Any]) -> bool:
    return str(entry.get("status")) in RUNTIME_ALLOWED_STATUSES


def validate_freeze_confirmed_strategy_module(spec: StrategySpec, entry: dict[str, Any]) -> None:
    if not infer_module_id(spec):
        raise ValueError("freeze-confirmed strategy must declare module_id")
    if str(entry.get("status")) == "retired":
        raise ValueError("retired module cannot enter runtime monitor or execution intent")
    if str(entry.get("module_id")) != infer_module_id(spec):
        raise ValueError("strategy module_id does not match registry entry")


def write_module_registry(path: Path, registry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def load_module_registry(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def module_audit_event(
    module_id: str,
    event_type: str,
    status: str,
    details: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    return {
        "module_id": module_id,
        "event_type": event_type,
        "status": status,
        "details": details,
        "recorded_at": now.isoformat(),
    }


def _catalog_payload(module_id: str) -> dict[str, Any] | None:
    module = DEFAULT_STRATEGY_MODULES.get(module_id)
    return module.to_dict() if module else None
