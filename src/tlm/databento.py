from __future__ import annotations

import csv
import io
import re
import subprocess
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, Sequence, TextIO

import duckdb

from .storage import bar_path


OUTRIGHT_NQ_PATTERN = re.compile(r"^NQ[HMUZ][0-9]$")


@dataclass(frozen=True)
class DatabentoOhlcvBar:
    timestamp: datetime
    instrument_id: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    raw_symbol: str


def import_databento_ohlcv_bars(
    csv_paths: Sequence[Path],
    data_root: Path,
    symbol: str,
    force: bool = False,
    zip_member: str | None = None,
) -> list[dict]:
    source_files = [Path(path) for path in csv_paths]
    with tempfile.TemporaryDirectory(prefix="tlm_databento_") as temp_dir:
        readable_sources = _prepare_duckdb_sources(source_files, Path(temp_dir), zip_member=zip_member)
        return _import_databento_ohlcv_with_duckdb(readable_sources, data_root, symbol, force)


def _import_databento_ohlcv_with_duckdb(
    csv_paths: Sequence[Path],
    data_root: Path,
    symbol: str,
    force: bool,
) -> list[dict]:
    con = duckdb.connect(":memory:")
    try:
        source_sql = _duckdb_source_literal(csv_paths)
        con.execute(
            f"""
            CREATE TEMP TABLE source AS
            SELECT
                STRPTIME(LEFT(CAST(ts_event AS VARCHAR), 19), '%Y-%m-%dT%H:%M:%S') AS timestamp,
                CAST(STRPTIME(LEFT(CAST(ts_event AS VARCHAR), 19), '%Y-%m-%dT%H:%M:%S') AS DATE) AS day,
                CAST(instrument_id AS VARCHAR) AS instrument_id,
                CAST(open AS DOUBLE) AS open,
                CAST(high AS DOUBLE) AS high,
                CAST(low AS DOUBLE) AS low,
                CAST(close AS DOUBLE) AS close,
                CAST(volume AS BIGINT) AS volume,
                CAST(symbol AS VARCHAR) AS raw_symbol
            FROM read_csv(
                {source_sql},
                header=true,
                union_by_name=true,
                columns={{
                    'ts_event': 'VARCHAR',
                    'rtype': 'VARCHAR',
                    'publisher_id': 'VARCHAR',
                    'instrument_id': 'VARCHAR',
                    'open': 'DOUBLE',
                    'high': 'DOUBLE',
                    'low': 'DOUBLE',
                    'close': 'DOUBLE',
                    'volume': 'BIGINT',
                    'symbol': 'VARCHAR'
                }}
            )
            WHERE regexp_matches(CAST(symbol AS VARCHAR), '^NQ[HMUZ][0-9]$')
            """
        )
        _validate_duckdb_source(con)
        con.execute(
            """
            CREATE TEMP TABLE daily_contract AS
            SELECT day, instrument_id
            FROM (
                SELECT
                    day,
                    instrument_id,
                    SUM(volume) AS total_volume,
                    ROW_NUMBER() OVER (
                        PARTITION BY day
                        ORDER BY SUM(volume) DESC, instrument_id ASC
                    ) AS rank
                FROM source
                GROUP BY day, instrument_id
            )
            WHERE rank = 1
            """
        )
        con.execute(
            """
            CREATE TEMP TABLE selected AS
            SELECT
                source.day,
                source.timestamp,
                source.instrument_id,
                source.raw_symbol,
                source.open,
                source.high,
                source.low,
                source.close,
                source.volume
            FROM source
            JOIN daily_contract
              ON source.day = daily_contract.day
             AND source.instrument_id = daily_contract.instrument_id
            """
        )
        duplicate_count = con.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT day, timestamp, COUNT(*) AS rows
                FROM selected
                GROUP BY day, timestamp
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        if duplicate_count:
            raise ValueError(f"Selected Databento OHLCV data contains duplicate minute rows: {duplicate_count}")

        days = con.execute(
            """
            SELECT day, instrument_id, ANY_VALUE(raw_symbol) AS raw_symbol, COUNT(*) AS rows
            FROM selected
            GROUP BY day, instrument_id
            ORDER BY day
            """
        ).fetchall()
        outputs: list[dict] = []
        for day_value, instrument_id, raw_symbol, row_count in days:
            output = bar_path(data_root, symbol, "1m", day_value)
            if output.exists() and output.stat().st_size > 0 and not force:
                outputs.append(
                    {
                        "day": day_value.isoformat(),
                        "path": str(output),
                        "rows": 0,
                        "status": "skipped_existing",
                        "instrument_id": instrument_id,
                        "raw_symbol": raw_symbol,
                    }
                )
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            output_sql = _sql_literal(str(output))
            symbol_sql = _sql_literal(symbol)
            day_sql = _sql_literal(day_value.isoformat())
            con.execute(
                f"""
                COPY (
                    SELECT
                        {symbol_sql} AS symbol,
                        timestamp,
                        open,
                        high,
                        low,
                        close,
                        close AS bid_close,
                        close AS ask_close,
                        CAST(volume AS INTEGER) AS tick_count,
                        CAST(volume AS DOUBLE) AS bid_size_sum,
                        CAST(volume AS DOUBLE) AS ask_size_sum,
                        NULL::DOUBLE AS avg_spread
                    FROM selected
                    WHERE day = CAST({day_sql} AS DATE)
                    ORDER BY timestamp
                ) TO {output_sql} (FORMAT PARQUET)
                """
            )
            outputs.append(
                {
                    "day": day_value.isoformat(),
                    "path": str(output),
                    "rows": row_count,
                    "status": "written",
                    "instrument_id": instrument_id,
                    "raw_symbol": raw_symbol,
                }
            )
        return outputs
    finally:
        con.close()


def _validate_duckdb_source(con: duckdb.DuckDBPyConnection) -> None:
    bad_ohlc = con.execute(
        """
        SELECT COUNT(*)
        FROM source
        WHERE high < GREATEST(open, low, close)
           OR low > LEAST(open, high, close)
        """
    ).fetchone()[0]
    if bad_ohlc:
        raise ValueError(f"Databento OHLCV source contains invalid OHLC rows: {bad_ohlc}")
    bad_volume = con.execute("SELECT COUNT(*) FROM source WHERE volume <= 0").fetchone()[0]
    if bad_volume:
        raise ValueError(f"Databento OHLCV source contains non-positive volume rows: {bad_volume}")
    bad_tick = con.execute(
        """
        SELECT COUNT(*)
        FROM source
        WHERE ABS(open * 4 - ROUND(open * 4)) > 0.000001
           OR ABS(high * 4 - ROUND(high * 4)) > 0.000001
           OR ABS(low * 4 - ROUND(low * 4)) > 0.000001
           OR ABS(close * 4 - ROUND(close * 4)) > 0.000001
        """
    ).fetchone()[0]
    if bad_tick:
        raise ValueError(f"Databento OHLCV source contains off-grid NQ prices: {bad_tick}")


def _prepare_duckdb_sources(paths: Sequence[Path], temp_dir: Path, zip_member: str | None = None) -> list[Path]:
    sources: list[Path] = []
    for path in paths:
        if path.suffix == ".zip":
            member = zip_member or _find_ohlcv_member(path)
            with zipfile.ZipFile(path) as archive:
                output = temp_dir / Path(member).name
                with archive.open(member) as source, output.open("wb") as target:
                    target.write(source.read())
                sources.append(output)
        else:
            sources.append(path)
    return sources


def _duckdb_source_literal(paths: Sequence[Path]) -> str:
    values = [str(path).replace("'", "''") for path in paths]
    if len(values) == 1:
        return f"'{values[0]}'"
    return "[" + ", ".join(f"'{value}'" for value in values) + "]"


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def iter_databento_ohlcv_csv(path: Path, zip_member: str | None = None) -> Iterator[DatabentoOhlcvBar]:
    with _open_databento_text(path, zip_member=zip_member) as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw_symbol = (row.get("symbol") or "").strip()
            if not OUTRIGHT_NQ_PATTERN.match(raw_symbol):
                continue
            bar = _parse_ohlcv_row(row, raw_symbol)
            _validate_bar(bar)
            yield bar


def _parse_ohlcv_row(row: dict[str, str], raw_symbol: str) -> DatabentoOhlcvBar:
    timestamp = _parse_timestamp(_required(row, "ts_event"))
    return DatabentoOhlcvBar(
        timestamp=timestamp,
        instrument_id=_required(row, "instrument_id"),
        open=float(_required(row, "open")),
        high=float(_required(row, "high")),
        low=float(_required(row, "low")),
        close=float(_required(row, "close")),
        volume=int(float(_required(row, "volume"))),
        raw_symbol=raw_symbol,
    )


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed.replace(tzinfo=None)


def _validate_bar(bar: DatabentoOhlcvBar) -> None:
    if bar.high < max(bar.open, bar.low, bar.close) or bar.low > min(bar.open, bar.high, bar.close):
        raise ValueError(f"Invalid Databento OHLC row at {bar.timestamp}: {bar!r}")
    if bar.volume <= 0:
        raise ValueError(f"Invalid Databento volume row at {bar.timestamp}: {bar!r}")
    for value in (bar.open, bar.high, bar.low, bar.close):
        if abs((value * 4) - round(value * 4)) > 0.000001:
            raise ValueError(f"Databento NQ price is not on 0.25 tick grid at {bar.timestamp}: {bar!r}")


def _bar_to_storage_row(symbol: str, bar: DatabentoOhlcvBar) -> tuple:
    return (
        symbol,
        bar.timestamp,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.close,
        bar.close,
        bar.volume,
        float(bar.volume),
        float(bar.volume),
        None,
    )


def _required(row: dict[str, str], key: str) -> str:
    value = row.get(key)
    if value is None or value == "":
        raise ValueError(f"Databento OHLCV row missing required column {key!r}: {row!r}")
    return value


@contextmanager
def _open_databento_text(path: Path, zip_member: str | None = None) -> Iterator[TextIO]:
    if path.suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            yield handle
        return

    command = _decompression_command(path, zip_member=zip_member)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, text=False)
    if process.stdout is None:
        raise RuntimeError(f"Failed to open Databento source: {path}")
    wrapper = io.TextIOWrapper(process.stdout, encoding="utf-8-sig", newline="")
    try:
        yield wrapper
    finally:
        wrapper.close()
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(f"Databento source decompression failed with code {return_code}: {path}")


def _decompression_command(path: Path, zip_member: str | None = None) -> list[str]:
    if path.suffix == ".zst":
        return ["zstdcat", str(path)]
    if path.suffix == ".zip":
        member = zip_member or _find_ohlcv_member(path)
        if member.endswith(".zst"):
            return ["sh", "-c", "unzip -p \"$1\" \"$2\" | zstdcat", "sh", str(path), member]
        return ["unzip", "-p", str(path), member]
    raise ValueError(f"Unsupported Databento OHLCV input format: {path}")


def _find_ohlcv_member(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        matches = [
            name
            for name in archive.namelist()
            if name.endswith(".ohlcv-1m.csv.zst") or name.endswith(".ohlcv-1m.csv")
        ]
    if not matches:
        raise ValueError(f"No Databento ohlcv-1m CSV member found in {path}")
    if len(matches) > 1:
        raise ValueError(f"Multiple Databento ohlcv-1m members found in {path}; pass --member")
    return matches[0]
