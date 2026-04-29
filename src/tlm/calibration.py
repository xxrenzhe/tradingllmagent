from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from .config import CostModelConfig


@dataclass(frozen=True)
class CostCalibrationSample:
    source: str
    timestamp: str
    instrument: str
    observed_spread_ticks: float
    observed_slippage_ticks: float
    quantity: int = 1
    account: str | None = None
    intent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_cost_calibration_sample(payload: dict[str, Any]) -> CostCalibrationSample:
    return CostCalibrationSample(
        source=str(payload.get("source", "paper_shadow")),
        timestamp=str(payload.get("timestamp") or datetime.now(UTC).isoformat()),
        instrument=str(payload["instrument"]),
        observed_spread_ticks=float(payload["observed_spread_ticks"]),
        observed_slippage_ticks=float(payload["observed_slippage_ticks"]),
        quantity=int(payload.get("quantity", 1)),
        account=payload.get("account"),
        intent_id=payload.get("intent_id"),
    )


def summarize_cost_calibration(
    samples: Sequence[CostCalibrationSample | dict[str, Any]],
    baseline: CostModelConfig,
) -> dict[str, Any]:
    parsed = [sample if isinstance(sample, CostCalibrationSample) else parse_cost_calibration_sample(sample) for sample in samples]
    spread_values = sorted(sample.observed_spread_ticks for sample in parsed)
    slippage_values = sorted(sample.observed_slippage_ticks for sample in parsed)
    p95_spread = quantile(spread_values, 0.95)
    p95_slippage = quantile(slippage_values, 0.95)
    return {
        "schema_version": 1,
        "sample_count": len(parsed),
        "baseline_cost_model": baseline.to_dict(),
        "source_counts": count_by(sample.source for sample in parsed),
        "spread_ticks": distribution_summary(spread_values),
        "slippage_ticks": distribution_summary(slippage_values),
        "recommended_cost_model": {
            **baseline.to_dict(),
            "name": f"{baseline.name}_calibrated",
            "slippage_ticks_per_side": max(baseline.slippage_ticks_per_side, p95_slippage or baseline.slippage_ticks_per_side),
        },
        "cost_model_version_hash": stable_hash(
            {
                "baseline": baseline.to_dict(),
                "p95_spread": p95_spread,
                "p95_slippage": p95_slippage,
                "sample_count": len(parsed),
            }
        ),
    }


def build_cost_calibration_artifact(
    *,
    baseline: CostModelConfig,
    samples: Sequence[CostCalibrationSample | dict[str, Any]],
    data_quality_report: dict[str, Any],
    proxy_instrument: str,
    executable_instrument: str,
) -> dict[str, Any]:
    summary = summarize_cost_calibration(samples, baseline)
    proxy_warning = proxy_instrument != executable_instrument
    artifact = {
        "schema_version": 1,
        "artifact": "cost_calibration",
        "generated_at": datetime.now(UTC).isoformat(),
        "data_quality_report_hash": stable_hash(data_quality_report),
        "data_quality_status": data_quality_report.get("status"),
        "proxy_instrument": proxy_instrument,
        "executable_instrument": executable_instrument,
        "proxy_warning": proxy_warning,
        "proxy_warning_reason": (
            "research_proxy_differs_from_executable_market" if proxy_warning else None
        ),
        "summary": summary,
    }
    artifact["artifact_hash"] = stable_hash({key: value for key, value in artifact.items() if key != "artifact_hash"})
    return artifact


def write_cost_calibration_artifact(path: Path, artifact: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def load_cost_calibration_samples(path: Path) -> list[CostCalibrationSample]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid cost calibration JSONL at {path}:{line_number}") from exc
        rows.append(parse_cost_calibration_sample(payload))
    return rows


def distribution_summary(values: Sequence[float]) -> dict[str, float | int | None]:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": ordered[0] if ordered else None,
        "p50": quantile(ordered, 0.50),
        "p95": quantile(ordered, 0.95),
        "max": ordered[-1] if ordered else None,
    }


def quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * probability
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def count_by(values: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
