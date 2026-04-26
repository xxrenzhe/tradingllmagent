from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

import duckdb


@dataclass(frozen=True)
class QualityReport:
    symbol: str
    expected_files: int
    files: int
    coverage_ratio: float
    status: str
    quality_flags: list[str]
    missing_files: list[str]
    zero_row_files: list[str]
    rows: int
    start_timestamp: str | None
    end_timestamp: str | None
    duplicate_timestamps: int
    avg_spread: float | None
    max_spread: float | None
    negative_spread_rows: int
    large_spread_rows: int
    price_jump_rows: int

    def to_dict(self) -> dict:
        return asdict(self)


def build_quality_report(
    symbol: str,
    tick_files: Sequence[Path],
    max_normal_spread: float = 10.0,
    max_normal_price_jump: float = 100.0,
) -> QualityReport:
    missing_files = [str(path) for path in tick_files if not path.exists()]
    existing_paths = [path for path in tick_files if path.exists()]
    expected_files = len(tick_files)
    files = [str(path) for path in existing_paths]
    zero_row_files = [
        str(path)
        for path in existing_paths
        if _parquet_row_count(path) == 0
    ]
    coverage_ratio = len(existing_paths) / expected_files if expected_files else 1.0
    if not files:
        quality_flags = _quality_flags(
            missing_files=missing_files,
            zero_row_files=[],
            negative_spread_rows=0,
            large_spread_rows=0,
            price_jump_rows=0,
        )
        return QualityReport(
            symbol=symbol,
            expected_files=expected_files,
            files=0,
            coverage_ratio=coverage_ratio,
            status=_quality_status(quality_flags),
            quality_flags=quality_flags,
            missing_files=missing_files,
            zero_row_files=[],
            rows=0,
            start_timestamp=None,
            end_timestamp=None,
            duplicate_timestamps=0,
            avg_spread=None,
            max_spread=None,
            negative_spread_rows=0,
            large_spread_rows=0,
            price_jump_rows=0,
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
            ),
            ordered_ticks AS (
                SELECT
                    timestamp,
                    mid,
                    lag(mid) OVER (ORDER BY timestamp) AS previous_mid
                FROM ticks
            )
            SELECT
                count(*)::INTEGER AS rows,
                min(timestamp)::VARCHAR AS start_timestamp,
                max(timestamp)::VARCHAR AS end_timestamp,
                coalesce((SELECT duplicates FROM duplicate_rows), 0)::INTEGER AS duplicate_timestamps,
                avg(spread) AS avg_spread,
                max(spread) AS max_spread,
                coalesce(sum(CASE WHEN spread < 0 THEN 1 ELSE 0 END), 0)::INTEGER AS negative_spread_rows,
                coalesce(sum(CASE WHEN spread > ? THEN 1 ELSE 0 END), 0)::INTEGER AS large_spread_rows,
                (
                    SELECT coalesce(sum(
                        CASE
                            WHEN previous_mid IS NOT NULL
                                 AND abs(mid - previous_mid) > ?
                            THEN 1 ELSE 0
                        END
                    ), 0)::INTEGER
                    FROM ordered_ticks
                ) AS price_jump_rows
            FROM ticks
            """,
            [files, max_normal_spread, max_normal_price_jump],
        ).fetchone()
    finally:
        con.close()

    quality_flags = _quality_flags(
        missing_files=missing_files,
        zero_row_files=zero_row_files,
        negative_spread_rows=int(row[6]),
        large_spread_rows=int(row[7]),
        price_jump_rows=int(row[8]),
    )
    return QualityReport(
        symbol=symbol,
        expected_files=expected_files,
        files=len(files),
        coverage_ratio=coverage_ratio,
        status=_quality_status(quality_flags),
        quality_flags=quality_flags,
        missing_files=missing_files,
        zero_row_files=zero_row_files,
        rows=int(row[0]),
        start_timestamp=row[1],
        end_timestamp=row[2],
        duplicate_timestamps=int(row[3]),
        avg_spread=float(row[4]) if row[4] is not None else None,
        max_spread=float(row[5]) if row[5] is not None else None,
        negative_spread_rows=int(row[6]),
        large_spread_rows=int(row[7]),
        price_jump_rows=int(row[8]),
    )


def _quality_flags(
    missing_files: Sequence[str],
    zero_row_files: Sequence[str],
    negative_spread_rows: int,
    large_spread_rows: int,
    price_jump_rows: int,
) -> list[str]:
    flags = []
    if missing_files:
        flags.append("missing_partitions")
    if zero_row_files:
        flags.append("empty_partitions")
    if negative_spread_rows:
        flags.append("negative_spread")
    if large_spread_rows:
        flags.append("large_spread")
    if price_jump_rows:
        flags.append("price_jumps")
    return flags


def _quality_status(flags: Sequence[str]) -> str:
    return "gaps_or_anomalies" if flags else "ok"


def _parquet_row_count(path: Path) -> int:
    con = duckdb.connect(":memory:")
    try:
        return int(con.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()[0])
    finally:
        con.close()
