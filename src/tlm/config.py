from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_DIR = Path("configs")


class ConfigError(ValueError):
    """Raised when a project configuration file is invalid."""


@dataclass(frozen=True)
class SymbolConfig:
    alias: str
    provider: str
    instrument: str
    description: str
    timezone: str
    price_scale: int
    tick_size: float
    point_value: float
    default_session_timezone: str
    default_trade_session: str
    default_flatten_time: str


def _load_json_yaml(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path} must be JSON-compatible YAML until a YAML parser is installed"
        ) from exc


def load_symbols(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict[str, SymbolConfig]:
    payload = _load_json_yaml(config_dir / "symbols.yaml")
    symbols = payload.get("symbols")
    if not isinstance(symbols, dict):
        raise ConfigError("symbols.yaml must contain a 'symbols' object")

    parsed: dict[str, SymbolConfig] = {}
    for alias, data in symbols.items():
        if not isinstance(data, dict):
            raise ConfigError(f"Symbol {alias} must be an object")
        try:
            parsed[alias] = SymbolConfig(
                alias=alias,
                provider=str(data["provider"]),
                instrument=str(data["instrument"]),
                description=str(data.get("description", "")),
                timezone=str(data.get("timezone", "UTC")),
                price_scale=int(data["price_scale"]),
                tick_size=float(data["tick_size"]),
                point_value=float(data["point_value"]),
                default_session_timezone=str(
                    data.get("default_session_timezone", "America/New_York")
                ),
                default_trade_session=str(data.get("default_trade_session", "09:35-15:45")),
                default_flatten_time=str(data.get("default_flatten_time", "15:55")),
            )
        except KeyError as exc:
            raise ConfigError(f"Symbol {alias} missing required key: {exc.args[0]}") from exc
    return parsed


def get_symbol(alias: str, config_dir: Path = DEFAULT_CONFIG_DIR) -> SymbolConfig:
    symbols = load_symbols(config_dir)
    try:
        return symbols[alias]
    except KeyError as exc:
        known = ", ".join(sorted(symbols)) or "<none>"
        raise ConfigError(f"Unknown symbol {alias!r}. Known symbols: {known}") from exc
