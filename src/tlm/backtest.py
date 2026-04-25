from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Sequence

import duckdb

from .config import SymbolConfig
from .metrics import BacktestMetrics, calculate_metrics
from .strategy import StrategySpec


@dataclass(frozen=True)
class Trade:
    symbol: str
    side: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    contracts: int
    gross_pnl: float
    fees: float
    slippage_cost: float
    net_pnl: float
    exit_reason: str

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["entry_time"] = self.entry_time.isoformat()
        payload["exit_time"] = self.exit_time.isoformat()
        return payload


@dataclass(frozen=True)
class BacktestResult:
    strategy_name: str
    symbol: str
    trades: list[Trade]
    metrics: BacktestMetrics

    def to_dict(self) -> dict:
        return {
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "trades": [trade.to_dict() for trade in self.trades],
            "metrics": self.metrics.to_dict(),
        }


def load_bar_rows(bar_files: Sequence[Path]) -> list[dict]:
    files = [str(path) for path in bar_files if path.exists()]
    if not files:
        return []
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            """
            SELECT
                symbol,
                timestamp,
                open,
                high,
                low,
                close,
                bid_close,
                ask_close,
                tick_count,
                avg_spread
            FROM read_parquet(?)
            ORDER BY timestamp
            """,
            [files],
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "symbol": row[0],
            "timestamp": row[1],
            "open": float(row[2]),
            "high": float(row[3]),
            "low": float(row[4]),
            "close": float(row[5]),
            "bid_close": float(row[6]),
            "ask_close": float(row[7]),
            "tick_count": int(row[8]),
            "avg_spread": float(row[9]) if row[9] is not None else 0.0,
        }
        for row in rows
    ]


def run_bar_backtest(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    bar_files: Sequence[Path],
    starting_equity: float = 100_000,
) -> BacktestResult:
    if spec.strategy_family != "opening_range_breakout":
        raise ValueError("Phase 2 bar backtester supports opening_range_breakout only")
    bars = load_bar_rows(bar_files)
    trades = run_opening_range_breakout(spec, symbol_config, bars)
    trade_pnls = [trade.net_pnl for trade in trades]
    equity = [starting_equity]
    for pnl in trade_pnls:
        equity.append(equity[-1] + pnl)
    days = len({bar["timestamp"].date() for bar in bars}) or 1
    metrics = calculate_metrics(trade_pnls, equity, starting_equity, days)
    return BacktestResult(spec.name, spec.symbol, trades, metrics)


def run_opening_range_breakout(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    bars: Sequence[dict],
) -> list[Trade]:
    if not bars:
        return []
    opening_range_minutes = int(
        spec.indicators.get("opening_range", {}).get(
            "minutes",
            spec.parameters.get("opening_range_minutes", {}).get("values", [15])[0],
        )
    )
    stop_points = float(_exit_value(spec.exit["stop_loss"]))
    take_profit_points = float(_exit_value(spec.exit["take_profit"]))
    max_holding_minutes = int(spec.exit["max_holding_minutes"])
    contracts = int(spec.risk.get("position_sizing", {}).get("contracts", 1))
    max_trades_per_day = int(spec.risk.get("max_trades_per_day", 999_999))
    trade_start, trade_end = parse_session_range(spec.session.trade)
    flatten_time = parse_clock(spec.session.flatten)

    trades: list[Trade] = []
    by_day: dict[object, list[dict]] = {}
    for bar in bars:
        by_day.setdefault(bar["timestamp"].date(), []).append(bar)

    for _, day_bars in sorted(by_day.items(), key=lambda item: item[0]):
        session_bars = [
            bar for bar in day_bars if trade_start <= bar["timestamp"].time() <= flatten_time
        ]
        if len(session_bars) <= opening_range_minutes:
            continue
        opening = session_bars[:opening_range_minutes]
        opening_high = max(bar["high"] for bar in opening)
        opening_low = min(bar["low"] for bar in opening)
        position = None
        trades_today = 0

        for index, bar in enumerate(session_bars[opening_range_minutes:], start=opening_range_minutes):
            if bar["timestamp"].time() > trade_end and position is None:
                continue
            if position is None and trades_today < max_trades_per_day:
                if spec.direction in {"long", "long_short"} and bar["close"] > opening_high:
                    position = _open_position("long", bar, contracts, index)
                    trades_today += 1
                    continue
                if spec.direction in {"short", "long_short"} and bar["close"] < opening_low:
                    position = _open_position("short", bar, contracts, index)
                    trades_today += 1
                    continue

            if position is not None:
                holding_minutes = index - position["entry_index"]
                exit_reason = None
                exit_price = None
                if position["side"] == "long":
                    stop_price = position["entry_price"] - stop_points
                    take_price = position["entry_price"] + take_profit_points
                    if bar["low"] <= stop_price:
                        exit_reason, exit_price = "stop_loss", stop_price
                    elif bar["high"] >= take_price:
                        exit_reason, exit_price = "take_profit", take_price
                    elif holding_minutes >= max_holding_minutes:
                        exit_reason, exit_price = "max_holding", bar["bid_close"]
                    elif bar["timestamp"].time() >= flatten_time:
                        exit_reason, exit_price = "session_flatten", bar["bid_close"]
                else:
                    stop_price = position["entry_price"] + stop_points
                    take_price = position["entry_price"] - take_profit_points
                    if bar["high"] >= stop_price:
                        exit_reason, exit_price = "stop_loss", stop_price
                    elif bar["low"] <= take_price:
                        exit_reason, exit_price = "take_profit", take_price
                    elif holding_minutes >= max_holding_minutes:
                        exit_reason, exit_price = "max_holding", bar["ask_close"]
                    elif bar["timestamp"].time() >= flatten_time:
                        exit_reason, exit_price = "session_flatten", bar["ask_close"]

                if exit_reason and exit_price is not None:
                    trades.append(
                        _close_position(position, bar, exit_price, exit_reason, symbol_config)
                    )
                    position = None

        if position is not None:
            last_bar = session_bars[-1]
            exit_price = last_bar["bid_close"] if position["side"] == "long" else last_bar["ask_close"]
            trades.append(_close_position(position, last_bar, exit_price, "end_of_data", symbol_config))

    return trades


def _exit_value(exit_config: dict) -> float:
    if exit_config.get("type") == "points":
        return float(exit_config["value"])
    raise ValueError("Phase 2 backtester supports point-based exits only")


def _open_position(side: str, bar: dict, contracts: int, entry_index: int) -> dict:
    entry_price = bar["ask_close"] if side == "long" else bar["bid_close"]
    return {
        "side": side,
        "entry_time": bar["timestamp"],
        "entry_price": entry_price,
        "entry_index": entry_index,
        "contracts": contracts,
    }


def _close_position(
    position: dict,
    bar: dict,
    exit_price: float,
    exit_reason: str,
    symbol_config: SymbolConfig,
) -> Trade:
    direction = 1 if position["side"] == "long" else -1
    gross_pnl = (
        (exit_price - position["entry_price"])
        * direction
        * symbol_config.point_value
        * position["contracts"]
    )
    fees = 5.0 * position["contracts"]
    slippage_cost = 2 * symbol_config.tick_size * symbol_config.point_value * position["contracts"]
    net_pnl = gross_pnl - fees - slippage_cost
    return Trade(
        symbol=bar["symbol"],
        side=position["side"],
        entry_time=position["entry_time"],
        exit_time=bar["timestamp"],
        entry_price=position["entry_price"],
        exit_price=exit_price,
        contracts=position["contracts"],
        gross_pnl=gross_pnl,
        fees=fees,
        slippage_cost=slippage_cost,
        net_pnl=net_pnl,
        exit_reason=exit_reason,
    )


def parse_session_range(value: str) -> tuple[time, time]:
    start, end = value.split("-", 1)
    return parse_clock(start), parse_clock(end)


def parse_clock(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def result_to_json(result: BacktestResult) -> str:
    return json.dumps(result.to_dict(), indent=2, sort_keys=True)
