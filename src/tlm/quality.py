from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

import duckdb


@dataclass(frozen=True)
class QualityReport:
    symbol: str
    rows: int
    start_timestamp: str | None
    end_timestamp: str | None
    duplicate_timestamps: int
    avg_spread: float | None
    max_spread: float | None
    negative_spread_rows: int

    def to_dict(self) -> dict:
        return asdict(self)


def build_quality_report(symbol: str, tick_files: Sequence[Path]) -> QualityReport:
    files = [str(path) for path in tick_files if path.exists()]
    if not files:
        return QualityReport(
            symbol=symbol,
            rows=0,
            start_timestamp=None,
            end_timestamp=None,
            duplicate_timestamps=0,
            avg_spread=None,
            max_spread=None,
            negative_spread_rows=0,
        )

    con = duckdb.connect(":memory:")
    try:
        row = con.execute(
            """
            WITH ticks AS (
                SELECT * FROM read_parquet(?)
            ),
            duplicate_rows AS (
                SELECT sum(cnt - 1) AS duplicates
                FROM (
                    SELECT timestamp, count(*) AS cnt
                    FROM ticks
                    GROUP BY timestamp
                    HAVING count(*) > 1
                )
            )
            SELECT
                count(*)::INTEGER AS rows,
                min(timestamp)::VARCHAR AS start_timestamp,
                max(timestamp)::VARCHAR AS end_timestamp,
                coalesce((SELECT duplicates FROM duplicate_rows), 0)::INTEGER AS duplicate_timestamps,
                avg(spread) AS avg_spread,
                max(spread) AS max_spread,
                sum(CASE WHEN spread < 0 THEN 1 ELSE 0 END)::INTEGER AS negative_spread_rows
            FROM ticks
            """,
            [files],
        ).fetchone()
    finally:
        con.close()

    return QualityReport(
        symbol=symbol,
        rows=int(row[0]),
        start_timestamp=row[1],
        end_timestamp=row[2],
        duplicate_timestamps=int(row[3]),
        avg_spread=float(row[4]) if row[4] is not None else None,
        max_spread=float(row[5]) if row[5] is not None else None,
        negative_spread_rows=int(row[6]),
    )
