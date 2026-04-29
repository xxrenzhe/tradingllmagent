from __future__ import annotations

from collections import Counter, defaultdict
from typing import Sequence

from .backtest import BacktestResult, Trade
from .metrics import BacktestMetrics


def build_pre_screen_report(
    trades: Sequence[Trade],
    metrics: BacktestMetrics,
    *,
    round_trip_cost: float,
    inverse_metrics: BacktestMetrics | None = None,
    mid_trade_pnls: Sequence[float] | None = None,
    min_trade_count: int = 30,
    min_cost_coverage: float = 2.0,
    min_avg_mid_trade: float = 15.0,
    min_trades_per_day: float = 2.0,
    max_trades_per_day: float = 12.0,
    max_stop_loss_ratio: float = 0.65,
    max_top_day_pnl_share: float = 0.4,
) -> dict:
    trade_count = len(trades)
    gross_pnl = sum(trade.gross_pnl for trade in trades)
    net_pnl = sum(trade.net_pnl for trade in trades)
    avg_gross_trade = gross_pnl / trade_count if trade_count else None
    avg_net_trade = net_pnl / trade_count if trade_count else None
    avg_explicit_cost_trade = (
        sum(trade.fees + trade.slippage_cost for trade in trades) / trade_count if trade_count else None
    )
    avg_mid_trade = sum(mid_trade_pnls) / len(mid_trade_pnls) if mid_trade_pnls else None
    cost_coverage = avg_gross_trade / round_trip_cost if avg_gross_trade is not None and round_trip_cost else None
    by_day: dict[str, float] = defaultdict(float)
    side_counts = Counter(trade.side for trade in trades)
    entry_reason_counts = Counter(trade.entry_reason for trade in trades)
    exit_reason_counts = Counter(trade.exit_reason for trade in trades)
    stop_loss_ratio = exit_reason_counts.get("stop_loss", 0) / trade_count if trade_count else None
    holding_minutes = [
        max((trade.exit_time - trade.entry_time).total_seconds() / 60, 0)
        for trade in trades
    ]
    avg_holding_minutes = sum(holding_minutes) / len(holding_minutes) if holding_minutes else None
    trading_days = {trade.entry_time.date().isoformat() for trade in trades}
    trades_per_day = trade_count / len(trading_days) if trading_days else None
    for trade in trades:
        by_day[trade.entry_time.date().isoformat()] += trade.net_pnl
    top_day_pnl = max(by_day.values(), default=0.0)
    top_day_pnl_share = top_day_pnl / net_pnl if net_pnl > 0 else 1.0 if trade_count else None
    reasons = []
    if trade_count < min_trade_count:
        reasons.append("trade_count_below_prescreen_minimum")
    if gross_pnl <= 0:
        reasons.append("negative_gross_edge")
    if cost_coverage is None or cost_coverage < min_cost_coverage:
        reasons.append("insufficient_cost_coverage")
    if avg_mid_trade is not None and avg_mid_trade < min_avg_mid_trade:
        reasons.append("insufficient_mid_price_edge")
    if trades_per_day is None or trades_per_day < min_trades_per_day:
        reasons.append("trades_per_day_below_prescreen_minimum")
    if trades_per_day is not None and trades_per_day > max_trades_per_day:
        reasons.append("trades_per_day_above_prescreen_maximum")
    if stop_loss_ratio is not None and stop_loss_ratio > max_stop_loss_ratio:
        reasons.append("stop_loss_ratio_above_limit")
    if top_day_pnl_share is not None and top_day_pnl_share > max_top_day_pnl_share:
        reasons.append("top_day_pnl_concentration")
    if inverse_metrics is not None and inverse_metrics.net_pnl > metrics.net_pnl:
        reasons.append("inverse_signal_better")
    stress_survival_flag = cost_coverage is not None and cost_coverage >= min_cost_coverage
    prescreen_stage = "pre_screen_passed" if not reasons else "pre_screen_rejected"
    return {
        "artifact": "cheap_pre_screen_report",
        "passed": not reasons,
        "reasons": reasons,
        "prescreen_stage": prescreen_stage,
        "trades_per_day": trades_per_day,
        "validation_avg_mid_trade": avg_mid_trade,
        "validation_cost_coverage": cost_coverage,
        "stress_survival_flag": stress_survival_flag,
        "thresholds": {
            "min_trade_count": min_trade_count,
            "min_cost_coverage": min_cost_coverage,
            "min_avg_mid_trade": min_avg_mid_trade,
            "min_trades_per_day": min_trades_per_day,
            "max_trades_per_day": max_trades_per_day,
            "max_stop_loss_ratio": max_stop_loss_ratio,
            "max_top_day_pnl_share": max_top_day_pnl_share,
        },
        "metrics": {
            "trade_count": trade_count,
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "avg_gross_trade": avg_gross_trade,
            "avg_net_trade": avg_net_trade,
            "avg_explicit_cost_trade": avg_explicit_cost_trade,
            "avg_mid_trade": avg_mid_trade,
            "cost_coverage": cost_coverage,
            "trades_per_day": trades_per_day,
            "validation_avg_mid_trade": avg_mid_trade,
            "validation_cost_coverage": cost_coverage,
            "stress_survival_flag": stress_survival_flag,
            "top_day_pnl": top_day_pnl,
            "top_day_pnl_share": top_day_pnl_share,
            "stop_loss_ratio": stop_loss_ratio,
            "avg_holding_minutes": avg_holding_minutes,
            "annual_trades": metrics.annual_trades,
            "sharpe": metrics.sharpe,
            "profit_factor": metrics.profit_factor,
            "max_drawdown": metrics.max_drawdown,
        },
        "side_counts": dict(sorted(side_counts.items())),
        "entry_reason_counts": dict(sorted(entry_reason_counts.items())),
        "exit_reason_counts": dict(sorted(exit_reason_counts.items())),
        "inverse_metrics": inverse_metrics.to_dict() if inverse_metrics is not None else None,
    }


def build_backtest_pre_screen_report(
    result: BacktestResult,
    *,
    inverse_result: BacktestResult | None = None,
    round_trip_cost: float | None = None,
) -> dict:
    if round_trip_cost is None:
        cost_model = result.cost_model
        round_trip_cost = (
            float(cost_model.get("round_trip_fees_usd", 0))
            + 2
            * float(cost_model.get("slippage_ticks_per_side", 0))
            * float(cost_model.get("tick_size", 0))
            * float(cost_model.get("point_value", 0))
        )
    return build_pre_screen_report(
        result.trades,
        result.metrics,
        round_trip_cost=round_trip_cost,
        inverse_metrics=inverse_result.metrics if inverse_result is not None else None,
    )
