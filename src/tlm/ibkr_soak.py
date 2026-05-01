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
    summary = {
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
    summary["closeout_status"] = soak_closeout_status(summary)
    return summary


def write_soak_sample(output_dir: Path, sample: dict[str, Any]) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = _dict(sample.get("summary"))
    compact_sample = compact_soak_sample(sample)
    with (output_dir / "samples.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(compact_sample, sort_keys=True) + "\n")
    (output_dir / "latest_sample.json").write_text(
        json.dumps(compact_sample, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_dir / "latest_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    write_soak_closeout(output_dir, summary)
    return summary


def compact_soak_sample(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "collected_at": sample.get("collected_at"),
        "symbol": sample.get("symbol"),
        "summary": _dict(sample.get("summary")),
        "health": _compact_health_payload(_dict(sample.get("health"))),
        "readiness": _compact_readiness_payload(_dict(sample.get("readiness"))),
        "market_data": _compact_market_data_payload(_dict(sample.get("market_data"))),
        "bracket_orders": _compact_bracket_orders_payload(_dict(sample.get("bracket_orders"))),
        "execution_ledger": _compact_execution_ledger_payload(_dict(sample.get("execution_ledger"))),
        "incidents": _compact_incidents_payload(_dict(sample.get("incidents"))),
        "poller": _compact_poller_payload(_dict(sample.get("poller"))),
        "report": _compact_report_payload(_dict(sample.get("report"))),
    }


def _compact_health_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        payload,
        "status",
        "connected",
        "paper_account_verified",
        "safe_mode",
        "live_trading_enabled",
    )


def _compact_readiness_payload(payload: dict[str, Any]) -> dict[str, Any]:
    compact = _pick(payload, "status", "mode", "paper_only", "checked_at")
    contract = _dict(payload.get("contract"))
    market_data = _dict(payload.get("market_data"))
    compact["missing_requirements"] = list(payload.get("missing_requirements", []))
    compact["contract"] = _pick(contract, "status", "symbol", "missing_requirements")
    compact["market_data"] = {
        **_pick(market_data, "status", "symbol", "max_stale_seconds", "missing_requirements"),
        "snapshot": _compact_market_data_snapshot(_dict(market_data.get("snapshot"))),
    }
    return compact


def _compact_market_data_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **_pick(payload, "status", "symbol", "max_stale_seconds", "missing_requirements"),
        "snapshot": _compact_market_data_snapshot(_dict(payload.get("snapshot"))),
    }


def _compact_market_data_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        snapshot,
        "symbol",
        "bid",
        "ask",
        "last",
        "spread",
        "market_data_type",
        "real_time",
        "order_ready",
        "snapshot_time",
        "age_seconds",
        "error_code",
        "error_message",
    )


def _compact_bracket_orders_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        payload,
        "open_bracket_order_count",
        "completed_bracket_order_count",
        "order_event_count",
        "accepted_parent_order_count",
        "bracket_child_missing_after_accept_count",
    )


def _compact_execution_ledger_payload(payload: dict[str, Any]) -> dict[str, Any]:
    compact = _pick(
        payload,
        "fill_count",
        "net_realized_pnl",
        "total_commission",
        "open_position_quantity",
    )
    compact["latest_account_snapshot"] = _compact_account_snapshot(_dict(payload.get("latest_account_snapshot")))
    return compact


def _compact_account_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        payload,
        "net_liquidation",
        "daily_pnl",
        "realized_pnl",
        "unrealized_pnl",
        "drawdown_usage",
        "recorded_at",
    )


def _compact_incidents_payload(payload: dict[str, Any]) -> dict[str, Any]:
    incidents = payload.get("incidents")
    latest = incidents[-1] if isinstance(incidents, list) and incidents else {}
    return {
        "count": payload.get("count", 0),
        "latest": _pick(_dict(latest), "event_type", "code", "message", "created_at"),
    }


def _compact_poller_payload(payload: dict[str, Any]) -> dict[str, Any]:
    compact = _pick(
        payload,
        "enabled",
        "running",
        "auto_submit",
        "readiness_check_count",
        "review_cycle_count",
        "live_order_attempt_count",
        "duplicate_order_event_count",
        "last_review_at",
        "last_review_fill_count",
        "warm_start_bar_count",
        "warm_start_source",
        "iteration_count",
        "last_run_at",
    )
    compact["strategy"] = _pick(
        _dict(payload.get("strategy")),
        "strategy_id",
        "strategy_spec_hash",
        "module_id",
        "family",
        "symbol",
        "timeframe",
        "preset",
        "enabled",
        "tick_size",
        "stop_loss_ticks",
        "take_profit_ticks",
        "max_holding_minutes",
    )
    compact["control_state"] = _pick(
        _dict(payload.get("control_state")),
        "mode",
        "min_confidence",
        "max_spread_ticks",
        "daily_trade_cap",
        "trade_session",
        "safe_mode",
        "kill_switch",
    )
    compact["last_result"] = _compact_last_result(_dict(payload.get("last_result")))
    compact["latest_signal"] = _compact_latest_signal(_dict(payload.get("latest_signal")))
    return compact


def _compact_last_result(payload: dict[str, Any]) -> dict[str, Any]:
    compact = _pick(payload, "status", "symbol", "action_count", "actions", "safe_mode")
    readiness = _dict(payload.get("readiness"))
    decision = _dict(payload.get("decision"))
    compact["readiness"] = {
        "status": readiness.get("status"),
        "missing_requirements": list(readiness.get("missing_requirements", [])),
        "market_data": _pick(_dict(readiness.get("market_data")), "status", "missing_requirements"),
    }
    compact["decision"] = _pick(decision, "status", "symbol", "bars_count", "signal_class", "review_due")
    return compact


def _compact_latest_signal(payload: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        payload,
        "schema_version",
        "source",
        "strategy_id",
        "strategy_spec_hash",
        "module_id",
        "symbol",
        "timeframe",
        "signal_class",
        "blocked",
        "reasons",
        "created_at",
    )


def _compact_report_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": payload.get("run_id"),
        "generated_at": payload.get("generated_at"),
        "acceptance_evidence": _dict(payload.get("acceptance_evidence")),
    }


def soak_closeout_status(summary: dict[str, Any]) -> str:
    if _soak_failure_reasons(summary):
        return "failed"
    if summary.get("acceptance_status") == "ready":
        return "ready"
    return "pending"


def write_soak_closeout(output_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    previous = _load_json(output_dir / "latest_closeout.json")
    closeout = build_soak_closeout(summary, previous=previous)
    latest_closeout = output_dir / "latest_closeout.json"
    latest_closeout.write_text(json.dumps(closeout, indent=2, sort_keys=True), encoding="utf-8")
    if closeout["status"] in {"ready", "failed"}:
        final_closeout = output_dir / "final_closeout.json"
        if not final_closeout.exists():
            final_closeout.write_text(json.dumps(closeout, indent=2, sort_keys=True), encoding="utf-8")
    return closeout


def build_soak_closeout(summary: dict[str, Any], *, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = previous if isinstance(previous, dict) else {}
    status = soak_closeout_status(summary)
    collected_at = summary.get("collected_at")
    started_at = previous.get("started_at") or collected_at
    ready_at = previous.get("ready_at")
    failed_at = previous.get("failed_at")
    if status == "ready" and ready_at is None:
        ready_at = collected_at
    if status == "failed" and failed_at is None:
        failed_at = collected_at
    warnings = _soak_warning_reasons(summary)
    failures = _soak_failure_reasons(summary)
    return {
        "status": status,
        "started_at": started_at,
        "last_collected_at": collected_at,
        "ready_at": ready_at,
        "failed_at": failed_at,
        "missing_requirements": list(summary.get("missing_requirements", [])),
        "warning_reasons": warnings,
        "failure_reasons": failures,
        "counts": {
            "trading_day_count": summary.get("trading_day_count", 0),
            "readiness_check_count": summary.get("readiness_check_count", 0),
            "review_cycle_count": summary.get("review_cycle_count", 0),
            "paper_order_lifecycle_event_count": summary.get("paper_order_lifecycle_event_count", 0),
            "live_order_attempt_count": summary.get("live_order_attempt_count", 0),
            "unexplained_duplicate_order_count": summary.get("unexplained_duplicate_order_count", 0),
            "bracket_child_missing_after_accept_count": summary.get("bracket_child_missing_after_accept_count", 0),
        },
        "summary": summary,
    }


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pick(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: payload.get(key) for key in keys if key in payload}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _dict(json.loads(path.read_text(encoding="utf-8")))


def _soak_warning_reasons(summary: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if summary.get("endpoint_errors"):
        warnings.extend(f"endpoint_error:{name}" for name in summary.get("endpoint_errors", []))
    if summary.get("connected") is False:
        warnings.append("gateway_disconnected")
    if summary.get("paper_account_verified") is False:
        warnings.append("paper_account_unverified")
    if summary.get("safe_mode"):
        warnings.append("safe_mode")
    if summary.get("readiness_status") not in {None, "ready"}:
        warnings.append(f"readiness:{summary.get('readiness_status')}")
    if summary.get("market_data_status") not in {None, "ready"}:
        warnings.append(f"market_data:{summary.get('market_data_status')}")
    return warnings


def _soak_failure_reasons(summary: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if int(summary.get("live_order_attempt_count", 0) or 0) > 0:
        failures.append("live_order_attempts_detected")
    if int(summary.get("unexplained_duplicate_order_count", 0) or 0) > 0:
        failures.append("unexplained_duplicate_orders_detected")
    if int(summary.get("bracket_child_missing_after_accept_count", 0) or 0) > 0:
        failures.append("missing_bracket_child_after_accept_detected")
    return failures
