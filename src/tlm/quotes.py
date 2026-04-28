from __future__ import annotations

import csv
import json
import re
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import duckdb

from .config import SymbolConfig
from .storage import normalized_quote_path, write_json, write_quotes_parquet


@dataclass(frozen=True)
class Quote:
    timestamp: datetime
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


def parse_databento_quote_csv(path: Path, source_timezone: str = "UTC") -> list[Quote]:
    timezone = ZoneInfo(source_timezone)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|") if sample else csv.excel
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        return [_parse_quote_row(row, timezone) for row in reader if any((value or "").strip() for value in row.values())]


def import_databento_quotes(
    csv_paths: Sequence[Path],
    data_root: Path,
    symbol: str,
    source_timezone: str = "UTC",
    force: bool = False,
) -> list[dict]:
    quotes_by_day: dict[date, list[Quote]] = defaultdict(list)
    for path in csv_paths:
        for quote in parse_databento_quote_csv(Path(path), source_timezone=source_timezone):
            quotes_by_day[quote.timestamp.date()].append(quote)

    outputs: list[dict] = []
    for day in sorted(quotes_by_day):
        output = normalized_quote_path(data_root, symbol, day)
        if output.exists() and output.stat().st_size > 0 and not force:
            outputs.append({"day": day.isoformat(), "path": str(output), "rows": 0, "status": "skipped_existing"})
            continue
        rows = [_quote_to_storage_row(symbol, quote) for quote in sorted(quotes_by_day[day], key=lambda item: item.timestamp)]
        write_quotes_parquet(output, rows)
        outputs.append({"day": day.isoformat(), "path": str(output), "rows": len(rows), "status": "written"})
    return outputs


def build_quote_execution_report(
    *,
    backtest_result_path: Path,
    quote_files: Sequence[Path],
    symbol_config: SymbolConfig,
    output_path: Path | None = None,
) -> dict[str, Any]:
    backtest = json.loads(backtest_result_path.read_text(encoding="utf-8"))
    trades = backtest.get("trades", [])
    quotes = load_quote_rows(quote_files)
    timestamps = [quote["timestamp"] for quote in quotes]
    validations = []
    missed_fills = 0
    gross_deltas = []
    spread_costs = []
    for trade in trades:
        entry_quote = _first_quote_at_or_after(quotes, timestamps, _parse_iso_datetime(trade["entry_time"]))
        exit_quote = _first_quote_at_or_after(quotes, timestamps, _parse_iso_datetime(trade["exit_time"]))
        if entry_quote is None or exit_quote is None:
            missed_fills += 1
            validations.append({"trade": trade, "status": "missing_quote"})
            continue
        contracts = int(trade.get("contracts", 1))
        side = trade["side"]
        entry_price = entry_quote["ask"] if side == "long" else entry_quote["bid"]
        exit_price = exit_quote["bid"] if side == "long" else exit_quote["ask"]
        quote_gross_pnl = _trade_pnl(side, entry_price, exit_price, contracts, symbol_config.point_value)
        original_gross_pnl = float(trade.get("gross_pnl", 0.0))
        gross_delta = quote_gross_pnl - original_gross_pnl
        entry_spread_usd = entry_quote["spread"] * symbol_config.point_value * contracts
        exit_spread_usd = exit_quote["spread"] * symbol_config.point_value * contracts
        gross_deltas.append(gross_delta)
        spread_costs.append(entry_spread_usd + exit_spread_usd)
        validations.append(
            {
                "status": "validated",
                "side": side,
                "entry_time": trade["entry_time"],
                "exit_time": trade["exit_time"],
                "entry_quote_time": entry_quote["timestamp"].isoformat(),
                "exit_quote_time": exit_quote["timestamp"].isoformat(),
                "original_entry_price": trade.get("entry_price"),
                "original_exit_price": trade.get("exit_price"),
                "quote_entry_price": entry_price,
                "quote_exit_price": exit_price,
                "original_gross_pnl": original_gross_pnl,
                "quote_gross_pnl": quote_gross_pnl,
                "gross_pnl_delta": gross_delta,
                "entry_spread_usd": entry_spread_usd,
                "exit_spread_usd": exit_spread_usd,
            }
        )
    report = {
        "schema_version": 1,
        "artifact": "quote_execution_validation",
        "symbol": symbol_config.alias,
        "source": symbol_config.provider,
        "backtest_result": str(backtest_result_path),
        "quote_files": [str(path) for path in quote_files if path.exists()],
        "trade_count": len(trades),
        "validated_trade_count": len(validations) - missed_fills,
        "missed_fill_count": missed_fills,
        "avg_gross_pnl_delta": _average(gross_deltas),
        "avg_bid_ask_cost_usd": _average(spread_costs),
        "validations": validations,
    }
    if output_path:
        write_json(output_path, report)
    return report


def load_quote_rows(quote_files: Sequence[Path]) -> list[dict[str, Any]]:
    files = [str(path) for path in quote_files if path.exists()]
    if not files:
        return []
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            """
            SELECT symbol, timestamp, bid, ask, bid_size, ask_size, mid, spread
            FROM read_parquet(?)
            ORDER BY timestamp
            """,
            [files],
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "symbol": row[0],
            "timestamp": row[1],
            "bid": float(row[2]),
            "ask": float(row[3]),
            "bid_size": float(row[4]),
            "ask_size": float(row[5]),
            "mid": float(row[6]),
            "spread": float(row[7]),
        }
        for row in rows
    ]


def _parse_quote_row(row: dict[str, str], timezone: ZoneInfo) -> Quote:
    normalized = {_normalize_header(key): (value or "").strip() for key, value in row.items()}
    timestamp = _parse_timestamp(normalized, timezone)
    bid = _first_float(normalized, ("bid", "bid_px_00", "bid_price", "best_bid"))
    ask = _first_float(normalized, ("ask", "ask_px_00", "ask_price", "best_ask"))
    if bid > ask:
        bid, ask = ask, bid
    return Quote(
        timestamp=timestamp,
        bid=bid,
        ask=ask,
        bid_size=_first_float(normalized, ("bid_size", "bid_sz_00", "bid_qty", "best_bid_size"), default=0.0),
        ask_size=_first_float(normalized, ("ask_size", "ask_sz_00", "ask_qty", "best_ask_size"), default=0.0),
    )


def _normalize_header(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _parse_timestamp(row: dict[str, str], timezone: ZoneInfo) -> datetime:
    for key in ("ts_event", "ts_recv", "timestamp", "datetime", "time"):
        if row.get(key):
            return _localize_timestamp(_parse_datetime(row[key]), timezone)
    raise ValueError("Databento quote row must include ts_event, ts_recv, timestamp, datetime, or time")


def _parse_datetime(value: str) -> datetime:
    normalized = re.sub(r"(\.\d{6})\d+", r"\1", value.strip()).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(value.strip(), fmt)
            except ValueError:
                continue
    raise ValueError(f"Invalid timestamp: {value}")


def _localize_timestamp(value: datetime, timezone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone)
    return value.astimezone(UTC).replace(tzinfo=None)


def _first_float(row: dict[str, str], keys: Sequence[str], default: float | None = None) -> float:
    for key in keys:
        if row.get(key) not in {None, ""}:
            return float(row[key])
    if default is not None:
        return default
    raise ValueError(f"Missing numeric quote column. Tried: {', '.join(keys)}")


def _quote_to_storage_row(symbol: str, quote: Quote) -> tuple:
    return (
        symbol,
        quote.timestamp,
        quote.bid,
        quote.ask,
        quote.bid_size,
        quote.ask_size,
        quote.mid,
        quote.spread,
    )


def _parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _first_quote_at_or_after(
    quotes: Sequence[dict[str, Any]],
    timestamps: Sequence[datetime],
    target: datetime,
) -> dict[str, Any] | None:
    index = bisect_left(timestamps, target)
    return quotes[index] if index < len(quotes) else None


def _trade_pnl(side: str, entry: float, exit: float, contracts: int, point_value: float) -> float:
    if side == "long":
        return (exit - entry) * point_value * contracts
    return (entry - exit) * point_value * contracts


def _average(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None
