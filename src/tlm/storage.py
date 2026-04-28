from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

import duckdb

from .dukascopy import Tick, tick_to_row


TICK_COLUMNS = [
    "symbol",
    "timestamp",
    "bid",
    "ask",
    "bid_size",
    "ask_size",
    "mid",
    "spread",
]


def normalized_tick_path(data_root: Path, symbol: str, day: date) -> Path:
    return data_root / "normalized" / "ticks" / symbol / f"date={day.isoformat()}" / "part-000.parquet"


def bar_path(data_root: Path, symbol: str, timeframe: str, day: date) -> Path:
    return data_root / "bars" / timeframe / symbol / f"date={day.isoformat()}" / "part-000.parquet"


def quality_path(data_root: Path, symbol: str, date_from: str, date_to: str) -> Path:
    return data_root / "quality" / symbol / f"{date_from}_{date_to}.json"


def event_context_path(data_root: Path, symbol: str, day: date) -> Path:
    return data_root / "events" / "context" / symbol / f"date={day.isoformat()}" / "part-000.parquet"


def _write_parquet(
    path: Path,
    table_name: str,
    schema_sql: str,
    insert_sql: str,
    rows: Sequence[tuple],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"CREATE TABLE {table_name} ({schema_sql})")
        if rows:
            con.executemany(insert_sql, rows)
        con.execute(f"COPY {table_name} TO ? (FORMAT PARQUET)", [str(temp_path)])
        os.replace(temp_path, path)
    finally:
        con.close()
        temp_path.unlink(missing_ok=True)


def write_ticks_parquet(path: Path, symbol_alias: str, ticks: Sequence[Tick]) -> None:
    rows = [tick_to_row(symbol_alias, tick) for tick in ticks]
    _write_parquet(
        path=path,
        table_name="ticks",
        schema_sql=(
            "symbol VARCHAR, timestamp TIMESTAMP, bid DOUBLE, ask DOUBLE, "
            "bid_size DOUBLE, ask_size DOUBLE, mid DOUBLE, spread DOUBLE"
        ),
        insert_sql="INSERT INTO ticks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows=rows,
    )


def write_bars_parquet(path: Path, rows: Sequence[tuple]) -> None:
    _write_parquet(
        path=path,
        table_name="bars",
        schema_sql=(
            "symbol VARCHAR, timestamp TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, "
            "close DOUBLE, bid_close DOUBLE, ask_close DOUBLE, tick_count INTEGER, "
            "bid_size_sum DOUBLE, ask_size_sum DOUBLE, avg_spread DOUBLE"
        ),
        insert_sql="INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows=rows,
    )


def parquet_files(paths: Iterable[Path]) -> list[str]:
    return [str(path) for path in paths if path.exists()]


def compute_data_version_hash(paths: Iterable[Path], metadata: dict) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        if not path.exists():
            continue
        stat = path.stat()
        digest.update(str(path).encode())
        digest.update(str(stat.st_size).encode())
        digest.update(str(int(stat.st_mtime)).encode())
    digest.update(json.dumps(metadata, sort_keys=True).encode())
    return digest.hexdigest()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
