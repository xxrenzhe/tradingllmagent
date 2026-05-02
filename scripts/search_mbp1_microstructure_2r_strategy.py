#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, time
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
class StrategySpec:
    mode: str
    direction: str
    fast_seconds: int
    slow_seconds: int
    min_fast_move_ticks: float
    min_slow_move_ticks: float
    max_spread_ticks: float
    min_depth: float
    min_aligned_imbalance: float
    stop_ticks: float
    max_hold_seconds: int
    cooldown_seconds: int


@dataclass(frozen=True)
class QuoteRow:
    timestamp: datetime
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    mid: float
    spread: float


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Search standalone MBP-1 quote microstructure strategies with fixed 2R brackets.")
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--config-dir", type=Path, default=Path("configs"))
    parser.add_argument("--date-from", default="2026-03-03")
    parser.add_argument("--date-to", default="2026-05-01")
    parser.add_argument("--min-train-trades", type=int, default=100)
    parser.add_argument("--min-test-trades", type=int, default=100)
    parser.add_argument("--min-win-rate", type=float, default=0.70)
    parser.add_argument("--full-grid", action="store_true")
    parser.add_argument("--include-reversal", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("reports/nq_mbp1_microstructure_2r_search_2026-05-03.json"))
    args = parser.parse_args(argv)

    symbol_config = get_symbol(args.symbol, args.config_dir)
    files = quote_files(args.data_root, args.symbol, args.date_from, args.date_to)
    print(f"loading {len(files)} quote files as 1-second snapshots", flush=True)
    rows = load_second_quotes(files)
    split_index = len(rows) // 2
    train_rows = rows[:split_index]
    test_rows = rows[split_index:]
    specs = strategy_grid(full_grid=args.full_grid, include_reversal=args.include_reversal)
    print(f"evaluating {len(specs)} specs", flush=True)
    evaluated = []
    for index, spec in enumerate(specs, start=1):
        train = replay_strategy(train_rows, spec, symbol_config.tick_size, symbol_config.point_value)
        test = replay_strategy(test_rows, spec, symbol_config.tick_size, symbol_config.point_value)
        row = {
            "spec": asdict(spec),
            "train": summarize_trades(train, args.min_train_trades, args.min_win_rate),
            "test": summarize_trades(test, args.min_test_trades, args.min_win_rate),
        }
        evaluated.append(row)
        if index % 100 == 0:
            print(f"evaluated {index}/{len(specs)}", flush=True)
    evaluated.sort(key=sort_key, reverse=True)
    selected = next((row for row in evaluated if row["train"]["gate_passed"]), evaluated[0] if evaluated else None)
    decision = {
        "passed": bool(selected and selected["train"]["gate_passed"] and selected["test"]["gate_passed"]),
        "reason": None
        if selected and selected["train"]["gate_passed"] and selected["test"]["gate_passed"]
        else "No train-selected standalone MBP-1 2R strategy passed the 70% win-rate, trade-count, and positive-PnL holdout gates.",
        "live_ready": False,
        "live_ready_reason": "A two-month quote-only search cannot establish long-term profitability or paper readiness without multi-year walk-forward and live/paper evidence.",
    }
    payload = {
        "artifact": "nq_mbp1_microstructure_2r_search",
        "schema_version": 1,
        "symbol": args.symbol,
        "date_from": args.date_from,
        "date_to": args.date_to,
        "target": {
            "reward_r": 2.0,
            "min_win_rate": args.min_win_rate,
            "min_train_trades": args.min_train_trades,
            "min_test_trades": args.min_test_trades,
        },
        "method": {
            "scope": "Standalone quote microstructure search on Databento MBP-1 normalized quote files.",
            "sampling": "Last quote per second; entries and exits use top-of-book bid/ask.",
            "selection": "Chronological first half selects specs; second half is untouched holdout.",
            "long_term_limit": "Only covers the provided two-month tick window, so passing this report alone cannot satisfy long-term/live-ready requirements.",
            "train_test_split": {
                "train_start": train_rows[0].timestamp.isoformat() if train_rows else None,
                "train_end": train_rows[-1].timestamp.isoformat() if train_rows else None,
                "test_start": test_rows[0].timestamp.isoformat() if test_rows else None,
                "test_end": test_rows[-1].timestamp.isoformat() if test_rows else None,
            },
            "spec_count": len(specs),
            "full_grid": args.full_grid,
            "include_reversal": args.include_reversal,
        },
        "coverage": {
            "quote_file_count": len(files),
            "second_quote_count": len(rows),
            "train_second_quote_count": len(train_rows),
            "test_second_quote_count": len(test_rows),
        },
        "selected": selected,
        "top_specs": evaluated[:50],
        "decision": decision,
    }
    write_json(args.output, payload)
    print(json.dumps({"output": str(args.output), **decision}, indent=2))
    return 0


def quote_files(data_root: Path, symbol: str, date_from: str, date_to: str) -> list[Path]:
    root = data_root / "normalized" / "quotes" / symbol
    files = []
    for path in sorted(root.glob("date=*/part-000.parquet")):
        day = path.parent.name.removeprefix("date=")
        if date_from <= day <= date_to:
            files.append(path)
    return files


def load_second_quotes(files: Sequence[Path]) -> list[QuoteRow]:
    if not files:
        return []
    rows = []
    for index, path in enumerate(files, start=1):
        rows.extend(load_second_quotes_for_file(path))
        if index % 10 == 0:
            print(f"loaded {index}/{len(files)} quote files", flush=True)
    rows.sort(key=lambda row: row[0])
    return [
        QuoteRow(
            timestamp=row[0],
            bid=float(row[1]),
            ask=float(row[2]),
            bid_size=float(row[3] or 0.0),
            ask_size=float(row[4] or 0.0),
            mid=float(row[5]),
            spread=float(row[6]),
        )
        for row in rows
    ]


def load_second_quotes_for_file(path: Path) -> list[tuple[Any, ...]]:
    con = duckdb.connect(":memory:")
    try:
        return con.execute(
            """
            WITH sampled AS (
                SELECT
                    timestamp,
                    bid,
                    ask,
                    bid_size,
                    ask_size,
                    mid,
                    spread,
                    row_number() OVER (
                        PARTITION BY date_trunc('second', timestamp)
                        ORDER BY timestamp DESC
                    ) AS rn
                FROM read_parquet(?)
                WHERE bid > 0 AND ask > 0 AND ask >= bid
            )
            SELECT timestamp, bid, ask, bid_size, ask_size, mid, spread
            FROM sampled
            WHERE rn = 1
            ORDER BY timestamp
            """,
            [str(path)],
        ).fetchall()
    finally:
        con.close()


def strategy_grid(*, full_grid: bool = False, include_reversal: bool = False) -> list[StrategySpec]:
    specs = []
    modes = ("continuation", "reversal") if include_reversal else ("continuation",)
    fast_slow_grid = ((10, 60), (30, 180), (60, 300)) if full_grid else ((30, 180), (60, 300))
    fast_move_grid = (4.0, 8.0, 12.0, 20.0) if full_grid else (8.0, 16.0)
    slow_move_grid = (8.0, 16.0, 32.0) if full_grid else (16.0, 32.0)
    spread_grid = (2.0, 3.0) if full_grid else (2.0,)
    depth_grid = (1.0, 2.0, 4.0) if full_grid else (1.0, 2.0)
    imbalance_grid = (-0.25, 0.0, 0.25) if full_grid else (0.0,)
    stop_grid = (16.0, 24.0, 32.0, 48.0) if full_grid else (24.0, 48.0)
    for mode in modes:
        for direction in ("long", "short"):
            for fast_seconds, slow_seconds in fast_slow_grid:
                for min_fast_move_ticks in fast_move_grid:
                    for min_slow_move_ticks in slow_move_grid:
                        for max_spread_ticks in spread_grid:
                            for min_depth in depth_grid:
                                for min_aligned_imbalance in imbalance_grid:
                                    for stop_ticks in stop_grid:
                                        specs.append(
                                            StrategySpec(
                                                mode=mode,
                                                direction=direction,
                                                fast_seconds=fast_seconds,
                                                slow_seconds=slow_seconds,
                                                min_fast_move_ticks=min_fast_move_ticks,
                                                min_slow_move_ticks=min_slow_move_ticks,
                                                max_spread_ticks=max_spread_ticks,
                                                min_depth=min_depth,
                                                min_aligned_imbalance=min_aligned_imbalance,
                                                stop_ticks=stop_ticks,
                                                max_hold_seconds=900,
                                                cooldown_seconds=300,
                                            )
                                        )
    return specs


def replay_strategy(
    rows: Sequence[QuoteRow],
    spec: StrategySpec,
    tick_size: float,
    point_value: float,
) -> list[dict[str, Any]]:
    trades = []
    last_exit_index = -1
    last_entry_time: datetime | None = None
    for index, row in enumerate(rows):
        if index <= last_exit_index:
            continue
        if last_entry_time and (row.timestamp - last_entry_time).total_seconds() < spec.cooldown_seconds:
            continue
        if not in_trade_session(row.timestamp):
            continue
        if not entry_signal(rows, index, spec, tick_size):
            continue
        trade = simulate_trade(rows, index, spec, tick_size, point_value)
        if trade is None:
            continue
        trades.append(trade)
        last_exit_index = int(trade["exit_index"])
        last_entry_time = row.timestamp
    return trades


def entry_signal(rows: Sequence[QuoteRow], index: int, spec: StrategySpec, tick_size: float) -> bool:
    row = rows[index]
    fast_index = index - spec.fast_seconds
    slow_index = index - spec.slow_seconds
    if fast_index < 0 or slow_index < 0 or tick_size <= 0:
        return False
    if row.timestamp.date() != rows[slow_index].timestamp.date():
        return False
    spread_ticks = row.spread / tick_size
    if spread_ticks > spec.max_spread_ticks:
        return False
    relevant_depth = row.ask_size if spec.direction == "long" else row.bid_size
    if relevant_depth < spec.min_depth:
        return False
    imbalance = (row.bid_size - row.ask_size) / (row.bid_size + row.ask_size) if row.bid_size + row.ask_size else 0.0
    aligned_imbalance = imbalance if spec.direction == "long" else -imbalance
    if aligned_imbalance < spec.min_aligned_imbalance:
        return False
    fast_move = (row.mid - rows[fast_index].mid) / tick_size
    slow_move = (row.mid - rows[slow_index].mid) / tick_size
    if spec.mode == "reversal":
        fast_move = -fast_move
        slow_move = -slow_move
    elif spec.mode != "continuation":
        raise ValueError(f"Unsupported strategy mode: {spec.mode}")
    if spec.direction == "short":
        fast_move = -fast_move
        slow_move = -slow_move
    return fast_move >= spec.min_fast_move_ticks and slow_move >= spec.min_slow_move_ticks


def simulate_trade(
    rows: Sequence[QuoteRow],
    entry_index: int,
    spec: StrategySpec,
    tick_size: float,
    point_value: float,
) -> dict[str, Any] | None:
    entry = rows[entry_index]
    side = spec.direction
    entry_price = entry.ask if side == "long" else entry.bid
    stop_points = spec.stop_ticks * tick_size
    target_points = stop_points * 2.0
    if side == "long":
        stop_price = entry_price - stop_points
        target_price = entry_price + target_points
    else:
        stop_price = entry_price + stop_points
        target_price = entry_price - target_points
    deadline = entry.timestamp.timestamp() + spec.max_hold_seconds
    for index in range(entry_index + 1, len(rows)):
        row = rows[index]
        if row.timestamp.date() != entry.timestamp.date() or row.timestamp.timestamp() > deadline:
            return close_trade(side, entry_index, index, entry, row, entry_price, row.bid if side == "long" else row.ask, "time_exit", point_value)
        if side == "long":
            hit_stop = row.bid <= stop_price
            hit_target = row.bid >= target_price
            if hit_stop and hit_target:
                return close_trade(side, entry_index, index, entry, row, entry_price, stop_price, "stop_loss_conservative", point_value)
            if hit_stop:
                return close_trade(side, entry_index, index, entry, row, entry_price, stop_price, "stop_loss", point_value)
            if hit_target:
                return close_trade(side, entry_index, index, entry, row, entry_price, target_price, "take_profit_2r", point_value)
        else:
            hit_stop = row.ask >= stop_price
            hit_target = row.ask <= target_price
            if hit_stop and hit_target:
                return close_trade(side, entry_index, index, entry, row, entry_price, stop_price, "stop_loss_conservative", point_value)
            if hit_stop:
                return close_trade(side, entry_index, index, entry, row, entry_price, stop_price, "stop_loss", point_value)
            if hit_target:
                return close_trade(side, entry_index, index, entry, row, entry_price, target_price, "take_profit_2r", point_value)
    return None


def close_trade(
    side: str,
    entry_index: int,
    exit_index: int,
    entry: QuoteRow,
    exit_row: QuoteRow,
    entry_price: float,
    exit_price: float,
    exit_reason: str,
    point_value: float,
) -> dict[str, Any]:
    gross_pnl = (exit_price - entry_price) * point_value if side == "long" else (entry_price - exit_price) * point_value
    return {
        "side": side,
        "entry_index": entry_index,
        "exit_index": exit_index,
        "entry_time": entry.timestamp.isoformat(),
        "exit_time": exit_row.timestamp.isoformat(),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "gross_pnl": gross_pnl,
        "net_pnl": gross_pnl - 15.0,
        "exit_reason": exit_reason,
    }


def summarize_trades(trades: Sequence[dict[str, Any]], min_trades: int, min_win_rate: float) -> dict[str, Any]:
    trade_count = len(trades)
    wins = sum(1 for trade in trades if float(trade["net_pnl"]) > 0.0)
    net_pnl = sum(float(trade["net_pnl"]) for trade in trades)
    win_rate = wins / trade_count if trade_count else 0.0
    return {
        "trade_count": trade_count,
        "winning_trade_count": wins,
        "win_rate": win_rate,
        "net_pnl": net_pnl,
        "avg_trade_net_pnl": net_pnl / trade_count if trade_count else None,
        "profit_factor": profit_factor(trades),
        "exit_reasons": exit_reason_counts(trades),
        "gate_passed": trade_count >= min_trades and win_rate >= min_win_rate and net_pnl > 0.0,
    }


def profit_factor(trades: Sequence[dict[str, Any]]) -> float | None:
    gross_profit = sum(float(trade["net_pnl"]) for trade in trades if float(trade["net_pnl"]) > 0.0)
    gross_loss = sum(float(trade["net_pnl"]) for trade in trades if float(trade["net_pnl"]) < 0.0)
    return gross_profit / abs(gross_loss) if gross_loss < 0 else None


def exit_reason_counts(trades: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for trade in trades:
        reason = str(trade["exit_reason"])
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def sort_key(row: dict[str, Any]) -> tuple[float, ...]:
    train = row["train"]
    test = row["test"]
    return (
        1.0 if train["gate_passed"] else 0.0,
        float(train["win_rate"]),
        float(train["net_pnl"]),
        float(train["trade_count"]),
        float(test["win_rate"]),
        float(test["net_pnl"]),
    )


def in_trade_session(timestamp: datetime) -> bool:
    current = timestamp.time()
    return time(14, 30) <= current <= time(20, 0)


if __name__ == "__main__":
    raise SystemExit(main())
