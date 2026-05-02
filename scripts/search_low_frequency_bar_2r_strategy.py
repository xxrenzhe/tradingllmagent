#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
import json
from pathlib import Path
import sys
from typing import Any, Sequence

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tlm.config import get_symbol
from tlm.storage import write_json


@dataclass(frozen=True)
class DailyBar:
    day: date
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    tick_count: int


@dataclass(frozen=True)
class StrategySpec:
    mode: str
    direction: str
    lookback_days: int
    entry_buffer_ticks: float
    stop_atr_multiple: float
    min_stop_ticks: float
    max_stop_ticks: float
    min_range_atr: float
    max_range_atr: float
    min_trend_atr: float
    reward_r: float
    max_hold_days: int


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Search low-frequency NQ daily/session bar strategies with fixed 2R and 1.5R brackets."
    )
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--config-dir", type=Path, default=Path("configs"))
    parser.add_argument("--date-from", default="2010-06-06")
    parser.add_argument("--date-to", default="2026-04-27")
    parser.add_argument("--train-years", type=int, default=5)
    parser.add_argument("--test-start-year", type=int, default=2016)
    parser.add_argument("--min-train-trades", type=int, default=40)
    parser.add_argument("--min-test-trades", type=int, default=8)
    parser.add_argument("--min-win-rate", type=float, default=0.70)
    parser.add_argument("--fallback-win-rate", type=float, default=0.55)
    parser.add_argument("--output", type=Path, default=Path("reports/nq_low_frequency_bar_2r_search_2026-05-03.json"))
    args = parser.parse_args(argv)

    symbol_config = get_symbol(args.symbol, args.config_dir)
    files = bar_files(args.data_root, args.symbol, args.date_from, args.date_to)
    print(f"loading {len(files)} 1m bar files as daily RTH bars", flush=True)
    bars = load_daily_bars(files)
    specs = strategy_grid()
    print(f"evaluating {len(specs)} low-frequency specs across walk-forward years", flush=True)
    folds = run_walk_forward(
        bars,
        specs,
        point_value=symbol_config.point_value,
        tick_size=symbol_config.tick_size,
        train_years=args.train_years,
        test_start_year=args.test_start_year,
        min_train_trades=args.min_train_trades,
        min_test_trades=args.min_test_trades,
        min_win_rate=args.min_win_rate,
        fallback_win_rate=args.fallback_win_rate,
    )
    summary = summarize_walk_forward(
        folds,
        min_test_trades=args.min_test_trades,
        min_win_rate=args.min_win_rate,
        fallback_win_rate=args.fallback_win_rate,
    )
    payload = {
        "artifact": "nq_low_frequency_bar_2r_search",
        "schema_version": 1,
        "symbol": args.symbol,
        "date_from": args.date_from,
        "date_to": args.date_to,
        "target": {
            "strict_win_rate": args.min_win_rate,
            "strict_reward_r": 2.0,
            "fallback_win_rate": args.fallback_win_rate,
            "fallback_reward_r": 1.5,
            "min_train_trades": args.min_train_trades,
            "min_test_trades": args.min_test_trades,
        },
        "method": {
            "scope": "Low-frequency session/daily strategy family built from historical data/bars 1m NQ_CME files.",
            "aggregation": "Regular trading hours 14:30-20:00 UTC are aggregated to one daily OHLC bar.",
            "selection": "Each fold selects specs using only the previous train-years; the next calendar year is untouched OOS.",
            "entry_exit": "Entry is next daily open adjusted by a tick buffer; stops/targets are replayed on daily high/low with conservative stop-first collision handling.",
            "spec_count": len(specs),
            "train_years": args.train_years,
            "test_start_year": args.test_start_year,
        },
        "coverage": {
            "source_file_count": len(files),
            "daily_bar_count": len(bars),
            "first_day": bars[0].day.isoformat() if bars else None,
            "last_day": bars[-1].day.isoformat() if bars else None,
            "year_count": len({bar.day.year for bar in bars}),
        },
        "folds": folds,
        "summary": summary,
        "decision": summary["decision"],
    }
    write_json(args.output, payload)
    print(json.dumps({"output": str(args.output), "decision": summary["decision"]}, indent=2))
    return 0


def bar_files(data_root: Path, symbol: str, date_from: str, date_to: str) -> list[Path]:
    root = data_root / "bars" / "1m" / symbol
    files = []
    for path in sorted(root.glob("date=*/part-000.parquet")):
        day = path.parent.name.removeprefix("date=")
        if date_from <= day <= date_to:
            files.append(path)
    return files


def load_daily_bars(files: Sequence[Path]) -> list[DailyBar]:
    if not files:
        return []
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            """
            SELECT
                CAST(timestamp AS DATE) AS day,
                min(timestamp) AS timestamp,
                first(open ORDER BY timestamp) AS open,
                max(high) AS high,
                min(low) AS low,
                last(close ORDER BY timestamp) AS close,
                sum(tick_count)::INTEGER AS tick_count
            FROM read_parquet(?)
            WHERE CAST(timestamp AS TIME) >= TIME '14:30:00'
              AND CAST(timestamp AS TIME) <= TIME '20:00:00'
            GROUP BY day
            HAVING count(*) >= 120
            ORDER BY day
            """,
            [[str(path) for path in files]],
        ).fetchall()
    finally:
        con.close()
    return [
        DailyBar(
            day=row[0],
            timestamp=row[1],
            open=float(row[2]),
            high=float(row[3]),
            low=float(row[4]),
            close=float(row[5]),
            tick_count=int(row[6] or 0),
        )
        for row in rows
    ]


def strategy_grid() -> list[StrategySpec]:
    specs = []
    for mode in ("breakout", "reversal"):
        for direction in ("long", "short"):
            for lookback_days in (10, 20, 40):
                for entry_buffer_ticks in (0.0, 8.0):
                    for stop_atr_multiple in (0.75, 1.25):
                        for min_stop_ticks in (48.0, 96.0):
                            for max_stop_ticks in (240.0, 360.0):
                                if min_stop_ticks >= max_stop_ticks:
                                    continue
                                for min_range_atr in (0.0, 0.8):
                                    for max_range_atr in (1.8, 3.0):
                                        if min_range_atr >= max_range_atr:
                                            continue
                                        for min_trend_atr in (0.0, 0.8):
                                            for reward_r in (1.5, 2.0):
                                                specs.append(
                                                    StrategySpec(
                                                        mode=mode,
                                                        direction=direction,
                                                        lookback_days=lookback_days,
                                                        entry_buffer_ticks=entry_buffer_ticks,
                                                        stop_atr_multiple=stop_atr_multiple,
                                                        min_stop_ticks=min_stop_ticks,
                                                        max_stop_ticks=max_stop_ticks,
                                                        min_range_atr=min_range_atr,
                                                        max_range_atr=max_range_atr,
                                                        min_trend_atr=min_trend_atr,
                                                        reward_r=reward_r,
                                                        max_hold_days=5,
                                                    )
                                                )
    return specs


def run_walk_forward(
    bars: Sequence[DailyBar],
    specs: Sequence[StrategySpec],
    *,
    point_value: float,
    tick_size: float,
    train_years: int,
    test_start_year: int,
    min_train_trades: int,
    min_test_trades: int,
    min_win_rate: float,
    fallback_win_rate: float,
) -> list[dict[str, Any]]:
    years = sorted({bar.day.year for bar in bars})
    folds = []
    for test_year in years:
        if test_year < test_start_year:
            continue
        train_year_set = tuple(range(test_year - train_years, test_year))
        train_bars = [bar for bar in bars if bar.day.year in train_year_set]
        test_bars = [bar for bar in bars if bar.day.year == test_year]
        if not train_bars or not test_bars:
            continue
        evaluated = []
        for spec in specs:
            train_trades = replay_strategy(train_bars, spec, tick_size, point_value)
            train_metrics = summarize_trades(train_trades)
            train_gate = train_metrics["trade_count"] >= min_train_trades and train_metrics["win_rate"] >= min_win_rate and train_metrics["net_pnl"] > 0.0
            fallback_train_gate = (
                train_metrics["trade_count"] >= min_train_trades
                and train_metrics["win_rate"] >= fallback_win_rate
                and train_metrics["net_pnl"] > 0.0
                and spec.reward_r >= 1.5
            )
            if not train_gate and not fallback_train_gate:
                continue
            test_trades = replay_strategy(test_bars, spec, tick_size, point_value)
            evaluated.append(
                {
                    "spec": asdict(spec),
                    "train": {**train_metrics, "strict_gate_passed": train_gate, "fallback_gate_passed": fallback_train_gate},
                    "test": summarize_trades(test_trades),
                }
            )
        evaluated.sort(key=selection_key, reverse=True)
        selected = evaluated[0] if evaluated else None
        fold = {
            "test_year": test_year,
            "train_years": train_year_set,
            "train_daily_bars": len(train_bars),
            "test_daily_bars": len(test_bars),
            "evaluated_train_passing_spec_count": len(evaluated),
            "selected": selected,
        }
        if selected:
            test = selected["test"]
            spec = selected["spec"]
            fold["strict_test_gate_passed"] = (
                spec["reward_r"] >= 2.0
                and test["trade_count"] >= min_test_trades
                and test["win_rate"] >= min_win_rate
                and test["net_pnl"] > 0.0
            )
            fold["fallback_test_gate_passed"] = (
                spec["reward_r"] >= 1.5
                and test["trade_count"] >= min_test_trades
                and test["win_rate"] >= fallback_win_rate
                and test["net_pnl"] > 0.0
            )
        else:
            fold["strict_test_gate_passed"] = False
            fold["fallback_test_gate_passed"] = False
        folds.append(fold)
        print(f"fold {test_year}: selected={bool(selected)}", flush=True)
    return folds


def replay_strategy(
    bars: Sequence[DailyBar],
    spec: StrategySpec,
    tick_size: float,
    point_value: float,
) -> list[dict[str, Any]]:
    trades = []
    index = max(spec.lookback_days, 21)
    while index < len(bars) - 1:
        if not entry_signal(bars, index, spec, tick_size):
            index += 1
            continue
        entry_index = index + 1
        trade = simulate_trade(bars, entry_index, spec, tick_size, point_value)
        if trade:
            trades.append(trade)
            index = int(trade["exit_index"]) + 1
        else:
            index += 1
    return trades


def entry_signal(bars: Sequence[DailyBar], index: int, spec: StrategySpec, tick_size: float) -> bool:
    bar = bars[index]
    history = bars[index - spec.lookback_days : index]
    atr = average_true_range(bars, index, 20)
    if atr <= 0.0 or not history:
        return False
    recent_range = max(item.high for item in history) - min(item.low for item in history)
    range_atr = recent_range / atr
    if range_atr < spec.min_range_atr or range_atr > spec.max_range_atr:
        return False
    trend_atr = abs(bar.close - bars[index - spec.lookback_days].close) / atr
    if trend_atr < spec.min_trend_atr:
        return False
    high_break = max(item.high for item in history) + spec.entry_buffer_ticks * tick_size
    low_break = min(item.low for item in history) - spec.entry_buffer_ticks * tick_size
    if spec.mode == "breakout":
        if spec.direction == "long":
            return bar.close > high_break
        return bar.close < low_break
    if spec.mode == "reversal":
        if spec.direction == "long":
            return bar.low <= low_break and bar.close > bar.open
        return bar.high >= high_break and bar.close < bar.open
    raise ValueError(f"Unsupported mode: {spec.mode}")


def simulate_trade(
    bars: Sequence[DailyBar],
    entry_index: int,
    spec: StrategySpec,
    tick_size: float,
    point_value: float,
) -> dict[str, Any] | None:
    entry = bars[entry_index]
    atr = average_true_range(bars, entry_index, 20)
    stop_ticks = min(max((atr * spec.stop_atr_multiple) / tick_size, spec.min_stop_ticks), spec.max_stop_ticks)
    stop_points = stop_ticks * tick_size
    target_points = stop_points * spec.reward_r
    side = spec.direction
    entry_price = entry.open
    if side == "long":
        stop_price = entry_price - stop_points
        target_price = entry_price + target_points
    else:
        stop_price = entry_price + stop_points
        target_price = entry_price - target_points
    deadline = min(entry_index + spec.max_hold_days, len(bars) - 1)
    for exit_index in range(entry_index, deadline + 1):
        bar = bars[exit_index]
        if side == "long":
            hit_stop = bar.low <= stop_price
            hit_target = bar.high >= target_price
            if hit_stop and hit_target:
                return close_trade(side, entry_index, exit_index, entry, bar, entry_price, stop_price, "stop_loss_conservative", point_value)
            if hit_stop:
                return close_trade(side, entry_index, exit_index, entry, bar, entry_price, stop_price, "stop_loss", point_value)
            if hit_target:
                return close_trade(side, entry_index, exit_index, entry, bar, entry_price, target_price, f"take_profit_{spec.reward_r:g}r", point_value)
        else:
            hit_stop = bar.high >= stop_price
            hit_target = bar.low <= target_price
            if hit_stop and hit_target:
                return close_trade(side, entry_index, exit_index, entry, bar, entry_price, stop_price, "stop_loss_conservative", point_value)
            if hit_stop:
                return close_trade(side, entry_index, exit_index, entry, bar, entry_price, stop_price, "stop_loss", point_value)
            if hit_target:
                return close_trade(side, entry_index, exit_index, entry, bar, entry_price, target_price, f"take_profit_{spec.reward_r:g}r", point_value)
    return close_trade(side, entry_index, deadline, entry, bars[deadline], entry_price, None, "time_exit", point_value)


def close_trade(
    side: str,
    entry_index: int,
    exit_index: int,
    entry: DailyBar,
    exit_bar: DailyBar,
    entry_price: float,
    exit_price: float | None,
    exit_reason: str,
    point_value: float,
) -> dict[str, Any]:
    realized_exit_price = exit_bar.close if exit_price is None else exit_price
    gross_pnl = (
        (realized_exit_price - entry_price) * point_value
        if side == "long"
        else (entry_price - realized_exit_price) * point_value
    )
    return {
        "side": side,
        "entry_index": entry_index,
        "exit_index": exit_index,
        "entry_day": entry.day.isoformat(),
        "exit_day": exit_bar.day.isoformat(),
        "entry_price": entry_price,
        "exit_price": realized_exit_price,
        "gross_pnl": gross_pnl,
        "net_pnl": gross_pnl - 15.0,
        "exit_reason": exit_reason,
    }


def average_true_range(bars: Sequence[DailyBar], index: int, lookback: int) -> float:
    if index <= 0:
        return 0.0
    start = max(1, index - lookback + 1)
    ranges = []
    for row_index in range(start, index + 1):
        bar = bars[row_index]
        previous_close = bars[row_index - 1].close
        ranges.append(max(bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close)))
    return sum(ranges) / len(ranges) if ranges else 0.0


def summarize_trades(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    trade_count = len(trades)
    wins = sum(1 for trade in trades if float(trade["net_pnl"]) > 0.0)
    net_pnl = sum(float(trade["net_pnl"]) for trade in trades)
    gross_profit = sum(float(trade["net_pnl"]) for trade in trades if float(trade["net_pnl"]) > 0.0)
    gross_loss = sum(float(trade["net_pnl"]) for trade in trades if float(trade["net_pnl"]) < 0.0)
    return {
        "trade_count": trade_count,
        "winning_trade_count": wins,
        "win_rate": wins / trade_count if trade_count else 0.0,
        "net_pnl": net_pnl,
        "avg_trade_net_pnl": net_pnl / trade_count if trade_count else None,
        "profit_factor": gross_profit / abs(gross_loss) if gross_loss < 0 else None,
        "exit_reasons": exit_reason_counts(trades),
    }


def summarize_walk_forward(
    folds: Sequence[dict[str, Any]],
    *,
    min_test_trades: int,
    min_win_rate: float,
    fallback_win_rate: float,
) -> dict[str, Any]:
    selected_folds = [fold for fold in folds if fold.get("selected")]
    test_metrics = [fold["selected"]["test"] for fold in selected_folds]
    strict_failed_years = [fold["test_year"] for fold in folds if not fold.get("strict_test_gate_passed")]
    fallback_failed_years = [fold["test_year"] for fold in folds if not fold.get("fallback_test_gate_passed")]
    total_trades = sum(int(metrics["trade_count"]) for metrics in test_metrics)
    total_wins = sum(int(metrics["winning_trade_count"]) for metrics in test_metrics)
    total_net = sum(float(metrics["net_pnl"]) for metrics in test_metrics)
    min_year_win_rate = min((float(metrics["win_rate"]) for metrics in test_metrics), default=0.0)
    min_year_pnl = min((float(metrics["net_pnl"]) for metrics in test_metrics), default=0.0)
    return {
        "test_year_count": len(folds),
        "selected_fold_count": len(selected_folds),
        "oos_total_trades": total_trades,
        "oos_winning_trade_count": total_wins,
        "oos_win_rate": total_wins / total_trades if total_trades else 0.0,
        "oos_total_net_pnl": total_net,
        "oos_min_year_win_rate": min_year_win_rate,
        "oos_min_year_pnl": min_year_pnl,
        "min_test_trades": min_test_trades,
        "strict_failed_years": strict_failed_years,
        "fallback_failed_years": fallback_failed_years,
        "decision": {
            "passed": not strict_failed_years and bool(folds),
            "fallback_passed": not fallback_failed_years and bool(folds),
            "reason": None
            if not strict_failed_years
            else f"No low-frequency bar strategy passed every next-year OOS {min_win_rate:.0%} win-rate / 2R fold.",
            "fallback_reason": None
            if not fallback_failed_years
            else f"No low-frequency bar strategy passed every next-year OOS {fallback_win_rate:.0%} win-rate / 1.5R fold.",
        },
    }


def selection_key(row: dict[str, Any]) -> tuple[float, ...]:
    train = row["train"]
    spec = row["spec"]
    return (
        1.0 if train["strict_gate_passed"] else 0.0,
        float(spec["reward_r"]),
        float(train["win_rate"]),
        float(train["net_pnl"]),
        float(train["trade_count"]),
    )


def exit_reason_counts(trades: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for trade in trades:
        reason = str(trade["exit_reason"])
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    raise SystemExit(main())
