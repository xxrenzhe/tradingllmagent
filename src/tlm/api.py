from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from time import sleep

from .calibration import build_cost_calibration_artifact
from .config import get_cost_model, get_symbol, load_symbols
from .events import (
    build_event_context_rows,
    load_event_calendar,
    validate_event_calendar,
)
from .experiments import load_experiment_audit_logs, load_experiment_summary
from .feature_catalog import FEATURE_CATALOG, feature_readiness_report
from .execution import (
    build_execution_intent_response_with_registry,
    build_execution_intent_response,
    evaluate_live_readiness,
    load_risk_profile_registry,
    list_approval_queue,
    submit_paper_shadow,
    write_risk_profile_registry,
)
from .modules import (
    build_target_frequency_pool,
    discover_module_memory_files,
    load_module_performance_memory,
    strategy_module_catalog,
    summarize_module_performance,
)
from .monitor import build_monitor_report
from .nt_gateway import Nt8SimGateway
from .paper import (
    build_paper_replay_attribution,
    export_ninjatrader_signals,
    load_backtest_result,
    replay_trades,
)
from .readiness import build_external_validation_artifact
from .research import load_leaderboard_report, load_research_artifacts
from .storage import bar_path
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
from .trigger_gate import (
    append_trigger_gate_outcome_from_payload,
    build_forward_test_schedule,
    build_trigger_gate_memory_view,
    load_trigger_gate_forward_report,
    run_trigger_gate_simulation,
)
from .vol import (
    build_vol_cost_stress_report,
    build_vol_feature_readiness,
    build_vol_mutation_memory,
    build_vol_paper_shadow_review,
    build_vol_quote_replay_report,
    build_vol_strategy_leaderboard,
)
from .worker import run_task, worker_loop


def build_paper_replay_response(payload: dict) -> dict:
    strategy_id = payload.get("strategy_id")
    if not strategy_id:
        raise ValueError("strategy_id is required")
    starting_equity = float(payload.get("starting_equity", 100_000))
    result = load_backtest_result(Path(strategy_id))
    replay = replay_trades(result["trades"], starting_equity=starting_equity)
    response = replay.to_dict()
    response["replay_attribution"] = build_paper_replay_attribution(result, replay)
    return response


def build_feature_readiness_response() -> dict:
    report = feature_readiness_report()
    report["features"] = [feature.to_dict() for feature in FEATURE_CATALOG]
    manifest_path = Path("strategies/generated/feature_combo_manifest.json")
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        report["generated_strategy_manifest"] = manifest
        report["generated_strategy_summary"] = {
            "method": manifest.get("method"),
            "strategy_count": len(manifest.get("strategies", [])),
            "complexity_limit": manifest.get("complexity_limit"),
            "max_complexity_score": manifest.get("max_complexity_score"),
            "random_seed": manifest.get("random_seed"),
        }
    else:
        report["generated_strategy_manifest"] = {}
        report["generated_strategy_summary"] = {
            "method": "none",
            "strategy_count": 0,
            "complexity_limit": None,
            "max_complexity_score": None,
            "random_seed": None,
        }
    return report


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
        for key in [
            "leaderboard",
            "candidate_leaderboard",
            "freeze_confirmed_leaderboard",
            "rejected",
            "rows",
        ]:
            report[key] = [
                row
                for row in report[key]
                if row["experiment_id"] == experiment_id
                or row["experiment_id"].startswith(f"{experiment_id}_")
            ]
        report["summary"] = {
            "passed": len(report["leaderboard"]),
            "candidate": len(report["candidate_leaderboard"]),
            "freeze_confirmed": len(report["freeze_confirmed_leaderboard"]),
            "rejected": len(report["rejected"]),
            "total": len(report["rows"]),
        }
        report["conclusion"] = (
            "qualified_strategies_found"
            if report["leaderboard"]
            else "no_qualified_strategies_found"
        )
        report["message"] = (
            f"Found {len(report['leaderboard'])} freeze-confirmed qualified strategies."
            if report["leaderboard"]
            else "No qualified strategies found under the current out-of-sample gates."
        )
    return report


def build_experiment_artifacts_response(
    experiments_root: Path,
    experiment_id: str,
    row_limit: int = 2_000,
) -> dict:
    return load_research_artifacts(experiments_root, experiment_id, row_limit=row_limit)


def build_event_list_response(
    calendar_path: Path,
    symbol: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    calendar = load_event_calendar(calendar_path)
    from_date = datetime_from_date(date_from) if date_from else None
    to_date = datetime_from_date(date_to, end_of_day=True) if date_to else None
    rows = []
    for event in calendar["events"]:
        if symbol and symbol not in event.affected_symbols and "*" not in event.affected_symbols:
            continue
        if from_date and event.timestamp_utc < from_date:
            continue
        if to_date and event.timestamp_utc > to_date:
            continue
        rows.append(event.to_dict())
    return {
        "calendar_id": calendar["calendar_id"],
        "event_calendar_hash": calendar["event_calendar_hash"],
        "events": rows,
        "event_count": len(rows),
    }


def build_event_context_response(payload: dict) -> dict:
    from .cli import parse_date

    calendar_path = Path(payload.get("calendar", "configs/macro_events.yaml"))
    symbol = str(payload.get("symbol", ""))
    timeframe = str(payload.get("timeframe", "5m"))
    if not symbol:
        raise ValueError("symbol is required")
    if not payload.get("date_from") or not payload.get("date_to"):
        raise ValueError("date_from and date_to are required")
    data_root = Path(payload.get("data_root", "data"))
    calendar = load_event_calendar(calendar_path)
    start = parse_date(payload["date_from"])
    end = parse_date(payload["date_to"])
    files = []
    day = start
    while day <= end:
        files.append(bar_path(data_root, symbol, timeframe, day))
        day = day + timedelta(days=1)
    contexts = build_event_context_rows(symbol, files, calendar["events"])
    return {
        "calendar_id": calendar["calendar_id"],
        "event_calendar_hash": calendar["event_calendar_hash"],
        "symbol": symbol,
        "timeframe": timeframe,
        "contexts": [context.to_dict() for context in contexts],
        "context_count": len(contexts),
    }


def build_module_memory_response(experiments_root: Path) -> dict:
    records = load_module_performance_memory(discover_module_memory_files(experiments_root))
    summary = summarize_module_performance(records)
    summary["target_frequency_pool"] = build_target_frequency_pool(records)
    return summary


def build_trigger_gate_simulation_response(payload: dict) -> dict:
    if payload.get("target_frequency_pool"):
        target_frequency_pool = payload["target_frequency_pool"]
    else:
        experiments_root = Path(payload.get("experiments_root", "experiments"))
        records = load_module_performance_memory(discover_module_memory_files(experiments_root))
        target_frequency_pool = build_target_frequency_pool(
            records,
            target_min_per_day=float(payload.get("target_min_per_day", 2.0)),
            target_max_per_day=float(payload.get("target_max_per_day", 3.0)),
            min_proxy_win_rate=float(payload.get("min_proxy_win_rate", 0.53)),
            lookback_days=int(payload.get("lookback_days", 90)),
            require_passed=not bool(payload.get("include_rejected", False)),
        )
    output_dir = payload.get("output_dir")
    if not output_dir:
        raise ValueError("output_dir is required")
    return run_trigger_gate_simulation(
        target_frequency_pool=target_frequency_pool,
        output_dir=Path(output_dir),
        replay_start=str(payload.get("from") or payload.get("date_from") or ""),
        replay_end=str(payload.get("to") or payload.get("date_to") or ""),
        enable_llm=bool(payload.get("enable_llm", False)),
        step_minutes=int(payload.get("step_minutes", 15)),
        model=str(payload.get("model", "local-trigger-gate")),
        daily_token_budget=(
            int(payload["daily_token_budget"])
            if payload.get("daily_token_budget") is not None
            else None
        ),
    )


def build_trigger_gate_report_response(payload: dict) -> dict:
    output_dir = payload.get("output_dir")
    if not output_dir:
        raise ValueError("output_dir is required")
    return load_trigger_gate_forward_report(
        Path(output_dir),
        previous_pool=payload.get("previous_pool"),
    )


def build_trigger_gate_schedule_response(payload: dict) -> dict:
    target_frequency_pool = payload.get("target_frequency_pool")
    if not isinstance(target_frequency_pool, dict):
        experiments_root = Path(payload.get("experiments_root", "experiments"))
        records = load_module_performance_memory(discover_module_memory_files(experiments_root))
        target_frequency_pool = build_target_frequency_pool(
            records,
            target_min_per_day=float(payload.get("target_min_per_day", 2.0)),
            target_max_per_day=float(payload.get("target_max_per_day", 3.0)),
            min_proxy_win_rate=float(payload.get("min_proxy_win_rate", 0.53)),
            lookback_days=int(payload.get("lookback_days", 90)),
            require_passed=not bool(payload.get("include_rejected", False)),
        )
    output_root = payload.get("output_root")
    if not output_root:
        raise ValueError("output_root is required")
    return build_forward_test_schedule(
        target_frequency_pool=target_frequency_pool,
        as_of=str(payload.get("as_of") or ""),
        output_root=Path(output_root),
        enable_llm=bool(payload.get("enable_llm", False)),
        daily_token_budget=(
            int(payload["daily_token_budget"])
            if payload.get("daily_token_budget") is not None
            else None
        ),
    )


def build_trigger_gate_memory_response(payload: dict) -> dict:
    output_dir = payload.get("output_dir")
    if not output_dir:
        raise ValueError("output_dir is required")
    return build_trigger_gate_memory_view(
        Path(output_dir),
        strategy_spec_hash=payload.get("strategy_spec_hash"),
        module_id=payload.get("module_id"),
        decision=payload.get("decision"),
        risk_level=payload.get("risk_level"),
        outcome_label=payload.get("outcome_label"),
        limit=int(payload.get("limit", 100)),
    )


def build_trigger_gate_outcome_response(payload: dict) -> dict:
    output_dir = payload.get("output_dir")
    if not output_dir:
        raise ValueError("output_dir is required")
    return append_trigger_gate_outcome_from_payload(Path(output_dir), payload)


def build_monitor_report_response(payload: dict) -> dict:
    from .cli import parse_date

    symbol = str(payload.get("symbol", ""))
    timeframe = str(payload.get("timeframe", "5m"))
    date_value = payload.get("date")
    if not symbol:
        raise ValueError("symbol is required")
    if not date_value:
        raise ValueError("date is required")
    data_root = Path(payload.get("data_root", "data"))
    files = [bar_path(data_root, symbol, timeframe, parse_date(date_value))]
    events = []
    if payload.get("calendar"):
        events = load_event_calendar(Path(payload["calendar"]))["events"]
    return build_monitor_report(
        symbol=symbol,
        timeframe=timeframe,
        bar_files=files,
        events=events,
        proximity_points=float(payload.get("proximity_points", 2.0)),
    )


def build_cost_calibration_response(payload: dict) -> dict:
    cost_model_name = str(payload.get("cost_model", "nq_conservative_v1"))
    config_dir = Path(payload.get("config_dir", "configs"))
    baseline = get_cost_model(cost_model_name, config_dir)
    return build_cost_calibration_artifact(
        baseline=baseline,
        samples=payload.get("samples", []),
        data_quality_report=dict(payload.get("data_quality_report") or {}),
        proxy_instrument=str(payload.get("proxy_instrument", "USATECHIDXUSD")),
        executable_instrument=str(payload.get("executable_instrument", "CME_NQ")),
    )


def build_vol_overview_response(
    experiments_root: Path = Path("experiments"),
    symbol: str = "NQ_CME",
    config_dir: Path = Path("configs"),
) -> dict:
    symbol_config = get_symbol(symbol, config_dir)
    feature_readiness = build_vol_feature_readiness()
    leaderboard = build_vol_strategy_leaderboard(experiments_root)
    return {
        "feature_readiness": feature_readiness,
        "strategy_leaderboard": leaderboard,
        "cost_stress": build_vol_cost_stress_report(leaderboard, symbol_config),
        "quote_replay": build_vol_quote_replay_report(quote_files=[]),
        "paper_shadow": build_vol_paper_shadow_review([]),
        "mutation_memory": build_vol_mutation_memory(leaderboard),
    }


def datetime_from_date(value: str, end_of_day: bool = False):
    from datetime import datetime, time

    date_value = datetime.fromisoformat(value).date()
    return datetime.combine(date_value, time.max if end_of_day else time.min)


def create_app():
    try:
        from fastapi import Body, FastAPI, HTTPException
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import StreamingResponse
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "FastAPI is not installed. Install the API extras before running the server."
        ) from exc

    nt8_gateway = Nt8SimGateway()

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

    @app.post("/api/data/import-firstrate")
    def data_import_firstrate(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "data.import_firstrate", payload)

    @app.post("/api/data/bar-quality")
    def data_bar_quality(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "data.bar_quality", payload)

    @app.post("/api/data/split-manifest")
    def data_split_manifest(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "data.split_manifest", payload)

    @app.post("/api/data/import-databento-quotes")
    def data_import_databento_quotes(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "data.import_databento_quotes", payload)

    @app.post("/api/data/import-databento-tbbo")
    def data_import_databento_tbbo(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "data.import_databento_tbbo", payload)

    @app.post("/api/data/import-databento-ohlcv")
    def data_import_databento_ohlcv(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "data.import_databento_ohlcv", payload)

    @app.post("/api/data/quote-replay")
    def data_quote_replay(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "data.quote_replay", payload)

    @app.get("/api/data/quality")
    def data_quality(symbol: str, date_from: str, date_to: str, data_root: str = "data") -> dict:
        from .cli import parse_date, tick_parquet_files
        from .quality import build_quality_report

        report = build_quality_report(
            symbol,
            tick_parquet_files(Path(data_root), symbol, parse_date(date_from), parse_date(date_to)),
        )
        return report.to_dict()

    @app.post("/api/events/validate")
    def events_validate(payload: dict = Body(...)) -> dict:
        try:
            calendar = payload.get("calendar")
            if not calendar:
                raise ValueError("calendar is required")
            return validate_event_calendar(Path(calendar))
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/events")
    def events_list(
        calendar: str = "configs/macro_events.yaml",
        symbol: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        try:
            return build_event_list_response(Path(calendar), symbol, date_from, date_to)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/events/context")
    def events_context(payload: dict = Body(...)) -> dict:
        try:
            return build_event_context_response(payload)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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

    @app.post("/api/experiments/proposals")
    def experiments_proposals(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "research.propose", payload)

    @app.post("/api/experiments/iterations")
    def experiments_iterations(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "research.iterate", payload)

    @app.post("/api/experiments/target-discovery")
    def experiments_target_discovery(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "research.discover_target", payload)

    @app.get("/api/vol/overview")
    def vol_overview(
        experiments_root: str = "experiments",
        symbol: str = "NQ_CME",
        config_dir: str = "configs",
    ) -> dict:
        try:
            return build_vol_overview_response(Path(experiments_root), symbol=symbol, config_dir=Path(config_dir))
        except (ValueError, KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/vol/readiness")
    def vol_readiness(payload: dict = Body(default={}), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "features.vol_readiness", payload)

    @app.post("/api/vol/seed-search")
    def vol_seed_search(payload: dict = Body(default={}), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "research.vol_seed_search", payload)

    @app.post("/api/vol/pre-screen")
    def vol_pre_screen(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "research.vol_prescreen", payload)

    @app.post("/api/vol/cost-stress")
    def vol_cost_stress(payload: dict = Body(default={}), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "research.vol_cost_stress", payload)

    @app.post("/api/vol/artifacts")
    def vol_artifacts(payload: dict = Body(default={}), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "research.vol_artifacts", payload)

    @app.post("/api/vol/quote-fill-replay")
    def vol_quote_fill_replay(payload: dict = Body(default={}), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "execution.quote_fill_replay", payload)

    @app.post("/api/vol/paper-shadow-review")
    def vol_paper_shadow_review(payload: dict = Body(default={}), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "paper.vol_shadow_review", payload)

    @app.post("/api/vol/mutation-backfill")
    def vol_mutation_backfill(payload: dict = Body(default={}), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "memory.vol_mutation_backfill", payload)

    @app.post("/api/monitor/once")
    def monitor_once(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "monitor.once", payload)

    @app.post("/api/monitor/run")
    def monitor_run(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "monitor.run", payload)

    @app.post("/api/monitor/replay")
    def monitor_replay(payload: dict = Body(...), task_db: str = "experiments/tasks.sqlite3") -> dict:
        return create_task(Path(task_db), "monitor.replay", payload)

    @app.post("/api/monitor/report")
    def monitor_report(payload: dict = Body(...)) -> dict:
        try:
            return build_monitor_report_response(payload)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/execution/intents")
    def execution_intents(
        payload: dict = Body(...),
        risk_profile_registry: str = "configs/risk_profiles.json",
    ) -> dict:
        try:
            if payload.get("risk_profile_id") and not payload.get("risk_profile"):
                return build_execution_intent_response_with_registry(payload, Path(risk_profile_registry))
            return build_execution_intent_response(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/execution/paper-shadow")
    def execution_paper_shadow(
        payload: dict = Body(...),
        audit_path: str = "experiments/execution_audit.jsonl",
    ) -> dict:
        try:
            return submit_paper_shadow(payload, Path(audit_path))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/execution/readiness")
    def execution_readiness(payload: dict = Body(...)) -> dict:
        try:
            return evaluate_live_readiness(str(payload.get("stage", "")), dict(payload.get("evidence") or {}))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/execution/approval-queue")
    def execution_approval_queue(
        execution_db: str = "experiments/execution.sqlite3",
        limit: int = 100,
    ) -> dict:
        return list_approval_queue(Path(execution_db), limit=limit)

    @app.get("/api/execution/risk-profiles")
    def execution_risk_profiles(risk_profile_registry: str = "configs/risk_profiles.json") -> dict:
        return load_risk_profile_registry(Path(risk_profile_registry))

    @app.post("/api/execution/risk-profiles")
    def execution_risk_profiles_write(
        payload: dict = Body(...),
        risk_profile_registry: str = "configs/risk_profiles.json",
    ) -> dict:
        try:
            return write_risk_profile_registry(Path(risk_profile_registry), payload.get("profiles", []))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/readiness/external-validation")
    def readiness_external_validation(payload: dict = Body(...)) -> dict:
        try:
            return build_external_validation_artifact(
                str(payload.get("stage", "")),
                dict(payload.get("evidence") or {}),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/calibration/costs")
    def calibration_costs(payload: dict = Body(...)) -> dict:
        try:
            return build_cost_calibration_response(payload)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/modules")
    def modules_list() -> dict:
        return {"modules": strategy_module_catalog()}

    @app.get("/api/modules/memory")
    def modules_memory(experiments_root: str = "experiments") -> dict:
        return build_module_memory_response(Path(experiments_root))

    @app.get("/api/features/readiness")
    def features_readiness() -> dict:
        return build_feature_readiness_response()

    @app.post("/api/trigger-gate/simulations")
    def trigger_gate_simulations(payload: dict = Body(...)) -> dict:
        try:
            return build_trigger_gate_simulation_response(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/trigger-gate/reports")
    def trigger_gate_reports(payload: dict = Body(...)) -> dict:
        try:
            return build_trigger_gate_report_response(payload)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/trigger-gate/schedules")
    def trigger_gate_schedules(payload: dict = Body(...)) -> dict:
        try:
            return build_trigger_gate_schedule_response(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/trigger-gate/memory")
    def trigger_gate_memory(payload: dict = Body(...)) -> dict:
        try:
            return build_trigger_gate_memory_response(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/trigger-gate/outcomes")
    def trigger_gate_outcomes(payload: dict = Body(...)) -> dict:
        try:
            return build_trigger_gate_outcome_response(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/gateways/nt8/health")
    def nt8_health() -> dict:
        return nt8_gateway.health()

    @app.get("/api/gateways/nt8/accounts")
    def nt8_accounts() -> dict:
        return {"accounts": nt8_gateway.accounts}

    @app.get("/api/gateways/nt8/instruments")
    def nt8_instruments() -> dict:
        return {"instruments": nt8_gateway.instruments}

    @app.post("/api/gateways/nt8/commands")
    def nt8_commands(payload: dict = Body(...)) -> dict:
        return nt8_gateway.execute(payload)

    @app.get("/api/gateways/nt8/reconciliation")
    def nt8_reconciliation() -> dict:
        return nt8_gateway.reconciliation_report()

    @app.post("/api/gateways/nt8/reconciliation")
    def nt8_reconciliation_post(payload: dict = Body(default={})) -> dict:
        return nt8_gateway.reconciliation_report(payload.get("expected_positions", []))

    @app.get("/api/gateways/nt8/order-updates")
    def nt8_order_updates() -> dict:
        return nt8_gateway.order_updates()

    @app.get("/api/gateways/nt8/incidents")
    def nt8_incidents() -> dict:
        return {"incidents": nt8_gateway.incident_events, "count": len(nt8_gateway.incident_events)}

    @app.post("/api/incidents")
    def nt8_incident(payload: dict = Body(...)) -> dict:
        event_type = str(payload.get("event_type", "manual_incident"))
        reason = str(payload.get("reason", "manual"))
        if event_type == "read_only":
            return nt8_gateway.set_read_only(True, reason)
        if event_type == "safe_mode":
            return nt8_gateway.enter_safe_mode(reason)
        return nt8_gateway.enter_safe_mode(f"{event_type}:{reason}")

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

    @app.get("/api/experiments/{experiment_id}/artifacts")
    def experiments_artifacts(
        experiment_id: str,
        experiments_root: str = "experiments",
        row_limit: int = 2_000,
    ) -> dict:
        try:
            return build_experiment_artifacts_response(
                Path(experiments_root),
                experiment_id,
                row_limit=row_limit,
            )
        except FileNotFoundError as exc:
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
