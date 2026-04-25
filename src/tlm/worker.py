from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .bars import build_minute_bars_from_parquet
from .backtest import run_bar_backtest, run_tick_backtest
from .cli import bar_parquet_files, day_bounds, parse_date, tick_parquet_files
from .cli_dates import iter_dates
from .config import get_cost_model, get_symbol
from .dukascopy import download_hour, iter_hours, parse_bi5_file
from .paper import export_ninjatrader_signals, load_backtest_result, replay_trades
from .research import run_budgeted_research, write_research_result
from .storage import (
    bar_path,
    compute_data_version_hash,
    normalized_tick_path,
    write_ticks_parquet,
)
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
    if task_type == "data.download":
        return execute_data_download(payload)
    if task_type == "data.build_bars":
        return execute_data_build_bars(payload)
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


def execute_data_download(payload: dict[str, Any]) -> dict[str, Any]:
    granularity = str(payload.get("granularity", "tick"))
    if granularity != "tick":
        raise ValueError("Only granularity=tick is supported")
    config_dir = Path(payload.get("config_dir", "configs"))
    data_root = Path(payload.get("data_root", "data"))
    symbol_alias = _required(payload, "symbol")
    symbol = get_symbol(symbol_alias, config_dir)
    date_from = parse_date(_required(payload, "date_from", "from"))
    date_to = parse_date(_required(payload, "date_to", "to"))

    total_ticks = 0
    status_counts: dict[str, int] = {}
    outputs: list[dict[str, Any]] = []
    for day in iter_dates(date_from, date_to):
        start, end = day_bounds(day)
        day_ticks = []
        raw_paths: list[Path] = []
        day_status_counts: dict[str, int] = {}
        for hour in iter_hours(start, end):
            result = download_hour(symbol, hour, data_root)
            status_counts[result.status] = status_counts.get(result.status, 0) + 1
            day_status_counts[result.status] = day_status_counts.get(result.status, 0) + 1
            if result.status in {"downloaded", "cached"}:
                raw_paths.append(result.path)
                day_ticks.extend(parse_bi5_file(result.path, hour, symbol.price_scale))

        output = normalized_tick_path(data_root, symbol_alias, day)
        write_ticks_parquet(output, symbol_alias, day_ticks)
        total_ticks += len(day_ticks)
        metadata = {
            "symbol": symbol_alias,
            "instrument": symbol.instrument,
            "price_scale": symbol.price_scale,
            "day": day.isoformat(),
        }
        outputs.append(
            {
                "date": day.isoformat(),
                "path": str(output),
                "rows": len(day_ticks),
                "raw_files": len(raw_paths),
                "status_counts": day_status_counts,
                "data_version_hash": compute_data_version_hash(raw_paths, metadata),
            }
        )

    return {
        "symbol": symbol_alias,
        "instrument": symbol.instrument,
        "granularity": granularity,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "rows": total_ticks,
        "status_counts": status_counts,
        "outputs": outputs,
    }


def execute_data_build_bars(payload: dict[str, Any]) -> dict[str, Any]:
    timeframe = str(payload.get("timeframe", "1m"))
    if timeframe != "1m":
        raise ValueError("Only timeframe=1m is supported")
    data_root = Path(payload.get("data_root", "data"))
    symbol = _required(payload, "symbol")
    date_from = parse_date(_required(payload, "date_from", "from"))
    date_to = parse_date(_required(payload, "date_to", "to"))

    total_rows = 0
    outputs = []
    for day in iter_dates(date_from, date_to):
        tick_file = normalized_tick_path(data_root, symbol, day)
        output = bar_path(data_root, symbol, timeframe, day)
        rows = build_minute_bars_from_parquet([tick_file], output)
        total_rows += rows
        outputs.append(
            {
                "date": day.isoformat(),
                "path": str(output),
                "rows": rows,
                "source_tick_files": [str(tick_file)] if tick_file.exists() else [],
            }
        )

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "rows": total_rows,
        "outputs": outputs,
    }


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
