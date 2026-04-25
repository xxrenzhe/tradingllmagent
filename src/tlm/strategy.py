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
ALLOWED_INDICATOR_TYPES = {
    "ema",
    "sma",
    "atr",
    "rsi",
    "bollinger_bands",
    "vwap",
    "session_vwap",
    "opening_range",
    "realized_volatility",
    "z_score",
    "momentum",
    "time_of_day",
    "gap",
}
ALLOWED_EXIT_TYPES = {"points", "atr_multiple"}
ALLOWED_POSITION_SIZING_TYPES = {"fixed_contracts"}
CODELIKE_TOKENS = (
    "import ",
    "exec(",
    "eval(",
    "subprocess",
    "os.system",
    "__import__",
    "lambda ",
    "function ",
    "=>",
    "<script",
    "rm -rf",
    "curl ",
    "wget ",
    "powershell",
)
MAX_VALUES_PER_PARAMETER = 500


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
    _reject_codelike_values(payload)

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
    _validate_indicators(indicators)
    _validate_entry(entry)
    _validate_parameters(parameters)
    _validate_exit(exit_spec)
    _validate_risk(risk)
    _validate_controlled_grid(family, indicators, exit_spec, risk, anti_martingale)

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


def _validate_indicators(indicators: dict[str, Any]) -> None:
    if not indicators:
        raise StrategySpecError("indicators must define at least one indicator")
    for name, indicator in indicators.items():
        if not isinstance(indicator, dict):
            raise StrategySpecError(f"indicators.{name} must be an object")
        indicator_type = indicator.get("type")
        if indicator_type not in ALLOWED_INDICATOR_TYPES:
            raise StrategySpecError(f"indicators.{name} uses unsupported type: {indicator_type}")


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
            if combinations > MAX_VALUES_PER_PARAMETER:
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
    for name in ["stop_loss", "take_profit"]:
        config = _require_object(exit_spec[name], f"exit.{name}")
        exit_type = config.get("type")
        if exit_type not in ALLOWED_EXIT_TYPES:
            raise StrategySpecError(f"exit.{name} uses unsupported type: {exit_type}")


def _validate_risk(risk: dict[str, Any]) -> None:
    sizing = _require_object(risk.get("position_sizing"), "risk.position_sizing")
    sizing_type = sizing.get("type")
    if sizing_type not in ALLOWED_POSITION_SIZING_TYPES:
        raise StrategySpecError(f"risk.position_sizing uses unsupported type: {sizing_type}")


def _validate_controlled_grid(
    family: str,
    indicators: dict[str, Any],
    exit_spec: dict[str, Any],
    risk: dict[str, Any],
    anti_martingale: dict[str, Any],
) -> None:
    if family != "controlled_grid":
        return
    range_indicators = {
        name: config
        for name, config in indicators.items()
        if config.get("type") in {"z_score", "bollinger_bands"}
    }
    if not range_indicators:
        raise StrategySpecError("controlled_grid requires a range-regime indicator")
    max_grid_levels = int(anti_martingale.get("max_grid_levels", 0))
    max_position_contracts = int(risk.get("max_position_contracts", 0))
    if max_position_contracts <= 0:
        raise StrategySpecError("controlled_grid requires positive risk.max_position_contracts")
    if max_grid_levels > max_position_contracts:
        raise StrategySpecError("controlled_grid max_grid_levels cannot exceed max_position_contracts")
    if float(risk.get("max_daily_loss_r", 0)) <= 0:
        raise StrategySpecError("controlled_grid requires positive risk.max_daily_loss_r")
    stop_loss = _require_object(exit_spec.get("stop_loss"), "exit.stop_loss")
    if float(stop_loss.get("value", 0)) <= 0:
        raise StrategySpecError("controlled_grid requires positive hard stop loss")


def _reject_codelike_values(value: Any, path: str = "strategy_spec") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_codelike_values(child, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_codelike_values(child, f"{path}[{index}]")
        return
    if not isinstance(value, str):
        return
    lowered = value.lower()
    if any(token in lowered for token in CODELIKE_TOKENS):
        raise StrategySpecError(f"{path} contains executable code or shell-like content")
