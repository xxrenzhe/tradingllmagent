from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .backtest import load_bar_rows
from .events import MacroEvent, context_for_timestamp


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    timeframe: str
    snapshot_time: datetime | None
    last_price: float | None
    session_high: float | None
    session_low: float | None
    opening_range_high: float | None
    opening_range_low: float | None
    vwap: float | None
    bar_count: int
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "snapshot_time": self.snapshot_time.isoformat() if self.snapshot_time else None,
            "last_price": self.last_price,
            "session_high": self.session_high,
            "session_low": self.session_low,
            "opening_range_high": self.opening_range_high,
            "opening_range_low": self.opening_range_low,
            "vwap": self.vwap,
            "bar_count": self.bar_count,
            "source": self.source,
        }


def build_market_snapshot(
    symbol: str,
    timeframe: str,
    bars: Sequence[dict[str, Any]],
    opening_range_bars: int = 3,
    source: str = "local_bars",
) -> MarketSnapshot:
    if not bars:
        return MarketSnapshot(symbol, timeframe, None, None, None, None, None, None, None, 0, source)
    ordered = sorted(bars, key=lambda row: row["timestamp"])
    latest = ordered[-1]
    opening = ordered[: max(opening_range_bars, 1)]
    total_weight = sum(max(int(row.get("tick_count", 1)), 1) for row in ordered)
    vwap = (
        sum(float(row["close"]) * max(int(row.get("tick_count", 1)), 1) for row in ordered)
        / total_weight
        if total_weight
        else None
    )
    return MarketSnapshot(
        symbol=symbol,
        timeframe=timeframe,
        snapshot_time=latest["timestamp"],
        last_price=float(latest["close"]),
        session_high=max(float(row["high"]) for row in ordered),
        session_low=min(float(row["low"]) for row in ordered),
        opening_range_high=max(float(row["high"]) for row in opening),
        opening_range_low=min(float(row["low"]) for row in opening),
        vwap=vwap,
        bar_count=len(ordered),
        source=source,
    )


def scan_key_levels(
    snapshot: MarketSnapshot,
    proximity_points: float = 2.0,
) -> list[dict[str, Any]]:
    if snapshot.last_price is None:
        return []
    levels = {
        "session_high": snapshot.session_high,
        "session_low": snapshot.session_low,
        "opening_range_high": snapshot.opening_range_high,
        "opening_range_low": snapshot.opening_range_low,
        "vwap": snapshot.vwap,
    }
    signals = []
    for name, level in levels.items():
        if level is None:
            continue
        distance = float(snapshot.last_price - level)
        abs_distance = abs(distance)
        if abs_distance <= proximity_points:
            signals.append(
                {
                    "level": name,
                    "level_price": level,
                    "distance_points": distance,
                    "abs_distance_points": abs_distance,
                    "proximity_score": max(0.0, 1.0 - abs_distance / max(proximity_points, 1e-9)),
                }
            )
    return sorted(signals, key=lambda row: row["abs_distance_points"])


def score_monitor_signal(
    snapshot: MarketSnapshot,
    key_level_signals: Sequence[dict[str, Any]],
    event_state: str = "normal",
    max_importance: str | None = None,
) -> dict[str, Any]:
    if snapshot.last_price is None:
        return {"strength": 0.0, "bucket": "none", "signal_class": "none", "reasons": ["no_bars"]}
    level_score = max((float(signal["proximity_score"]) for signal in key_level_signals), default=0.0)
    event_bonus = 0.15 if event_state in {"pre_event", "release_window", "event_release", "post_event"} else 0.0
    strength = min(1.0, level_score + event_bonus)
    if event_state in {"release_window", "event_release"} and max_importance == "high":
        bucket = "blocked"
    elif strength >= 0.78:
        bucket = "strong_review"
    elif strength >= 0.60:
        bucket = "medium_watch"
    elif strength >= 0.35:
        bucket = "weak_notice"
    else:
        bucket = "none"
    reasons = [f"near_{signal['level']}" for signal in key_level_signals[:3]]
    if event_bonus:
        reasons.append(f"event_state_{event_state}")
    if bucket == "blocked":
        reasons.append("blocked_by_high_impact_release_window")
    return {"strength": strength, "bucket": bucket, "signal_class": bucket, "reasons": reasons}


def build_monitor_report(
    *,
    symbol: str,
    timeframe: str,
    bar_files: Sequence[Path],
    events: Sequence[MacroEvent] = (),
    proximity_points: float = 2.0,
) -> dict[str, Any]:
    bars = load_bar_rows(bar_files)
    snapshot = build_market_snapshot(symbol, timeframe, bars)
    event_context = (
        context_for_timestamp(symbol, snapshot.snapshot_time, events).to_dict()
        if snapshot.snapshot_time and events
        else {
            "symbol": symbol,
            "timestamp": snapshot.snapshot_time.isoformat() if snapshot.snapshot_time else None,
            "event_state": "normal",
            "active_event_ids": [],
            "max_importance": None,
            "policy_ref": None,
            "minutes_to_event": None,
            "minutes_since_event": None,
        }
    )
    key_levels = scan_key_levels(snapshot, proximity_points=proximity_points)
    signal = score_monitor_signal(
        snapshot,
        key_levels,
        event_state=event_context["event_state"],
        max_importance=event_context.get("max_importance"),
    )
    return {
        "monitor_run_id": monitor_run_id(symbol, timeframe, snapshot.snapshot_time),
        "snapshot": snapshot.to_dict(),
        "key_levels": key_levels,
        "event_context": event_context,
        "signal": signal,
    }


def monitor_run_id(symbol: str, timeframe: str, timestamp: datetime | None) -> str:
    suffix = timestamp.strftime("%Y%m%dT%H%M%S") if timestamp else "empty"
    return f"{symbol}_{timeframe}_{suffix}"


def render_monitor_report(payload: dict[str, Any]) -> str:
    snapshot = payload["snapshot"]
    signal = payload["signal"]
    levels = payload["key_levels"]
    lines = [
        f"# Runtime Monitor {payload['monitor_run_id']}",
        "",
        f"- symbol: {snapshot['symbol']}",
        f"- timeframe: {snapshot['timeframe']}",
        f"- snapshot_time: {snapshot['snapshot_time']}",
        f"- last_price: {snapshot['last_price']}",
        f"- signal_bucket: {signal['bucket']}",
        f"- signal_strength: {signal['strength']:.2f}",
        f"- reasons: {', '.join(signal['reasons']) if signal['reasons'] else 'none'}",
        "",
        "## Key Levels",
    ]
    if levels:
        lines.extend(
            f"- {level['level']}: {level['level_price']} ({level['distance_points']:+.2f})"
            for level in levels
        )
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def write_monitor_outputs(output_dir: Path, payload: dict[str, Any]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = payload["monitor_run_id"]
    json_path = output_dir / f"{run_id}.json"
    report_path = output_dir / f"{run_id}.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.write_text(render_monitor_report(payload), encoding="utf-8")
    return {"json": str(json_path), "report": str(report_path)}
