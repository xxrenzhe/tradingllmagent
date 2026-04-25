from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);
"""


def connect_task_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    return connection


def create_task(
    path: Path,
    task_type: str,
    payload: dict[str, Any],
    status: str = "queued",
    task_id: str | None = None,
) -> dict[str, Any]:
    now = _now()
    task_id = task_id or uuid.uuid4().hex
    with connect_task_db(path) as connection:
        connection.execute(
            """
            INSERT INTO tasks (
                task_id, task_type, status, payload_json, result_json,
                error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                task_id,
                task_type,
                status,
                json.dumps(payload, sort_keys=True, default=str),
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO task_logs (task_id, level, message, created_at) VALUES (?, ?, ?, ?)",
            (task_id, "info", f"Task {task_type} created", now),
        )
    return get_task(path, task_id)


def get_task(path: Path, task_id: str) -> dict[str, Any]:
    with connect_task_db(path) as connection:
        row = connection.execute(
            """
            SELECT task_id, task_type, status, payload_json, result_json,
                   error, created_at, updated_at
            FROM tasks WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
    if row is None:
        raise KeyError(f"Unknown task_id: {task_id}")
    return _task_from_row(row)


def list_tasks(path: Path, limit: int = 100) -> list[dict[str, Any]]:
    with connect_task_db(path) as connection:
        rows = connection.execute(
            """
            SELECT task_id, task_type, status, payload_json, result_json,
                   error, created_at, updated_at
            FROM tasks
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_task_from_row(row) for row in rows]


def update_task(
    path: Path,
    task_id: str,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    current = get_task(path, task_id)
    if current["status"] in TERMINAL_STATUSES:
        raise ValueError(f"Task {task_id} is already terminal: {current['status']}")
    now = _now()
    with connect_task_db(path) as connection:
        connection.execute(
            """
            UPDATE tasks
            SET status = ?, result_json = ?, error = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (
                status,
                json.dumps(result, sort_keys=True, default=str) if result is not None else None,
                error,
                now,
                task_id,
            ),
        )
        connection.execute(
            "INSERT INTO task_logs (task_id, level, message, created_at) VALUES (?, ?, ?, ?)",
            (task_id, "info" if error is None else "error", f"Task status changed to {status}", now),
        )
    return get_task(path, task_id)


def cancel_task(path: Path, task_id: str) -> dict[str, Any]:
    current = get_task(path, task_id)
    if current["status"] in TERMINAL_STATUSES:
        return current
    return update_task(path, task_id, "cancelled")


def append_task_log(path: Path, task_id: str, message: str, level: str = "info") -> dict[str, Any]:
    get_task(path, task_id)
    with connect_task_db(path) as connection:
        connection.execute(
            "INSERT INTO task_logs (task_id, level, message, created_at) VALUES (?, ?, ?, ?)",
            (task_id, level, message, _now()),
        )
    return {"task_id": task_id, "level": level, "message": message}


def get_task_logs(path: Path, task_id: str, limit: int = 200) -> list[dict[str, Any]]:
    get_task(path, task_id)
    with connect_task_db(path) as connection:
        rows = connection.execute(
            """
            SELECT level, message, created_at
            FROM task_logs
            WHERE task_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (task_id, limit),
        ).fetchall()
    return [
        {
            "task_id": task_id,
            "level": row[0],
            "message": row[1],
            "created_at": row[2],
        }
        for row in rows
    ]


def _task_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "task_id": row[0],
        "task_type": row[1],
        "status": row[2],
        "payload": json.loads(row[3]),
        "result": json.loads(row[4]) if row[4] else None,
        "error": row[5],
        "created_at": row[6],
        "updated_at": row[7],
    }


def _now() -> str:
    return datetime.now(UTC).isoformat()
