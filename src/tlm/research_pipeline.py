from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Sequence

from .config import get_cost_model, get_symbol
from .experiments import record_audit_event, record_experiment, record_trial
from .research import ResearchRunResult, run_budgeted_research, write_research_result
from .research_config import ResearchPolicy, primary_research_policy
from .strategy import StrategySpec, parse_strategy_spec
from .variants import DEFAULT_PARAMETER_BUDGET


ResearchRunner = Callable[..., list[ResearchRunResult]]
ResearchWriter = Callable[..., None]


@dataclass(frozen=True)
class ResearchPipelineResult:
    experiment_id: str
    trials: int
    result_paths: list[str]
    policy: dict[str, Any]


class ResearchPipeline:
    def __init__(
        self,
        *,
        policy: ResearchPolicy | None = None,
        runner: ResearchRunner = run_budgeted_research,
        writer: ResearchWriter = write_research_result,
    ) -> None:
        self.policy = policy or primary_research_policy()
        self.runner = runner
        self.writer = writer

    def run(
        self,
        *,
        seed_spec: StrategySpec,
        data_root: Path,
        experiments_root: Path,
        experiment_db: Path,
        config_dir: Path,
        date_from: date,
        date_to: date,
        experiment_id: str,
        max_trials: int = 1,
        starting_equity: float = 100_000,
        max_parameter_combinations: int = DEFAULT_PARAMETER_BUDGET,
        allow_high_parameter_budget: bool = False,
        random_seed: int = 0,
        llm_model: str = "local-deterministic-template",
        llm_parameters: dict[str, Any] | None = None,
        quote_reports: Sequence[Path] = (),
        paper_reports: Sequence[Path] = (),
    ) -> ResearchPipelineResult:
        llm_parameters = llm_parameters or {}
        primary_spec = self._primary_spec(seed_spec)
        symbol = get_symbol(self.policy.symbol, config_dir)
        cost_model = get_cost_model(self.policy.cost_model, config_dir)
        validation = self.policy.validation
        execution = self.policy.execution
        record_experiment(
            experiment_db,
            experiment_id=experiment_id,
            symbol=self.policy.symbol,
            status="running",
            metadata={
                "primary_research_policy": self.policy.to_dict(),
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "execution_mode": execution.default_execution_mode,
                "max_trials": max_trials,
                "llm_model": llm_model,
                "llm_parameters": llm_parameters,
                "seed_spec": primary_spec.name,
                "quote_reports": [str(path) for path in quote_reports],
                "paper_reports": [str(path) for path in paper_reports],
            },
        )
        results = self.runner(
            seed_spec=primary_spec,
            symbol_config=symbol,
            data_root=data_root,
            experiment_id=experiment_id,
            date_from=date_from,
            date_to=date_to,
            max_trials=max_trials,
            starting_equity=starting_equity,
            train_days=validation.train_days,
            validation_days=validation.validation_days,
            test_days=validation.test_days,
            step_days=validation.step_days,
            embargo_days=validation.embargo_days,
            final_holdout_days=validation.final_holdout_days,
            min_folds=validation.min_folds,
            indicator_warmup_days=validation.indicator_warmup_days,
            max_parameter_combinations=max_parameter_combinations,
            allow_high_parameter_budget=allow_high_parameter_budget,
            execution_mode=execution.default_execution_mode,
            cost_model=cost_model,
            config_dir=config_dir,
            random_seed=random_seed,
            llm_model=llm_model,
            llm_parameters=llm_parameters,
        )
        result_paths: list[str] = []
        for result in results:
            output_path = experiments_root / result.experiment_id / "leaderboard.json"
            self.writer(
                output_path,
                result,
                quote_reports=quote_reports,
                paper_reports=paper_reports,
            )
            record_trial(experiment_db, experiment_id, result)
            record_audit_event(
                experiment_db,
                experiment_id=experiment_id,
                trial_id=result.experiment_id,
                event_type="primary_research_trial_completed",
                payload={
                    "trial_id": result.experiment_id,
                    "strategy_spec_hash": result.strategy_spec_hash,
                    "prompt_hash": result.prompt_hash,
                    "gates": result.gates,
                    "primary_research_policy": self.policy.to_dict(),
                },
            )
            result_paths.append(str(output_path))
        record_experiment(
            experiment_db,
            experiment_id=experiment_id,
            symbol=self.policy.symbol,
            status="completed",
            metadata={
                "trials": len(results),
                "primary_research_policy": self.policy.to_dict(),
                "execution_mode": execution.default_execution_mode,
                "quote_reports": [str(path) for path in quote_reports],
                "paper_reports": [str(path) for path in paper_reports],
            },
        )
        return ResearchPipelineResult(
            experiment_id=experiment_id,
            trials=len(results),
            result_paths=result_paths,
            policy=self.policy.to_dict(),
        )

    def _primary_spec(self, seed_spec: StrategySpec) -> StrategySpec:
        raw = {
            **seed_spec.raw,
            "symbol": self.policy.symbol,
            "timeframe": self.policy.timeframe,
            "cost_model": self.policy.cost_model,
        }
        return parse_strategy_spec(raw)
