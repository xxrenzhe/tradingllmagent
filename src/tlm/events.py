from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

import duckdb


@dataclass(frozen=True)
class MacroEvent:
    event_id: str
    name: str
    timestamp_utc: datetime
    importance: str
    affected_symbols: list[str]
    pre_event_minutes: int
    release_window_minutes: int
    post_event_minutes: int
    policy_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "name": self.name,
            "timestamp_utc": self.timestamp_utc.isoformat(),
            "importance": self.importance,
            "affected_symbols": self.affected_symbols,
            "pre_event_minutes": self.pre_event_minutes,
            "release_window_minutes": self.release_window_minutes,
            "post_event_minutes": self.post_event_minutes,
            "policy_ref": self.policy_ref,
        }


@dataclass(frozen=True)
class EventContext:
    symbol: str
    timestamp: datetime
    event_state: str
    active_event_ids: list[str]
    max_importance: str | None
    policy_ref: str | None
    minutes_to_event: float | None
    minutes_since_event: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "event_state": self.event_state,
            "active_event_ids": self.active_event_ids,
            "max_importance": self.max_importance,
            "policy_ref": self.policy_ref,
            "minutes_to_event": self.minutes_to_event,
            "minutes_since_event": self.minutes_since_event,
        }


IMPORTANCE_RANK = {"low": 1, "medium": 2, "high": 3}


def load_event_calendar(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    events = [parse_event(raw) for raw in payload.get("events", [])]
    calendar_id = str(payload.get("calendar_id") or path.stem)
    return {
        "calendar_id": calendar_id,
        "events": events,
        "event_calendar_hash": event_calendar_hash(calendar_id, events),
    }


def resolve_event_calendar_path(ref: str | Path, config_dir: Path = Path("configs")) -> Path:
    candidate = Path(ref)
    if candidate.exists():
        return candidate
    ref_text = str(ref)
    candidates = [
        config_dir / ref_text,
        config_dir / f"{ref_text}.json",
        config_dir / f"{ref_text}.yaml",
        config_dir / "macro_events.yaml",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if payload.get("calendar_id") == ref_text or path.stem == ref_text:
            return path
    raise FileNotFoundError(f"Unknown event calendar reference: {ref}")


def parse_event(raw: dict[str, Any]) -> MacroEvent:
    timestamp = parse_timestamp(raw.get("timestamp_utc") or raw.get("time_utc") or raw.get("time"))
    affected_symbols = raw.get("affected_symbols", raw.get("symbols", []))
    if not isinstance(affected_symbols, list):
        raise ValueError("affected_symbols must be a list")
    pre_event_minutes = raw.get("pre_event_minutes", raw.get("pre_window_minutes", 30))
    post_event_minutes = raw.get("post_event_minutes", raw.get("post_window_minutes", 30))
    return MacroEvent(
        event_id=str(raw["event_id"]),
        name=str(raw.get("name", raw["event_id"])),
        timestamp_utc=timestamp,
        importance=str(raw.get("importance", "medium")).lower(),
        affected_symbols=[str(symbol) for symbol in affected_symbols],
        pre_event_minutes=int(pre_event_minutes),
        release_window_minutes=int(raw.get("release_window_minutes", 0)),
        post_event_minutes=int(post_event_minutes),
        policy_ref=str(raw.get("policy_ref", "default_macro_policy")),
    )


def parse_timestamp(value: Any) -> datetime:
    if not value:
        raise ValueError("event timestamp is required")
    timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC).replace(tzinfo=None)


def event_calendar_hash(calendar_id: str, events: Sequence[MacroEvent]) -> str:
    digest = hashlib.sha256()
    digest.update(calendar_id.encode())
    for event in sorted(events, key=lambda item: item.event_id):
        digest.update(json.dumps(event.to_dict(), sort_keys=True).encode())
    return digest.hexdigest()


def validate_event_calendar(path: Path) -> dict[str, Any]:
    calendar = load_event_calendar(path)
    seen = set()
    errors = []
    for event in calendar["events"]:
        if event.event_id in seen:
            errors.append(f"duplicate_event_id:{event.event_id}")
        seen.add(event.event_id)
        if event.importance not in IMPORTANCE_RANK:
            errors.append(f"unsupported_importance:{event.event_id}:{event.importance}")
        if event.pre_event_minutes < 0 or event.post_event_minutes < 0:
            errors.append(f"negative_window:{event.event_id}")
        if event.release_window_minutes < 0:
            errors.append(f"negative_release_window:{event.event_id}")
    return {
        "calendar_id": calendar["calendar_id"],
        "event_calendar_hash": calendar["event_calendar_hash"],
        "event_count": len(calendar["events"]),
        "valid": not errors,
        "errors": errors,
    }


def context_for_timestamp(symbol: str, timestamp: datetime, events: Sequence[MacroEvent]) -> EventContext:
    timestamp = timestamp.replace(tzinfo=None)
    active = []
    minutes_to_values = []
    minutes_since_values = []
    for event in events:
        if symbol not in event.affected_symbols and "*" not in event.affected_symbols:
            continue
        window_start = event.timestamp_utc - timedelta(minutes=event.pre_event_minutes)
        window_end = event.timestamp_utc + timedelta(minutes=event.post_event_minutes)
        if window_start <= timestamp <= window_end:
            active.append(event)
            delta_minutes = (event.timestamp_utc - timestamp).total_seconds() / 60
            if delta_minutes >= 0:
                minutes_to_values.append(delta_minutes)
            else:
                minutes_since_values.append(abs(delta_minutes))
    if not active:
        return EventContext(symbol, timestamp, "normal", [], None, None, None, None)
    max_event = max(active, key=lambda event: IMPORTANCE_RANK.get(event.importance, 0))
    release_half_window = timedelta(minutes=max_event.release_window_minutes / 2)
    if max_event.release_window_minutes and (
        max_event.timestamp_utc - release_half_window
        <= timestamp
        <= max_event.timestamp_utc + release_half_window
    ):
        state = "release_window"
    elif timestamp < max_event.timestamp_utc:
        state = "pre_event"
    elif timestamp > max_event.timestamp_utc:
        state = "post_event"
    else:
        state = "release_window"
    return EventContext(
        symbol=symbol,
        timestamp=timestamp,
        event_state=state,
        active_event_ids=[event.event_id for event in active],
        max_importance=max_event.importance,
        policy_ref=max_event.policy_ref,
        minutes_to_event=min(minutes_to_values) if minutes_to_values else None,
        minutes_since_event=min(minutes_since_values) if minutes_since_values else None,
    )


def load_bar_timestamps(bar_files: Sequence[Path]) -> list[tuple[str, datetime]]:
    files = [str(path) for path in bar_files if path.exists()]
    if not files:
        return []
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            "SELECT symbol, timestamp FROM read_parquet(?) ORDER BY timestamp",
            [files],
        ).fetchall()
    finally:
        con.close()
    return [(str(symbol), timestamp) for symbol, timestamp in rows]


def build_event_context_rows(
    symbol: str,
    bar_files: Sequence[Path],
    events: Sequence[MacroEvent],
) -> list[EventContext]:
    return [
        context_for_timestamp(symbol or row_symbol, timestamp, events)
        for row_symbol, timestamp in load_bar_timestamps(bar_files)
    ]


def write_event_context_parquet(path: Path, contexts: Sequence[EventContext]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        (
            context.symbol,
            context.timestamp,
            context.event_state,
            json.dumps(context.active_event_ids, sort_keys=True),
            context.max_importance,
            context.policy_ref,
            context.minutes_to_event,
            context.minutes_since_event,
        )
        for context in contexts
    ]
    con = duckdb.connect(":memory:")
    try:
        con.execute(
            """
            CREATE TABLE event_context (
                symbol VARCHAR,
                timestamp TIMESTAMP,
                event_state VARCHAR,
                active_event_ids VARCHAR,
                max_importance VARCHAR,
                policy_ref VARCHAR,
                minutes_to_event DOUBLE,
                minutes_since_event DOUBLE
            )
            """
        )
        if rows:
            con.executemany("INSERT INTO event_context VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        con.execute("COPY event_context TO ? (FORMAT PARQUET)", [str(path)])
    finally:
        con.close()
