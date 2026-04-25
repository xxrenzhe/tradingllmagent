from __future__ import annotations

import copy
import hashlib
import itertools
import json
from typing import Any

from .strategy import StrategySpec, parse_strategy_spec


SUPPORTED_PARAMETER_TARGETS = {
    "opening_range_minutes": ("indicators", "opening_range", "minutes"),
    "stop_points": ("exit", "stop_loss", "value"),
    "take_profit_points": ("exit", "take_profit", "value"),
    "max_holding_minutes": ("exit", "max_holding_minutes"),
}


def stable_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def stable_hash(payload: Any) -> str:
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def strategy_spec_hash(spec: StrategySpec) -> str:
    return stable_hash(spec.raw)


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


def expand_strategy_variants(
    spec: StrategySpec,
    max_trials: int,
    max_parameter_combinations: int = 500,
) -> list[StrategySpec]:
    parameters = {
        name: parameter_values(parameter_spec)
        for name, parameter_spec in spec.parameters.items()
        if name in SUPPORTED_PARAMETER_TARGETS
    }
    if not parameters:
        return [spec]

    names = sorted(parameters)
    combinations = list(itertools.product(*(parameters[name] for name in names)))
    if len(combinations) > max_parameter_combinations:
        combinations = combinations[:max_parameter_combinations]

    variants: list[StrategySpec] = []
    seen: set[str] = set()
    for index, combination in enumerate(combinations[:max_trials]):
        values = dict(zip(names, combination, strict=True))
        raw = copy.deepcopy(spec.raw)
        raw["name"] = f"{spec.name}_trial_{index:04d}"
        raw["variant_parameters"] = values
        for name, value in values.items():
            set_nested(raw, SUPPORTED_PARAMETER_TARGETS[name], value)
        variant = parse_strategy_spec(raw)
        digest = strategy_spec_hash(variant)
        if digest in seen:
            continue
        seen.add(digest)
        variants.append(variant)
    return variants or [spec]


def set_nested(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    target = payload
    for key in path[:-1]:
        target = target.setdefault(key, {})
    target[path[-1]] = value
