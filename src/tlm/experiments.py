from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trials (
    experiment_id TEXT NOT NULL,
    trial_id TEXT PRIMARY KEY,
    strategy_name TEXT NOT NULL,
    strategy_spec_hash TEXT NOT NULL,
    prompt_hash TEXT,
    status TEXT NOT NULL,
    passed INTEGER NOT NULL,
    robustness_score REAL,
    net_pnl_test REAL NOT NULL,
    sharpe_test REAL,
    annual_trades_test REAL NOT NULL,
    net_pnl_holdout REAL NOT NULL,
    reasons_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    trial_id TEXT,
    event_type TEXT NOT NULL,
    prompt_hash TEXT,
    response_hash TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id)
);
"""


def connect_experiment_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    return connection


def record_experiment(
    path: Path,
    experiment_id: str,
    symbol: str,
    status: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    metadata_json = json.dumps(metadata or {}, sort_keys=True, default=str)
    with connect_experiment_db(path) as connection:
        connection.execute(
            """
            INSERT INTO experiments (
                experiment_id, symbol, status, created_at, updated_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(experiment_id) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json
            """,
            (experiment_id, symbol, status, now, now, metadata_json),
        )


def record_trial(path: Path, experiment_id: str, result: Any, status: str = "completed") -> None:
    payload = result.to_dict()
    gates = payload["gates"]
    test_metrics = payload["aggregate_test_metrics"]
    holdout_metrics = payload["final_holdout_metrics"]
    now = datetime.now(UTC).isoformat()
    with connect_experiment_db(path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO trials (
                experiment_id, trial_id, strategy_name, strategy_spec_hash, prompt_hash,
                status, passed, robustness_score, net_pnl_test, sharpe_test,
                annual_trades_test, net_pnl_holdout, reasons_json, result_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                experiment_id,
                payload["experiment_id"],
                payload["strategy_name"],
                payload["strategy_spec_hash"],
                payload.get("prompt_hash"),
                status,
                1 if gates["passed"] else 0,
                payload["robustness_score"],
                test_metrics["net_pnl"],
                test_metrics["sharpe"],
                test_metrics["annual_trades"],
                holdout_metrics["net_pnl"],
                json.dumps(gates["reasons"], sort_keys=True, default=str),
                json.dumps(payload, sort_keys=True, default=str),
                now,
            ),
        )


def record_audit_event(
    path: Path,
    experiment_id: str,
    event_type: str,
    payload: dict[str, Any],
    trial_id: str | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    with connect_experiment_db(path) as connection:
        connection.execute(
            """
            INSERT INTO audit_logs (
                experiment_id, trial_id, event_type, prompt_hash, response_hash,
                payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                experiment_id,
                trial_id,
                event_type,
                payload.get("prompt_hash"),
                payload.get("response_hash"),
                json.dumps(payload, sort_keys=True, default=str),
                now,
            ),
        )


def load_experiment_summary(path: Path, experiment_id: str) -> dict[str, Any]:
    with connect_experiment_db(path) as connection:
        experiment = connection.execute(
            "SELECT experiment_id, symbol, status, created_at, updated_at, metadata_json "
            "FROM experiments WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        if experiment is None:
            raise KeyError(f"Unknown experiment_id: {experiment_id}")
        trials = connection.execute(
            "SELECT trial_id, strategy_name, passed, robustness_score, net_pnl_test, "
            "sharpe_test, annual_trades_test, net_pnl_holdout, reasons_json, result_json "
            "FROM trials WHERE experiment_id = ? ORDER BY trial_id",
            (experiment_id,),
        ).fetchall()
    return {
        "experiment": {
            "experiment_id": experiment[0],
            "symbol": experiment[1],
            "status": experiment[2],
            "created_at": experiment[3],
            "updated_at": experiment[4],
            "metadata": json.loads(experiment[5]),
        },
        "trials": [_trial_summary(row) for row in trials],
    }


def load_experiment_audit_logs(
    path: Path,
    experiment_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    with connect_experiment_db(path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM experiments WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        if exists is None:
            raise KeyError(f"Unknown experiment_id: {experiment_id}")
        rows = connection.execute(
            "SELECT id, experiment_id, trial_id, event_type, prompt_hash, response_hash, "
            "payload_json, created_at FROM audit_logs WHERE experiment_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (experiment_id, max(int(limit), 1)),
        ).fetchall()
    return [
        {
            "id": row[0],
            "experiment_id": row[1],
            "trial_id": row[2],
            "event_type": row[3],
            "prompt_hash": row[4],
            "response_hash": row[5],
            "payload": json.loads(row[6]),
            "created_at": row[7],
        }
        for row in rows
    ]


def _trial_summary(row: tuple[Any, ...]) -> dict[str, Any]:
    result = json.loads(row[9])
    return {
        "trial_id": row[0],
        "strategy_name": row[1],
        "passed": bool(row[2]),
        "robustness_score": row[3],
        "net_pnl_test": row[4],
        "sharpe_test": row[5],
        "annual_trades_test": row[6],
        "net_pnl_holdout": row[7],
        "reasons": json.loads(row[8]),
        "trial_count": result.get("trial_count", 1),
        "execution_mode": result.get("execution_mode", "bar"),
        "data_version_hash": result.get("data_version_hash"),
        "snapshot": result.get("snapshot", {}),
        "cost_model": result.get("cost_model", {}),
        "parameter_combination_count": result.get("parameter_combination_count", 1),
        "parameter_budget_exceeded": result.get("parameter_budget_exceeded", False),
        "parameter_grid_hash": result.get("parameter_grid_hash"),
        "positive_year_ratio": result.get("positive_year_ratio"),
        "round_trip_cost": result.get("round_trip_cost"),
        "yearly_results": result.get("yearly_results", []),
        "validation_to_test_sharpe_decay": result.get("validation_to_test_sharpe_decay"),
        "test_to_holdout_sharpe_decay": result.get("test_to_holdout_sharpe_decay"),
        "overlapping_test_folds": result.get("overlapping_test_folds", False),
        "non_overlap_test_fold_indexes": result.get("non_overlap_test_fold_indexes", []),
        "net_pnl_validation": result.get("aggregate_validation_metrics", {}).get("net_pnl"),
        "sharpe_validation": result.get("aggregate_validation_metrics", {}).get("sharpe"),
        "net_pnl_non_overlap_test": result.get("non_overlap_test_metrics", {}).get("net_pnl"),
        "sharpe_non_overlap_test": result.get("non_overlap_test_metrics", {}).get("sharpe"),
        "annual_trades_non_overlap_test": (
            result.get("non_overlap_test_metrics", {}).get("annual_trades")
        ),
    }
