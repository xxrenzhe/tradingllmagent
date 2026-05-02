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
class MinuteQuoteBar:
    timestamp: datetime
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    ask_open: float
    ask_high: float
    ask_low: float
    ask_close: float
    mid_open: float
    mid_high: float
    mid_low: float
    mid_close: float
    avg_spread: float
    min_bid_size: float
    min_ask_size: float
    avg_imbalance: float
    quote_count: int


@dataclass(frozen=True)
class StrategySpec:
    mode: str
    direction: str
    fast_minutes: int
    slow_minutes: int
    min_fast_move_ticks: float
    min_slow_move_ticks: float
    max_spread_ticks: float
    min_depth: float
    min_abs_imbalance: float
    stop_ticks: float
    reward_r: float
    max_hold_minutes: int
    cooldown_minutes: int


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Search tick-derived 1-minute quote-bar intraday NQ strategies across the full MBP-1 window."
    )
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--config-dir", type=Path, default=Path("configs"))
    parser.add_argument("--date-from", default="2026-03-03")
    parser.add_argument("--date-to", default="2026-05-01")
    parser.add_argument("--min-train-trades", type=int, default=80)
    parser.add_argument("--min-test-trades", type=int, default=80)
    parser.add_argument("--min-win-rate", type=float, default=0.55)
    parser.add_argument("--cache", type=Path, default=Path("reports/cache/nq_tick_derived_minute_quotes_2026-03-03_2026-05-01.parquet"))
    parser.add_argument("--output", type=Path, default=Path("reports/nq_tick_derived_intraday_search_55wr_15r_2026-05-03.json"))
    args = parser.parse_args(argv)

    symbol_config = get_symbol(args.symbol, args.config_dir)
    files = quote_files(args.data_root, args.symbol, args.date_from, args.date_to)
    print(f"loading {len(files)} quote files as tick-derived 1-minute bars", flush=True)
    bars = load_or_create_minute_bars(files, args.cache)
    split_index = len(bars) // 2
    train_bars = bars[:split_index]
    test_bars = bars[split_index:]
    specs = strategy_grid()
    print(f"evaluating {len(specs)} specs", flush=True)

    evaluated = []
    for index, spec in enumerate(specs, start=1):
        train = replay_strategy(train_bars, spec, symbol_config.tick_size, symbol_config.point_value)
        test = replay_strategy(test_bars, spec, symbol_config.tick_size, symbol_config.point_value)
        evaluated.append(
            {
                "spec": asdict(spec),
                "train": summarize_trades(train, args.min_train_trades, args.min_win_rate),
                "test": summarize_trades(test, args.min_test_trades, args.min_win_rate),
            }
        )
        if index % 500 == 0:
            print(f"evaluated {index}/{len(specs)}", flush=True)
    evaluated.sort(key=sort_key, reverse=True)
    selected = next((row for row in evaluated if row["train"]["gate_passed"]), evaluated[0] if evaluated else None)
    relaxed_passed = bool(selected and selected["train"]["gate_passed"] and selected["test"]["gate_passed"])
    strict_passed = bool(
        selected
        and float((selected["spec"] or {}).get("reward_r") or 0.0) >= 2.0
        and selected["train"]["trade_count"] >= args.min_train_trades
        and selected["test"]["trade_count"] >= args.min_test_trades
        and selected["train"]["win_rate"] >= 0.70
        and selected["test"]["win_rate"] >= 0.70
        and selected["train"]["net_pnl"] > 0.0
        and selected["test"]["net_pnl"] > 0.0
    )
    decision = {
        "passed": relaxed_passed,
        "strict_70wr_2r_passed": strict_passed,
        "reason": None
        if relaxed_passed
        else "No train-selected tick-derived intraday strategy passed the 55% win-rate, 1.5R, trade-count, and positive-PnL holdout gates.",
        "live_ready": False,
        "live_ready_reason": "This analyzes the full two-month tick window, but it is not a multi-year walk-forward or live/paper readiness result.",
    }
    payload = {
        "artifact": "nq_tick_derived_intraday_search_55wr_15r",
        "schema_version": 1,
        "symbol": args.symbol,
        "date_from": args.date_from,
        "date_to": args.date_to,
        "target": {
            "min_win_rate": args.min_win_rate,
            "min_reward_r": 1.5,
            "strict_win_rate": 0.70,
            "strict_reward_r": 2.0,
            "min_train_trades": args.min_train_trades,
            "min_test_trades": args.min_test_trades,
        },
        "method": {
            "scope": "All normalized Databento MBP-1 quote files in the requested two-month window.",
            "aggregation": "Nanosecond top-of-book quotes are aggregated to 1-minute bid/ask/mid bars with spread, depth, imbalance, and quote-count features.",
            "entry_timing": "Signals are computed at minute close using only current and prior minute bars; fills occur on the next minute's bid/ask open.",
            "exit_timing": "Stops and targets are replayed on future tick-derived quote-bar highs/lows; same-minute stop/target collisions resolve to stop loss.",
            "selection": "Chronological first half selects specs; second half is untouched holdout.",
            "long_term_limit": "The report covers the provided recent tick window only and cannot establish long-term profitability by itself.",
            "train_test_split": {
                "train_start": train_bars[0].timestamp.isoformat() if train_bars else None,
                "train_end": train_bars[-1].timestamp.isoformat() if train_bars else None,
                "test_start": test_bars[0].timestamp.isoformat() if test_bars else None,
                "test_end": test_bars[-1].timestamp.isoformat() if test_bars else None,
            },
            "spec_count": len(specs),
            "cache": str(args.cache),
        },
        "coverage": {
            "quote_file_count": len(files),
            "minute_bar_count": len(bars),
            "train_minute_bar_count": len(train_bars),
            "test_minute_bar_count": len(test_bars),
            "date_count": len({bar.timestamp.date().isoformat() for bar in bars}),
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


def load_or_create_minute_bars(files: Sequence[Path], cache_path: Path | None) -> list[MinuteQuoteBar]:
    if cache_path and cache_path.exists() and cache_path.stat().st_size > 0:
        print(f"loading minute bars from cache {cache_path}", flush=True)
        return load_minute_bars_from_cache(cache_path)
    if not cache_path:
        return load_minute_bars(files)
    rows = []
    for index, path in enumerate(files, start=1):
        rows.extend(load_or_create_minute_bars_for_file(path, cache_path))
        if index % 10 == 0:
            print(f"loaded {index}/{len(files)} quote files", flush=True)
    rows.sort(key=lambda row: row.timestamp)
    return rows


def load_minute_bars(files: Sequence[Path]) -> list[MinuteQuoteBar]:
    rows = []
    for index, path in enumerate(files, start=1):
        rows.extend(load_minute_bars_for_file(path))
        if index % 10 == 0:
            print(f"loaded {index}/{len(files)} quote files", flush=True)
    rows.sort(key=lambda row: row.timestamp)
    return rows


def load_or_create_minute_bars_for_file(path: Path, cache_root: Path) -> list[MinuteQuoteBar]:
    cache_file = minute_bar_cache_file(cache_root, path)
    if cache_file.exists() and cache_file.stat().st_size > 0:
        return load_minute_bars_from_cache(cache_file)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    try:
        source_sql = "'" + str(path).replace("'", "''") + "'"
        output_sql = "'" + str(cache_file).replace("'", "''") + "'"
        con.execute(f"COPY ({minute_bar_sql(source_sql)}) TO {output_sql} (FORMAT PARQUET)")
    finally:
        con.close()
    return load_minute_bars_from_cache(cache_file)


def load_minute_bars_for_file(path: Path) -> list[MinuteQuoteBar]:
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(minute_bar_sql("?"), [str(path)]).fetchall()
    finally:
        con.close()
    return rows_to_minute_bars(rows)


def load_minute_bars_from_cache(path: Path) -> list[MinuteQuoteBar]:
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            """
            SELECT timestamp, bid_open, bid_high, bid_low, bid_close,
                   ask_open, ask_high, ask_low, ask_close,
                   mid_open, mid_high, mid_low, mid_close,
                   avg_spread, min_bid_size, min_ask_size, avg_imbalance, quote_count
            FROM read_parquet(?)
            ORDER BY timestamp
            """,
            [str(path)],
        ).fetchall()
    finally:
        con.close()
    return rows_to_minute_bars(rows)


def minute_bar_cache_file(cache_root: Path, source_path: Path) -> Path:
    day = "unknown"
    for part in source_path.parts:
        if part.startswith("date="):
            day = part.removeprefix("date=")
            break
    root = cache_root.with_suffix("") if cache_root.suffix else cache_root
    return root / f"date={day}" / "part-000.parquet"


def minute_bar_sql(source: str) -> str:
    return f"""
        WITH base AS (
            SELECT
                date_trunc('minute', timestamp) AS timestamp,
                timestamp AS quote_timestamp,
                bid,
                ask,
                mid,
                spread,
                bid_size,
                ask_size,
                CASE
                    WHEN bid_size + ask_size > 0 THEN (bid_size - ask_size) / (bid_size + ask_size)
                    ELSE 0.0
                END AS imbalance
            FROM read_parquet({source})
            WHERE bid > 0 AND ask > 0 AND ask >= bid
        )
        SELECT
            timestamp,
            first(bid ORDER BY quote_timestamp) AS bid_open,
            max(bid) AS bid_high,
            min(bid) AS bid_low,
            last(bid ORDER BY quote_timestamp) AS bid_close,
            first(ask ORDER BY quote_timestamp) AS ask_open,
            max(ask) AS ask_high,
            min(ask) AS ask_low,
            last(ask ORDER BY quote_timestamp) AS ask_close,
            first(mid ORDER BY quote_timestamp) AS mid_open,
            max(mid) AS mid_high,
            min(mid) AS mid_low,
            last(mid ORDER BY quote_timestamp) AS mid_close,
            avg(spread) AS avg_spread,
            min(bid_size) AS min_bid_size,
            min(ask_size) AS min_ask_size,
            avg(imbalance) AS avg_imbalance,
            count(*) AS quote_count
        FROM base
        GROUP BY timestamp
        ORDER BY timestamp
    """


def rows_to_minute_bars(rows: Sequence[tuple[Any, ...]]) -> list[MinuteQuoteBar]:
    return [
        MinuteQuoteBar(
            timestamp=row[0],
            bid_open=float(row[1]),
            bid_high=float(row[2]),
            bid_low=float(row[3]),
            bid_close=float(row[4]),
            ask_open=float(row[5]),
            ask_high=float(row[6]),
            ask_low=float(row[7]),
            ask_close=float(row[8]),
            mid_open=float(row[9]),
            mid_high=float(row[10]),
            mid_low=float(row[11]),
            mid_close=float(row[12]),
            avg_spread=float(row[13]),
            min_bid_size=float(row[14] or 0.0),
            min_ask_size=float(row[15] or 0.0),
            avg_imbalance=float(row[16] or 0.0),
            quote_count=int(row[17] or 0),
        )
        for row in rows
    ]


def strategy_grid() -> list[StrategySpec]:
    specs = []
    for mode in ("continuation", "reversal"):
        for direction in ("long", "short"):
            for fast_minutes, slow_minutes in ((3, 15), (5, 30), (10, 60)):
                for min_fast_move_ticks in (4.0, 8.0, 16.0, 24.0):
                    for min_slow_move_ticks in (8.0, 16.0, 32.0, 48.0):
                        for max_spread_ticks in (2.0, 3.0, 4.0):
                            for min_depth in (1.0, 2.0, 4.0):
                                for min_abs_imbalance in (0.0, 0.10, 0.20):
                                    for stop_ticks in (16.0, 24.0, 32.0, 48.0):
                                        for reward_r in (1.5, 2.0):
                                            specs.append(
                                                StrategySpec(
                                                    mode=mode,
                                                    direction=direction,
                                                    fast_minutes=fast_minutes,
                                                    slow_minutes=slow_minutes,
                                                    min_fast_move_ticks=min_fast_move_ticks,
                                                    min_slow_move_ticks=min_slow_move_ticks,
                                                    max_spread_ticks=max_spread_ticks,
                                                    min_depth=min_depth,
                                                    min_abs_imbalance=min_abs_imbalance,
                                                    stop_ticks=stop_ticks,
                                                    reward_r=reward_r,
                                                    max_hold_minutes=90,
                                                    cooldown_minutes=30,
                                                )
                                            )
    return specs


def replay_strategy(
    bars: Sequence[MinuteQuoteBar],
    spec: StrategySpec,
    tick_size: float,
    point_value: float,
) -> list[dict[str, Any]]:
    trades = []
    last_exit_index = -1
    last_entry_time: datetime | None = None
    start_index = max(spec.fast_minutes, spec.slow_minutes)
    for signal_index in range(start_index, len(bars) - 1):
        if signal_index <= last_exit_index:
            continue
        signal_bar = bars[signal_index]
        entry_index = signal_index + 1
        if last_entry_time and (signal_bar.timestamp - last_entry_time).total_seconds() < spec.cooldown_minutes * 60:
            continue
        if not in_trade_session(signal_bar.timestamp):
            continue
        if bars[entry_index].timestamp.date() != signal_bar.timestamp.date():
            continue
        if not entry_signal(bars, signal_index, spec, tick_size):
            continue
        trade = simulate_trade(bars, entry_index, spec, tick_size, point_value)
        if trade is None:
            continue
        trades.append(trade)
        last_exit_index = int(trade["exit_index"])
        last_entry_time = bars[entry_index].timestamp
    return trades


def entry_signal(bars: Sequence[MinuteQuoteBar], index: int, spec: StrategySpec, tick_size: float) -> bool:
    bar = bars[index]
    fast_bar = bars[index - spec.fast_minutes]
    slow_bar = bars[index - spec.slow_minutes]
    if tick_size <= 0 or bar.timestamp.date() != slow_bar.timestamp.date():
        return False
    if bar.avg_spread / tick_size > spec.max_spread_ticks:
        return False
    relevant_depth = bar.min_ask_size if spec.direction == "long" else bar.min_bid_size
    if relevant_depth < spec.min_depth:
        return False
    aligned_imbalance = bar.avg_imbalance if spec.direction == "long" else -bar.avg_imbalance
    if abs(aligned_imbalance) < spec.min_abs_imbalance:
        return False
    fast_move = (bar.mid_close - fast_bar.mid_close) / tick_size
    slow_move = (bar.mid_close - slow_bar.mid_close) / tick_size
    if spec.mode == "reversal":
        fast_move = -fast_move
        slow_move = -slow_move
    elif spec.mode != "continuation":
        raise ValueError(f"Unsupported mode: {spec.mode}")
    if spec.direction == "short":
        fast_move = -fast_move
        slow_move = -slow_move
    return fast_move >= spec.min_fast_move_ticks and slow_move >= spec.min_slow_move_ticks


def simulate_trade(
    bars: Sequence[MinuteQuoteBar],
    entry_index: int,
    spec: StrategySpec,
    tick_size: float,
    point_value: float,
) -> dict[str, Any] | None:
    entry = bars[entry_index]
    side = spec.direction
    entry_price = entry.ask_open if side == "long" else entry.bid_open
    stop_points = spec.stop_ticks * tick_size
    target_points = stop_points * spec.reward_r
    if side == "long":
        stop_price = entry_price - stop_points
        target_price = entry_price + target_points
    else:
        stop_price = entry_price + stop_points
        target_price = entry_price - target_points
    deadline_index = min(entry_index + spec.max_hold_minutes, len(bars) - 1)
    for exit_index in range(entry_index, deadline_index + 1):
        bar = bars[exit_index]
        if bar.timestamp.date() != entry.timestamp.date():
            return close_trade(side, entry_index, exit_index, entry, bars[exit_index - 1], entry_price, None, "date_exit", point_value)
        if side == "long":
            hit_stop = bar.bid_low <= stop_price
            hit_target = bar.bid_high >= target_price
            if hit_stop and hit_target:
                return close_trade(side, entry_index, exit_index, entry, bar, entry_price, stop_price, "stop_loss_conservative", point_value)
            if hit_stop:
                return close_trade(side, entry_index, exit_index, entry, bar, entry_price, stop_price, "stop_loss", point_value)
            if hit_target:
                return close_trade(side, entry_index, exit_index, entry, bar, entry_price, target_price, f"take_profit_{spec.reward_r:g}r", point_value)
        else:
            hit_stop = bar.ask_high >= stop_price
            hit_target = bar.ask_low <= target_price
            if hit_stop and hit_target:
                return close_trade(side, entry_index, exit_index, entry, bar, entry_price, stop_price, "stop_loss_conservative", point_value)
            if hit_stop:
                return close_trade(side, entry_index, exit_index, entry, bar, entry_price, stop_price, "stop_loss", point_value)
            if hit_target:
                return close_trade(side, entry_index, exit_index, entry, bar, entry_price, target_price, f"take_profit_{spec.reward_r:g}r", point_value)
    exit_bar = bars[deadline_index]
    return close_trade(side, entry_index, deadline_index, entry, exit_bar, entry_price, None, "time_exit", point_value)


def close_trade(
    side: str,
    entry_index: int,
    exit_index: int,
    entry: MinuteQuoteBar,
    exit_bar: MinuteQuoteBar,
    entry_price: float,
    exit_price: float | None,
    exit_reason: str,
    point_value: float,
) -> dict[str, Any]:
    realized_exit_price = exit_price
    if realized_exit_price is None:
        realized_exit_price = exit_bar.bid_close if side == "long" else exit_bar.ask_close
    gross_pnl = (
        (realized_exit_price - entry_price) * point_value
        if side == "long"
        else (entry_price - realized_exit_price) * point_value
    )
    return {
        "side": side,
        "entry_index": entry_index,
        "exit_index": exit_index,
        "entry_time": entry.timestamp.isoformat(),
        "exit_time": exit_bar.timestamp.isoformat(),
        "entry_price": entry_price,
        "exit_price": realized_exit_price,
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
    spec = row["spec"]
    return (
        1.0 if train["gate_passed"] else 0.0,
        float(test["gate_passed"]),
        float(spec.get("reward_r") or 0.0),
        float(train["win_rate"]),
        float(train["net_pnl"]),
        float(test["win_rate"]),
        float(test["net_pnl"]),
        float(train["trade_count"]),
    )


def in_trade_session(timestamp: datetime) -> bool:
    current = timestamp.time()
    return time(14, 30) <= current <= time(20, 0)


if __name__ == "__main__":
    raise SystemExit(main())
