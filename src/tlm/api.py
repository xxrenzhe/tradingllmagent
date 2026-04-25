from __future__ import annotations

from pathlib import Path

from .config import load_symbols
from .experiments import load_experiment_summary
from .research import load_leaderboard
from .strategy import StrategySpecError, load_strategy_spec
from .tasks import cancel_task, create_task, get_task, get_task_logs, list_tasks


def create_app():
    try:
        from fastapi import Body, FastAPI, HTTPException
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "FastAPI is not installed. Install the API extras before running the server."
        ) from exc

    app = FastAPI(title="Trading LLM Agent", version="0.1.0")

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

    @app.get("/api/tasks/{task_id}/logs")
    def task_logs(task_id: str, task_db: str = "experiments/tasks.sqlite3", limit: int = 200) -> dict:
        try:
            return {"logs": get_task_logs(Path(task_db), task_id, limit=limit)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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

    @app.get("/api/reports/leaderboard")
    def reports_leaderboard(experiments_root: str = "experiments") -> dict:
        return {"rows": load_leaderboard(Path(experiments_root))}

    return app


try:
    app = create_app()
except RuntimeError:
    app = None
