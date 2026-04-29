from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Sequence

from .metrics import BacktestMetrics
from .variants import DEFAULT_PARAMETER_BUDGET


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reasons: list[str]

    def to_dict(self) -> dict:
        return {"passed": self.passed, "reasons": self.reasons}


def evaluate_hard_gates(
    test_metrics: BacktestMetrics,
    fold_test_metrics: Sequence[BacktestMetrics],
    holdout_metrics: BacktestMetrics,
    validation_metrics: BacktestMetrics | None = None,
    max_drawdown_limit: float = 10_000,
    min_annual_trades: float = 1000,
    min_sharpe: float = 2,
    min_median_fold_sharpe: float = 1.5,
    min_positive_year_ratio: float = 0.6,
    positive_year_ratio: float | None = None,
    round_trip_cost: float = 0,
    min_avg_trade_cost_multiple: float = 1.5,
    max_sharpe_decay: float = 0.5,
) -> GateResult:
    reasons: list[str] = []
    if test_metrics.annual_trades <= min_annual_trades:
        reasons.append("annual_trades_test")
    if test_metrics.sharpe is None or test_metrics.sharpe <= min_sharpe:
        reasons.append("sharpe_test_aggregate")
    fold_sharpes = [metric.sharpe for metric in fold_test_metrics if metric.sharpe is not None]
    if not fold_sharpes or median(fold_sharpes) <= min_median_fold_sharpe:
        reasons.append("median_sharpe_test_fold")
    if test_metrics.net_pnl <= 0:
        reasons.append("net_pnl_test")
    if holdout_metrics.net_pnl <= 0:
        reasons.append("net_pnl_final_holdout")
    if test_metrics.max_drawdown > max_drawdown_limit:
        reasons.append("max_drawdown_test")
    if test_metrics.profit_factor is None or test_metrics.profit_factor <= 1.1:
        reasons.append("profit_factor_test")
    min_avg_trade_net_pnl = round_trip_cost * min_avg_trade_cost_multiple
    if test_metrics.avg_trade_net_pnl is None or test_metrics.avg_trade_net_pnl <= min_avg_trade_net_pnl:
        reasons.append("avg_trade_net_pnl")
    positive_folds = sum(1 for metric in fold_test_metrics if metric.net_pnl > 0)
    if fold_test_metrics and positive_folds / len(fold_test_metrics) < 0.6:
        reasons.append("positive_test_fold_ratio")
    if positive_year_ratio is not None and positive_year_ratio < min_positive_year_ratio:
        reasons.append("positive_year_ratio")
    if validation_metrics is not None:
        validation_to_test_decay = calculate_sharpe_decay(
            validation_metrics.sharpe,
            test_metrics.sharpe,
        )
        if validation_metrics.sharpe is None or validation_metrics.sharpe <= 0:
            reasons.append("validation_sharpe")
        elif validation_to_test_decay is None or validation_to_test_decay > max_sharpe_decay:
            reasons.append("validation_to_test_sharpe_decay")
    if (
        test_metrics.sharpe is not None
        and holdout_metrics.sharpe is not None
        and holdout_metrics.sharpe < 0.7 * test_metrics.sharpe
    ):
        reasons.append("final_holdout_sharpe_decay")
    test_to_holdout_decay = calculate_sharpe_decay(test_metrics.sharpe, holdout_metrics.sharpe)
    if test_to_holdout_decay is not None and test_to_holdout_decay > max_sharpe_decay:
        reasons.append("test_to_holdout_sharpe_decay")
    return GateResult(passed=not reasons, reasons=reasons)


def robustness_score(
    test_metrics: BacktestMetrics,
    holdout_metrics: BacktestMetrics,
    fold_test_metrics: Sequence[BacktestMetrics],
    validation_metrics: BacktestMetrics | None = None,
    parameter_budget_exceeded: bool = False,
    parameter_combination_count: int = 1,
    default_parameter_budget: int = DEFAULT_PARAMETER_BUDGET,
    positive_year_ratio: float | None = None,
    round_trip_cost: float = 0,
) -> float | None:
    gates = evaluate_hard_gates(
        test_metrics,
        fold_test_metrics,
        holdout_metrics,
        validation_metrics=validation_metrics,
        positive_year_ratio=positive_year_ratio,
        round_trip_cost=round_trip_cost,
    )
    if not gates.passed:
        return None
    sharpe_basis = min(test_metrics.sharpe or 0, holdout_metrics.sharpe or 0)
    sharpe_score = min(sharpe_basis / 4, 1)
    pnl_score = min(max(test_metrics.net_pnl, 0) / 100_000, 1)
    positive_folds = sum(1 for metric in fold_test_metrics if metric.net_pnl > 0)
    positive_fold_ratio = positive_folds / len(fold_test_metrics) if fold_test_metrics else 0
    year_ratio = positive_year_ratio if positive_year_ratio is not None else positive_fold_ratio
    stability_score = (positive_fold_ratio + year_ratio) / 2
    drawdown_score = 1 / (1 + test_metrics.max_drawdown / 10_000)
    trade_count_score = min(test_metrics.annual_trades / 3000, 1)
    score = (
        0.35 * sharpe_score
        + 0.20 * pnl_score
        + 0.20 * stability_score
        + 0.15 * drawdown_score
        + 0.10 * trade_count_score
    )
    if parameter_budget_exceeded:
        score *= min(1.0, (default_parameter_budget / max(parameter_combination_count, 1)) ** 0.5)
    return score


def calculate_sharpe_decay(
    source_sharpe: float | None,
    target_sharpe: float | None,
) -> float | None:
    if source_sharpe is None or target_sharpe is None or source_sharpe <= 0:
        return None
    return 1 - target_sharpe / source_sharpe
