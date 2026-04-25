from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ALLOWED_FAMILIES = {
    "opening_range_breakout",
    "trend_pullback",
    "volatility_expansion",
    "intraday_momentum",
    "regime_filtered_mean_reversion",
    "time_of_day_edge",
    "gap_fade_or_continuation",
    "controlled_grid",
}

BANNED_FAMILIES = {
    "martingale",
    "infinite_dca",
    "unbounded_grid",
    "loss_doubling",
    "no_stop_inventory_accumulation",
}

ALLOWED_DIRECTIONS = {"long", "short", "long_short"}
ALLOWED_LOGIC = {"all", "any", "not"}
ALLOWED_OPERATORS = {">", ">=", "<", "<=", "==", "crosses_above", "crosses_below"}


class StrategySpecError(ValueError):
    """Raised when a Strategy Spec is invalid."""


@dataclass(frozen=True)
class SessionSpec:
    timezone: str
    trade: str
    flatten: str


@dataclass(frozen=True)
class StrategySpec:
    schema_version: int
    name: str
    strategy_family: str
    market_hypothesis: str
    symbol: str
    timeframe: str
    direction: str
    session: SessionSpec
    indicators: dict[str, Any]
    entry: dict[str, Any]
    exit: dict[str, Any]
    risk: dict[str, Any]
    anti_martingale_constraints: dict[str, Any]
    parameters: dict[str, Any]
    cost_model: str
    raw: dict[str, Any]


def load_strategy_spec(path: Path) -> StrategySpec:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StrategySpecError(f"Strategy spec not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise StrategySpecError(
            f"{path} must be JSON-compatible YAML until a YAML parser is installed"
        ) from exc
    return parse_strategy_spec(payload)


def parse_strategy_spec(payload: dict[str, Any]) -> StrategySpec:
    if not isinstance(payload, dict):
        raise StrategySpecError("Strategy spec must be an object")

    required = [
        "schema_version",
        "name",
        "strategy_family",
        "market_hypothesis",
        "symbol",
        "timeframe",
        "direction",
        "session",
        "indicators",
        "entry",
        "exit",
        "risk",
        "anti_martingale_constraints",
        "parameters",
        "cost_model",
    ]
    for key in required:
        if key not in payload:
            raise StrategySpecError(f"Missing required field: {key}")

    if payload["schema_version"] != 0:
        raise StrategySpecError("Only schema_version 0 is supported")

    family = str(payload["strategy_family"])
    if family in BANNED_FAMILIES:
        raise StrategySpecError(f"Banned strategy_family: {family}")
    if family not in ALLOWED_FAMILIES:
        raise StrategySpecError(f"Unsupported strategy_family: {family}")

    hypothesis = str(payload["market_hypothesis"]).strip()
    if len(hypothesis) < 20:
        raise StrategySpecError("market_hypothesis must be specific and non-empty")

    direction = str(payload["direction"])
    if direction not in ALLOWED_DIRECTIONS:
        raise StrategySpecError(f"Unsupported direction: {direction}")

    session_payload = _require_object(payload["session"], "session")
    session = SessionSpec(
        timezone=str(session_payload.get("timezone", "UTC")),
        trade=str(session_payload["trade"]),
        flatten=str(session_payload["flatten"]),
    )

    indicators = _require_object(payload["indicators"], "indicators")
    entry = _require_object(payload["entry"], "entry")
    exit_spec = _require_object(payload["exit"], "exit")
    risk = _require_object(payload["risk"], "risk")
    anti_martingale = _require_object(
        payload["anti_martingale_constraints"], "anti_martingale_constraints"
    )
    parameters = _require_object(payload["parameters"], "parameters")

    _validate_anti_martingale(family, anti_martingale, risk)
    _validate_entry(entry)
    _validate_parameters(parameters)
    _validate_exit(exit_spec)

    return StrategySpec(
        schema_version=int(payload["schema_version"]),
        name=str(payload["name"]),
        strategy_family=family,
        market_hypothesis=hypothesis,
        symbol=str(payload["symbol"]),
        timeframe=str(payload["timeframe"]),
        direction=direction,
        session=session,
        indicators=indicators,
        entry=entry,
        exit=exit_spec,
        risk=risk,
        anti_martingale_constraints=anti_martingale,
        parameters=parameters,
        cost_model=str(payload["cost_model"]),
        raw=payload,
    )


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StrategySpecError(f"{name} must be an object")
    return value


def _validate_anti_martingale(
    family: str,
    anti_martingale: dict[str, Any],
    risk: dict[str, Any],
) -> None:
    if anti_martingale.get("forbid_loss_doubling") is not True:
        raise StrategySpecError("forbid_loss_doubling must be true")
    if anti_martingale.get("forbid_position_increase_when_unrealized_loss") is not True:
        raise StrategySpecError("forbid_position_increase_when_unrealized_loss must be true")

    max_grid_levels = int(anti_martingale.get("max_grid_levels", 0))
    if family != "controlled_grid" and max_grid_levels != 0:
        raise StrategySpecError("Only controlled_grid may define grid levels")
    if family == "controlled_grid":
        if max_grid_levels <= 0:
            raise StrategySpecError("controlled_grid requires finite max_grid_levels")
        for key in ["max_position_contracts", "max_daily_loss_r"]:
            if key not in risk:
                raise StrategySpecError(f"controlled_grid requires risk.{key}")


def _validate_entry(entry: dict[str, Any]) -> None:
    for side in ["long", "short"]:
        if side in entry:
            _validate_logic_node(entry[side], f"entry.{side}")


def _validate_logic_node(node: Any, path: str) -> None:
    if not isinstance(node, dict):
        raise StrategySpecError(f"{path} must be an object")
    logic_keys = [key for key in node if key in ALLOWED_LOGIC]
    if logic_keys:
        for key in logic_keys:
            value = node[key]
            if key == "not":
                _validate_logic_node(value, f"{path}.not")
            else:
                if not isinstance(value, list) or not value:
                    raise StrategySpecError(f"{path}.{key} must be a non-empty list")
                for index, child in enumerate(value):
                    _validate_logic_node(child, f"{path}.{key}[{index}]")
        return

    for key in ["left", "op", "right"]:
        if key not in node:
            raise StrategySpecError(f"{path} comparison missing {key}")
    if node["op"] not in ALLOWED_OPERATORS:
        raise StrategySpecError(f"{path} uses unsupported operator: {node['op']}")
    for key in ["left", "right"]:
        value = str(node[key]).lower()
        if "future" in value or "lookahead" in value:
            raise StrategySpecError(f"{path}.{key} references future data")


def _validate_parameters(parameters: dict[str, Any]) -> None:
    for name, spec in parameters.items():
        if not isinstance(spec, dict):
            raise StrategySpecError(f"parameters.{name} must be an object")
        if "values" in spec:
            values = spec["values"]
            if not isinstance(values, list) or not values:
                raise StrategySpecError(f"parameters.{name}.values must be a non-empty list")
            continue
        if {"min", "max", "step"}.issubset(spec):
            minimum = float(spec["min"])
            maximum = float(spec["max"])
            step = float(spec["step"])
            if maximum < minimum or step <= 0:
                raise StrategySpecError(f"parameters.{name} has invalid min/max/step")
            combinations = int((maximum - minimum) / step) + 1
            if combinations > 500:
                raise StrategySpecError(f"parameters.{name} expands to too many values")
            continue
        raise StrategySpecError(f"parameters.{name} must define values or min/max/step")


def _validate_exit(exit_spec: dict[str, Any]) -> None:
    if "stop_loss" not in exit_spec:
        raise StrategySpecError("exit.stop_loss is required")
    if "take_profit" not in exit_spec:
        raise StrategySpecError("exit.take_profit is required")
    if "max_holding_minutes" not in exit_spec:
        raise StrategySpecError("exit.max_holding_minutes is required")
