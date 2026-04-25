from __future__ import annotations

import lzma
import struct
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .config import SymbolConfig


DATAFEED_BASE_URL = "https://datafeed.dukascopy.com/datafeed"
TICK_STRUCT = struct.Struct(">IIIff")


@dataclass(frozen=True)
class Tick:
    timestamp: datetime
    bid: float
    ask: float
    bid_size: float
    ask_size: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class DownloadResult:
    url: str
    path: Path
    status: str
    bytes_written: int = 0


def hour_floor(value: datetime) -> datetime:
    value = value.astimezone(UTC)
    return value.replace(minute=0, second=0, microsecond=0)


def iter_hours(start: datetime, end: datetime):
    current = hour_floor(start)
    stop = hour_floor(end)
    while current < stop:
        yield current
        current += timedelta(hours=1)


def dukascopy_url(instrument: str, hour: datetime) -> str:
    hour = hour.astimezone(UTC)
    return (
        f"{DATAFEED_BASE_URL}/{instrument}/"
        f"{hour.year:04d}/{hour.month - 1:02d}/{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"
    )


def raw_tick_path(data_root: Path, instrument: str, hour: datetime) -> Path:
    hour = hour.astimezone(UTC)
    return (
        data_root
        / "raw"
        / "dukascopy"
        / instrument
        / f"{hour.year:04d}"
        / f"{hour.month - 1:02d}"
        / f"{hour.day:02d}"
        / f"{hour.hour:02d}h_ticks.bi5"
    )


def download_hour(
    symbol: SymbolConfig,
    hour: datetime,
    data_root: Path,
    retries: int = 3,
    timeout_seconds: int = 30,
) -> DownloadResult:
    target = raw_tick_path(data_root, symbol.instrument, hour)
    if target.exists() and target.stat().st_size > 0:
        return DownloadResult(
            url=dukascopy_url(symbol.instrument, hour),
            path=target,
            status="cached",
            bytes_written=target.stat().st_size,
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    url = dukascopy_url(symbol.instrument, hour)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urlopen(url, timeout=timeout_seconds) as response:
                payload = response.read()
            target.write_bytes(payload)
            return DownloadResult(url=url, path=target, status="downloaded", bytes_written=len(payload))
        except HTTPError as exc:
            if exc.code == 404:
                return DownloadResult(url=url, path=target, status="empty_hour")
            last_error = exc
        except URLError as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(min(2**attempt, 8))

    raise RuntimeError(f"Failed to download {url}: {last_error}")


def parse_bi5_ticks(payload: bytes, hour_start: datetime, price_scale: int) -> list[Tick]:
    if not payload:
        return []
    decompressed = lzma.decompress(payload)
    if len(decompressed) % TICK_STRUCT.size != 0:
        raise ValueError(
            f"Invalid Dukascopy tick payload: {len(decompressed)} bytes is not divisible by "
            f"{TICK_STRUCT.size}"
        )

    hour_start = hour_floor(hour_start)
    ticks: list[Tick] = []
    for offset in range(0, len(decompressed), TICK_STRUCT.size):
        millis, ask_raw, bid_raw, ask_size, bid_size = TICK_STRUCT.unpack_from(
            decompressed, offset
        )
        ask = ask_raw / price_scale
        bid = bid_raw / price_scale
        if bid > ask:
            bid, ask = ask, bid
            bid_size, ask_size = ask_size, bid_size
        ticks.append(
            Tick(
                timestamp=hour_start + timedelta(milliseconds=millis),
                bid=bid,
                ask=ask,
                bid_size=float(bid_size),
                ask_size=float(ask_size),
            )
        )
    return ticks


def parse_bi5_file(path: Path, hour_start: datetime, price_scale: int) -> list[Tick]:
    return parse_bi5_ticks(path.read_bytes(), hour_start, price_scale)


def tick_to_row(symbol_alias: str, tick: Tick) -> tuple:
    return (
        symbol_alias,
        tick.timestamp,
        tick.bid,
        tick.ask,
        tick.bid_size,
        tick.ask_size,
        tick.mid,
        tick.spread,
    )
