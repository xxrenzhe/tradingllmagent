#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status-dir", default="logs/data-fetch")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQmain")
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    return parser.parse_args()


def iter_days(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def normalized_tick_path(data_root: Path, symbol: str, day: date) -> Path:
    return data_root / "normalized" / "ticks" / symbol / f"date={day.isoformat()}" / "part-000.parquet"


def load_latest_statuses(status_dir: Path, symbol: str) -> dict[str, dict[str, str]]:
    by_day: dict[str, list[dict[str, str]]] = defaultdict(list)
    for path in sorted(status_dir.glob(f"dukascopy-{symbol}-shard-*.status.tsv")):
        with path.open("r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            for row in reader:
                if not row.get("day"):
                    continue
                row["_source"] = str(path)
                by_day[row["day"]].append(row)

    latest: dict[str, dict[str, str]] = {}
    for day, rows in by_day.items():
        latest[day] = sorted(rows, key=lambda row: row["timestamp_utc"])[-1]
    return latest


def collapse_ranges(days: list[date]) -> list[tuple[date, date]]:
    if not days:
        return []
    ordered = sorted(days)
    ranges: list[tuple[date, date]] = []
    start = ordered[0]
    end = ordered[0]
    for current in ordered[1:]:
        if current == end + timedelta(days=1):
            end = current
            continue
        ranges.append((start, end))
        start = end = current
    ranges.append((start, end))
    return ranges


def main() -> int:
    args = parse_args()
    status_dir = Path(args.status_dir)
    data_root = Path(args.data_root)
    symbol = args.symbol
    start = date.fromisoformat(args.date_from)
    end = date.fromisoformat(args.date_to)
    latest = load_latest_statuses(status_dir, symbol)

    missing_days: list[date] = []
    failed_days: list[date] = []
    untracked_days: list[date] = []
    for day in iter_days(start, end):
        output = normalized_tick_path(data_root, symbol, day)
        latest_row = latest.get(day.isoformat())
        if output.exists() and output.stat().st_size > 0:
            continue
        if latest_row is None:
            untracked_days.append(day)
            continue
        if latest_row.get("status") in {"failed", "skipped_failed"}:
            failed_days.append(day)
            continue
        missing_days.append(day)

    print(f"range\t{start.isoformat()}\t{end.isoformat()}")
    print(f"failed_days\t{len(failed_days)}")
    print(f"missing_days\t{len(missing_days)}")
    print(f"untracked_days\t{len(untracked_days)}")

    for label, days in [
        ("failed_ranges", failed_days),
        ("missing_ranges", missing_days),
        ("untracked_ranges", untracked_days),
    ]:
        print(label)
        for begin, finish in collapse_ranges(days):
            print(f"{begin.isoformat()}\t{finish.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
