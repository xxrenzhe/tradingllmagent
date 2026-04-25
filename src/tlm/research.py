from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

from .backtest import (
    BacktestResult,
    backtest_data_version_hash,
    default_cost_model,
    run_bar_backtest,
    run_tick_backtest,
)
from .cli_dates import iter_dates
from .config import CostModelConfig, SymbolConfig
from .leaderboard import evaluate_hard_gates, robustness_score
from .metrics import BacktestMetrics, calculate_metrics
from .storage import bar_path, normalized_tick_path
from .strategy import StrategySpec
from .variants import (
    DEFAULT_PARAMETER_BUDGET,
    ParameterGridMetadata,
    expand_strategy_variants,
    parameter_grid_metadata,
    prompt_hash,
    strategy_spec_hash,
)
from .validation import ValidationPlan, generate_rolling_folds


@dataclass(frozen=True)
class ResearchRunResult:
    experiment_id: str
    execution_mode: str
    data_version_hash: str
    cost_model: dict
    strategy_name: str
    strategy_spec_hash: str
    prompt_hash: str
    variant_parameters: dict
    parameter_grid: dict
    trial_count: int
    parameter_combination_count: int
    parameter_budget_exceeded: bool
    parameter_grid_hash: str
    validation_plan: ValidationPlan
    fold_results: list[dict]
    aggregate_test_metrics: BacktestMetrics
    final_holdout_data_version_hash: str
    final_holdout_metrics: BacktestMetrics
    gates: dict
    robustness_score: float | None

    def to_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "execution_mode": self.execution_mode,
            "data_version_hash": self.data_version_hash,
            "cost_model": self.cost_model,
            "strategy_name": self.strategy_name,
            "strategy_spec_hash": self.strategy_spec_hash,
            "prompt_hash": self.prompt_hash,
            "variant_parameters": self.variant_parameters,
            "parameter_grid": self.parameter_grid,
            "trial_count": self.trial_count,
            "parameter_combination_count": self.parameter_combination_count,
            "parameter_budget_exceeded": self.parameter_budget_exceeded,
            "parameter_grid_hash": self.parameter_grid_hash,
            "validation_plan": self.validation_plan.to_dict(),
            "fold_results": self.fold_results,
            "aggregate_test_metrics": self.aggregate_test_metrics.to_dict(),
            "final_holdout_data_version_hash": self.final_holdout_data_version_hash,
            "final_holdout_metrics": self.final_holdout_metrics.to_dict(),
            "gates": self.gates,
            "robustness_score": self.robustness_score,
        }


def run_research_bar_validation(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    data_root: Path,
    experiment_id: str,
    date_from: date,
    date_to: date,
    starting_equity: float = 100_000,
    train_days: int = 730,
    validation_days: int = 182,
    test_days: int = 182,
    step_days: int = 91,
    embargo_days: int = 5,
    final_holdout_days: int = 365,
    min_folds: int = 1,
    grid_metadata: ParameterGridMetadata | None = None,
    execution_mode: str = "bar",
    cost_model: CostModelConfig | None = None,
) -> ResearchRunResult:
    grid_metadata = grid_metadata or parameter_grid_metadata(spec, max_trials=1)
    if execution_mode not in {"bar", "tick"}:
        raise ValueError(f"Unsupported execution_mode: {execution_mode}")
    active_cost_model = cost_model or default_cost_model(symbol_config, spec.cost_model)
    data_version_hash = _range_data_version_hash(
        spec,
        symbol_config,
        data_root,
        date_from,
        date_to,
        execution_mode,
        active_cost_model,
    )
    plan = generate_rolling_folds(
        start=date_from,
        end=date_to,
        train_days=train_days,
        validation_days=validation_days,
        test_days=test_days,
        step_days=step_days,
        embargo_days=embargo_days,
        final_holdout_days=final_holdout_days,
        min_folds=min_folds,
    )
    fold_results: list[dict] = []
    fold_test_metrics: list[BacktestMetrics] = []
    all_test_pnls: list[float] = []
    all_test_equity = [starting_equity]

    for fold in plan.folds:
        train = _run_range(
            spec,
            symbol_config,
            data_root,
            fold.train.start,
            fold.train.end,
            starting_equity,
            execution_mode,
            active_cost_model,
        )
        validation = _run_range(
            spec,
            symbol_config,
            data_root,
            fold.validation.start,
            fold.validation.end,
            starting_equity,
            execution_mode,
            active_cost_model,
        )
        test = _run_range(
            spec,
            symbol_config,
            data_root,
            fold.test.start,
            fold.test.end,
            starting_equity,
            execution_mode,
            active_cost_model,
        )
        fold_test_metrics.append(test.metrics)
        for trade in test.trades:
            all_test_pnls.append(trade.net_pnl)
            all_test_equity.append(all_test_equity[-1] + trade.net_pnl)
        fold_results.append(
            {
                "fold": fold.to_dict(),
                "train_data_version_hash": train.data_version_hash,
                "train_metrics": train.metrics.to_dict(),
                "validation_data_version_hash": validation.data_version_hash,
                "validation_metrics": validation.metrics.to_dict(),
                "test_data_version_hash": test.data_version_hash,
                "test_metrics": test.metrics.to_dict(),
            }
        )

    aggregate_days = sum(
        (fold.test.end - fold.test.start).days + 1
        for fold in plan.folds
    )
    aggregate_test_metrics = calculate_metrics(
        all_test_pnls,
        all_test_equity,
        starting_equity,
        aggregate_days,
    )
    holdout = _run_range(
        spec,
        symbol_config,
        data_root,
        plan.final_holdout.start,
        plan.final_holdout.end,
        starting_equity,
        execution_mode,
        active_cost_model,
    )
    gates = evaluate_hard_gates(aggregate_test_metrics, fold_test_metrics, holdout.metrics)
    score = robustness_score(
        aggregate_test_metrics,
        holdout.metrics,
        fold_test_metrics,
        parameter_budget_exceeded=grid_metadata.budget_exceeded,
        parameter_combination_count=grid_metadata.total_combinations,
        default_parameter_budget=grid_metadata.default_budget,
    )
    return ResearchRunResult(
        experiment_id=experiment_id,
        execution_mode=execution_mode,
        data_version_hash=data_version_hash,
        cost_model=active_cost_model.to_dict(),
        strategy_name=spec.name,
        strategy_spec_hash=strategy_spec_hash(spec),
        prompt_hash=prompt_hash(spec),
        variant_parameters=spec.raw.get("variant_parameters", {}),
        parameter_grid=grid_metadata.to_dict(),
        trial_count=grid_metadata.selected_combinations,
        parameter_combination_count=grid_metadata.total_combinations,
        parameter_budget_exceeded=grid_metadata.budget_exceeded,
        parameter_grid_hash=grid_metadata.parameter_grid_hash,
        validation_plan=plan,
        fold_results=fold_results,
        aggregate_test_metrics=aggregate_test_metrics,
        final_holdout_data_version_hash=holdout.data_version_hash,
        final_holdout_metrics=holdout.metrics,
        gates=gates.to_dict(),
        robustness_score=score,
    )


def run_budgeted_research(
    seed_spec: StrategySpec,
    symbol_config: SymbolConfig,
    data_root: Path,
    experiment_id: str,
    date_from: date,
    date_to: date,
    max_trials: int,
    starting_equity: float = 100_000,
    train_days: int = 730,
    validation_days: int = 182,
    test_days: int = 182,
    step_days: int = 91,
    embargo_days: int = 5,
    final_holdout_days: int = 365,
    min_folds: int = 1,
    max_parameter_combinations: int = DEFAULT_PARAMETER_BUDGET,
    allow_high_parameter_budget: bool = False,
    execution_mode: str = "bar",
    cost_model: CostModelConfig | None = None,
) -> list[ResearchRunResult]:
    if execution_mode not in {"bar", "tick"}:
        raise ValueError(f"Unsupported execution_mode: {execution_mode}")
    grid_metadata = parameter_grid_metadata(
        seed_spec,
        max_trials=max_trials,
        max_parameter_combinations=max_parameter_combinations,
    )
    variants = expand_strategy_variants(
        seed_spec,
        max_trials=max_trials,
        max_parameter_combinations=max_parameter_combinations,
        allow_high_parameter_budget=allow_high_parameter_budget,
    )
    results = []
    for index, variant in enumerate(variants[:max_trials]):
        variant_experiment_id = f"{experiment_id}_trial_{index:04d}"
        results.append(
            run_research_bar_validation(
                spec=variant,
                symbol_config=symbol_config,
                data_root=data_root,
                experiment_id=variant_experiment_id,
                date_from=date_from,
                date_to=date_to,
                starting_equity=starting_equity,
                train_days=train_days,
                validation_days=validation_days,
                test_days=test_days,
                step_days=step_days,
                embargo_days=embargo_days,
                final_holdout_days=final_holdout_days,
                min_folds=min_folds,
                grid_metadata=grid_metadata,
                execution_mode=execution_mode,
                cost_model=cost_model,
            )
        )
    return results


def _run_range(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    data_root: Path,
    start: date,
    end: date,
    starting_equity: float,
    execution_mode: str,
    cost_model: CostModelConfig | None,
) -> BacktestResult:
    if execution_mode == "bar":
        files = [bar_path(data_root, spec.symbol, spec.timeframe, day) for day in iter_dates(start, end)]
        return run_bar_backtest(
            spec,
            symbol_config,
            files,
            starting_equity=starting_equity,
            cost_model=cost_model,
        )
    if execution_mode == "tick":
        files = [normalized_tick_path(data_root, spec.symbol, day) for day in iter_dates(start, end)]
        return run_tick_backtest(
            spec,
            symbol_config,
            files,
            starting_equity=starting_equity,
            cost_model=cost_model,
        )
    raise ValueError(f"Unsupported execution_mode: {execution_mode}")


def _range_data_version_hash(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    data_root: Path,
    start: date,
    end: date,
    execution_mode: str,
    cost_model: CostModelConfig,
) -> str:
    files = _data_files_for_range(spec, data_root, start, end, execution_mode)
    return backtest_data_version_hash(files, spec, symbol_config, cost_model, execution_mode)


def _data_files_for_range(
    spec: StrategySpec,
    data_root: Path,
    start: date,
    end: date,
    execution_mode: str,
) -> list[Path]:
    if execution_mode == "bar":
        return [bar_path(data_root, spec.symbol, spec.timeframe, day) for day in iter_dates(start, end)]
    if execution_mode == "tick":
        return [normalized_tick_path(data_root, spec.symbol, day) for day in iter_dates(start, end)]
    raise ValueError(f"Unsupported execution_mode: {execution_mode}")


def write_research_result(path: Path, result: ResearchRunResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_leaderboard(experiments_root: Path) -> list[dict]:
    rows = []
    for result_path in sorted(experiments_root.glob("*/leaderboard.json")):
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        rows.append(
            {
                "experiment_id": payload["experiment_id"],
                "execution_mode": payload.get("execution_mode", "bar"),
                "data_version_hash": payload.get("data_version_hash"),
                "cost_model": payload.get("cost_model", {}),
                "strategy_name": payload["strategy_name"],
                "strategy_spec_hash": payload.get("strategy_spec_hash"),
                "variant_parameters": payload.get("variant_parameters", {}),
                "trial_count": payload.get("trial_count", 1),
                "parameter_combination_count": payload.get("parameter_combination_count", 1),
                "parameter_budget_exceeded": payload.get("parameter_budget_exceeded", False),
                "parameter_grid_hash": payload.get("parameter_grid_hash"),
                "passed": payload["gates"]["passed"],
                "robustness_score": payload["robustness_score"],
                "net_pnl_test": payload["aggregate_test_metrics"]["net_pnl"],
                "sharpe_test": payload["aggregate_test_metrics"]["sharpe"],
                "annual_trades_test": payload["aggregate_test_metrics"]["annual_trades"],
                "net_pnl_holdout": payload["final_holdout_metrics"]["net_pnl"],
                "reasons": payload["gates"]["reasons"],
            }
        )
    return sorted(
        rows,
        key=lambda item: (
            not item["passed"],
            -(item["robustness_score"] or -1),
            -item["net_pnl_test"],
        ),
    )
