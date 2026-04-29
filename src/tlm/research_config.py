from __future__ import annotations

from dataclasses import asdict, dataclass


PRIMARY_RESEARCH_SYMBOL = "NQ_CME"
PRIMARY_RESEARCH_TIMEFRAME = "1m"
PRIMARY_COST_MODEL = "nq_conservative_v1"
PRIMARY_TOP_STRATEGY_OBJECTIVE = "expectancy_first"
PRIMARY_TOP_STRATEGY_COMPARISON_OBJECTIVES = (
    "stability_first",
    "annualized_quality",
)


@dataclass(frozen=True)
class ValidationWindowPolicy:
    train_days: int = 730
    validation_days: int = 182
    test_days: int = 182
    step_days: int = 91
    embargo_days: int = 5
    final_holdout_days: int = 365
    min_folds: int = 1
    indicator_warmup_days: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionValidationPolicy:
    required_symbol: str = PRIMARY_RESEARCH_SYMBOL
    default_execution_mode: str = "bar_then_tick"
    quote_evidence_required: bool = True
    paper_shadow_required: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RankingPolicy:
    primary_objective: str = PRIMARY_TOP_STRATEGY_OBJECTIVE
    comparison_objectives: tuple[str, ...] = PRIMARY_TOP_STRATEGY_COMPARISON_OBJECTIVES

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["comparison_objectives"] = list(self.comparison_objectives)
        return payload


@dataclass(frozen=True)
class ResearchPolicy:
    symbol: str = PRIMARY_RESEARCH_SYMBOL
    timeframe: str = PRIMARY_RESEARCH_TIMEFRAME
    cost_model: str = PRIMARY_COST_MODEL
    validation: ValidationWindowPolicy = ValidationWindowPolicy()
    execution: ExecutionValidationPolicy = ExecutionValidationPolicy()
    ranking: RankingPolicy = RankingPolicy()

    @property
    def top_strategy_objective(self) -> str:
        return self.ranking.primary_objective

    @property
    def top_strategy_comparison_objectives(self) -> tuple[str, ...]:
        return self.ranking.comparison_objectives

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "cost_model": self.cost_model,
            "validation": self.validation.to_dict(),
            "execution": self.execution.to_dict(),
            "ranking": self.ranking.to_dict(),
        }


PrimaryResearchPolicy = ResearchPolicy


def primary_research_policy() -> ResearchPolicy:
    return ResearchPolicy()
