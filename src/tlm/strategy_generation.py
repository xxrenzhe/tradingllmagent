from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .feature_catalog import FEATURE_CATALOG, FeatureDefinition, features_for_data_levels
from .strategy import parse_strategy_spec
from .variants import parameter_grid_metadata


EXECUTABLE_RANDOM_FEATURE_FAMILIES = (
    "opening_range_breakout",
    "trend_pullback",
    "volatility_expansion",
    "intraday_momentum",
    "regime_filtered_mean_reversion",
    "time_of_day_edge",
    "gap_fade_or_continuation",
)

_FAMILY_FEATURE_WEIGHTS = {
    "opening_range_breakout": {"opening_range", "breakout", "volume", "vwap", "time", "volatility"},
    "trend_pullback": {"trend", "return", "vwap", "volume", "volatility"},
    "volatility_expansion": {"volatility", "breakout", "volume", "candle", "time"},
    "intraday_momentum": {"return", "trend", "volume", "vwap", "cross_asset", "time"},
    "regime_filtered_mean_reversion": {"mean_reversion", "oscillator", "level", "vwap", "volatility"},
    "time_of_day_edge": {"time", "volume", "event", "return", "volatility"},
    "gap_fade_or_continuation": {"gap", "level", "opening_range", "volume", "event", "volatility"},
}


def generate_feature_combo_strategy_specs(
    count: int,
    random_seed: int = 0,
    symbol: str = "NQmain",
    timeframe: str = "1m",
    prefix: str = "generated_feature_combo",
) -> list[dict[str, Any]]:
    if count <= 0:
        raise ValueError("count must be positive")
    rng = random.Random(random_seed)
    features = features_for_data_levels({"bar", "calendar", "external_bar"})
    specs = []
    family_offsets = {family: 0 for family in EXECUTABLE_RANDOM_FEATURE_FAMILIES}
    for index in range(count):
        family = EXECUTABLE_RANDOM_FEATURE_FAMILIES[index % len(EXECUTABLE_RANDOM_FEATURE_FAMILIES)]
        family_offsets[family] += 1
        chosen = _sample_features(rng, family, features)
        spec = _build_spec(
            family=family,
            family_index=family_offsets[family],
            global_index=index + 1,
            features=chosen,
            symbol=symbol,
            timeframe=timeframe,
            prefix=prefix,
        )
        parsed = parse_strategy_spec(spec)
        metadata = parameter_grid_metadata(parsed, max_trials=1)
        if metadata.high_risk_budget:
            raise ValueError(
                f"generated strategy {parsed.name} exceeds high-risk grid budget: "
                f"{metadata.total_combinations}"
            )
        specs.append(spec)
    return specs


def write_feature_combo_strategy_specs(
    output_dir: Path,
    count: int,
    random_seed: int = 0,
    symbol: str = "NQmain",
    timeframe: str = "1m",
    prefix: str = "generated_feature_combo",
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    specs = generate_feature_combo_strategy_specs(
        count=count,
        random_seed=random_seed,
        symbol=symbol,
        timeframe=timeframe,
        prefix=prefix,
    )
    paths = []
    for spec in specs:
        path = output_dir / f"{spec['name']}.yaml"
        path.write_text(json.dumps(spec, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        paths.append(path)
    return paths


def _sample_features(
    rng: random.Random,
    family: str,
    features: tuple[FeatureDefinition, ...],
) -> tuple[FeatureDefinition, ...]:
    preferred_categories = _FAMILY_FEATURE_WEIGHTS[family]
    preferred = [feature for feature in features if feature.category in preferred_categories]
    other = [feature for feature in features if feature.category not in preferred_categories]
    sample_size = rng.randint(4, 6)
    preferred_count = min(len(preferred), max(3, sample_size - 1))
    chosen = rng.sample(preferred, preferred_count)
    remaining = sample_size - len(chosen)
    if remaining > 0:
        chosen.extend(rng.sample(other, remaining))
    rng.shuffle(chosen)
    return tuple(chosen)


def _build_spec(
    family: str,
    family_index: int,
    global_index: int,
    features: tuple[FeatureDefinition, ...],
    symbol: str,
    timeframe: str,
    prefix: str,
) -> dict[str, Any]:
    template = _family_template(family, family_index)
    feature_names = [feature.name for feature in features]
    name = f"nq_{prefix}_{_family_slug(family)}_{family_index:02d}"
    hypothesis = (
        f"{family.replace('_', ' ')} candidate generated from random intraday feature "
        f"combination {', '.join(feature_names[:4])}; the feature mix is tested as a "
        "bounded NQ one-minute seed for LLM-guided parameter search."
    )
    return {
        "schema_version": 0,
        "name": name,
        "strategy_family": family,
        "market_hypothesis": hypothesis,
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": template["direction"],
        "session": {
            "timezone": "UTC",
            "trade": template["trade_session"],
            "flatten": "20:55",
        },
        "feature_set": [
            {
                "name": feature.name,
                "category": feature.category,
                "data_level": feature.data_level,
                "description": feature.description,
            }
            for feature in features
        ],
        "generation": {
            "method": "deterministic_random_feature_combo",
            "global_index": global_index,
            "feature_catalog_size": len(FEATURE_CATALOG),
        },
        "regime_filter": {},
        "indicators": template["indicators"],
        "entry": template["entry"],
        "exit": template["exit"],
        "risk": {
            "position_sizing": {"type": "fixed_contracts", "contracts": 1},
            "max_position_contracts": 1,
            "max_trades_per_day": template["max_trades_per_day"],
            "max_daily_loss_r": 3,
        },
        "anti_martingale_constraints": {
            "forbid_loss_doubling": True,
            "forbid_position_increase_when_unrealized_loss": True,
            "max_grid_levels": 0,
        },
        "parameters": template["parameters"],
        "cost_model": "nq_conservative_v1",
    }


def _family_template(family: str, family_index: int) -> dict[str, Any]:
    if family == "opening_range_breakout":
        minutes = [5, 10, 15, 20][family_index % 4]
        return {
            "direction": "long_short",
            "trade_session": "13:45-20:45",
            "max_trades_per_day": 24,
            "indicators": {"opening_range": {"type": "opening_range", "minutes": minutes}},
            "entry": {
                "long": {"all": [{"left": "close", "op": ">", "right": "opening_range.high"}]},
                "short": {"all": [{"left": "close", "op": "<", "right": "opening_range.low"}]},
            },
            "exit": _exit(14, 22, 35),
            "parameters": {
                "opening_range_minutes": {"values": [5, 10, 15]},
                "stop_points": {"values": [10, 14, 18]},
                "take_profit_points": {"values": [16, 22, 28]},
                "max_holding_minutes": {"values": [25, 35]},
            },
        }
    if family == "trend_pullback":
        fast = [6, 8, 9, 12][family_index % 4]
        slow = [18, 21, 26, 34][family_index % 4]
        return {
            "direction": "long_short",
            "trade_session": "13:35-20:30",
            "max_trades_per_day": 35,
            "indicators": {
                "ema_fast": {"type": "ema", "window": fast},
                "ema_slow": {"type": "ema", "window": slow},
            },
            "entry": {
                "long": {"all": [{"left": "ema_fast", "op": ">", "right": "ema_slow"}]},
                "short": {"all": [{"left": "ema_fast", "op": "<", "right": "ema_slow"}]},
            },
            "exit": _exit(12, 20, 28),
            "parameters": {
                "ema_fast_window": {"values": [6, 8, 10]},
                "ema_slow_window": {"values": [18, 21, 26]},
                "stop_points": {"values": [10, 12, 16]},
                "take_profit_points": {"values": [16, 20, 24]},
                "max_holding_minutes": {"values": [20, 28]},
            },
        }
    if family == "volatility_expansion":
        window = [4, 5, 8, 10][family_index % 4]
        return {
            "direction": "long_short",
            "trade_session": "13:35-20:35",
            "max_trades_per_day": 45,
            "indicators": {
                "realized_volatility": {
                    "type": "realized_volatility",
                    "window": window,
                    "expansion_multiple": 1.2,
                }
            },
            "entry": {
                "long": {"all": [{"left": "close", "op": ">", "right": "prior.high"}]},
                "short": {"all": [{"left": "close", "op": "<", "right": "prior.low"}]},
            },
            "exit": _exit(10, 18, 22),
            "parameters": {
                "volatility_window": {"values": [4, 5, 8]},
                "volatility_expansion_multiple": {"values": [1.1, 1.2, 1.35]},
                "stop_points": {"values": [8, 10, 14]},
                "take_profit_points": {"values": [14, 18, 24]},
                "max_holding_minutes": {"values": [16, 22]},
            },
        }
    if family == "intraday_momentum":
        lookback = [2, 3, 5, 8][family_index % 4]
        return {
            "direction": "long_short",
            "trade_session": "13:35-20:35",
            "max_trades_per_day": 60,
            "indicators": {
                "momentum": {
                    "type": "momentum",
                    "lookback_minutes": lookback,
                    "threshold_points": 4,
                }
            },
            "entry": {
                "long": {"all": [{"left": "momentum", "op": ">=", "right": "threshold_points"}]},
                "short": {"all": [{"left": "momentum", "op": "<=", "right": "negative_threshold_points"}]},
            },
            "exit": _exit(8, 12, 16),
            "parameters": {
                "momentum_lookback_minutes": {"values": [2, 3, 5]},
                "momentum_threshold_points": {"values": [3, 4, 6]},
                "stop_points": {"values": [6, 8, 10]},
                "take_profit_points": {"values": [10, 12, 16]},
                "max_holding_minutes": {"values": [10, 16]},
            },
        }
    if family == "regime_filtered_mean_reversion":
        window = [12, 16, 20, 30][family_index % 4]
        return {
            "direction": "long_short",
            "trade_session": "13:40-20:35",
            "max_trades_per_day": 40,
            "indicators": {"z_close": {"type": "z_score", "source": "close", "window": window, "entry_z": 1.2}},
            "entry": {
                "long": {"all": [{"left": "z_close", "op": "<=", "right": "-entry_z"}]},
                "short": {"all": [{"left": "z_close", "op": ">=", "right": "entry_z"}]},
            },
            "exit": _exit(10, 14, 20),
            "parameters": {
                "mean_reversion_window": {"values": [12, 16, 20]},
                "mean_reversion_entry_z": {"values": [1.0, 1.2, 1.4]},
                "stop_points": {"values": [8, 10, 12]},
                "take_profit_points": {"values": [12, 14, 18]},
                "max_holding_minutes": {"values": [14, 20]},
            },
        }
    if family == "time_of_day_edge":
        entry_time = ["13:35", "14:10", "19:30", "20:00"][family_index % 4]
        return {
            "direction": "long_short",
            "trade_session": "13:30-20:45",
            "max_trades_per_day": 8,
            "indicators": {"time_of_day": {"type": "time_of_day", "entry_time": entry_time, "entry_side": "long"}},
            "entry": {
                "long": {"all": [{"left": "time", "op": ">=", "right": entry_time}]},
                "short": {"all": [{"left": "time", "op": ">=", "right": entry_time}]},
            },
            "exit": _exit(12, 18, 30),
            "parameters": {
                "entry_time": {"values": ["13:35", "14:10", "19:30"]},
                "entry_side": {"values": ["long", "short"]},
                "stop_points": {"values": [10, 12, 16]},
                "take_profit_points": {"values": [16, 18, 24]},
                "max_holding_minutes": {"values": [20, 30]},
            },
        }
    if family == "gap_fade_or_continuation":
        mode = "fade" if family_index % 2 else "continuation"
        return {
            "direction": "long_short",
            "trade_session": "13:30-20:20",
            "max_trades_per_day": 6,
            "indicators": {"gap": {"type": "gap", "threshold_points": 6, "mode": mode}},
            "entry": {
                "long": {"all": [{"left": "gap_signal", "op": "==", "right": "long"}]},
                "short": {"all": [{"left": "gap_signal", "op": "==", "right": "short"}]},
            },
            "exit": _exit(12, 18, 35),
            "parameters": {
                "gap_threshold_points": {"values": [4, 6, 8]},
                "gap_mode": {"values": ["fade", "continuation"]},
                "stop_points": {"values": [10, 12, 16]},
                "take_profit_points": {"values": [16, 18, 24]},
                "max_holding_minutes": {"values": [25, 35]},
            },
        }
    raise ValueError(f"Unsupported strategy family: {family}")


def _exit(stop: float, take_profit: float, max_holding_minutes: int) -> dict[str, Any]:
    return {
        "stop_loss": {"type": "points", "value": stop},
        "take_profit": {"type": "points", "value": take_profit},
        "max_holding_minutes": max_holding_minutes,
    }


def _family_slug(family: str) -> str:
    return {
        "opening_range_breakout": "orb",
        "trend_pullback": "trend",
        "volatility_expansion": "vol",
        "intraday_momentum": "mom",
        "regime_filtered_mean_reversion": "revert",
        "time_of_day_edge": "tod",
        "gap_fade_or_continuation": "gap",
    }[family]
