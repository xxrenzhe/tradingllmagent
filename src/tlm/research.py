from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

from .backtest import BacktestResult, run_bar_backtest
from .cli_dates import iter_dates
from .config import SymbolConfig
from .leaderboard import evaluate_hard_gates, robustness_score
from .metrics import BacktestMetrics, calculate_metrics
from .storage import bar_path
from .strategy import StrategySpec
from .variants import expand_strategy_variants, prompt_hash, strategy_spec_hash
from .validation import ValidationPlan, generate_rolling_folds


@dataclass(frozen=True)
class ResearchRunResult:
    experiment_id: str
    strategy_name: str
    strategy_spec_hash: str
    prompt_hash: str
    variant_parameters: dict
    validation_plan: ValidationPlan
    fold_results: list[dict]
    aggregate_test_metrics: BacktestMetrics
    final_holdout_metrics: BacktestMetrics
    gates: dict
    robustness_score: float | None

    def to_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "strategy_name": self.strategy_name,
            "strategy_spec_hash": self.strategy_spec_hash,
            "prompt_hash": self.prompt_hash,
            "variant_parameters": self.variant_parameters,
            "validation_plan": self.validation_plan.to_dict(),
            "fold_results": self.fold_results,
            "aggregate_test_metrics": self.aggregate_test_metrics.to_dict(),
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
) -> ResearchRunResult:
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
        train = _run_range(spec, symbol_config, data_root, fold.train.start, fold.train.end, starting_equity)
        validation = _run_range(
            spec, symbol_config, data_root, fold.validation.start, fold.validation.end, starting_equity
        )
        test = _run_range(spec, symbol_config, data_root, fold.test.start, fold.test.end, starting_equity)
        fold_test_metrics.append(test.metrics)
        for trade in test.trades:
            all_test_pnls.append(trade.net_pnl)
            all_test_equity.append(all_test_equity[-1] + trade.net_pnl)
        fold_results.append(
            {
                "fold": fold.to_dict(),
                "train_metrics": train.metrics.to_dict(),
                "validation_metrics": validation.metrics.to_dict(),
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
    )
    gates = evaluate_hard_gates(aggregate_test_metrics, fold_test_metrics, holdout.metrics)
    score = robustness_score(aggregate_test_metrics, holdout.metrics, fold_test_metrics)
    return ResearchRunResult(
        experiment_id=experiment_id,
        strategy_name=spec.name,
        strategy_spec_hash=strategy_spec_hash(spec),
        prompt_hash=prompt_hash(spec),
        variant_parameters=spec.raw.get("variant_parameters", {}),
        validation_plan=plan,
        fold_results=fold_results,
        aggregate_test_metrics=aggregate_test_metrics,
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
    max_parameter_combinations: int = 500,
) -> list[ResearchRunResult]:
    variants = expand_strategy_variants(
        seed_spec,
        max_trials=max_trials,
        max_parameter_combinations=max_parameter_combinations,
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
) -> BacktestResult:
    files = [bar_path(data_root, spec.symbol, spec.timeframe, day) for day in iter_dates(start, end)]
    return run_bar_backtest(spec, symbol_config, files, starting_equity=starting_equity)


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
                "strategy_name": payload["strategy_name"],
                "strategy_spec_hash": payload.get("strategy_spec_hash"),
                "variant_parameters": payload.get("variant_parameters", {}),
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
