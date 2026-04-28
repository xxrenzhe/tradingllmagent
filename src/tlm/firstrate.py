from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from .storage import bar_path, write_bars_parquet


@dataclass(frozen=True)
class FirstRateBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def parse_firstrate_csv(path: Path, source_timezone: str = "UTC") -> list[FirstRateBar]:
    timezone = ZoneInfo(source_timezone)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|") if sample else csv.excel
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(handle, dialect))

    if not rows:
        return []

    header = [_normalize_header(value) for value in rows[0]]
    if _looks_like_header(header):
        return [_parse_header_row(header, row, timezone) for row in rows[1:] if _non_empty(row)]
    return [_parse_positional_row(row, timezone) for row in rows if _non_empty(row)]


def import_firstrate_bars(
    csv_paths: Sequence[Path],
    data_root: Path,
    symbol: str,
    source_timezone: str = "UTC",
    force: bool = False,
) -> list[dict]:
    bars_by_day: dict[date, list[FirstRateBar]] = defaultdict(list)
    source_files = [Path(path) for path in csv_paths]
    for path in source_files:
        for bar in parse_firstrate_csv(path, source_timezone=source_timezone):
            bars_by_day[bar.timestamp.date()].append(bar)

    outputs: list[dict] = []
    for day in sorted(bars_by_day):
        output = bar_path(data_root, symbol, "1m", day)
        if output.exists() and output.stat().st_size > 0 and not force:
            outputs.append({"day": day.isoformat(), "path": str(output), "rows": 0, "status": "skipped_existing"})
            continue
        rows = [_bar_to_storage_row(symbol, bar) for bar in sorted(bars_by_day[day], key=lambda item: item.timestamp)]
        write_bars_parquet(output, rows)
        outputs.append({"day": day.isoformat(), "path": str(output), "rows": len(rows), "status": "written"})
    return outputs


def _normalize_header(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _looks_like_header(header: Sequence[str]) -> bool:
    required = {"open", "high", "low", "close"}
    return bool(required.intersection(header)) or "timestamp" in header or "datetime" in header


def _non_empty(row: Sequence[str]) -> bool:
    return any(value.strip() for value in row)


def _parse_header_row(header: Sequence[str], row: Sequence[str], timezone: ZoneInfo) -> FirstRateBar:
    data = {name: row[index].strip() for index, name in enumerate(header) if index < len(row)}
    timestamp = _parse_timestamp(data, timezone)
    volume = data.get("volume") or data.get("vol") or "0"
    return FirstRateBar(
        timestamp=timestamp,
        open=float(data["open"]),
        high=float(data["high"]),
        low=float(data["low"]),
        close=float(data["close"]),
        volume=float(volume),
    )


def _parse_positional_row(row: Sequence[str], timezone: ZoneInfo) -> FirstRateBar:
    values = [value.strip() for value in row]
    if len(values) >= 7:
        timestamp = _localize_timestamp(_parse_datetime(f"{values[0]} {values[1]}"), timezone)
        offset = 2
    elif len(values) >= 6:
        timestamp = _localize_timestamp(_parse_datetime(values[0]), timezone)
        offset = 1
    else:
        raise ValueError(f"FirstRate row must have at least 6 columns: {row!r}")
    return FirstRateBar(
        timestamp=timestamp,
        open=float(values[offset]),
        high=float(values[offset + 1]),
        low=float(values[offset + 2]),
        close=float(values[offset + 3]),
        volume=float(values[offset + 4]),
    )


def _parse_timestamp(data: dict[str, str], timezone: ZoneInfo) -> datetime:
    for key in ("timestamp", "datetime", "date_time"):
        if data.get(key):
            return _localize_timestamp(_parse_datetime(data[key]), timezone)
    if data.get("date") and data.get("time"):
        return _localize_timestamp(_parse_datetime(f"{data['date']} {data['time']}"), timezone)
    raise ValueError("FirstRate row must include timestamp/datetime or date+time columns")


def _parse_datetime(value: str) -> datetime:
    value = value.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y%m%d %H:%M:%S",
        "%Y%m%d %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
    ):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(value)


def _localize_timestamp(value: datetime, timezone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone)
    return value.astimezone(UTC).replace(tzinfo=None)


def _bar_to_storage_row(symbol: str, bar: FirstRateBar) -> tuple:
    volume = max(int(bar.volume), 1)
    return (
        symbol,
        bar.timestamp,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.close,
        bar.close,
        volume,
        bar.volume,
        bar.volume,
        None,
    )
