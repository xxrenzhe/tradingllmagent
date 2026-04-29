from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Sequence

import duckdb

from .dukascopy import Tick
from .storage import write_bars_parquet


def timeframe_minutes(timeframe: str) -> int:
    if not timeframe.endswith("m"):
        raise ValueError("Only minute timeframes such as 1m, 5m, 15m, and 30m are supported")
    try:
        minutes = int(timeframe[:-1])
    except ValueError as exc:
        raise ValueError(f"Invalid timeframe: {timeframe}") from exc
    if minutes <= 0 or 60 % minutes != 0:
        raise ValueError("Timeframe minutes must be a positive divisor of 60")
    return minutes


def minute_bucket(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def build_minute_bars_from_ticks(symbol: str, ticks: Sequence[Tick]) -> list[tuple]:
    buckets: dict[datetime, list[Tick]] = defaultdict(list)
    for tick in ticks:
        buckets[minute_bucket(tick.timestamp)].append(tick)

    rows: list[tuple] = []
    for timestamp in sorted(buckets):
        bucket_ticks = sorted(buckets[timestamp], key=lambda item: item.timestamp)
        mids = [tick.mid for tick in bucket_ticks]
        rows.append(
            (
                symbol,
                timestamp,
                mids[0],
                max(mids),
                min(mids),
                mids[-1],
                bucket_ticks[-1].bid,
                bucket_ticks[-1].ask,
                len(bucket_ticks),
                sum(tick.bid_size for tick in bucket_ticks),
                sum(tick.ask_size for tick in bucket_ticks),
                sum(tick.spread for tick in bucket_ticks) / len(bucket_ticks),
            )
        )
    return rows


def build_minute_bars_from_parquet(
    tick_files: Sequence[Path],
    output_path: Path,
) -> int:
    files = [str(path) for path in tick_files if path.exists()]
    if not files:
        write_bars_parquet(output_path, [])
        return 0

    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            """
            SELECT
                symbol,
                date_trunc('minute', timestamp) AS bar_ts,
                first(mid ORDER BY timestamp) AS open,
                max(mid) AS high,
                min(mid) AS low,
                last(mid ORDER BY timestamp) AS close,
                last(bid ORDER BY timestamp) AS bid_close,
                last(ask ORDER BY timestamp) AS ask_close,
                count(*)::INTEGER AS tick_count,
                sum(bid_size) AS bid_size_sum,
                sum(ask_size) AS ask_size_sum,
                avg(spread) AS avg_spread
            FROM read_parquet(?)
            GROUP BY symbol, bar_ts
            ORDER BY bar_ts
            """,
            [files],
        ).fetchall()
    finally:
        con.close()

    write_bars_parquet(output_path, rows)
    return len(rows)


def build_timeframe_bars_from_1m_parquet(
    bar_files: Sequence[Path],
    output_path: Path,
    timeframe: str,
) -> int:
    minutes = timeframe_minutes(timeframe)
    if minutes == 1:
        raise ValueError("Use build_minute_bars_from_parquet for 1m bars")
    files = [str(path) for path in bar_files if path.exists()]
    if not files:
        write_bars_parquet(output_path, [])
        return 0

    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            f"""
            SELECT
                symbol,
                time_bucket(INTERVAL '{minutes} minutes', timestamp) AS bar_ts,
                first(open ORDER BY timestamp) AS open,
                max(high) AS high,
                min(low) AS low,
                last(close ORDER BY timestamp) AS close,
                last(bid_close ORDER BY timestamp) AS bid_close,
                last(ask_close ORDER BY timestamp) AS ask_close,
                sum(tick_count)::INTEGER AS tick_count,
                sum(bid_size_sum) AS bid_size_sum,
                sum(ask_size_sum) AS ask_size_sum,
                sum(avg_spread * tick_count) / nullif(sum(tick_count), 0) AS avg_spread
            FROM read_parquet(?)
            GROUP BY symbol, bar_ts
            ORDER BY bar_ts
            """,
            [files],
        ).fetchall()
    finally:
        con.close()

    write_bars_parquet(output_path, rows)
    return len(rows)
