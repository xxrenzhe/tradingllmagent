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
    winning_trade_count: int = 0
    losing_trade_count: int = 0
    win_rate: float | None = None
    avg_win_net_pnl: float | None = None
    avg_loss_net_pnl: float | None = None
    largest_win_net_pnl: float | None = None
    largest_loss_net_pnl: float | None = None
    payoff_ratio: float | None = None
    max_consecutive_losses: int = 0
    max_drawdown_pct: float | None = None
    net_pnl_to_max_drawdown: float | None = None

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
    wins = [value for value in trade_pnls if value > 0]
    losses = [value for value in trade_pnls if value < 0]
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
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    largest_win = max(wins) if wins else None
    largest_loss = min(losses) if losses else None
    payoff_ratio = None
    if avg_win is not None and avg_loss is not None and avg_loss < 0:
        payoff_ratio = avg_win / abs(avg_loss)
    drawdown = max_drawdown(equity_curve)

    return BacktestMetrics(
        trade_count=trade_count,
        net_pnl=net_pnl,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        sharpe=sharpe,
        max_drawdown=drawdown,
        annual_trades=annual_trades,
        avg_trade_net_pnl=avg_trade,
        winning_trade_count=len(wins),
        losing_trade_count=len(losses),
        win_rate=len(wins) / trade_count if trade_count else None,
        avg_win_net_pnl=avg_win,
        avg_loss_net_pnl=avg_loss,
        largest_win_net_pnl=largest_win,
        largest_loss_net_pnl=largest_loss,
        payoff_ratio=payoff_ratio,
        max_consecutive_losses=max_consecutive_losses(trade_pnls),
        max_drawdown_pct=drawdown / starting_equity if starting_equity else None,
        net_pnl_to_max_drawdown=net_pnl / drawdown if drawdown else None,
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


def max_consecutive_losses(trade_pnls: Sequence[float]) -> int:
    worst = 0
    current = 0
    for value in trade_pnls:
        if value < 0:
            current += 1
            worst = max(worst, current)
        else:
            current = 0
    return worst
