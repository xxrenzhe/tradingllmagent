from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import urlopen


FetchJson = Callable[[str, str, float], dict[str, Any]]


def run_ibkr_soak_monitor(
    *,
    api_base: str = "http://127.0.0.1:8000",
    output_dir: Path,
    symbol: str = "MNQ",
    max_stale_seconds: int = 30,
    interval_seconds: float = 60.0,
    max_samples: int | None = None,
    stop_when_ready: bool = False,
    timeout_seconds: float = 10.0,
    fetch_json: FetchJson | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    fetcher = fetch_json or fetch_api_json
    samples = 0
    latest: dict[str, Any] | None = None
    while max_samples is None or samples < max_samples:
        sample = collect_soak_sample(
            api_base=api_base,
            symbol=symbol,
            max_stale_seconds=max_stale_seconds,
            timeout_seconds=timeout_seconds,
            fetch_json=fetcher,
        )
        latest = write_soak_sample(output_dir, sample)
        samples += 1
        if stop_when_ready and latest.get("acceptance_status") == "ready":
            break
        if max_samples is not None and samples >= max_samples:
            break
        sleep(max(interval_seconds, 0.0))
    return latest or {}


def collect_soak_sample(
    *,
    api_base: str,
    symbol: str,
    max_stale_seconds: int,
    timeout_seconds: float,
    fetch_json: FetchJson | None = None,
) -> dict[str, Any]:
    fetcher = fetch_json or fetch_api_json
    query = urlencode({"symbol": symbol, "max_stale_seconds": max_stale_seconds})
    collected_at = datetime.now(UTC).isoformat()
    sample: dict[str, Any] = {"collected_at": collected_at, "symbol": symbol}
    endpoints = {
        "health": "/api/gateways/ibkr/health",
        "readiness": f"/api/gateways/ibkr/readiness?{query}",
        "market_data": f"/api/gateways/ibkr/market-data?{query}",
        "bracket_orders": "/api/gateways/ibkr/bracket-orders",
        "execution_ledger": "/api/gateways/ibkr/execution-ledger",
        "incidents": "/api/gateways/ibkr/incidents",
        "poller": "/api/gateways/ibkr/poller",
        "report": "/api/ibkr-paper/reports/current",
    }
    for name, path in endpoints.items():
        try:
            sample[name] = fetcher(api_base, path, timeout_seconds)
        except Exception as exc:
            sample[name] = {"error": str(exc)}
    sample["summary"] = summarize_soak_sample(sample)
    return sample


def fetch_api_json(api_base: str, path: str, timeout_seconds: float) -> dict[str, Any]:
    base = api_base.rstrip("/")
    with urlopen(f"{base}{path}", timeout=timeout_seconds) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def summarize_soak_sample(sample: dict[str, Any]) -> dict[str, Any]:
    report = _dict(sample.get("report"))
    health = _dict(sample.get("health"))
    readiness = _dict(sample.get("readiness"))
    market_data = _dict(sample.get("market_data"))
    bracket_orders = _dict(sample.get("bracket_orders"))
    ledger = _dict(sample.get("execution_ledger"))
    incidents = _dict(sample.get("incidents"))
    poller = _dict(sample.get("poller") or health.get("poller"))
    acceptance = _dict(report.get("acceptance_evidence"))
    gates = _dict(acceptance.get("gates"))
    return {
        "collected_at": sample.get("collected_at"),
        "symbol": sample.get("symbol"),
        "gateway_status": health.get("status"),
        "connected": health.get("connected"),
        "paper_account_verified": health.get("paper_account_verified"),
        "safe_mode": health.get("safe_mode"),
        "readiness_status": readiness.get("status"),
        "market_data_status": market_data.get("status"),
        "market_data_type": _dict(market_data.get("snapshot")).get("market_data_type"),
        "acceptance_status": acceptance.get("status"),
        "trading_day_count": acceptance.get("trading_day_count", 0),
        "readiness_check_count": acceptance.get("readiness_check_count", poller.get("readiness_check_count", 0)),
        "review_cycle_count": acceptance.get("review_cycle_count", poller.get("review_cycle_count", 0)),
        "paper_order_lifecycle_event_count": acceptance.get(
            "paper_order_lifecycle_event_count",
            bracket_orders.get("order_event_count", 0),
        ),
        "live_order_attempt_count": acceptance.get(
            "live_order_attempt_count",
            poller.get("live_order_attempt_count", 0),
        ),
        "unexplained_duplicate_order_count": acceptance.get(
            "unexplained_duplicate_order_count",
            poller.get("duplicate_order_event_count", 0),
        ),
        "bracket_child_missing_after_accept_count": acceptance.get("bracket_child_missing_after_accept_count", 0),
        "incident_count": acceptance.get("incident_count", incidents.get("count", 0)),
        "fill_count": ledger.get("fill_count", 0),
        "open_bracket_order_count": bracket_orders.get("open_bracket_order_count", 0),
        "completed_bracket_order_count": bracket_orders.get("completed_bracket_order_count", 0),
        "net_realized_pnl": ledger.get("net_realized_pnl"),
        "total_commission": ledger.get("total_commission"),
        "missing_requirements": acceptance.get("missing_requirements", []),
        "gates": gates,
        "endpoint_errors": sorted(
            name
            for name, payload in sample.items()
            if isinstance(payload, dict) and payload.get("error")
        ),
    }


def write_soak_sample(output_dir: Path, sample: dict[str, Any]) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = _dict(sample.get("summary"))
    with (output_dir / "samples.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample, sort_keys=True) + "\n")
    (output_dir / "latest_sample.json").write_text(json.dumps(sample, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "latest_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
