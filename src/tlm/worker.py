from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .backtest import run_bar_backtest, run_tick_backtest
from .cli import bar_parquet_files, parse_date, tick_parquet_files
from .config import get_cost_model, get_symbol
from .paper import export_ninjatrader_signals, load_backtest_result, replay_trades
from .research import run_budgeted_research, write_research_result
from .strategy import load_strategy_spec
from .tasks import append_task_log, claim_queued_task, get_task, next_queued_task, update_task


def run_task(task_db: Path, task_id: str) -> dict[str, Any]:
    task = get_task(task_db, task_id)
    if task["status"] in {"completed", "failed", "cancelled"}:
        return task
    if task["status"] == "queued":
        task = claim_queued_task(task_db, task_id) or get_task(task_db, task_id)
    elif task["status"] != "running":
        update_task(task_db, task_id, "running")
    append_task_log(task_db, task_id, f"Running {task['task_type']}")
    try:
        result = execute_task(task["task_type"], task["payload"])
    except Exception as exc:
        append_task_log(task_db, task_id, str(exc), level="error")
        return update_task(task_db, task_id, "failed", error=str(exc))
    append_task_log(task_db, task_id, f"Completed {task['task_type']}")
    return update_task(task_db, task_id, "completed", result=result)


def run_next_queued_task(task_db: Path) -> dict[str, Any] | None:
    task = next_queued_task(task_db)
    if task is None:
        return None
    claimed = claim_queued_task(task_db, task["task_id"])
    if claimed is None:
        return None
    return run_task(task_db, task["task_id"])


async def worker_loop(task_db: Path, stop_event: asyncio.Event, poll_seconds: float = 1.0) -> None:
    while not stop_event.is_set():
        ran = await asyncio.to_thread(run_next_queued_task, task_db)
        if ran is None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(poll_seconds, 0.1))
            except TimeoutError:
                pass


def execute_task(task_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if task_type == "strategy.validate":
        return execute_strategy_validate(payload)
    if task_type == "backtest.bar":
        return execute_backtest(payload, execution_mode="bar")
    if task_type == "backtest.tick":
        return execute_backtest(payload, execution_mode="tick")
    if task_type == "paper.replay":
        return execute_paper_replay(payload)
    if task_type == "paper.nt_export_signal":
        return execute_nt_export_signal(payload)
    if task_type == "research.run":
        return execute_research_run(payload)
    raise ValueError(f"Unsupported task_type: {task_type}")


def execute_strategy_validate(payload: dict[str, Any]) -> dict[str, Any]:
    spec = load_strategy_spec(Path(_required(payload, "spec")))
    return {
        "status": "valid",
        "name": spec.name,
        "symbol": spec.symbol,
        "strategy_family": spec.strategy_family,
        "timeframe": spec.timeframe,
    }


def execute_backtest(payload: dict[str, Any], execution_mode: str) -> dict[str, Any]:
    spec = load_strategy_spec(Path(_required(payload, "spec")))
    config_dir = Path(payload.get("config_dir", "configs"))
    data_root = Path(payload.get("data_root", "data"))
    date_from = parse_date(_required(payload, "date_from", "from"))
    date_to = parse_date(_required(payload, "date_to", "to"))
    symbol = get_symbol(payload.get("symbol", spec.symbol), config_dir)
    cost_model = get_cost_model(spec.cost_model, config_dir)
    if execution_mode == "bar":
        files = bar_parquet_files(data_root, spec.symbol, spec.timeframe, date_from, date_to)
        result = run_bar_backtest(
            spec,
            symbol,
            files,
            starting_equity=float(payload.get("starting_equity", 100_000)),
            cost_model=cost_model,
        )
        return result.to_dict()
    files = tick_parquet_files(data_root, spec.symbol, date_from, date_to)
    result = run_tick_backtest(
        spec,
        symbol,
        files,
        starting_equity=float(payload.get("starting_equity", 100_000)),
        cost_model=cost_model,
    )
    return result.to_dict()


def execute_paper_replay(payload: dict[str, Any]) -> dict[str, Any]:
    result = load_backtest_result(Path(_required(payload, "strategy_id")))
    return replay_trades(
        result["trades"],
        starting_equity=float(payload.get("starting_equity", 100_000)),
    ).to_dict()


def execute_nt_export_signal(payload: dict[str, Any]) -> dict[str, Any]:
    result = load_backtest_result(Path(_required(payload, "strategy_id")))
    export_format = _required(payload, "format")
    content = export_ninjatrader_signals(
        result["trades"],
        export_format=export_format,
        account=_required(payload, "account"),
        instrument=_required(payload, "instrument"),
    )
    return {
        "format": export_format,
        "content": content,
        "line_count": len([line for line in content.splitlines() if line.strip()]),
    }


def execute_research_run(payload: dict[str, Any]) -> dict[str, Any]:
    spec = load_strategy_spec(Path(_required(payload, "spec")))
    config_dir = Path(payload.get("config_dir", "configs"))
    data_root = Path(payload.get("data_root", "data"))
    experiments_root = Path(payload.get("experiments_root", "experiments"))
    date_from = parse_date(_required(payload, "date_from", "from"))
    date_to = parse_date(_required(payload, "date_to", "to"))
    experiment_id = payload.get("experiment_id") or f"{spec.name}_{date_from}_{date_to}"
    symbol = get_symbol(payload.get("symbol", spec.symbol), config_dir)
    cost_model = get_cost_model(spec.cost_model, config_dir)
    results = run_budgeted_research(
        seed_spec=spec,
        symbol_config=symbol,
        data_root=data_root,
        experiment_id=experiment_id,
        date_from=date_from,
        date_to=date_to,
        max_trials=int(payload.get("max_trials", 1)),
        starting_equity=float(payload.get("starting_equity", 100_000)),
        train_days=int(payload.get("train_days", 730)),
        validation_days=int(payload.get("validation_days", 182)),
        test_days=int(payload.get("test_days", 182)),
        step_days=int(payload.get("step_days", 91)),
        embargo_days=int(payload.get("embargo_days", 5)),
        final_holdout_days=int(payload.get("final_holdout_days", 365)),
        min_folds=int(payload.get("min_folds", 1)),
        max_parameter_combinations=int(payload.get("max_parameter_combinations", 50)),
        allow_high_parameter_budget=bool(payload.get("allow_high_parameter_budget", False)),
        execution_mode=payload.get("execution_mode", "bar"),
        cost_model=cost_model,
        config_dir=config_dir,
        random_seed=int(payload.get("random_seed", 0)),
    )
    result_paths = []
    for result in results:
        output_path = experiments_root / result.experiment_id / "leaderboard.json"
        write_research_result(output_path, result)
        result_paths.append(str(output_path))
    return {"trials": len(results), "result_paths": result_paths}


def _required(payload: dict[str, Any], key: str, *aliases: str) -> str:
    for candidate in (key, *aliases):
        value = payload.get(candidate)
        if value:
            return str(value)
    raise ValueError(f"{key} is required")
