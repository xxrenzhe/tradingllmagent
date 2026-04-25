from __future__ import annotations

import copy
import hashlib
import itertools
import json
from dataclasses import dataclass
from typing import Any

from .strategy import StrategySpec, parse_strategy_spec


DEFAULT_PARAMETER_BUDGET = 50
DEFAULT_HIGH_RISK_PARAMETER_LIMIT = 200

SUPPORTED_PARAMETER_TARGETS = {
    "opening_range_minutes": ("indicators", "opening_range", "minutes"),
    "ema_fast_window": ("indicators", "ema_fast", "window"),
    "ema_slow_window": ("indicators", "ema_slow", "window"),
    "mean_reversion_window": ("indicators", "z_close", "window"),
    "mean_reversion_entry_z": ("indicators", "z_close", "entry_z"),
    "volatility_window": ("indicators", "realized_volatility", "window"),
    "volatility_expansion_multiple": ("indicators", "realized_volatility", "expansion_multiple"),
    "momentum_lookback_minutes": ("indicators", "momentum", "lookback_minutes"),
    "momentum_threshold_points": ("indicators", "momentum", "threshold_points"),
    "entry_time": ("indicators", "time_of_day", "entry_time"),
    "entry_side": ("indicators", "time_of_day", "entry_side"),
    "gap_threshold_points": ("indicators", "gap", "threshold_points"),
    "gap_mode": ("indicators", "gap", "mode"),
    "stop_points": ("exit", "stop_loss", "value"),
    "take_profit_points": ("exit", "take_profit", "value"),
    "max_holding_minutes": ("exit", "max_holding_minutes"),
}


@dataclass(frozen=True)
class ParameterGridMetadata:
    parameter_names: list[str]
    parameter_ranges: dict[str, Any]
    total_combinations: int
    unique_combinations: int
    duplicate_combinations: int
    selected_combinations: int
    default_budget: int
    high_risk_limit: int
    budget_exceeded: bool
    high_risk_budget: bool
    parameter_grid_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameter_names": self.parameter_names,
            "parameter_ranges": self.parameter_ranges,
            "total_combinations": self.total_combinations,
            "unique_combinations": self.unique_combinations,
            "duplicate_combinations": self.duplicate_combinations,
            "selected_combinations": self.selected_combinations,
            "default_budget": self.default_budget,
            "high_risk_limit": self.high_risk_limit,
            "budget_exceeded": self.budget_exceeded,
            "high_risk_budget": self.high_risk_budget,
            "parameter_grid_hash": self.parameter_grid_hash,
        }


class ParameterBudgetError(ValueError):
    """Raised when a strategy parameter grid exceeds the configured research budget."""


def stable_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def stable_hash(payload: Any) -> str:
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def strategy_spec_hash(spec: StrategySpec) -> str:
    return stable_hash(spec.raw)


def strategy_logic_hash(spec: StrategySpec) -> str:
    raw = copy.deepcopy(spec.raw)
    raw.pop("name", None)
    return stable_hash(raw)


def prompt_hash(spec: StrategySpec) -> str:
    return stable_hash(
        {
            "strategy_family": spec.strategy_family,
            "market_hypothesis": spec.market_hypothesis,
            "name": spec.name,
        }
    )


def parameter_values(parameter_spec: dict[str, Any]) -> list[Any]:
    if "values" in parameter_spec:
        return list(parameter_spec["values"])
    minimum = float(parameter_spec["min"])
    maximum = float(parameter_spec["max"])
    step = float(parameter_spec["step"])
    values = []
    current = minimum
    while current <= maximum + 1e-12:
        values.append(int(current) if current.is_integer() else round(current, 10))
        current += step
    return values


def parameter_grid_metadata(
    spec: StrategySpec,
    max_trials: int,
    max_parameter_combinations: int = DEFAULT_PARAMETER_BUDGET,
    default_budget: int = DEFAULT_PARAMETER_BUDGET,
    high_risk_limit: int = DEFAULT_HIGH_RISK_PARAMETER_LIMIT,
) -> ParameterGridMetadata:
    parameters = {
        name: parameter_values(parameter_spec)
        for name, parameter_spec in spec.parameters.items()
        if name in SUPPORTED_PARAMETER_TARGETS
    }
    names = sorted(parameters)
    total = 1
    for name in names:
        total *= len(parameters[name])
    if not names:
        total = 1
    parameter_ranges = {name: parameters[name] for name in names}
    unique_combinations = len(
        unique_parameter_combinations(
            spec,
            max_parameter_combinations=max_parameter_combinations,
        )
    )
    duplicate_combinations = max(0, min(total, max_parameter_combinations) - unique_combinations)
    selected = min(unique_combinations, max_trials, max_parameter_combinations)
    return ParameterGridMetadata(
        parameter_names=names,
        parameter_ranges=parameter_ranges,
        total_combinations=total,
        unique_combinations=unique_combinations,
        duplicate_combinations=duplicate_combinations,
        selected_combinations=selected,
        default_budget=default_budget,
        high_risk_limit=high_risk_limit,
        budget_exceeded=total > default_budget,
        high_risk_budget=total > high_risk_limit,
        parameter_grid_hash=stable_hash(
            {
                "parameter_names": names,
                "parameter_ranges": parameter_ranges,
            }
        ),
    )


def expand_strategy_variants(
    spec: StrategySpec,
    max_trials: int,
    max_parameter_combinations: int = DEFAULT_PARAMETER_BUDGET,
    allow_high_parameter_budget: bool = False,
) -> list[StrategySpec]:
    metadata = parameter_grid_metadata(
        spec,
        max_trials=max_trials,
        max_parameter_combinations=max_parameter_combinations,
    )
    if metadata.high_risk_budget and not allow_high_parameter_budget:
        raise ParameterBudgetError(
            "Parameter grid exceeds high-risk limit "
            f"({metadata.total_combinations} > {metadata.high_risk_limit}); "
            "explicitly allow high parameter budget to continue"
        )
    parameters = {
        name: parameter_values(parameter_spec)
        for name, parameter_spec in spec.parameters.items()
        if name in SUPPORTED_PARAMETER_TARGETS
    }
    if not parameters:
        return [spec]

    names = sorted(parameters)
    combinations = unique_parameter_combinations(
        spec,
        max_parameter_combinations=max_parameter_combinations,
    )
    variants: list[StrategySpec] = []
    seen: set[str] = set()
    for index, combination in enumerate(combinations[:max_trials]):
        raw = copy.deepcopy(spec.raw)
        raw["name"] = f"{spec.name}_trial_{index:04d}"
        raw["variant_parameters"] = combination
        for name, value in combination.items():
            set_nested(raw, SUPPORTED_PARAMETER_TARGETS[name], value)
        variant = parse_strategy_spec(raw)
        digest = strategy_logic_hash(variant)
        if digest in seen:
            continue
        seen.add(digest)
        variants.append(variant)
    return variants or [spec]


def unique_parameter_combinations(
    spec: StrategySpec,
    max_parameter_combinations: int = DEFAULT_PARAMETER_BUDGET,
) -> list[dict[str, Any]]:
    parameters = {
        name: parameter_values(parameter_spec)
        for name, parameter_spec in spec.parameters.items()
        if name in SUPPORTED_PARAMETER_TARGETS
    }
    if not parameters:
        return [{}]
    names = sorted(parameters)
    combinations = list(itertools.product(*(parameters[name] for name in names)))
    if len(combinations) > max_parameter_combinations:
        combinations = combinations[:max_parameter_combinations]

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for combination in combinations:
        values = dict(zip(names, combination, strict=True))
        raw = copy.deepcopy(spec.raw)
        raw.pop("name", None)
        raw["variant_parameters"] = values
        for name, value in values.items():
            set_nested(raw, SUPPORTED_PARAMETER_TARGETS[name], value)
        digest = stable_hash(raw)
        if digest in seen:
            continue
        seen.add(digest)
        unique.append(values)
    return unique


def set_nested(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    target = payload
    for key in path[:-1]:
        target = target.setdefault(key, {})
    target[path[-1]] = value
