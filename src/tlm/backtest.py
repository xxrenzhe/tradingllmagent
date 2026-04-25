from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, time
from math import ceil, floor
from pathlib import Path
from typing import Sequence

import duckdb

from .config import CostModelConfig, SymbolConfig
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
    cost_model: dict
    trades: list[Trade]
    metrics: BacktestMetrics

    def to_dict(self) -> dict:
        return {
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "cost_model": self.cost_model,
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


def load_tick_rows(tick_files: Sequence[Path]) -> list[dict]:
    files = [str(path) for path in tick_files if path.exists()]
    if not files:
        return []
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            """
            SELECT
                symbol,
                timestamp,
                bid,
                ask,
                mid,
                spread
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
            "bid": float(row[2]),
            "ask": float(row[3]),
            "mid": float(row[4]),
            "spread": float(row[5]),
        }
        for row in rows
    ]


def run_bar_backtest(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    bar_files: Sequence[Path],
    starting_equity: float = 100_000,
    cost_model: CostModelConfig | None = None,
) -> BacktestResult:
    if spec.strategy_family != "opening_range_breakout":
        raise ValueError("Phase 2 bar backtester supports opening_range_breakout only")
    cost_model = cost_model or default_cost_model(symbol_config, spec.cost_model)
    bars = load_bar_rows(bar_files)
    trades = run_opening_range_breakout(spec, symbol_config, bars, cost_model)
    trade_pnls = [trade.net_pnl for trade in trades]
    equity = [starting_equity]
    for pnl in trade_pnls:
        equity.append(equity[-1] + pnl)
    days = len({bar["timestamp"].date() for bar in bars}) or 1
    metrics = calculate_metrics(trade_pnls, equity, starting_equity, days)
    return BacktestResult(spec.name, spec.symbol, cost_model.to_dict(), trades, metrics)


def run_tick_backtest(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    tick_files: Sequence[Path],
    starting_equity: float = 100_000,
    cost_model: CostModelConfig | None = None,
) -> BacktestResult:
    if spec.strategy_family != "opening_range_breakout":
        raise ValueError("Phase 3 tick replay supports opening_range_breakout only")
    cost_model = cost_model or default_cost_model(symbol_config, spec.cost_model)
    ticks = load_tick_rows(tick_files)
    trades = run_opening_range_breakout_tick_replay(spec, symbol_config, ticks, cost_model)
    trade_pnls = [trade.net_pnl for trade in trades]
    equity = [starting_equity]
    for pnl in trade_pnls:
        equity.append(equity[-1] + pnl)
    days = len({tick["timestamp"].date() for tick in ticks}) or 1
    metrics = calculate_metrics(trade_pnls, equity, starting_equity, days)
    return BacktestResult(spec.name, spec.symbol, cost_model.to_dict(), trades, metrics)


def run_opening_range_breakout(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    bars: Sequence[dict],
    cost_model: CostModelConfig | None = None,
) -> list[Trade]:
    if not bars:
        return []
    cost_model = cost_model or default_cost_model(symbol_config, spec.cost_model)
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
                        _close_position(position, bar, exit_price, exit_reason, cost_model)
                    )
                    position = None

        if position is not None:
            last_bar = session_bars[-1]
            exit_price = last_bar["bid_close"] if position["side"] == "long" else last_bar["ask_close"]
            trades.append(_close_position(position, last_bar, exit_price, "end_of_data", cost_model))

    return trades


def run_opening_range_breakout_tick_replay(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    ticks: Sequence[dict],
    cost_model: CostModelConfig | None = None,
) -> list[Trade]:
    if not ticks:
        return []
    cost_model = cost_model or default_cost_model(symbol_config, spec.cost_model)
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
    for tick in ticks:
        by_day.setdefault(tick["timestamp"].date(), []).append(tick)

    for _, day_ticks in sorted(by_day.items(), key=lambda item: item[0]):
        session_ticks = [
            tick for tick in day_ticks if trade_start <= tick["timestamp"].time() <= flatten_time
        ]
        if not session_ticks:
            continue

        opening_end = datetime.combine(
            session_ticks[0]["timestamp"].date(),
            trade_start,
        ) + timedelta(minutes=opening_range_minutes)
        opening = [tick for tick in session_ticks if tick["timestamp"] < opening_end]
        if not opening:
            continue
        opening_high = max(tick["mid"] for tick in opening)
        opening_low = min(tick["mid"] for tick in opening)

        position = None
        pending_side = None
        trades_today = 0
        for tick in session_ticks:
            tick_time = tick["timestamp"].time()
            if tick["timestamp"] < opening_end:
                continue

            if pending_side is not None:
                position = _open_tick_position(
                    pending_side,
                    tick,
                    contracts,
                    cost_model,
                )
                pending_side = None

            if position is not None:
                exit_reason, exit_price = _tick_exit_signal(
                    position,
                    tick,
                    stop_points,
                    take_profit_points,
                    max_holding_minutes,
                    flatten_time,
                    cost_model,
                )
                if exit_reason and exit_price is not None:
                    trades.append(
                        _close_position(position, tick, exit_price, exit_reason, cost_model)
                    )
                    position = None
                continue

            if tick_time > trade_end or trades_today >= max_trades_per_day:
                continue
            if spec.direction in {"long", "long_short"} and tick["mid"] > opening_high:
                pending_side = "long"
                trades_today += 1
                continue
            if spec.direction in {"short", "long_short"} and tick["mid"] < opening_low:
                pending_side = "short"
                trades_today += 1

        if position is not None:
            last_tick = session_ticks[-1]
            side = "sell" if position["side"] == "long" else "buy"
            exit_price = _align_price(
                last_tick["bid"] if position["side"] == "long" else last_tick["ask"],
                cost_model.tick_size,
                side,
            )
            trades.append(_close_position(position, last_tick, exit_price, "end_of_data", cost_model))

    return trades


def _open_tick_position(
    side: str,
    tick: dict,
    contracts: int,
    cost_model: CostModelConfig,
) -> dict:
    entry_side = "buy" if side == "long" else "sell"
    entry_price = _align_price(
        tick["ask"] if side == "long" else tick["bid"],
        cost_model.tick_size,
        entry_side,
    )
    return {
        "side": side,
        "entry_time": tick["timestamp"],
        "entry_price": entry_price,
        "entry_index": None,
        "contracts": contracts,
    }


def _tick_exit_signal(
    position: dict,
    tick: dict,
    stop_points: float,
    take_profit_points: float,
    max_holding_minutes: int,
    flatten_time: time,
    cost_model: CostModelConfig,
) -> tuple[str | None, float | None]:
    holding_minutes = (tick["timestamp"] - position["entry_time"]).total_seconds() / 60
    if position["side"] == "long":
        stop_price = position["entry_price"] - stop_points
        take_price = position["entry_price"] + take_profit_points
        if tick["bid"] <= stop_price:
            return "stop_loss", _align_price(tick["bid"], cost_model.tick_size, "sell")
        if tick["bid"] >= take_price:
            return "take_profit", _align_price(tick["bid"], cost_model.tick_size, "sell")
        if holding_minutes >= max_holding_minutes:
            return "max_holding", _align_price(tick["bid"], cost_model.tick_size, "sell")
        if tick["timestamp"].time() >= flatten_time:
            return "session_flatten", _align_price(tick["bid"], cost_model.tick_size, "sell")
        return None, None

    stop_price = position["entry_price"] + stop_points
    take_price = position["entry_price"] - take_profit_points
    if tick["ask"] >= stop_price:
        return "stop_loss", _align_price(tick["ask"], cost_model.tick_size, "buy")
    if tick["ask"] <= take_price:
        return "take_profit", _align_price(tick["ask"], cost_model.tick_size, "buy")
    if holding_minutes >= max_holding_minutes:
        return "max_holding", _align_price(tick["ask"], cost_model.tick_size, "buy")
    if tick["timestamp"].time() >= flatten_time:
        return "session_flatten", _align_price(tick["ask"], cost_model.tick_size, "buy")
    return None, None


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
    cost_model: CostModelConfig,
) -> Trade:
    direction = 1 if position["side"] == "long" else -1
    gross_pnl = (
        (exit_price - position["entry_price"])
        * direction
        * cost_model.point_value
        * position["contracts"]
    )
    fees = cost_model.round_trip_fees_usd * position["contracts"]
    slippage_cost = (
        2
        * cost_model.slippage_ticks_per_side
        * cost_model.tick_size
        * cost_model.point_value
        * position["contracts"]
    )
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


def _align_price(price: float, tick_size: float, side: str) -> float:
    ticks = price / tick_size
    if side == "buy":
        return ceil(ticks) * tick_size
    if side == "sell":
        return floor(ticks) * tick_size
    raise ValueError(f"Unsupported side for price alignment: {side}")


def default_cost_model(symbol_config: SymbolConfig, name: str) -> CostModelConfig:
    return CostModelConfig(
        name=name,
        tick_size=symbol_config.tick_size,
        point_value=symbol_config.point_value,
        tick_value=symbol_config.tick_size * symbol_config.point_value,
        slippage_ticks_per_side=1,
        round_trip_fees_usd=5,
    )


def parse_session_range(value: str) -> tuple[time, time]:
    start, end = value.split("-", 1)
    return parse_clock(start), parse_clock(end)


def parse_clock(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def result_to_json(result: BacktestResult) -> str:
    return json.dumps(result.to_dict(), indent=2, sort_keys=True)
