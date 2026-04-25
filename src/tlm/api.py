from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from time import sleep

from .config import load_symbols
from .experiments import load_experiment_audit_logs, load_experiment_summary
from .paper import export_ninjatrader_signals, load_backtest_result, replay_trades
from .research import load_leaderboard_report
from .strategy import StrategySpecError, load_strategy_spec
from .tasks import (
    TERMINAL_STATUSES,
    cancel_task,
    create_task,
    encode_sse_event,
    get_task,
    get_task_logs,
    list_tasks,
)
from .worker import run_task, worker_loop


def build_paper_replay_response(payload: dict) -> dict:
    strategy_id = payload.get("strategy_id")
    if not strategy_id:
        raise ValueError("strategy_id is required")
    starting_equity = float(payload.get("starting_equity", 100_000))
    result = load_backtest_result(Path(strategy_id))
    return replay_trades(result["trades"], starting_equity=starting_equity).to_dict()


def build_nt_export_signal_response(payload: dict) -> dict:
    strategy_id = payload.get("strategy_id")
    export_format = payload.get("format")
    account = payload.get("account")
    instrument = payload.get("instrument")
    if not strategy_id:
        raise ValueError("strategy_id is required")
    if export_format not in {"csv", "oif"}:
        raise ValueError("format must be csv or oif")
    if not account:
        raise ValueError("account is required")
    if not instrument:
        raise ValueError("instrument is required")
    result = load_backtest_result(Path(strategy_id))
    content = export_ninjatrader_signals(
        result["trades"],
        export_format=export_format,
        account=account,
        instrument=instrument,
    )
    return {
        "format": export_format,
        "account": account,
        "instrument": instrument,
        "content": content,
        "line_count": len([line for line in content.splitlines() if line.strip()]),
    }


def build_leaderboard_response(experiments_root: Path, experiment_id: str | None = None) -> dict:
    report = load_leaderboard_report(experiments_root)
    if experiment_id:
        for key in ["leaderboard", "rejected", "rows"]:
            report[key] = [
                row
                for row in report[key]
                if row["experiment_id"] == experiment_id
                or row["experiment_id"].startswith(f"{experiment_id}_")
            ]
        report["summary"] = {
            "passed": len(report["leaderboard"]),
            "rejected": len(report["rejected"]),
            "total": len(report["rows"]),
        }
        report["conclusion"] = (
            "qualified_strategies_found"
            if report["leaderboard"]
            else "no_qualified_strategies_found"
        )
        report["message"] = (
            f"Found {len(report['leaderboard'])} qualified strategies."
            if report["leaderboard"]
            else "No qualified strategies found under the current out-of-sample gates."
        )
    return report


def create_app():
    try:
        from fastapi import Body, FastAPI, HTTPException
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import StreamingResponse
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "FastAPI is not installed. Install the API extras before running the server."
        ) from exc

    @asynccontextmanager
    async def lifespan(_app):
        task_db = Path(os.environ.get("TLM_TASK_DB", "experiments/tasks.sqlite3"))
        stop_event = asyncio.Event()
        worker = asyncio.create_task(worker_loop(task_db, stop_event))
        try:
            yield
        finally:
            stop_event.set()
            await worker

    app = FastAPI(title="Trading LLM Agent", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:4173",
            "http://localhost:4173",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/data/symbols")
    def data_symbols(config_dir: str = "configs") -> dict:
        symbols = load_symbols(Path(config_dir))
        return {
            "symbols": {
                alias: {
                    "provider": symbol.provider,
                    "instrument": symbol.instrument,
                    "description": symbol.description,
                    "price_scale": symbol.price_scale,
                }
                for alias, symbol in symbols.items()
            }
        }

    @app.get("/api/tasks")
    def tasks(task_db: str = "experiments/tasks.sqlite3", limit: int = 100) -> dict:
        return {"tasks": list_tasks(Path(task_db), limit=limit)}

    @app.get("/api/tasks/{task_id}")
    def task_status(task_id: str, task_db: str = "experiments/tasks.sqlite3") -> dict:
        try:
            return get_task(Path(task_db), task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/tasks/{task_id}/cancel")
    def task_cancel(task_id: str, task_db: str = "experiments/tasks.sqlite3") -> dict:
        try:
            return cancel_task(Path(task_db), task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/tasks/{task_id}/run")
    def task_run(task_id: str, task_db: str = "experiments/tasks.sqlite3") -> dict:
        try:
            return run_task(Path(task_db), task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/tasks/{task_id}/logs")
    def task_logs(task_id: str, task_db: str = "experiments/tasks.sqlite3", limit: int = 200) -> dict:
        try:
            return {"logs": get_task_logs(Path(task_db), task_id, limit=limit)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/tasks/{task_id}/events")
    def task_events(
        task_id: str,
        task_db: str = "experiments/tasks.sqlite3",
        poll_seconds: float = 1.0,
    ):
        path = Path(task_db)
        try:
            get_task(path, task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        def event_stream():
            seen_log_count = 0
            while True:
                task = get_task(path, task_id)
                yield encode_sse_event("task", task)
                logs = get_task_logs(path, task_id)
                for log in logs[seen_log_count:]:
                    yield encode_sse_event("log", log)
                seen_log_count = len(logs)
                if task["status"] in TERMINAL_STATUSES:
                    break
                sleep(max(poll_seconds, 0.1))

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/data/download")
    def data_download(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "data.download", payload)

    @app.post("/api/data/build-bars")
    def data_build_bars(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "data.build_bars", payload)

    @app.get("/api/data/quality")
    def data_quality(symbol: str, date_from: str, date_to: str, data_root: str = "data") -> dict:
        from .cli import parse_date, tick_parquet_files
        from .quality import build_quality_report

        report = build_quality_report(
            symbol,
            tick_parquet_files(Path(data_root), symbol, parse_date(date_from), parse_date(date_to)),
        )
        return report.to_dict()

    @app.post("/api/strategies/validate")
    def strategies_validate(payload: dict = Body(...)) -> dict:
        try:
            spec_path = payload.get("spec")
            if not spec_path:
                raise StrategySpecError("spec is required")
            spec = load_strategy_spec(Path(spec_path))
        except StrategySpecError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "status": "valid",
            "name": spec.name,
            "symbol": spec.symbol,
            "strategy_family": spec.strategy_family,
            "timeframe": spec.timeframe,
        }

    @app.post("/api/backtests/bar")
    def backtests_bar(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "backtest.bar", payload)

    @app.post("/api/backtests/tick")
    def backtests_tick(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "backtest.tick", payload)

    @app.post("/api/experiments/research-runs")
    def experiments_research_runs(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "research.run", payload)

    @app.get("/api/experiments/{experiment_id}")
    def experiments_get(
        experiment_id: str,
        experiment_db: str = "experiments/research.sqlite3",
    ) -> dict:
        try:
            return load_experiment_summary(Path(experiment_db), experiment_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/experiments/{experiment_id}/audit-logs")
    def experiments_audit_logs(
        experiment_id: str,
        experiment_db: str = "experiments/research.sqlite3",
        limit: int = 100,
    ) -> dict:
        try:
            return {
                "audit_logs": load_experiment_audit_logs(
                    Path(experiment_db),
                    experiment_id,
                    limit=limit,
                )
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/reports/leaderboard")
    def reports_leaderboard(
        experiments_root: str = "experiments",
        experiment_id: str | None = None,
    ) -> dict:
        return build_leaderboard_response(Path(experiments_root), experiment_id=experiment_id)

    @app.post("/api/paper/replay")
    def paper_replay(payload: dict = Body(...)) -> dict:
        try:
            return build_paper_replay_response(payload)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/paper/nt-export-signal")
    def paper_nt_export_signal(payload: dict = Body(...)) -> dict:
        try:
            return build_nt_export_signal_response(payload)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


try:
    app = create_app()
except RuntimeError:
    app = None
