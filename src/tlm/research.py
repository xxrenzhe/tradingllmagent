from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from typing import Sequence

import duckdb

from .backtest import (
    BacktestResult,
    Trade,
    backtest_data_version_hash,
    default_cost_model,
    run_bar_backtest,
    run_tick_backtest,
)
from .cli_dates import iter_dates
from .config import CostModelConfig, SymbolConfig
from .leaderboard import calculate_sharpe_decay, evaluate_hard_gates, robustness_score
from .metrics import BacktestMetrics, calculate_metrics
from .snapshot import research_snapshot
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
from .validation import (
    ValidationPlan,
    generate_rolling_folds,
    has_overlapping_test_folds,
    non_overlapping_test_fold_indexes,
)


@dataclass(frozen=True)
class ResearchSplitArtifact:
    split: str
    fold_index: int | None
    start: date
    end: date
    data_version_hash: str
    metrics: BacktestMetrics
    trades: list[Trade]
    starting_equity: float


@dataclass(frozen=True)
class ResearchRunResult:
    experiment_id: str
    execution_mode: str
    data_version_hash: str
    snapshot: dict
    cost_model: dict
    strategy_name: str
    strategy_spec_hash: str
    strategy_spec: dict
    prompt_hash: str
    variant_parameters: dict
    parameter_grid: dict
    trial_count: int
    parameter_combination_count: int
    parameter_budget_exceeded: bool
    parameter_grid_hash: str
    validation_plan: ValidationPlan
    fold_results: list[dict]
    yearly_results: list[dict]
    positive_year_ratio: float
    round_trip_cost: float
    aggregate_validation_metrics: BacktestMetrics
    aggregate_test_metrics: BacktestMetrics
    overlapping_test_folds: bool
    non_overlap_test_fold_indexes: list[int]
    non_overlap_test_metrics: BacktestMetrics
    validation_to_test_sharpe_decay: float | None
    test_to_holdout_sharpe_decay: float | None
    overfitting_report: dict
    cost_sensitivity_report: dict
    parameter_stability_report: dict
    final_holdout_data_version_hash: str
    final_holdout_metrics: BacktestMetrics
    gates: dict
    robustness_score: float | None
    split_artifacts: list[ResearchSplitArtifact]
    promotion_report: dict
    strategy_card: dict
    next_round_suggestions: list[str]
    final_holdout_policy: dict

    def to_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "execution_mode": self.execution_mode,
            "data_version_hash": self.data_version_hash,
            "snapshot": self.snapshot,
            "cost_model": self.cost_model,
            "strategy_name": self.strategy_name,
            "strategy_spec_hash": self.strategy_spec_hash,
            "strategy_spec": self.strategy_spec,
            "prompt_hash": self.prompt_hash,
            "variant_parameters": self.variant_parameters,
            "parameter_grid": self.parameter_grid,
            "trial_count": self.trial_count,
            "parameter_combination_count": self.parameter_combination_count,
            "parameter_budget_exceeded": self.parameter_budget_exceeded,
            "parameter_grid_hash": self.parameter_grid_hash,
            "validation_plan": self.validation_plan.to_dict(),
            "fold_results": self.fold_results,
            "yearly_results": self.yearly_results,
            "positive_year_ratio": self.positive_year_ratio,
            "round_trip_cost": self.round_trip_cost,
            "aggregate_validation_metrics": self.aggregate_validation_metrics.to_dict(),
            "aggregate_test_metrics": self.aggregate_test_metrics.to_dict(),
            "overlapping_test_folds": self.overlapping_test_folds,
            "non_overlap_test_fold_indexes": self.non_overlap_test_fold_indexes,
            "non_overlap_test_metrics": self.non_overlap_test_metrics.to_dict(),
            "validation_to_test_sharpe_decay": self.validation_to_test_sharpe_decay,
            "test_to_holdout_sharpe_decay": self.test_to_holdout_sharpe_decay,
            "overfitting_report": self.overfitting_report,
            "cost_sensitivity_report": self.cost_sensitivity_report,
            "parameter_stability_report": self.parameter_stability_report,
            "final_holdout_data_version_hash": self.final_holdout_data_version_hash,
            "final_holdout_metrics": self.final_holdout_metrics.to_dict(),
            "gates": self.gates,
            "robustness_score": self.robustness_score,
            "promotion_report": self.promotion_report,
            "strategy_card": self.strategy_card,
            "next_round_suggestions": self.next_round_suggestions,
            "final_holdout_policy": self.final_holdout_policy,
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
    indicator_warmup_days: int | None = None,
    grid_metadata: ParameterGridMetadata | None = None,
    execution_mode: str = "bar",
    cost_model: CostModelConfig | None = None,
    config_dir: Path = Path("configs"),
    random_seed: int = 0,
    llm_model: str = "local-deterministic-template",
    llm_parameters: dict | None = None,
) -> ResearchRunResult:
    grid_metadata = grid_metadata or parameter_grid_metadata(spec, max_trials=1)
    if execution_mode not in {"bar", "tick"}:
        raise ValueError(f"Unsupported execution_mode: {execution_mode}")
    active_cost_model = cost_model or default_cost_model(symbol_config, spec.cost_model)
    warmup_days = (
        estimate_indicator_warmup_days(spec)
        if indicator_warmup_days is None
        else indicator_warmup_days
    )
    data_version_hash = _range_data_version_hash(
        spec,
        symbol_config,
        data_root,
        max(date_from - timedelta(days=warmup_days), date_from),
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
        indicator_warmup_days=warmup_days,
    )
    current_strategy_spec_hash = strategy_spec_hash(spec)
    current_prompt_hash = prompt_hash(spec)
    snapshot = research_snapshot(
        plan=plan,
        cost_model=active_cost_model,
        config_dir=config_dir,
        random_seed=random_seed,
        experiment_id=experiment_id,
        strategy_spec_hash=current_strategy_spec_hash,
        prompt_hash=current_prompt_hash,
        data_version_hash=data_version_hash,
        llm_model=llm_model,
        llm_parameters=llm_parameters,
    )
    fold_results: list[dict] = []
    fold_test_metrics: list[BacktestMetrics] = []
    all_validation_pnls: list[float] = []
    all_validation_equity = [starting_equity]
    all_test_pnls: list[float] = []
    all_test_equity = [starting_equity]
    all_test_trades: list[Trade] = []
    test_trades_by_fold: dict[int, list[Trade]] = {}
    split_artifacts: list[ResearchSplitArtifact] = []

    for fold in plan.folds:
        train = _run_range(
            spec,
            symbol_config,
            data_root,
            fold.train.warmup_start or fold.train.start,
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
            fold.validation.warmup_start or fold.validation.start,
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
            fold.test.warmup_start or fold.test.start,
            fold.test.start,
            fold.test.end,
            starting_equity,
            execution_mode,
            active_cost_model,
        )
        fold_test_metrics.append(test.metrics)
        test_trades_by_fold[fold.index] = test.trades
        for trade in validation.trades:
            all_validation_pnls.append(trade.net_pnl)
            all_validation_equity.append(all_validation_equity[-1] + trade.net_pnl)
        for trade in test.trades:
            all_test_trades.append(trade)
            all_test_pnls.append(trade.net_pnl)
            all_test_equity.append(all_test_equity[-1] + trade.net_pnl)
        split_artifacts.extend(
            [
                ResearchSplitArtifact(
                    split="train",
                    fold_index=fold.index,
                    start=fold.train.start,
                    end=fold.train.end,
                    data_version_hash=train.data_version_hash,
                    metrics=train.metrics,
                    trades=train.trades,
                    starting_equity=starting_equity,
                ),
                ResearchSplitArtifact(
                    split="validation",
                    fold_index=fold.index,
                    start=fold.validation.start,
                    end=fold.validation.end,
                    data_version_hash=validation.data_version_hash,
                    metrics=validation.metrics,
                    trades=validation.trades,
                    starting_equity=starting_equity,
                ),
                ResearchSplitArtifact(
                    split="test",
                    fold_index=fold.index,
                    start=fold.test.start,
                    end=fold.test.end,
                    data_version_hash=test.data_version_hash,
                    metrics=test.metrics,
                    trades=test.trades,
                    starting_equity=starting_equity,
                ),
            ]
        )
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

    aggregate_validation_days = sum(
        (fold.validation.end - fold.validation.start).days + 1
        for fold in plan.folds
    )
    aggregate_days = sum(
        (fold.test.end - fold.test.start).days + 1
        for fold in plan.folds
    )
    aggregate_validation_metrics = calculate_metrics(
        all_validation_pnls,
        all_validation_equity,
        starting_equity,
        aggregate_validation_days,
    )
    aggregate_test_metrics = calculate_metrics(
        all_test_pnls,
        all_test_equity,
        starting_equity,
        aggregate_days,
    )
    non_overlap_indexes = non_overlapping_test_fold_indexes(plan.folds)
    non_overlap_days = sum(
        (fold.test.end - fold.test.start).days + 1
        for fold in plan.folds
        if fold.index in non_overlap_indexes
    )
    non_overlap_test_metrics = calculate_trade_metrics(
        [
            trade
            for index in non_overlap_indexes
            for trade in test_trades_by_fold.get(index, [])
        ],
        starting_equity,
        non_overlap_days,
    )
    yearly_results = summarize_yearly_trades(all_test_trades)
    positive_year_ratio = calculate_positive_year_ratio(yearly_results)
    holdout = _run_range(
        spec,
        symbol_config,
        data_root,
        plan.final_holdout.warmup_start or plan.final_holdout.start,
        plan.final_holdout.start,
        plan.final_holdout.end,
        starting_equity,
        execution_mode,
        active_cost_model,
    )
    split_artifacts.append(
        ResearchSplitArtifact(
            split="final_holdout",
            fold_index=None,
            start=plan.final_holdout.start,
            end=plan.final_holdout.end,
            data_version_hash=holdout.data_version_hash,
            metrics=holdout.metrics,
            trades=holdout.trades,
            starting_equity=starting_equity,
        )
    )
    round_trip_cost = calculate_round_trip_cost(active_cost_model)
    validation_to_test_decay = calculate_sharpe_decay(
        aggregate_validation_metrics.sharpe,
        aggregate_test_metrics.sharpe,
    )
    test_to_holdout_decay = calculate_sharpe_decay(
        aggregate_test_metrics.sharpe,
        holdout.metrics.sharpe,
    )
    holdout_days = (plan.final_holdout.end - plan.final_holdout.start).days + 1
    gates = evaluate_hard_gates(
        aggregate_test_metrics,
        fold_test_metrics,
        holdout.metrics,
        validation_metrics=aggregate_validation_metrics,
        positive_year_ratio=positive_year_ratio,
        round_trip_cost=round_trip_cost,
    )
    overfitting_report = build_overfitting_report(
        grid_metadata=grid_metadata,
        fold_test_metrics=fold_test_metrics,
        aggregate_validation_metrics=aggregate_validation_metrics,
        aggregate_test_metrics=aggregate_test_metrics,
        final_holdout_metrics=holdout.metrics,
        positive_year_ratio=positive_year_ratio,
        validation_to_test_sharpe_decay=validation_to_test_decay,
        test_to_holdout_sharpe_decay=test_to_holdout_decay,
    )
    cost_sensitivity_report = build_cost_sensitivity_report(
        test_trades=all_test_trades,
        holdout_trades=holdout.trades,
        cost_model=active_cost_model,
        starting_equity=starting_equity,
        test_days=aggregate_days,
        holdout_days=holdout_days,
    )
    score = robustness_score(
        aggregate_test_metrics,
        holdout.metrics,
        fold_test_metrics,
        validation_metrics=aggregate_validation_metrics,
        parameter_budget_exceeded=grid_metadata.budget_exceeded,
        parameter_combination_count=grid_metadata.total_combinations,
        default_parameter_budget=grid_metadata.default_budget,
        positive_year_ratio=positive_year_ratio,
        round_trip_cost=round_trip_cost,
    )
    gates_payload = gates.to_dict()
    final_holdout_policy = build_final_holdout_policy(plan)
    promotion_report = build_direct_promotion_report(execution_mode)
    next_round_suggestions = build_next_round_suggestions(
        gates_payload["reasons"],
        overfitting_report,
        cost_sensitivity_report,
        grid_metadata,
    )
    strategy_card = build_strategy_card(
        result_status="qualified" if gates_payload["passed"] else "rejected",
        spec=spec,
        execution_mode=execution_mode,
        aggregate_validation_metrics=aggregate_validation_metrics,
        aggregate_test_metrics=aggregate_test_metrics,
        final_holdout_metrics=holdout.metrics,
        gates=gates_payload,
        robustness_score=score,
        promotion_report=promotion_report,
        next_round_suggestions=next_round_suggestions,
        final_holdout_policy=final_holdout_policy,
    )
    return ResearchRunResult(
        experiment_id=experiment_id,
        execution_mode=execution_mode,
        data_version_hash=data_version_hash,
        snapshot=snapshot,
        cost_model=active_cost_model.to_dict(),
        strategy_name=spec.name,
        strategy_spec_hash=current_strategy_spec_hash,
        strategy_spec=spec.raw,
        prompt_hash=current_prompt_hash,
        variant_parameters=spec.raw.get("variant_parameters", {}),
        parameter_grid=grid_metadata.to_dict(),
        trial_count=grid_metadata.selected_combinations,
        parameter_combination_count=grid_metadata.total_combinations,
        parameter_budget_exceeded=grid_metadata.budget_exceeded,
        parameter_grid_hash=grid_metadata.parameter_grid_hash,
        validation_plan=plan,
        fold_results=fold_results,
        yearly_results=yearly_results,
        positive_year_ratio=positive_year_ratio,
        round_trip_cost=round_trip_cost,
        aggregate_validation_metrics=aggregate_validation_metrics,
        aggregate_test_metrics=aggregate_test_metrics,
        overlapping_test_folds=has_overlapping_test_folds(plan.folds),
        non_overlap_test_fold_indexes=non_overlap_indexes,
        non_overlap_test_metrics=non_overlap_test_metrics,
        validation_to_test_sharpe_decay=validation_to_test_decay,
        test_to_holdout_sharpe_decay=test_to_holdout_decay,
        overfitting_report=overfitting_report,
        cost_sensitivity_report=cost_sensitivity_report,
        parameter_stability_report=single_trial_parameter_stability_report(grid_metadata),
        final_holdout_data_version_hash=holdout.data_version_hash,
        final_holdout_metrics=holdout.metrics,
        gates=gates_payload,
        robustness_score=score,
        split_artifacts=split_artifacts,
        promotion_report=promotion_report,
        strategy_card=strategy_card,
        next_round_suggestions=next_round_suggestions,
        final_holdout_policy=final_holdout_policy,
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
    indicator_warmup_days: int | None = None,
    max_parameter_combinations: int = DEFAULT_PARAMETER_BUDGET,
    allow_high_parameter_budget: bool = False,
    execution_mode: str = "bar",
    cost_model: CostModelConfig | None = None,
    config_dir: Path = Path("configs"),
    random_seed: int = 0,
    llm_model: str = "local-deterministic-template",
    llm_parameters: dict | None = None,
) -> list[ResearchRunResult]:
    if execution_mode not in {"bar", "tick", "bar_then_tick"}:
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
            run_staged_research_validation(
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
                indicator_warmup_days=indicator_warmup_days,
                grid_metadata=grid_metadata,
                execution_mode=execution_mode,
                cost_model=cost_model,
                config_dir=config_dir,
                random_seed=random_seed,
                llm_model=llm_model,
                llm_parameters=llm_parameters,
            )
        )
    stability_report = build_parameter_stability_report(results, grid_metadata)
    return [
        replace(result, parameter_stability_report=stability_report)
        for result in results
    ]


def run_staged_research_validation(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    data_root: Path,
    experiment_id: str,
    date_from: date,
    date_to: date,
    starting_equity: float,
    train_days: int,
    validation_days: int,
    test_days: int,
    step_days: int,
    embargo_days: int,
    final_holdout_days: int,
    min_folds: int,
    indicator_warmup_days: int | None,
    grid_metadata: ParameterGridMetadata,
    execution_mode: str,
    cost_model: CostModelConfig | None,
    config_dir: Path,
    random_seed: int,
    llm_model: str,
    llm_parameters: dict | None,
) -> ResearchRunResult:
    if execution_mode in {"bar", "tick"}:
        return run_research_bar_validation(
            spec=spec,
            symbol_config=symbol_config,
            data_root=data_root,
            experiment_id=experiment_id,
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
            indicator_warmup_days=indicator_warmup_days,
            grid_metadata=grid_metadata,
            execution_mode=execution_mode,
            cost_model=cost_model,
            config_dir=config_dir,
            random_seed=random_seed,
            llm_model=llm_model,
            llm_parameters=llm_parameters,
        )
    if execution_mode != "bar_then_tick":
        raise ValueError(f"Unsupported execution_mode: {execution_mode}")

    bar_result = run_research_bar_validation(
        spec=spec,
        symbol_config=symbol_config,
        data_root=data_root,
        experiment_id=experiment_id,
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
        indicator_warmup_days=indicator_warmup_days,
        grid_metadata=grid_metadata,
        execution_mode="bar",
        cost_model=cost_model,
        config_dir=config_dir,
        random_seed=random_seed,
        llm_model=llm_model,
        llm_parameters=llm_parameters,
    )
    promotion_report = build_bar_to_tick_promotion_report(bar_result, tick_result=None)
    if not promotion_report["promoted"]:
        return with_promotion_outputs(bar_result, promotion_report)

    tick_result = run_research_bar_validation(
        spec=spec,
        symbol_config=symbol_config,
        data_root=data_root,
        experiment_id=experiment_id,
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
        indicator_warmup_days=indicator_warmup_days,
        grid_metadata=grid_metadata,
        execution_mode="tick",
        cost_model=cost_model,
        config_dir=config_dir,
        random_seed=random_seed,
        llm_model=llm_model,
        llm_parameters=llm_parameters,
    )
    return with_promotion_outputs(
        tick_result,
        build_bar_to_tick_promotion_report(bar_result, tick_result=tick_result),
    )


def with_promotion_outputs(result: ResearchRunResult, promotion_report: dict) -> ResearchRunResult:
    next_round_suggestions = build_next_round_suggestions(
        result.gates["reasons"],
        result.overfitting_report,
        result.cost_sensitivity_report,
        result.parameter_grid,
        promotion_report=promotion_report,
    )
    current_card = result.strategy_card
    strategy_card = build_strategy_card(
        result_status="qualified" if result.gates["passed"] else "rejected",
        spec_name=current_card.get("name", result.strategy_name),
        strategy_family=current_card.get("strategy_family"),
        symbol=current_card.get("symbol"),
        market_hypothesis=current_card.get("market_hypothesis"),
        execution_mode=result.execution_mode,
        aggregate_validation_metrics=result.aggregate_validation_metrics,
        aggregate_test_metrics=result.aggregate_test_metrics,
        final_holdout_metrics=result.final_holdout_metrics,
        gates=result.gates,
        robustness_score=result.robustness_score,
        promotion_report=promotion_report,
        next_round_suggestions=next_round_suggestions,
        final_holdout_policy=result.final_holdout_policy,
    )
    return replace(
        result,
        promotion_report=promotion_report,
        next_round_suggestions=next_round_suggestions,
        strategy_card=strategy_card,
    )


def build_direct_promotion_report(execution_mode: str) -> dict:
    return {
        "mode": execution_mode,
        "stage": f"direct_{execution_mode}",
        "promoted": execution_mode == "tick",
        "reasons": [],
        "bar_summary": None,
        "tick_summary": None,
        "final_holdout_used_for_promotion": False,
    }


def build_bar_to_tick_promotion_report(
    bar_result: ResearchRunResult,
    tick_result: ResearchRunResult | None,
) -> dict:
    reasons = bar_candidate_rejection_reasons(bar_result)
    promoted = not reasons
    return {
        "mode": "bar_then_tick",
        "stage": "promoted_to_tick" if promoted and tick_result else "bar_rejected_before_tick",
        "promoted": promoted,
        "reasons": reasons,
        "bar_summary": promotion_summary(bar_result),
        "tick_summary": promotion_summary(tick_result) if tick_result else None,
        "final_holdout_used_for_promotion": False,
    }


def bar_candidate_rejection_reasons(result: ResearchRunResult) -> list[str]:
    reasons = []
    if result.aggregate_validation_metrics.trade_count <= 0:
        reasons.append("bar_validation_no_trades")
    if result.aggregate_validation_metrics.net_pnl <= 0:
        reasons.append("bar_validation_net_pnl")
    if result.aggregate_test_metrics.trade_count <= 0:
        reasons.append("bar_test_no_trades")
    if result.aggregate_test_metrics.net_pnl <= 0:
        reasons.append("bar_test_net_pnl")
    if result.parameter_budget_exceeded:
        reasons.append("parameter_budget_exceeded_before_tick")
    return reasons


def promotion_summary(result: ResearchRunResult | None) -> dict | None:
    if result is None:
        return None
    return {
        "experiment_id": result.experiment_id,
        "execution_mode": result.execution_mode,
        "validation": result.aggregate_validation_metrics.to_dict(),
        "test": result.aggregate_test_metrics.to_dict(),
        "gates": result.gates,
    }


def build_final_holdout_policy(plan: ValidationPlan) -> dict:
    return {
        "status": "frozen_once_after_candidate_selection",
        "llm_feedback_includes_final_holdout": False,
        "promotion_uses_final_holdout": False,
        "range": plan.final_holdout.to_dict(),
        "embargo_days": plan.embargo_days,
        "indicator_warmup_days": plan.indicator_warmup_days,
    }


def build_strategy_card(
    result_status: str,
    execution_mode: str,
    aggregate_validation_metrics: BacktestMetrics,
    aggregate_test_metrics: BacktestMetrics,
    final_holdout_metrics: BacktestMetrics,
    gates: dict,
    robustness_score: float | None,
    promotion_report: dict,
    next_round_suggestions: list[str],
    final_holdout_policy: dict,
    spec: StrategySpec | None = None,
    spec_name: str | None = None,
    strategy_family: str | None = None,
    symbol: str | None = None,
    market_hypothesis: str | None = None,
) -> dict:
    return {
        "status": result_status,
        "name": spec.name if spec else spec_name,
        "strategy_family": spec.strategy_family if spec else strategy_family,
        "symbol": spec.symbol if spec else symbol,
        "market_hypothesis": spec.market_hypothesis if spec else market_hypothesis,
        "execution_mode": execution_mode,
        "promotion_stage": promotion_report.get("stage"),
        "passed": gates["passed"],
        "robustness_score": robustness_score,
        "key_metrics": {
            "validation": compact_metrics(aggregate_validation_metrics),
            "test": compact_metrics(aggregate_test_metrics),
            "final_holdout": compact_metrics(final_holdout_metrics),
        },
        "risk_flags": gates["reasons"],
        "next_round_suggestions": next_round_suggestions,
        "final_holdout_policy": final_holdout_policy,
    }


def compact_metrics(metrics: BacktestMetrics) -> dict:
    return {
        "trade_count": metrics.trade_count,
        "net_pnl": metrics.net_pnl,
        "sharpe": metrics.sharpe,
        "max_drawdown": metrics.max_drawdown,
        "annual_trades": metrics.annual_trades,
        "avg_trade_net_pnl": metrics.avg_trade_net_pnl,
    }


def build_next_round_suggestions(
    gate_reasons: Sequence[str],
    overfitting_report: dict,
    cost_sensitivity_report: dict,
    grid_metadata: ParameterGridMetadata | dict,
    promotion_report: dict | None = None,
) -> list[str]:
    suggestions = []
    reason_set = set(gate_reasons)
    promotion_reasons = set((promotion_report or {}).get("reasons", []))
    if promotion_reasons:
        suggestions.append("Do not run tick replay again until bar validation and bar test are both positive.")
    if "annual_trades_test" in reason_set:
        suggestions.append("Increase trade frequency through broader time-of-day coverage without increasing position size.")
    if "sharpe_test" in reason_set or "median_sharpe_test_fold" in reason_set:
        suggestions.append("Tighten regime filters or exit timing; do not widen the parameter grid to chase Sharpe.")
    if "avg_trade_net_pnl" in reason_set:
        suggestions.append("Improve per-trade edge before costs; avoid strategies whose edge is smaller than round-trip cost.")
    if "positive_year_ratio" in reason_set or "positive_test_fold_ratio" in overfitting_report.get("reasons", []):
        suggestions.append("Prefer hypotheses that work across more years and folds rather than one narrow market regime.")
    if "final_holdout_sharpe_decay" in reason_set or "test_to_holdout_sharpe_decay" in reason_set:
        suggestions.append("Treat recent-period degradation as a rejection signal; propose a simpler hypothesis next round.")
    if not cost_sensitivity_report.get("worst_case_survives", True):
        suggestions.append("Reduce dependence on tight fills; require the strategy to survive extra slippage before promotion.")
    budget_exceeded = (
        grid_metadata.budget_exceeded
        if isinstance(grid_metadata, ParameterGridMetadata)
        else grid_metadata.get("budget_exceeded", False)
    )
    if budget_exceeded:
        suggestions.append("Shrink the parameter grid and retest neighboring values before considering the strategy stable.")
    if not suggestions:
        suggestions.append("Freeze the current spec and monitor tick replay versus bar replay drift before expanding scope.")
    return suggestions


def _run_range(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    data_root: Path,
    warmup_start: date,
    start: date,
    end: date,
    starting_equity: float,
    execution_mode: str,
    cost_model: CostModelConfig | None,
) -> BacktestResult:
    if execution_mode == "bar":
        files = [bar_path(data_root, spec.symbol, spec.timeframe, day) for day in iter_dates(warmup_start, end)]
        return trim_backtest_result(
            run_bar_backtest(
                spec,
                symbol_config,
                files,
                starting_equity=starting_equity,
                cost_model=cost_model,
            ),
            start=start,
            end=end,
            starting_equity=starting_equity,
        )
    if execution_mode == "tick":
        files = [normalized_tick_path(data_root, spec.symbol, day) for day in iter_dates(warmup_start, end)]
        return trim_backtest_result(
            run_tick_backtest(
                spec,
                symbol_config,
                files,
                starting_equity=starting_equity,
                cost_model=cost_model,
            ),
            start=start,
            end=end,
            starting_equity=starting_equity,
        )
    raise ValueError(f"Unsupported execution_mode: {execution_mode}")


def trim_backtest_result(
    result: BacktestResult,
    start: date,
    end: date,
    starting_equity: float,
) -> BacktestResult:
    trades = [
        trade
        for trade in result.trades
        if start <= trade.entry_time.date() and trade.exit_time.date() <= end
    ]
    return BacktestResult(
        result.strategy_name,
        result.symbol,
        result.data_version_hash,
        result.cost_model,
        trades,
        calculate_trade_metrics(
            trades,
            starting_equity,
            (end - start).days + 1,
        ),
    )


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
    write_research_artifacts(path.parent, result, leaderboard_path=path)


def write_research_artifacts(
    experiment_dir: Path,
    result: ResearchRunResult,
    leaderboard_path: Path | None = None,
) -> None:
    experiment_dir.mkdir(parents=True, exist_ok=True)
    artifact_paths = {
        "trades": experiment_dir / "trades.parquet",
        "equity": experiment_dir / "equity.parquet",
        "fold_metrics": experiment_dir / "fold_metrics.parquet",
        "strategy_spec": experiment_dir / "strategy_spec.json",
        "run_manifest": experiment_dir / "run_manifest.json",
    }
    row_counts = {
        "trades": _write_research_trades(artifact_paths["trades"], result),
        "equity": _write_research_equity(artifact_paths["equity"], result),
        "fold_metrics": _write_research_fold_metrics(artifact_paths["fold_metrics"], result),
        "strategy_spec": 1,
        "run_manifest": 1,
    }
    manifest = {
        "schema_version": 2,
        "experiment_id": result.experiment_id,
        "execution_mode": result.execution_mode,
        "data_version_hash": result.data_version_hash,
        "strategy_name": result.strategy_name,
        "strategy_spec_hash": result.strategy_spec_hash,
        "prompt_hash": result.prompt_hash,
        "snapshot": result.snapshot,
        "cost_model": result.cost_model,
        "validation_plan": result.validation_plan.to_dict(),
        "reproducibility": {
            "code_version": result.snapshot.get("code_version"),
            "config_snapshot_hash": result.snapshot.get("config_snapshot_hash"),
            "cost_model_hash": result.snapshot.get("cost_model_hash"),
            "fold_definition_hash": result.snapshot.get("fold_definition_hash"),
            "random_seed": result.snapshot.get("random_seed"),
        },
        "data_disclaimer": {
            "data_source": "Dukascopy USATECHIDXUSD CFD proxy",
            "system_symbol": result.strategy_spec.get("symbol"),
            "not_cme_futures": True,
            "note": "NQmain research uses a CFD proxy and must not be interpreted as executable CME NQ futures performance.",
        },
        "leaderboard_path": (
            leaderboard_path.name
            if leaderboard_path and leaderboard_path.parent == experiment_dir
            else str(leaderboard_path) if leaderboard_path else None
        ),
        "artifacts": {
            name: {"path": path.name, "row_count": row_counts[name]}
            for name, path in artifact_paths.items()
        },
    }
    artifact_paths["strategy_spec"].write_text(
        json.dumps(result.strategy_spec, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (experiment_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    artifact_paths["run_manifest"].write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_research_artifacts(
    experiments_root: Path,
    experiment_id: str,
    row_limit: int = 2_000,
) -> dict:
    experiment_dir = experiments_root / experiment_id
    manifest_path = experiment_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Artifact manifest not found for experiment: {experiment_id}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    trades = _read_parquet_dicts(
        experiment_dir / "trades.parquet",
        "ORDER BY exit_time, split, fold_index, trade_index",
        row_limit,
    )
    equity = _read_parquet_dicts(
        experiment_dir / "equity.parquet",
        "ORDER BY split, fold_index, sequence",
        row_limit,
    )
    fold_metrics = _read_parquet_dicts(
        experiment_dir / "fold_metrics.parquet",
        "ORDER BY split, fold_index",
        row_limit,
    )
    return {
        "experiment_id": experiment_id,
        "manifest": manifest,
        "trades": [_json_ready_row(row) for row in trades],
        "equity": [_json_ready_row(row) for row in equity],
        "fold_metrics": [_json_ready_row(row) for row in fold_metrics],
        "distributions": build_trade_distributions(trades),
    }


def build_trade_distributions(trades: Sequence[dict]) -> dict:
    distributions = {
        "by_year": {},
        "by_month": {},
        "by_hour": {},
        "by_direction": {},
        "by_holding_minutes": {},
    }
    for trade in trades:
        exit_time = trade.get("exit_time")
        entry_time = trade.get("entry_time")
        net_pnl = float(trade.get("net_pnl") or 0)
        if exit_time:
            distributions["by_year"] = _add_distribution_row(
                distributions["by_year"],
                f"{exit_time.year}",
                net_pnl,
            )
            distributions["by_month"] = _add_distribution_row(
                distributions["by_month"],
                exit_time.strftime("%Y-%m"),
                net_pnl,
            )
            distributions["by_hour"] = _add_distribution_row(
                distributions["by_hour"],
                f"{exit_time.hour:02d}:00",
                net_pnl,
            )
        distributions["by_direction"] = _add_distribution_row(
            distributions["by_direction"],
            str(trade.get("side") or "unknown"),
            net_pnl,
        )
        if entry_time and exit_time:
            holding_minutes = max((exit_time - entry_time).total_seconds() / 60, 0)
            distributions["by_holding_minutes"] = _add_distribution_row(
                distributions["by_holding_minutes"],
                holding_duration_bucket(holding_minutes),
                net_pnl,
            )
    return {
        name: [
            {"bucket": bucket, "trade_count": row["trade_count"], "net_pnl": row["net_pnl"]}
            for bucket, row in sorted(values.items())
        ]
        for name, values in distributions.items()
    }


def holding_duration_bucket(minutes: float) -> str:
    if minutes < 5:
        return "00-05m"
    if minutes < 15:
        return "05-15m"
    if minutes < 60:
        return "15-60m"
    return "60m+"


def _add_distribution_row(rows: dict, bucket: str, net_pnl: float) -> dict:
    row = rows.setdefault(bucket, {"trade_count": 0, "net_pnl": 0.0})
    row["trade_count"] += 1
    row["net_pnl"] += net_pnl
    return rows


def _read_parquet_dicts(path: Path, order_by: str, row_limit: int) -> list[dict]:
    if not path.exists():
        return []
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            f"SELECT * FROM read_parquet(?) {order_by} LIMIT ?",
            [str(path), max(row_limit, 0)],
        ).fetchall()
        columns = [column[0] for column in con.description]
    finally:
        con.close()
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _json_ready_row(row: dict) -> dict:
    return {key: _json_ready_value(value) for key, value in row.items()}


def _json_ready_value(value: object) -> object:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _write_research_trades(path: Path, result: ResearchRunResult) -> int:
    rows = []
    for artifact in result.split_artifacts:
        for trade_index, trade in enumerate(artifact.trades):
            rows.append(
                (
                    result.experiment_id,
                    result.execution_mode,
                    artifact.split,
                    artifact.fold_index,
                    trade_index,
                    artifact.start,
                    artifact.end,
                    artifact.data_version_hash,
                    trade.symbol,
                    trade.side,
                    trade.entry_time,
                    trade.exit_time,
                    trade.entry_price,
                    trade.exit_price,
                    trade.contracts,
                    trade.gross_pnl,
                    trade.fees,
                    trade.slippage_cost,
                    trade.net_pnl,
                    trade.entry_reason,
                    trade.exit_reason,
                )
            )
    _write_parquet_rows(
        path,
        "research_trades",
        (
            "experiment_id VARCHAR, execution_mode VARCHAR, split VARCHAR, fold_index INTEGER, "
            "trade_index INTEGER, start_date DATE, end_date DATE, data_version_hash VARCHAR, "
            "symbol VARCHAR, side VARCHAR, entry_time TIMESTAMP, exit_time TIMESTAMP, "
            "entry_price DOUBLE, exit_price DOUBLE, contracts INTEGER, gross_pnl DOUBLE, "
            "fees DOUBLE, slippage_cost DOUBLE, net_pnl DOUBLE, entry_reason VARCHAR, "
            "exit_reason VARCHAR"
        ),
        "INSERT INTO research_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _write_research_equity(path: Path, result: ResearchRunResult) -> int:
    rows = []
    for artifact in result.split_artifacts:
        equity = artifact.starting_equity
        rows.append(
            (
                result.experiment_id,
                result.execution_mode,
                artifact.split,
                artifact.fold_index,
                0,
                artifact.start,
                artifact.end,
                artifact.data_version_hash,
                None,
                equity,
                0.0,
            )
        )
        for sequence, trade in enumerate(artifact.trades, start=1):
            equity += trade.net_pnl
            rows.append(
                (
                    result.experiment_id,
                    result.execution_mode,
                    artifact.split,
                    artifact.fold_index,
                    sequence,
                    artifact.start,
                    artifact.end,
                    artifact.data_version_hash,
                    trade.exit_time,
                    equity,
                    trade.net_pnl,
                )
            )
    _write_parquet_rows(
        path,
        "research_equity",
        (
            "experiment_id VARCHAR, execution_mode VARCHAR, split VARCHAR, fold_index INTEGER, "
            "sequence INTEGER, start_date DATE, end_date DATE, data_version_hash VARCHAR, "
            "timestamp TIMESTAMP, equity DOUBLE, net_pnl DOUBLE"
        ),
        "INSERT INTO research_equity VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _write_research_fold_metrics(path: Path, result: ResearchRunResult) -> int:
    rows = []
    for artifact in result.split_artifacts:
        metrics = artifact.metrics
        rows.append(
            (
                result.experiment_id,
                result.execution_mode,
                artifact.split,
                artifact.fold_index,
                artifact.start,
                artifact.end,
                artifact.data_version_hash,
                metrics.trade_count,
                metrics.net_pnl,
                metrics.gross_profit,
                metrics.gross_loss,
                metrics.profit_factor,
                metrics.sharpe,
                metrics.max_drawdown,
                metrics.annual_trades,
                metrics.avg_trade_net_pnl,
            )
        )
    _write_parquet_rows(
        path,
        "research_fold_metrics",
        (
            "experiment_id VARCHAR, execution_mode VARCHAR, split VARCHAR, fold_index INTEGER, "
            "start_date DATE, end_date DATE, data_version_hash VARCHAR, trade_count INTEGER, "
            "net_pnl DOUBLE, gross_profit DOUBLE, gross_loss DOUBLE, profit_factor DOUBLE, "
            "sharpe DOUBLE, max_drawdown DOUBLE, annual_trades DOUBLE, avg_trade_net_pnl DOUBLE"
        ),
        "INSERT INTO research_fold_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _write_parquet_rows(
    path: Path,
    table_name: str,
    schema_sql: str,
    insert_sql: str,
    rows: Sequence[tuple],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"CREATE TABLE {table_name} ({schema_sql})")
        if rows:
            con.executemany(insert_sql, rows)
        con.execute(f"COPY {table_name} TO ? (FORMAT PARQUET)", [str(path)])
    finally:
        con.close()


def load_leaderboard(experiments_root: Path) -> list[dict]:
    rows = []
    for result_path in sorted(experiments_root.glob("*/leaderboard.json")):
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        rows.append(
            {
                "experiment_id": payload["experiment_id"],
                "execution_mode": payload.get("execution_mode", "bar"),
                "data_version_hash": payload.get("data_version_hash"),
                "snapshot": payload.get("snapshot", {}),
                "cost_model": payload.get("cost_model", {}),
                "strategy_name": payload["strategy_name"],
                "strategy_spec_hash": payload.get("strategy_spec_hash"),
                "variant_parameters": payload.get("variant_parameters", {}),
                "trial_count": payload.get("trial_count", 1),
                "parameter_combination_count": payload.get("parameter_combination_count", 1),
                "parameter_budget_exceeded": payload.get("parameter_budget_exceeded", False),
                "parameter_grid_hash": payload.get("parameter_grid_hash"),
                "positive_year_ratio": payload.get("positive_year_ratio"),
                "round_trip_cost": payload.get("round_trip_cost"),
                "yearly_results": payload.get("yearly_results", []),
                "validation_to_test_sharpe_decay": payload.get("validation_to_test_sharpe_decay"),
                "test_to_holdout_sharpe_decay": payload.get("test_to_holdout_sharpe_decay"),
                "overfitting_report": payload.get("overfitting_report", {}),
                "cost_sensitivity_report": payload.get("cost_sensitivity_report", {}),
                "parameter_stability_report": payload.get("parameter_stability_report", {}),
                "promotion_report": payload.get("promotion_report", {}),
                "strategy_card": payload.get("strategy_card", {}),
                "next_round_suggestions": payload.get("next_round_suggestions", []),
                "final_holdout_policy": payload.get("final_holdout_policy", {}),
                "overlapping_test_folds": payload.get("overlapping_test_folds", False),
                "non_overlap_test_fold_indexes": payload.get("non_overlap_test_fold_indexes", []),
                "passed": payload["gates"]["passed"],
                "robustness_score": payload["robustness_score"],
                "net_pnl_validation": payload.get("aggregate_validation_metrics", {}).get("net_pnl"),
                "sharpe_validation": payload.get("aggregate_validation_metrics", {}).get("sharpe"),
                "net_pnl_test": payload["aggregate_test_metrics"]["net_pnl"],
                "sharpe_test": payload["aggregate_test_metrics"]["sharpe"],
                "annual_trades_test": payload["aggregate_test_metrics"]["annual_trades"],
                "net_pnl_non_overlap_test": payload.get("non_overlap_test_metrics", {}).get("net_pnl"),
                "sharpe_non_overlap_test": payload.get("non_overlap_test_metrics", {}).get("sharpe"),
                "annual_trades_non_overlap_test": (
                    payload.get("non_overlap_test_metrics", {}).get("annual_trades")
                ),
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


def load_leaderboard_report(experiments_root: Path) -> dict:
    rows = load_leaderboard(experiments_root)
    leaderboard = sorted(
        [row for row in rows if row["passed"]],
        key=lambda item: (-(item["robustness_score"] or -1), -item["net_pnl_test"]),
    )
    rejected = sorted(
        [row for row in rows if not row["passed"]],
        key=lambda item: (-item["net_pnl_test"], item["experiment_id"]),
    )
    return {
        "leaderboard": leaderboard,
        "rejected": rejected,
        "rows": rows,
        "conclusion": (
            "qualified_strategies_found"
            if leaderboard
            else "no_qualified_strategies_found"
        ),
        "message": (
            f"Found {len(leaderboard)} qualified strategies."
            if leaderboard
            else "No qualified strategies found under the current out-of-sample gates."
        ),
        "summary": {
            "passed": len(leaderboard),
            "rejected": len(rejected),
            "total": len(rows),
        },
    }


def summarize_yearly_trades(trades: Sequence[Trade]) -> list[dict]:
    by_year: dict[int, dict] = {}
    for trade in trades:
        year = trade.exit_time.year
        row = by_year.setdefault(year, {"year": year, "trade_count": 0, "net_pnl": 0.0})
        row["trade_count"] += 1
        row["net_pnl"] += trade.net_pnl
    return [by_year[year] for year in sorted(by_year)]


def calculate_positive_year_ratio(yearly_results: Sequence[dict]) -> float:
    if not yearly_results:
        return 0.0
    positive = sum(1 for row in yearly_results if row["net_pnl"] > 0)
    return positive / len(yearly_results)


def calculate_trade_metrics(
    trades: Sequence[Trade],
    starting_equity: float,
    calendar_days: int,
) -> BacktestMetrics:
    equity = [starting_equity]
    pnls = []
    for trade in trades:
        pnls.append(trade.net_pnl)
        equity.append(equity[-1] + trade.net_pnl)
    return calculate_metrics(pnls, equity, starting_equity, calendar_days)


def calculate_round_trip_cost(cost_model: CostModelConfig) -> float:
    slippage_cost = (
        2
        * cost_model.slippage_ticks_per_side
        * cost_model.tick_size
        * cost_model.point_value
    )
    return cost_model.round_trip_fees_usd + slippage_cost


def estimate_indicator_warmup_days(spec: StrategySpec, bars_per_day: int = 390) -> int:
    max_lookback = 0
    for config in spec.indicators.values():
        if not isinstance(config, dict):
            continue
        for key in ("window", "lookback", "lookback_minutes"):
            value = config.get(key)
            if isinstance(value, int | float):
                max_lookback = max(max_lookback, int(value))
    if max_lookback <= 1:
        return 0
    return max(1, (max_lookback + bars_per_day - 1) // bars_per_day)


def single_trial_parameter_stability_report(grid_metadata: ParameterGridMetadata) -> dict:
    return {
        "status": "insufficient_variants",
        "parameter_names": grid_metadata.parameter_names,
        "evaluated_trials": 1,
        "adjacent_pair_count": 0,
        "stable_adjacent_pair_count": 0,
        "positive_neighbor_ratio": None,
        "narrow_single_point_risk": False,
        "adjacent_pairs": [],
    }


def build_parameter_stability_report(
    results: Sequence[ResearchRunResult],
    grid_metadata: ParameterGridMetadata,
) -> dict:
    parameter_names = grid_metadata.parameter_names
    adjacent_pairs = []
    if len(results) < 2 or not parameter_names:
        return {
            "status": "insufficient_variants",
            "parameter_names": parameter_names,
            "evaluated_trials": len(results),
            "adjacent_pair_count": 0,
            "stable_adjacent_pair_count": 0,
            "positive_neighbor_ratio": None,
            "narrow_single_point_risk": False,
            "adjacent_pairs": [],
        }

    for parameter_name in parameter_names:
        groups: dict[tuple[tuple[str, str], ...], list[ResearchRunResult]] = {}
        for result in results:
            if parameter_name not in result.variant_parameters:
                continue
            group_key = tuple(
                sorted(
                    (name, stable_json_value(value))
                    for name, value in result.variant_parameters.items()
                    if name != parameter_name
                )
            )
            groups.setdefault(group_key, []).append(result)
        for group in groups.values():
            ordered = sorted(
                group,
                key=lambda item: sortable_parameter_value(item.variant_parameters[parameter_name]),
            )
            for left, right in zip(ordered, ordered[1:], strict=False):
                left_sharpe = left.aggregate_test_metrics.sharpe
                right_sharpe = right.aggregate_test_metrics.sharpe
                stable = is_stable_adjacent_pair(left, right)
                adjacent_pairs.append(
                    {
                        "parameter": parameter_name,
                        "left_trial": left.experiment_id,
                        "right_trial": right.experiment_id,
                        "left_value": left.variant_parameters.get(parameter_name),
                        "right_value": right.variant_parameters.get(parameter_name),
                        "left_test_sharpe": left_sharpe,
                        "right_test_sharpe": right_sharpe,
                        "left_test_net_pnl": left.aggregate_test_metrics.net_pnl,
                        "right_test_net_pnl": right.aggregate_test_metrics.net_pnl,
                        "sharpe_degradation": adjacent_sharpe_degradation(left_sharpe, right_sharpe),
                        "stable": stable,
                    }
                )

    stable_count = sum(1 for pair in adjacent_pairs if pair["stable"])
    ratio = stable_count / len(adjacent_pairs) if adjacent_pairs else None
    narrow_single_point_risk = bool(adjacent_pairs and ratio is not None and ratio < 0.5)
    if not adjacent_pairs:
        status = "insufficient_adjacent_pairs"
    elif narrow_single_point_risk:
        status = "fragile"
    else:
        status = "stable"
    return {
        "status": status,
        "parameter_names": parameter_names,
        "evaluated_trials": len(results),
        "adjacent_pair_count": len(adjacent_pairs),
        "stable_adjacent_pair_count": stable_count,
        "positive_neighbor_ratio": ratio,
        "narrow_single_point_risk": narrow_single_point_risk,
        "adjacent_pairs": adjacent_pairs,
    }


def is_stable_adjacent_pair(left: ResearchRunResult, right: ResearchRunResult) -> bool:
    left_sharpe = left.aggregate_test_metrics.sharpe
    right_sharpe = right.aggregate_test_metrics.sharpe
    if left.aggregate_test_metrics.net_pnl <= 0 or right.aggregate_test_metrics.net_pnl <= 0:
        return False
    if left_sharpe is None or right_sharpe is None:
        return False
    stronger = max(left_sharpe, right_sharpe)
    weaker = min(left_sharpe, right_sharpe)
    if stronger <= 0:
        return False
    return weaker >= 0.5 * stronger


def adjacent_sharpe_degradation(left_sharpe: float | None, right_sharpe: float | None) -> float | None:
    if left_sharpe is None or right_sharpe is None:
        return None
    stronger = max(left_sharpe, right_sharpe)
    weaker = min(left_sharpe, right_sharpe)
    if stronger <= 0:
        return None
    return 1 - weaker / stronger


def sortable_parameter_value(value: object) -> tuple[int, object]:
    if isinstance(value, int | float):
        return (0, float(value))
    return (1, str(value))


def stable_json_value(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def build_overfitting_report(
    grid_metadata: ParameterGridMetadata,
    fold_test_metrics: Sequence[BacktestMetrics],
    aggregate_validation_metrics: BacktestMetrics,
    aggregate_test_metrics: BacktestMetrics,
    final_holdout_metrics: BacktestMetrics,
    positive_year_ratio: float,
    validation_to_test_sharpe_decay: float | None,
    test_to_holdout_sharpe_decay: float | None,
    max_sharpe_decay: float = 0.5,
) -> dict:
    fold_sharpes = [metric.sharpe for metric in fold_test_metrics if metric.sharpe is not None]
    positive_test_fold_ratio = (
        sum(1 for metric in fold_test_metrics if metric.net_pnl > 0) / len(fold_test_metrics)
        if fold_test_metrics
        else 0.0
    )
    reasons = []
    if aggregate_validation_metrics.sharpe is None or aggregate_validation_metrics.sharpe <= 0:
        reasons.append("validation_sharpe_non_positive")
    if validation_to_test_sharpe_decay is not None and validation_to_test_sharpe_decay > max_sharpe_decay:
        reasons.append("validation_to_test_sharpe_decay")
    if test_to_holdout_sharpe_decay is not None and test_to_holdout_sharpe_decay > max_sharpe_decay:
        reasons.append("test_to_holdout_sharpe_decay")
    if aggregate_test_metrics.sharpe is not None and final_holdout_metrics.sharpe is not None:
        if final_holdout_metrics.sharpe < 0.7 * aggregate_test_metrics.sharpe:
            reasons.append("final_holdout_sharpe_decay")
    if positive_test_fold_ratio < 0.6:
        reasons.append("positive_test_fold_ratio")
    if positive_year_ratio < 0.6:
        reasons.append("positive_year_ratio")
    if grid_metadata.budget_exceeded:
        reasons.append("parameter_budget_exceeded")
    if grid_metadata.high_risk_budget:
        reasons.append("high_risk_parameter_budget")

    if grid_metadata.high_risk_budget or "validation_sharpe_non_positive" in reasons:
        risk_level = "high"
    elif reasons:
        risk_level = "medium"
    else:
        risk_level = "low"

    return {
        "risk_level": risk_level,
        "reasons": reasons,
        "trial_count": grid_metadata.selected_combinations,
        "parameter_combination_count": grid_metadata.total_combinations,
        "parameter_budget_exceeded": grid_metadata.budget_exceeded,
        "high_risk_parameter_budget": grid_metadata.high_risk_budget,
        "default_parameter_budget": grid_metadata.default_budget,
        "fold_count": len(fold_test_metrics),
        "positive_test_fold_ratio": positive_test_fold_ratio,
        "median_test_fold_sharpe": median(fold_sharpes) if fold_sharpes else None,
        "validation_sharpe": aggregate_validation_metrics.sharpe,
        "test_sharpe": aggregate_test_metrics.sharpe,
        "final_holdout_sharpe": final_holdout_metrics.sharpe,
        "validation_to_test_sharpe_decay": validation_to_test_sharpe_decay,
        "test_to_holdout_sharpe_decay": test_to_holdout_sharpe_decay,
        "pbo_status": "not_computed_v1",
        "pbo_note": (
            "V1 records trial count, fold count, and out-of-sample performance; "
            "full Probability of Backtest Overfitting estimation is deferred."
        ),
    }


def build_cost_sensitivity_report(
    test_trades: Sequence[Trade],
    holdout_trades: Sequence[Trade],
    cost_model: CostModelConfig,
    starting_equity: float,
    test_days: int,
    holdout_days: int,
) -> dict:
    extra_slippage_round_trip = 2 * cost_model.tick_size * cost_model.point_value
    baseline_round_trip_cost = calculate_round_trip_cost(cost_model)
    scenarios = [
        {
            "name": "baseline",
            "extra_round_trip_cost": 0.0,
        },
        {
            "name": "plus_1_tick_slippage_per_side",
            "extra_round_trip_cost": extra_slippage_round_trip,
        },
        {
            "name": "double_fees",
            "extra_round_trip_cost": cost_model.round_trip_fees_usd,
        },
        {
            "name": "plus_1_tick_slippage_per_side_and_double_fees",
            "extra_round_trip_cost": extra_slippage_round_trip + cost_model.round_trip_fees_usd,
        },
    ]
    scenario_reports = []
    for scenario in scenarios:
        extra_cost = float(scenario["extra_round_trip_cost"])
        test_metrics = calculate_stressed_trade_metrics(
            test_trades,
            extra_cost,
            starting_equity,
            test_days,
        )
        holdout_metrics = calculate_stressed_trade_metrics(
            holdout_trades,
            extra_cost,
            starting_equity,
            holdout_days,
        )
        scenario_reports.append(
            {
                **scenario,
                "total_round_trip_cost": baseline_round_trip_cost + extra_cost,
                "test_metrics": test_metrics.to_dict(),
                "final_holdout_metrics": holdout_metrics.to_dict(),
                "survives": test_metrics.net_pnl > 0 and holdout_metrics.net_pnl > 0,
            }
        )

    return {
        "baseline_round_trip_cost": baseline_round_trip_cost,
        "stress_scenarios": scenario_reports,
        "worst_case_survives": scenario_reports[-1]["survives"],
        "max_extra_round_trip_cost_before_test_pnl_zero": max_extra_cost_before_pnl_zero(test_trades),
        "method": "deterministic_trade_pnl_adjustment",
    }


def calculate_stressed_trade_metrics(
    trades: Sequence[Trade],
    extra_round_trip_cost: float,
    starting_equity: float,
    calendar_days: int,
) -> BacktestMetrics:
    equity = [starting_equity]
    pnls = []
    for trade in trades:
        pnl = trade.net_pnl - extra_round_trip_cost * trade.contracts
        pnls.append(pnl)
        equity.append(equity[-1] + pnl)
    return calculate_metrics(pnls, equity, starting_equity, calendar_days)


def max_extra_cost_before_pnl_zero(trades: Sequence[Trade]) -> float | None:
    total_contract_trips = sum(trade.contracts for trade in trades)
    if total_contract_trips <= 0:
        return None
    return sum(trade.net_pnl for trade in trades) / total_contract_trips
