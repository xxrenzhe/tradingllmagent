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
    min_trade_count: int = 30,
    min_cost_coverage: float = 2.0,
    max_top_day_pnl_share: float = 0.4,
) -> dict:
    trade_count = len(trades)
    gross_pnl = sum(trade.gross_pnl for trade in trades)
    net_pnl = sum(trade.net_pnl for trade in trades)
    avg_gross_trade = gross_pnl / trade_count if trade_count else None
    avg_net_trade = net_pnl / trade_count if trade_count else None
    cost_coverage = avg_gross_trade / round_trip_cost if avg_gross_trade is not None and round_trip_cost else None
    by_day: dict[str, float] = defaultdict(float)
    side_counts = Counter(trade.side for trade in trades)
    entry_reason_counts = Counter(trade.entry_reason for trade in trades)
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
    if top_day_pnl_share is not None and top_day_pnl_share > max_top_day_pnl_share:
        reasons.append("top_day_pnl_concentration")
    if inverse_metrics is not None and inverse_metrics.net_pnl > metrics.net_pnl:
        reasons.append("inverse_signal_better")
    return {
        "artifact": "cheap_pre_screen_report",
        "passed": not reasons,
        "reasons": reasons,
        "thresholds": {
            "min_trade_count": min_trade_count,
            "min_cost_coverage": min_cost_coverage,
            "max_top_day_pnl_share": max_top_day_pnl_share,
        },
        "metrics": {
            "trade_count": trade_count,
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "avg_gross_trade": avg_gross_trade,
            "avg_net_trade": avg_net_trade,
            "cost_coverage": cost_coverage,
            "top_day_pnl": top_day_pnl,
            "top_day_pnl_share": top_day_pnl_share,
            "annual_trades": metrics.annual_trades,
            "sharpe": metrics.sharpe,
            "profit_factor": metrics.profit_factor,
            "max_drawdown": metrics.max_drawdown,
        },
        "side_counts": dict(sorted(side_counts.items())),
        "entry_reason_counts": dict(sorted(entry_reason_counts.items())),
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
