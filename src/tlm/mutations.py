from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .strategy import StrategySpec, parse_strategy_spec
from .variants import stable_hash, strategy_spec_hash


@dataclass(frozen=True)
class StrategyMutation:
    mutation_type: str
    parent_hash: str
    mutation_id: str
    strategy: StrategySpec

    def to_dict(self) -> dict[str, Any]:
        return {
            "mutation_type": self.mutation_type,
            "parent_hash": self.parent_hash,
            "mutation_id": self.mutation_id,
            "strategy_name": self.strategy.name,
            "strategy_spec_hash": strategy_spec_hash(self.strategy),
        }


def generate_controlled_mutations(spec: StrategySpec) -> list[StrategyMutation]:
    return [inverse_signal_mutation(spec)]


def inverse_signal_mutation(spec: StrategySpec) -> StrategyMutation:
    raw = copy.deepcopy(spec.raw)
    parent_hash = strategy_spec_hash(spec)
    raw["name"] = f"{raw['name']}_inverse"
    raw["market_hypothesis"] = (
        f"Inverse-signal mutation of {spec.name}; tests whether the original edge direction "
        "was systematically wrong under the same risk and execution constraints."
    )
    raw["direction"] = _inverse_direction(str(raw.get("direction", spec.direction)))
    raw["entry"] = _swap_long_short(raw.get("entry", {}))
    if isinstance(raw.get("signal_grammar"), dict):
        grammar = copy.deepcopy(raw["signal_grammar"])
        grammar["entry"] = _swap_long_short(grammar.get("entry", {}))
        raw["signal_grammar"] = grammar
    raw["mutation"] = {
        "type": "inverse_signal",
        "parent_strategy": spec.name,
        "parent_hash": parent_hash,
    }
    mutation_id = stable_hash({"type": "inverse_signal", "parent_hash": parent_hash})[:16]
    raw["mutation"]["mutation_id"] = mutation_id
    parsed = parse_strategy_spec(raw)
    return StrategyMutation(
        mutation_type="inverse_signal",
        parent_hash=parent_hash,
        mutation_id=mutation_id,
        strategy=parsed,
    )


def _inverse_direction(direction: str) -> str:
    if direction == "long":
        return "short"
    if direction == "short":
        return "long"
    return "long_short"


def _swap_long_short(entry: dict) -> dict:
    if not isinstance(entry, dict):
        return entry
    swapped = copy.deepcopy(entry)
    long_rule = entry.get("long")
    short_rule = entry.get("short")
    if long_rule is not None:
        swapped["short"] = long_rule
    else:
        swapped.pop("short", None)
    if short_rule is not None:
        swapped["long"] = short_rule
    else:
        swapped.pop("long", None)
    return swapped
