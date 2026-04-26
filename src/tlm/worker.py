from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .bars import (
    build_minute_bars_from_parquet,
    build_timeframe_bars_from_1m_parquet,
    timeframe_minutes,
)
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
from .variants import strategy_logic_hash


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
    minutes = timeframe_minutes(timeframe)
    data_root = Path(payload.get("data_root", "data"))
    symbol = _required(payload, "symbol")
    date_from = parse_date(_required(payload, "date_from", "from"))
    date_to = parse_date(_required(payload, "date_to", "to"))

    total_rows = 0
    outputs = []
    for day in iter_dates(date_from, date_to):
        output = bar_path(data_root, symbol, timeframe, day)
        if minutes == 1:
            source_file = normalized_tick_path(data_root, symbol, day)
            rows = build_minute_bars_from_parquet([source_file], output)
        else:
            source_file = bar_path(data_root, symbol, "1m", day)
            rows = build_timeframe_bars_from_1m_parquet([source_file], output, timeframe)
        total_rows += rows
        outputs.append(
            {
                "date": day.isoformat(),
                "path": str(output),
                "rows": rows,
                "source_files": [str(source_file)] if source_file.exists() else [],
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
    specs, deduplication_report = deduplicate_research_specs(_research_specs(payload))
    config_dir = Path(payload.get("config_dir", "configs"))
    data_root = Path(payload.get("data_root", "data"))
    experiments_root = Path(payload.get("experiments_root", "experiments"))
    date_from = parse_date(_required(payload, "date_from", "from"))
    date_to = parse_date(_required(payload, "date_to", "to"))
    experiment_id = payload.get("experiment_id") or f"research_{date_from}_{date_to}"
    llm_parameters = payload.get("llm_parameters", {})
    if not isinstance(llm_parameters, dict):
        raise ValueError("llm_parameters must be an object")
    result_paths = []
    by_family: dict[str, int] = {}
    family_outcomes: dict[str, dict[str, Any]] = {}
    for spec in specs:
        requested_family_trials = _family_trial_quota(payload, spec.strategy_family)
        family_weight = _family_sampling_weight(payload, spec.strategy_family)
        family_trials = apply_family_trial_weight(requested_family_trials, family_weight)
        outcome = family_outcomes.setdefault(
            spec.strategy_family,
            {
                "strategy_family": spec.strategy_family,
                "requested_quota": 0,
                "quota": 0,
                "applied_weight": family_weight,
                "completed_trials": 0,
                "passed_trials": 0,
                "failed_trials": 0,
                "failure_reasons": {},
            },
        )
        outcome["requested_quota"] += max(requested_family_trials, 0)
        outcome["quota"] += max(family_trials, 0)
        if family_trials <= 0:
            outcome["skipped_by_quota"] = True
            continue
        symbol = get_symbol(payload.get("symbol", spec.symbol), config_dir)
        cost_model = get_cost_model(spec.cost_model, config_dir)
        family_experiment_id = (
            experiment_id
            if len(specs) == 1
            else f"{experiment_id}_{spec.strategy_family}"
        )
        results = run_budgeted_research(
            seed_spec=spec,
            symbol_config=symbol,
            data_root=data_root,
            experiment_id=family_experiment_id,
            date_from=date_from,
            date_to=date_to,
            max_trials=family_trials,
            starting_equity=float(payload.get("starting_equity", 100_000)),
            train_days=int(payload.get("train_days", 730)),
            validation_days=int(payload.get("validation_days", 182)),
            test_days=int(payload.get("test_days", 182)),
            step_days=int(payload.get("step_days", 91)),
            embargo_days=int(payload.get("embargo_days", 5)),
            final_holdout_days=int(payload.get("final_holdout_days", 365)),
            min_folds=int(payload.get("min_folds", 1)),
            indicator_warmup_days=optional_int(payload.get("indicator_warmup_days")),
            max_parameter_combinations=int(payload.get("max_parameter_combinations", 50)),
            allow_high_parameter_budget=bool(payload.get("allow_high_parameter_budget", False)),
            execution_mode=payload.get("execution_mode", "bar"),
            cost_model=cost_model,
            config_dir=config_dir,
            random_seed=int(payload.get("random_seed", 0)),
            llm_model=str(payload.get("llm_model", "local-deterministic-template")),
            llm_parameters=llm_parameters,
        )
        by_family[spec.strategy_family] = by_family.get(spec.strategy_family, 0) + len(results)
        outcome["completed_trials"] += len(results)
        for result in results:
            if result.gates["passed"]:
                outcome["passed_trials"] += 1
            else:
                outcome["failed_trials"] += 1
                for reason in result.gates["reasons"]:
                    reasons = outcome["failure_reasons"]
                    reasons[reason] = reasons.get(reason, 0) + 1
        for result in results:
            output_path = experiments_root / result.experiment_id / "leaderboard.json"
            write_research_result(output_path, result)
            result_paths.append(str(output_path))
    return {
        "trials": sum(by_family.values()),
        "by_family": by_family,
        "family_weight_report": build_family_weight_report(family_outcomes),
        "result_paths": result_paths,
        "deduplication_report": deduplication_report,
    }


def build_family_weight_report(
    family_outcomes: dict[str, dict[str, Any]],
    min_weight: float = 0.2,
    failure_weight_penalty: float = 0.8,
) -> dict[str, Any]:
    families = []
    next_weights = {}
    for family in sorted(family_outcomes):
        outcome = family_outcomes[family]
        completed = int(outcome.get("completed_trials", 0))
        failed = int(outcome.get("failed_trials", 0))
        passed = int(outcome.get("passed_trials", 0))
        failure_ratio = failed / completed if completed else None
        next_weight = (
            1.0
            if failure_ratio is None
            else max(min_weight, 1.0 - failure_weight_penalty * failure_ratio)
        )
        if completed == 0:
            status = "skipped_by_quota" if outcome.get("skipped_by_quota") else "no_trials"
        elif failed == 0:
            status = "passing"
        elif passed == 0:
            status = "failing"
        else:
            status = "mixed"
        row = {
            "strategy_family": family,
            "status": status,
            "requested_quota": int(outcome.get("requested_quota", outcome.get("quota", 0))),
            "quota": int(outcome.get("quota", 0)),
            "applied_weight": float(outcome.get("applied_weight", 1.0)),
            "completed_trials": completed,
            "passed_trials": passed,
            "failed_trials": failed,
            "failure_ratio": failure_ratio,
            "next_weight": next_weight,
            "failure_reasons": dict(sorted(outcome.get("failure_reasons", {}).items())),
        }
        families.append(row)
        next_weights[family] = next_weight
    return {
        "status": "computed_v1",
        "min_weight": min_weight,
        "failure_weight_penalty": failure_weight_penalty,
        "families": families,
        "next_weights": next_weights,
    }


def apply_family_trial_weight(requested_trials: int, weight: float, min_positive_trials: int = 1) -> int:
    if requested_trials <= 0:
        return 0
    weighted_trials = int(requested_trials * max(weight, 0))
    return max(min_positive_trials, weighted_trials)


def _family_sampling_weight(payload: dict[str, Any], strategy_family: str) -> float:
    weights = payload.get("family_weights")
    if not isinstance(weights, dict):
        previous_report = payload.get("family_weight_report")
        if isinstance(previous_report, dict):
            weights = previous_report.get("next_weights")
    if isinstance(weights, dict) and strategy_family in weights:
        return float(weights[strategy_family])
    return 1.0


def _research_specs(payload: dict[str, Any]) -> list[Any]:
    if payload.get("specs"):
        paths = payload["specs"]
        if not isinstance(paths, list) or not paths:
            raise ValueError("specs must be a non-empty list")
        return [load_strategy_spec(Path(str(path))) for path in paths]
    return [load_strategy_spec(Path(_required(payload, "spec")))]


def deduplicate_research_specs(specs: list[Any]) -> tuple[list[Any], dict[str, Any]]:
    unique_specs = []
    skipped = []
    seen: dict[str, Any] = {}
    for spec in specs:
        digest = strategy_logic_hash(spec)
        duplicate_of = seen.get(digest)
        if duplicate_of is not None:
            skipped.append(
                {
                    "strategy_name": spec.name,
                    "strategy_family": spec.strategy_family,
                    "strategy_logic_hash": digest,
                    "duplicate_of": duplicate_of.name,
                    "reason": "duplicate_strategy_logic",
                }
            )
            continue
        seen[digest] = spec
        unique_specs.append(spec)
    return unique_specs, {
        "input_specs": len(specs),
        "unique_specs": len(unique_specs),
        "skipped_specs": len(skipped),
        "skipped": skipped,
    }


def _family_trial_quota(payload: dict[str, Any], strategy_family: str) -> int:
    family_quotas = payload.get("family_quotas", {})
    if isinstance(family_quotas, dict) and strategy_family in family_quotas:
        return int(family_quotas[strategy_family])
    if "max_trials_per_family" in payload:
        return int(payload["max_trials_per_family"])
    return int(payload.get("max_trials", 1))


def optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _required(payload: dict[str, Any], key: str, *aliases: str) -> str:
    for candidate in (key, *aliases):
        value = payload.get(candidate)
        if value:
            return str(value)
    raise ValueError(f"{key} is required")
