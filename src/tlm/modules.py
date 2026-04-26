from __future__ import annotations

import json
from dataclasses import dataclass
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_id": self.module_id,
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
) -> dict[str, Any]:
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
        "win_rate": None,
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
    }


def strategy_module_catalog() -> list[dict[str, Any]]:
    return [module.to_dict() for module in DEFAULT_STRATEGY_MODULES.values()]


def _catalog_payload(module_id: str) -> dict[str, Any] | None:
    module = DEFAULT_STRATEGY_MODULES.get(module_id)
    return module.to_dict() if module else None
