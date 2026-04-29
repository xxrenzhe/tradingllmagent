from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


IBKR_PAPER_ARTIFACT_VERSION = 1


def build_ibkr_paper_report(
    *,
    run_id: str,
    health: dict[str, Any],
    readiness: dict[str, Any],
    contracts: dict[str, Any],
    market_data: dict[str, Any],
    bracket_orders: dict[str, Any],
    execution_ledger: dict[str, Any],
    incidents: dict[str, Any],
    reviews: list[dict[str, Any]] | None = None,
    optimizer_reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    fills = execution_ledger.get("fills") or []
    order_events = bracket_orders.get("recent_order_events") or []
    latest_account = execution_ledger.get("latest_account_snapshot") or {}
    net_realized_pnl = float(execution_ledger.get("net_realized_pnl") or 0.0)
    total_commission = float(execution_ledger.get("total_commission") or 0.0)
    order_reject_count = sum(1 for event in order_events if str(event.get("event_type", "")).endswith("rejected"))
    partial_fill_count = sum(
        1
        for event in order_events
        if event.get("event_type") == "order_status_recorded"
        and float((event.get("details") or {}).get("filled") or 0.0) > 0
        and float((event.get("details") or {}).get("remaining") or 0.0) > 0
    )
    metrics = {
        "realized_pnl": net_realized_pnl,
        "unrealized_pnl": latest_account.get("unrealized_pnl"),
        "net_pnl_after_commission": net_realized_pnl - total_commission,
        "expectancy": (net_realized_pnl - total_commission) / len(fills) if fills else None,
        "profit_factor": _profit_factor(fills),
        "win_rate": _win_rate(fills),
        "max_drawdown": None,
        "daily_loss_usage": latest_account.get("drawdown_usage"),
        "slippage_ticks": None,
        "order_reject_rate": order_reject_count / len(order_events) if order_events else 0.0,
        "partial_fill_rate": partial_fill_count / len(order_events) if order_events else 0.0,
        "time_to_fill_seconds": None,
        "bracket_failure_count": sum(1 for event in order_events if "bracket" in event.get("event_type", "") and event.get("event_type", "").endswith("rejected")),
        "paper_allow_would_have_lost_rate": None,
        "llm_action_confusion_matrix": {},
    }
    report = {
        "schema_version": IBKR_PAPER_ARTIFACT_VERSION,
        "run_id": run_id,
        "source": "ibkr_paper_runtime_report",
        "created_at": _now(),
        "execution_environment": "ibkr_paper",
        "live_execution_claim": False,
        "health": health,
        "readiness": readiness,
        "contracts": contracts,
        "market_data": market_data,
        "bracket_orders": bracket_orders,
        "execution_ledger": execution_ledger,
        "incidents": incidents,
        "reviews": reviews or [],
        "optimizer_reports": optimizer_reports or [],
        "promotion_blockers": _promotion_blockers(health, readiness, market_data, bracket_orders, incidents),
        "metrics": metrics,
    }
    report["report_hash"] = stable_hash(report)
    return report


def create_ibkr_paper_run_artifacts(
    *,
    run_id: str | None,
    root: Path,
    report: dict[str, Any],
) -> dict[str, Any]:
    resolved_run_id = run_id or f"ibkr_paper_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
    run_dir = root / resolved_run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    daily_report = {**report, "run_id": resolved_run_id}
    daily_report["report_hash"] = stable_hash({key: value for key, value in daily_report.items() if key != "report_hash"})

    artifacts = {
        "gateway_events": _write_jsonl(run_dir / "gateway_events.jsonl", daily_report.get("health", {}), resolved_run_id, "ibkr_gateway_health"),
        "market_data_readiness": _write_json(run_dir / "market_data_readiness.json", daily_report.get("market_data", {}), resolved_run_id, "ibkr_market_data_readiness"),
        "contract_details": _write_json(run_dir / "contract_details.json", daily_report.get("contracts", {}), resolved_run_id, "ibkr_contract_details"),
        "signals": _write_jsonl(run_dir / "signals.jsonl", {}, resolved_run_id, "ibkr_local_signal_engine"),
        "llm_reviews": _write_jsonl(run_dir / "llm_reviews.jsonl", daily_report.get("reviews", []), resolved_run_id, "ibkr_5m_review_loop"),
        "orders": _write_jsonl(run_dir / "orders.jsonl", daily_report.get("bracket_orders", {}), resolved_run_id, "ibkr_bracket_order_report"),
        "executions": _write_jsonl(run_dir / "executions.jsonl", daily_report.get("execution_ledger", {}).get("fills", []), resolved_run_id, "ibkr_execution_fills"),
        "positions": _write_jsonl(run_dir / "positions.jsonl", daily_report.get("execution_ledger", {}).get("positions", []), resolved_run_id, "ibkr_position_snapshots"),
        "account_snapshots": _write_jsonl(run_dir / "account_snapshots.jsonl", daily_report.get("execution_ledger", {}).get("latest_account_snapshot") or {}, resolved_run_id, "ibkr_account_snapshots"),
        "risk_control_diffs": _write_jsonl(run_dir / "risk_control_diffs.jsonl", daily_report.get("optimizer_reports", []), resolved_run_id, "ibkr_fast_path_optimizer"),
        "incidents": _write_jsonl(run_dir / "incidents.jsonl", daily_report.get("incidents", {}).get("incidents", []), resolved_run_id, "ibkr_incidents"),
        "daily_report": _write_json(run_dir / "daily_report.json", daily_report, resolved_run_id, "ibkr_daily_report"),
    }
    return {
        "schema_version": IBKR_PAPER_ARTIFACT_VERSION,
        "run_id": resolved_run_id,
        "status": "created",
        "run_dir": str(run_dir),
        "artifacts": artifacts,
        "created_at": _now(),
    }


def load_ibkr_paper_report(root: Path, run_id: str) -> dict[str, Any]:
    report_path = root / run_id / "daily_report.json"
    if not report_path.exists():
        raise FileNotFoundError(str(report_path))
    envelope = json.loads(report_path.read_text(encoding="utf-8"))
    return envelope.get("payload", envelope)


def stable_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _write_json(path: Path, payload: Any, run_id: str, source: str) -> str:
    path.write_text(json.dumps(_envelope(run_id, source, payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(path)


def _write_jsonl(path: Path, payload: Any, run_id: str, source: str) -> str:
    rows = payload if isinstance(payload, list) else [payload]
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(_envelope(run_id, source, row), sort_keys=True) + "\n")
    return str(path)


def _envelope(run_id: str, source: str, payload: Any) -> dict[str, Any]:
    envelope = {
        "schema_version": IBKR_PAPER_ARTIFACT_VERSION,
        "run_id": run_id,
        "created_at": _now(),
        "source": source,
        "strategy_spec_hash": _extract(payload, "strategy_spec_hash"),
        "module_id": _extract(payload, "module_id"),
        "contract_id": _extract(payload, "contract_id") or _extract(payload, "symbol") or "MNQ",
        "market_data_status": _extract(payload, "status"),
        "paper_account_hash": _extract(payload, "account_id_hash"),
        "payload": payload,
    }
    envelope["payload_hash"] = stable_hash({"payload": payload})
    return envelope


def _extract(payload: Any, key: str) -> Any:
    if not isinstance(payload, dict):
        return None
    if key in payload:
        return payload[key]
    for value in payload.values():
        if isinstance(value, dict):
            found = _extract(value, key)
            if found is not None:
                return found
    return None


def _profit_factor(fills: list[dict[str, Any]]) -> float | None:
    wins = sum(float(fill.get("realized_pnl") or 0.0) for fill in fills if float(fill.get("realized_pnl") or 0.0) > 0)
    losses = abs(sum(float(fill.get("realized_pnl") or 0.0) for fill in fills if float(fill.get("realized_pnl") or 0.0) < 0))
    if losses == 0:
        return None
    return wins / losses


def _win_rate(fills: list[dict[str, Any]]) -> float | None:
    outcomes = [float(fill.get("realized_pnl") or 0.0) for fill in fills if fill.get("realized_pnl") is not None]
    if not outcomes:
        return None
    return sum(1 for pnl in outcomes if pnl > 0) / len(outcomes)


def _promotion_blockers(
    health: dict[str, Any],
    readiness: dict[str, Any],
    market_data: dict[str, Any],
    bracket_orders: dict[str, Any],
    incidents: dict[str, Any],
) -> list[str]:
    blockers = []
    if health.get("live_trading_enabled"):
        blockers.append("live_trading_enabled")
    if not health.get("paper_account_verified"):
        blockers.append("paper_account_not_verified")
    if readiness.get("status") != "ready":
        blockers.extend(str(item) for item in readiness.get("missing_requirements", []))
    if market_data.get("status") != "ready":
        blockers.extend(f"market_data:{item}" for item in market_data.get("missing_requirements", []))
    if bracket_orders.get("open_bracket_order_count", 0) and health.get("safe_mode"):
        blockers.append("open_brackets_while_safe_mode")
    if incidents.get("count", 0):
        blockers.append("incident_review_required")
    return sorted(set(blockers))


def _now() -> str:
    return datetime.now(UTC).isoformat()
