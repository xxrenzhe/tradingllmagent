#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence
import zipfile

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tlm.storage import compute_data_version_hash, write_json


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit full Databento MBP-1 quote coverage and replay trade coverage.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--raw-zip", type=Path, required=True)
    parser.add_argument("--quote-replay-report", type=Path)
    parser.add_argument("--quote-replay-trades", type=Path)
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    expected_dates = expected_mbp1_dates(args.raw_zip)
    if args.date_from:
        expected_dates = [day for day in expected_dates if day >= args.date_from]
    if args.date_to:
        expected_dates = [day for day in expected_dates if day <= args.date_to]

    quote_files = quote_files_for_dates(Path(args.data_root), args.symbol, expected_dates)
    daily = [summarize_quote_file(day, path) for day, path in quote_files]
    trade_counts = load_trade_counts(args.quote_replay_trades)
    replay_report = load_json(args.quote_replay_report) if args.quote_replay_report else {}
    for row in daily:
        row["replay_trade_count"] = trade_counts.get(row["date"], 0)
        row["trade_replay_status"] = "traded_and_validated" if row["replay_trade_count"] else "no_strategy_trade"

    missing_dates = [row["date"] for row in daily if row["status"] == "missing_quote_parquet"]
    zero_row_dates = [row["date"] for row in daily if int(row.get("row_count") or 0) == 0 and row["status"] != "missing_quote_parquet"]
    analyzed_dates = [row["date"] for row in daily if row["status"] == "analyzed"]
    trade_dates = sorted(day for day, count in trade_counts.items() if count > 0)
    validated_count = int(replay_report.get("validated_trade_count") or 0)
    replay_trade_count = int(replay_report.get("trade_count") or sum(trade_counts.values()))

    payload = {
        "artifact": "databento_mbp1_quote_coverage_audit",
        "schema_version": 1,
        "symbol": args.symbol,
        "raw_zip": str(args.raw_zip),
        "date_from": expected_dates[0] if expected_dates else None,
        "date_to": expected_dates[-1] if expected_dates else None,
        "expected_quote_days": len(expected_dates),
        "analyzed_quote_days": len(analyzed_dates),
        "missing_quote_days": missing_dates,
        "zero_row_quote_days": zero_row_dates,
        "quote_replay_report": str(args.quote_replay_report) if args.quote_replay_report else None,
        "quote_replay_trades": str(args.quote_replay_trades) if args.quote_replay_trades else None,
        "strategy_trade_days": len(trade_dates),
        "strategy_trade_dates": trade_dates,
        "strategy_days_without_trades": [row["date"] for row in daily if row["date"] not in trade_counts],
        "replay_trade_count": replay_trade_count,
        "validated_trade_count": validated_count,
        "missed_fill_count": int(replay_report.get("missed_fill_count") or 0),
        "all_expected_quote_days_analyzed": len(expected_dates) > 0 and not missing_dates and not zero_row_dates,
        "all_replay_trades_validated": replay_trade_count > 0 and replay_trade_count == validated_count,
        "data_version_hash": compute_data_version_hash(
            [path for _, path in quote_files if path.exists()],
            {
                "artifact": "databento_mbp1_quote_coverage_audit",
                "symbol": args.symbol,
                "raw_zip": str(args.raw_zip),
                "expected_dates": expected_dates,
                "quote_replay_report": str(args.quote_replay_report) if args.quote_replay_report else None,
            },
        ),
        "daily_quote_coverage": daily,
    }
    payload["decision"] = {
        "passed": payload["all_expected_quote_days_analyzed"] and payload["all_replay_trades_validated"],
        "reason": decision_reason(payload),
    }
    write_json(args.output, payload)
    print(json.dumps({"output": str(args.output), **payload["decision"]}, indent=2))
    return 0


def expected_mbp1_dates(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    dates = []
    for name in names:
        match = re.search(r"glbx-mdp3-(\d{8})\.mbp-1\.csv(?:\.zst)?$", name)
        if match:
            dates.append(datetime.strptime(match.group(1), "%Y%m%d").date().isoformat())
    return sorted(set(dates))


def quote_files_for_dates(data_root: Path, symbol: str, dates: Sequence[str]) -> list[tuple[str, Path]]:
    return [
        (day, data_root / "normalized" / "quotes" / symbol / f"date={day}" / "part-000.parquet")
        for day in dates
    ]


def summarize_quote_file(day: str, path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"date": day, "path": str(path), "status": "missing_quote_parquet", "row_count": 0}
    con = duckdb.connect(":memory:")
    try:
        row = con.execute(
            """
            SELECT
                COUNT(*) AS row_count,
                MIN(timestamp) AS first_timestamp,
                MAX(timestamp) AS last_timestamp,
                AVG(spread) AS avg_spread,
                MAX(spread) AS max_spread,
                MIN(bid) AS min_bid,
                MAX(ask) AS max_ask,
                SUM(CASE WHEN spread < 0 THEN 1 ELSE 0 END) AS negative_spread_count,
                COUNT(*) - COUNT(DISTINCT timestamp) AS duplicate_timestamp_count
            FROM read_parquet(?)
            """,
            [str(path)],
        ).fetchone()
    finally:
        con.close()
    row_count = int(row[0] or 0)
    return {
        "date": day,
        "path": str(path),
        "status": "analyzed" if row_count else "empty_quote_parquet",
        "row_count": row_count,
        "first_timestamp": row[1].isoformat() if row[1] else None,
        "last_timestamp": row[2].isoformat() if row[2] else None,
        "avg_spread": float(row[3]) if row[3] is not None else None,
        "max_spread": float(row[4]) if row[4] is not None else None,
        "min_bid": float(row[5]) if row[5] is not None else None,
        "max_ask": float(row[6]) if row[6] is not None else None,
        "negative_spread_count": int(row[7] or 0),
        "duplicate_timestamp_count": int(row[8] or 0),
    }


def load_trade_counts(path: Path | None) -> Counter[str]:
    if not path or not path.exists():
        return Counter()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return Counter(str(trade.get("entry_time", ""))[:10] for trade in payload.get("trades", []) if trade.get("entry_time"))


def load_json(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def decision_reason(payload: dict[str, Any]) -> str | None:
    if not payload["all_expected_quote_days_analyzed"]:
        return "One or more expected MBP-1 quote days are missing or empty."
    if not payload["all_replay_trades_validated"]:
        return "One or more strategy replay trades lack quote validation."
    return None


if __name__ == "__main__":
    raise SystemExit(main())
