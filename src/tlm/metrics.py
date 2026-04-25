from __future__ import annotations

from dataclasses import asdict, dataclass
from math import sqrt
from statistics import mean, pstdev
from typing import Sequence


@dataclass(frozen=True)
class BacktestMetrics:
    trade_count: int
    net_pnl: float
    gross_profit: float
    gross_loss: float
    profit_factor: float | None
    sharpe: float | None
    max_drawdown: float
    annual_trades: float
    avg_trade_net_pnl: float | None

    def to_dict(self) -> dict:
        return asdict(self)


def calculate_metrics(
    trade_pnls: Sequence[float],
    equity_curve: Sequence[float],
    starting_equity: float,
    calendar_days: int,
) -> BacktestMetrics:
    gross_profit = sum(value for value in trade_pnls if value > 0)
    gross_loss = sum(value for value in trade_pnls if value < 0)
    net_pnl = sum(trade_pnls)
    trade_count = len(trade_pnls)

    profit_factor = None
    if gross_loss < 0:
        profit_factor = gross_profit / abs(gross_loss)

    returns = [value / starting_equity for value in trade_pnls]
    sharpe = None
    if len(returns) > 1:
        std = pstdev(returns)
        if std > 0:
            sharpe = mean(returns) / std * sqrt(252)

    annual_trades = trade_count / max(calendar_days, 1) * 365
    avg_trade = net_pnl / trade_count if trade_count else None

    return BacktestMetrics(
        trade_count=trade_count,
        net_pnl=net_pnl,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        sharpe=sharpe,
        max_drawdown=max_drawdown(equity_curve),
        annual_trades=annual_trades,
        avg_trade_net_pnl=avg_trade,
    )


def max_drawdown(equity_curve: Sequence[float]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    worst = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        worst = min(worst, value - peak)
    return abs(worst)
