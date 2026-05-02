from __future__ import annotations

import csv
import json
import re
import tempfile
import zipfile
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import duckdb

from .config import SymbolConfig
from .storage import normalized_quote_path, write_json, write_quotes_parquet


MBP1_COLUMN_TYPES = {
    "ts_event": "VARCHAR",
    "bid_px_00": "DOUBLE",
    "ask_px_00": "DOUBLE",
    "bid_sz_00": "DOUBLE",
    "ask_sz_00": "DOUBLE",
    "symbol": "VARCHAR",
}


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


def import_databento_mbp1_quotes(
    paths: Sequence[Path],
    data_root: Path,
    symbol: str,
    force: bool = False,
    zip_member: str | None = None,
) -> list[dict[str, Any]]:
    outputs = []
    for path in paths:
        source_outputs = _import_databento_mbp1_source(Path(path), data_root, symbol, force, zip_member)
        outputs.extend(source_outputs)
    return outputs


def _import_databento_mbp1_source(
    path: Path,
    data_root: Path,
    symbol: str,
    force: bool,
    zip_member: str | None,
) -> list[dict[str, Any]]:
    members = _mbp1_members(path, zip_member)
    outputs = []
    for member in members:
        outputs.extend(_import_databento_mbp1_member(path, data_root, symbol, force, member))
    return outputs


def _import_databento_mbp1_member(
    path: Path,
    data_root: Path,
    symbol: str,
    force: bool,
    member: str | None,
) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="tlm_mbp1_") as temp_dir:
        source = _prepare_mbp1_source(path, Path(temp_dir), member)
        return _import_databento_mbp1_csv_with_duckdb(source, data_root, symbol, force, str(path), member)


def _prepare_mbp1_source(path: Path, temp_dir: Path, member: str | None) -> Path:
    if path.suffix == ".zip":
        if not member:
            raise ValueError(f"Databento MBP-1 zip import requires a member for source preparation: {path}")
        output = temp_dir / Path(member).name
        with zipfile.ZipFile(path) as archive:
            with archive.open(member) as source, output.open("wb") as target:
                target.write(source.read())
        return output
    return path


def _import_databento_mbp1_csv_with_duckdb(
    source: Path,
    data_root: Path,
    symbol: str,
    force: bool,
    source_label: str,
    member: str | None,
) -> list[dict[str, Any]]:
    con = duckdb.connect(":memory:")
    try:
        member_day = _mbp1_member_day(member)
        types_sql = "{" + ", ".join(f"'{name}': '{kind}'" for name, kind in MBP1_COLUMN_TYPES.items()) + "}"
        source_sql = _sql_literal(str(source))
        member_day_filter = f"AND day = CAST({_sql_literal(member_day.isoformat())} AS DATE)" if member_day else ""
        con.execute(
            f"""
            CREATE TEMP TABLE source AS
            SELECT
                CAST(strptime(regexp_replace(CAST(ts_event AS VARCHAR), '(\\.\\d{{6}})\\d+Z$', '\\1Z'), '%Y-%m-%dT%H:%M:%S.%fZ') AS TIMESTAMP) AS timestamp,
                CAST(CAST(strptime(regexp_replace(CAST(ts_event AS VARCHAR), '(\\.\\d{{6}})\\d+Z$', '\\1Z'), '%Y-%m-%dT%H:%M:%S.%fZ') AS TIMESTAMP) AS DATE) AS day,
                CAST(symbol AS VARCHAR) AS raw_symbol,
                CAST(bid_px_00 AS DOUBLE) AS bid,
                CAST(ask_px_00 AS DOUBLE) AS ask,
                CAST(bid_sz_00 AS DOUBLE) AS bid_size,
                CAST(ask_sz_00 AS DOUBLE) AS ask_size
            FROM read_csv(
                {source_sql},
                header=true,
                union_by_name=true,
                sample_size=100000,
                types={types_sql}
            )
            WHERE regexp_matches(CAST(symbol AS VARCHAR), '^NQ[HMUZ][0-9]$')
              AND CAST(bid_px_00 AS DOUBLE) > 0
              AND CAST(ask_px_00 AS DOUBLE) > 0
              {member_day_filter}
            """
        )
        con.execute(
            """
            CREATE TEMP TABLE active_contract AS
            SELECT day, raw_symbol
            FROM (
                SELECT
                    day,
                    raw_symbol,
                    COUNT(*) AS row_count,
                    SUM(GREATEST(bid_size, 0.0) + GREATEST(ask_size, 0.0)) AS size_sum,
                    ROW_NUMBER() OVER (
                        PARTITION BY day
                        ORDER BY COUNT(*) DESC, SUM(GREATEST(bid_size, 0.0) + GREATEST(ask_size, 0.0)) DESC, raw_symbol ASC
                    ) AS rank
                FROM source
                GROUP BY day, raw_symbol
            )
            WHERE rank = 1
            """
        )
        days = con.execute(
            """
            SELECT source.day, active_contract.raw_symbol, COUNT(*) AS row_count
            FROM source
            JOIN active_contract USING (day, raw_symbol)
            GROUP BY source.day, active_contract.raw_symbol
            ORDER BY source.day
            """
        ).fetchall()
        outputs = []
        for day_value, contract, row_count in days:
            output = normalized_quote_path(data_root, symbol, day_value)
            if output.exists() and output.stat().st_size > 0 and not force:
                outputs.append(
                    {
                        "day": day_value.isoformat(),
                        "path": str(output),
                        "rows": 0,
                        "status": "skipped_existing",
                        "source": source_label,
                        "member": member,
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
                        LEAST(bid, ask) AS bid,
                        GREATEST(bid, ask) AS ask,
                        bid_size,
                        ask_size,
                        (LEAST(bid, ask) + GREATEST(bid, ask)) / 2.0 AS mid,
                        GREATEST(bid, ask) - LEAST(bid, ask) AS spread
                    FROM source
                    JOIN active_contract USING (day, raw_symbol)
                    WHERE day = CAST({day_sql} AS DATE)
                    ORDER BY timestamp
                ) TO {output_sql} (FORMAT PARQUET)
                """
            )
            outputs.append(
                {
                    "day": day_value.isoformat(),
                    "path": str(output),
                    "rows": int(row_count),
                    "status": "written",
                    "selected_contract": str(contract),
                    "source": source_label,
                    "member": member,
                }
            )
        return outputs
    finally:
        con.close()


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _mbp1_member_day(member: str | None) -> date | None:
    if not member:
        return None
    match = re.search(r"(\d{8})\.mbp-1\.csv(?:\.zst)?$", member)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d").date()


def build_quote_execution_report(
    *,
    backtest_result_path: Path,
    quote_files: Sequence[Path],
    symbol_config: SymbolConfig,
    output_path: Path | None = None,
    limit_timeout_seconds: int = 60,
    latency_seconds: Sequence[int] = (0, 1, 5),
    quote_window_seconds: int = 5,
) -> dict[str, Any]:
    backtest = json.loads(backtest_result_path.read_text(encoding="utf-8"))
    trades = backtest.get("trades", [])
    windows = _quote_windows_for_trades(
        trades,
        limit_timeout_seconds=max(limit_timeout_seconds, 0),
        latency_seconds=latency_seconds,
        padding_seconds=max(quote_window_seconds, 0),
    )
    quotes = load_quote_rows(quote_files, windows=windows)
    timestamps = [quote["timestamp"] for quote in quotes]
    validations = []
    missed_fills = 0
    limit_fills = 0
    limit_misses = 0
    gross_deltas = []
    spread_costs = []
    conservative_gross_deltas = []
    missed_opportunity_costs = []
    partial_fill_ratios = []
    latency_model_values: dict[str, list[float]] = {str(seconds): [] for seconds in latency_seconds}
    adverse_selection: dict[str, list[float]] = {"1m": [], "3m": [], "5m": [], "15m": []}
    for trade in trades:
        entry_time = _parse_iso_datetime(trade["entry_time"])
        exit_time = _parse_iso_datetime(trade["exit_time"])
        entry_quote = _first_quote_at_or_after(quotes, timestamps, entry_time)
        exit_quote = _first_quote_at_or_after(quotes, timestamps, exit_time)
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
        partial_ratio = _top_level_fill_ratio(entry_quote, side, contracts)
        partial_fill_ratios.append(partial_ratio)
        conservative_entry_price = entry_price + symbol_config.tick_size if side == "long" else entry_price - symbol_config.tick_size
        conservative_exit_price = exit_price - symbol_config.tick_size if side == "long" else exit_price + symbol_config.tick_size
        fixed_conservative_gross_pnl = _trade_pnl(
            side,
            conservative_entry_price,
            conservative_exit_price,
            contracts,
            symbol_config.point_value,
        )
        conservative_gross_deltas.append(fixed_conservative_gross_pnl - original_gross_pnl)
        latency_payload = {}
        for seconds in latency_seconds:
            latency_entry = _first_quote_at_or_after(quotes, timestamps, entry_time + timedelta(seconds=max(seconds, 0)))
            latency_exit = _first_quote_at_or_after(quotes, timestamps, exit_time + timedelta(seconds=max(seconds, 0)))
            if latency_entry is None or latency_exit is None:
                latency_payload[str(seconds)] = {"status": "missing_quote"}
                continue
            latency_entry_price = latency_entry["ask"] if side == "long" else latency_entry["bid"]
            latency_exit_price = latency_exit["bid"] if side == "long" else latency_exit["ask"]
            latency_gross_pnl = _trade_pnl(
                side,
                latency_entry_price,
                latency_exit_price,
                contracts,
                symbol_config.point_value,
            )
            latency_delta = latency_gross_pnl - original_gross_pnl
            latency_model_values[str(seconds)].append(latency_delta)
            latency_payload[str(seconds)] = {
                "status": "validated",
                "entry_time": latency_entry["timestamp"].isoformat(),
                "exit_time": latency_exit["timestamp"].isoformat(),
                "gross_pnl": latency_gross_pnl,
                "gross_pnl_delta": latency_delta,
            }
        adverse_ticks = _adverse_selection_ticks(
            quotes,
            timestamps,
            entry_quote["timestamp"],
            side,
            entry_price,
            symbol_config.tick_size,
        )
        for horizon, value in adverse_ticks.items():
            if value is not None:
                adverse_selection[horizon].append(value)
        limit_price = float(trade.get("entry_price", entry_price))
        limit_fill = _limit_fill_quote(
            quotes,
            timestamps,
            entry_time,
            side,
            limit_price,
            contracts,
            limit_timeout_seconds,
        )
        limit_payload: dict[str, Any]
        if limit_fill is None:
            limit_misses += 1
            missed_opportunity_costs.append(max(original_gross_pnl, 0.0))
            limit_payload = {
                "status": "missed_limit",
                "limit_price": limit_price,
                "timeout_seconds": limit_timeout_seconds,
                "missed_fill_opportunity_cost": max(original_gross_pnl, 0.0),
            }
        else:
            limit_fills += 1
            fill_quote, fill_price = limit_fill
            limit_adverse_ticks = _adverse_selection_ticks(
                quotes,
                timestamps,
                fill_quote["timestamp"],
                side,
                fill_price,
                symbol_config.tick_size,
            )
            limit_payload = {
                "status": "filled_limit",
                "limit_price": limit_price,
                "fill_time": fill_quote["timestamp"].isoformat(),
                "fill_price": fill_price,
                "bid_size": fill_quote["bid_size"],
                "ask_size": fill_quote["ask_size"],
                "top_level_size_sufficient": _top_level_size_sufficient(fill_quote, side, contracts),
                "adverse_selection_ticks": limit_adverse_ticks,
            }
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
                "fixed_conservative_gross_pnl": fixed_conservative_gross_pnl,
                "fixed_conservative_gross_pnl_delta": fixed_conservative_gross_pnl - original_gross_pnl,
                "entry_spread_usd": entry_spread_usd,
                "exit_spread_usd": exit_spread_usd,
                "top_level_fill_ratio": partial_ratio,
                "latency": latency_payload,
                "adverse_excursion_ticks": adverse_ticks,
                "limit_order": limit_payload,
            }
        )
    validated_trade_count = len(validations) - missed_fills
    report = {
        "schema_version": 1,
        "artifact": "quote_execution_validation",
        "symbol": symbol_config.alias,
        "source": symbol_config.provider,
        "backtest_result": str(backtest_result_path),
        "quote_files": [str(path) for path in quote_files if path.exists()],
        "trade_count": len(trades),
        "validated_trade_count": validated_trade_count,
        "missed_fill_count": missed_fills,
        "avg_gross_pnl_delta": _average(gross_deltas),
        "avg_bid_ask_cost_usd": _average(spread_costs),
        "execution_models": {
            "market_order_bid_ask_replay": {
                "validated_trade_count": validated_trade_count,
                "avg_gross_pnl_delta": _average(gross_deltas),
                "avg_bid_ask_cost_usd": _average(spread_costs),
            },
            "fixed_conservative": {
                "extra_round_trip_ticks": 2,
                "avg_gross_pnl_delta": _average(conservative_gross_deltas),
            },
            "latency": {
                str(seconds): {
                    "delay_seconds": seconds,
                    "sample_count": len(latency_model_values[str(seconds)]),
                    "avg_gross_pnl_delta": _average(latency_model_values[str(seconds)]),
                    "p05_gross_pnl_delta": _percentile(latency_model_values[str(seconds)], 0.05),
                }
                for seconds in latency_seconds
            },
            "partial_fill": {
                "sample_count": len(partial_fill_ratios),
                "full_top_level_fill_count": sum(1 for ratio in partial_fill_ratios if ratio >= 1.0),
                "min_top_level_fill_ratio": min(partial_fill_ratios) if partial_fill_ratios else None,
                "avg_top_level_fill_ratio": _average(partial_fill_ratios),
            },
            "limit_missed_fill": {
                "timeout_seconds": limit_timeout_seconds,
                "filled_count": limit_fills,
                "missed_count": limit_misses,
                "fill_rate": limit_fills / validated_trade_count if validated_trade_count else None,
                "avg_missed_fill_opportunity_cost": _average(missed_opportunity_costs),
            },
            "adverse_selection": {
                horizon: {
                    "sample_count": len(values),
                    "avg_ticks": _average(values),
                    "p95_ticks": _percentile(values, 0.95),
                }
                for horizon, values in adverse_selection.items()
            },
        },
        "validations": validations,
    }
    if output_path:
        write_json(output_path, report)
    return report


def load_quote_rows(
    quote_files: Sequence[Path],
    windows: Sequence[tuple[datetime, datetime]] | None = None,
) -> list[dict[str, Any]]:
    files = [path for path in quote_files if path.exists()]
    if not files:
        return []
    if windows:
        return _load_quote_rows_by_file(files, windows)
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            """
            SELECT symbol, timestamp, bid, ask, bid_size, ask_size, mid, spread
            FROM read_parquet(?)
            ORDER BY timestamp
            """,
            [[str(path) for path in files]],
        ).fetchall()
    finally:
        con.close()
    return _quote_rows_from_duckdb(rows)


def _load_quote_rows_by_file(
    quote_files: Sequence[Path],
    windows: Sequence[tuple[datetime, datetime]],
) -> list[dict[str, Any]]:
    windows_by_day: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for start, end in windows:
        current_day = start.date()
        while current_day <= end.date():
            day_start = datetime.combine(current_day, datetime.min.time())
            day_end = datetime.combine(current_day, datetime.max.time())
            windows_by_day[current_day.isoformat()].append((max(start, day_start), min(end, day_end)))
            current_day += timedelta(days=1)

    all_rows = []
    for path in quote_files:
        day_windows = windows_by_day.get(_date_partition(path))
        if not day_windows:
            continue
        merged = _merge_quote_windows(day_windows)
        clauses = []
        params: list[Any] = [str(path)]
        for start, end in merged:
            clauses.append("(timestamp BETWEEN ? AND ?)")
            params.extend([start, end])
        con = duckdb.connect(":memory:")
        try:
            all_rows.extend(
                con.execute(
                    f"""
                    SELECT symbol, timestamp, bid, ask, bid_size, ask_size, mid, spread
                    FROM read_parquet(?)
                    WHERE {" OR ".join(clauses)}
                    ORDER BY timestamp
                    """,
                    params,
                ).fetchall()
            )
        finally:
            con.close()
    return _quote_rows_from_duckdb(sorted(all_rows, key=lambda row: row[1]))


def _quote_rows_from_duckdb(rows: Sequence[tuple]) -> list[dict[str, Any]]:
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


def _mbp1_members(path: Path, zip_member: str | None) -> list[str | None]:
    if path.suffix == ".zip":
        import zipfile

        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        if zip_member:
            if zip_member not in names:
                raise ValueError(f"Databento MBP-1 zip member not found: {zip_member}")
            return [zip_member]
        members = sorted(name for name in names if name.endswith(".mbp-1.csv.zst") or name.endswith(".mbp-1.csv"))
        if not members:
            raise ValueError(f"No Databento MBP-1 CSV members found in {path}")
        return members
    return [zip_member]


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


def _quote_windows_for_trades(
    trades: Sequence[dict[str, Any]],
    *,
    limit_timeout_seconds: int,
    latency_seconds: Sequence[int],
    padding_seconds: int,
) -> list[tuple[datetime, datetime]]:
    windows = []
    for trade in trades:
        entry_time = _parse_iso_datetime(trade["entry_time"])
        exit_time = _parse_iso_datetime(trade["exit_time"])
        windows.append(
            (
                entry_time - timedelta(seconds=padding_seconds),
                entry_time + timedelta(seconds=limit_timeout_seconds + 900 + padding_seconds),
            )
        )
        event_offsets = {0, limit_timeout_seconds, 60, 180, 300, 900, *latency_seconds}
        for offset in event_offsets:
            event_time = entry_time + timedelta(seconds=max(offset, 0))
            windows.append(
                (
                    event_time - timedelta(seconds=padding_seconds),
                    event_time + timedelta(seconds=padding_seconds),
                )
            )
        for offset in {0, *latency_seconds}:
            event_time = exit_time + timedelta(seconds=max(offset, 0))
            windows.append(
                (
                    event_time - timedelta(seconds=padding_seconds),
                    event_time + timedelta(seconds=padding_seconds),
                )
            )
    return windows


def _merge_quote_windows(windows: Sequence[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged = []
    for start, end in sorted(windows):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
            continue
        if end > merged[-1][1]:
            merged[-1][1] = end
    return [(start, end) for start, end in merged]


def _date_partition(path: Path) -> str:
    for part in path.parts:
        if part.startswith("date="):
            return part.removeprefix("date=")
    return ""


def _first_quote_at_or_after(
    quotes: Sequence[dict[str, Any]],
    timestamps: Sequence[datetime],
    target: datetime,
) -> dict[str, Any] | None:
    index = bisect_left(timestamps, target)
    return quotes[index] if index < len(quotes) else None


def _limit_fill_quote(
    quotes: Sequence[dict[str, Any]],
    timestamps: Sequence[datetime],
    target: datetime,
    side: str,
    limit_price: float,
    contracts: int,
    timeout_seconds: int,
) -> tuple[dict[str, Any], float] | None:
    index = bisect_left(timestamps, target)
    deadline = target + timedelta(seconds=max(timeout_seconds, 0))
    for quote in quotes[index:]:
        if quote["timestamp"] > deadline:
            break
        if side == "long" and quote["ask"] <= limit_price and _top_level_size_sufficient(quote, side, contracts):
            return quote, limit_price
        if side == "short" and quote["bid"] >= limit_price and _top_level_size_sufficient(quote, side, contracts):
            return quote, limit_price
    return None


def _top_level_size_sufficient(quote: dict[str, Any], side: str, contracts: int) -> bool:
    if side == "long":
        return float(quote.get("ask_size") or 0.0) >= contracts
    return float(quote.get("bid_size") or 0.0) >= contracts


def _top_level_fill_ratio(quote: dict[str, Any], side: str, contracts: int) -> float:
    if contracts <= 0:
        return 0.0
    size_key = "ask_size" if side == "long" else "bid_size"
    available = float(quote.get(size_key) or 0.0)
    return min(max(available, 0.0) / contracts, 1.0)


def _adverse_selection_ticks(
    quotes: Sequence[dict[str, Any]],
    timestamps: Sequence[datetime],
    fill_time: datetime,
    side: str,
    fill_price: float,
    tick_size: float,
) -> dict[str, float | None]:
    horizons = {"1m": 60, "3m": 180, "5m": 300, "15m": 900}
    values: dict[str, float | None] = {}
    for name, seconds in horizons.items():
        quote = _first_quote_at_or_after(
            quotes,
            timestamps,
            fill_time + timedelta(seconds=seconds),
        )
        if quote is None or tick_size <= 0:
            values[name] = None
            continue
        future_mid = quote["mid"]
        values[name] = (fill_price - future_mid) / tick_size if side == "long" else (future_mid - fill_price) / tick_size
    return values


def _trade_pnl(side: str, entry: float, exit: float, contracts: int, point_value: float) -> float:
    if side == "long":
        return (exit - entry) * point_value * contracts
    return (entry - exit) * point_value * contracts


def _average(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(max(int(round((len(ordered) - 1) * quantile)), 0), len(ordered) - 1)
    return ordered[index]
