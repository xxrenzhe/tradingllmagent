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
from .experiments import record_audit_event, record_experiment, record_trial
from .llm import append_audit_log, create_llm_adapter, load_train_validation_feedback
from .monitor import build_monitor_report, write_monitor_outputs
from .modules import build_target_frequency_pool, discover_module_memory_files, load_module_performance_memory
from .paper import export_ninjatrader_signals, load_backtest_result, replay_trades
from .research import (
    StrategyTargetCriteria,
    discover_strategy_seed_specs,
    run_budgeted_research,
    run_llm_seed_pool_target_discovery,
    run_llm_target_discovery,
    write_research_result,
    write_strategy_discovery_result,
)
from .storage import (
    bar_path,
    compute_data_version_hash,
    normalized_tick_path,
    write_json,
    write_ticks_parquet,
)
from .strategy import load_strategy_spec
from .tasks import append_task_log, claim_queued_task, get_task, next_queued_task, update_task
from .trigger_gate import load_trigger_gate_forward_report, run_trigger_gate_simulation
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
    if task_type == "research.propose":
        return execute_research_propose(payload)
    if task_type == "research.iterate":
        return execute_research_iterate(payload)
    if task_type == "research.discover_target":
        return execute_research_discover_target(payload)
    if task_type == "monitor.once":
        return execute_monitor_once(payload)
    if task_type == "trigger_gate.simulate":
        return execute_trigger_gate_simulate(payload)
    if task_type == "trigger_gate.report":
        return execute_trigger_gate_report(payload)
    raise ValueError(f"Unsupported task_type: {task_type}")


def execute_trigger_gate_simulate(payload: dict[str, Any]) -> dict[str, Any]:
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
    output_dir = payload.get("output_dir")
    if not output_dir:
        raise ValueError("output_dir is required")
    return run_trigger_gate_simulation(
        target_frequency_pool=target_frequency_pool,
        output_dir=Path(str(output_dir)),
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


def execute_trigger_gate_report(payload: dict[str, Any]) -> dict[str, Any]:
    output_dir = payload.get("output_dir")
    if not output_dir:
        raise ValueError("output_dir is required")
    previous_pool = payload.get("previous_pool")
    if previous_pool is not None and not isinstance(previous_pool, dict):
        raise ValueError("previous_pool must be an object")
    return load_trigger_gate_forward_report(Path(str(output_dir)), previous_pool=previous_pool)


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
    hour_retries = int(payload.get("hour_retries", 3))
    hour_timeout_seconds = int(payload.get("hour_timeout_seconds", 30))

    total_ticks = 0
    status_counts: dict[str, int] = {}
    outputs: list[dict[str, Any]] = []
    for day in iter_dates(date_from, date_to):
        output = normalized_tick_path(data_root, symbol_alias, day)
        if output.exists() and output.stat().st_size > 0 and not payload.get("force", False):
            outputs.append(
                {
                    "date": day.isoformat(),
                    "path": str(output),
                    "rows": 0,
                    "raw_files": 0,
                    "status_counts": {"skipped_existing_day": 1},
                    "data_version_hash": None,
                }
            )
            status_counts["skipped_existing_day"] = status_counts.get("skipped_existing_day", 0) + 1
            continue
        start, end = day_bounds(day)
        day_ticks = []
        raw_paths: list[Path] = []
        day_status_counts: dict[str, int] = {}
        failed_hours: list[dict[str, str]] = []
        for hour in iter_hours(start, end):
            try:
                result = download_hour(
                    symbol,
                    hour,
                    data_root,
                    retries=hour_retries,
                    timeout_seconds=hour_timeout_seconds,
                )
            except Exception as exc:
                status_counts["failed"] = status_counts.get("failed", 0) + 1
                day_status_counts["failed"] = day_status_counts.get("failed", 0) + 1
                failed_hours.append({"hour": hour.isoformat(), "error": str(exc)})
                continue
            status_counts[result.status] = status_counts.get(result.status, 0) + 1
            day_status_counts[result.status] = day_status_counts.get(result.status, 0) + 1
            if result.status in {"downloaded", "cached"}:
                raw_paths.append(result.path)
                day_ticks.extend(parse_bi5_file(result.path, hour, symbol.price_scale))

        if failed_hours:
            raise RuntimeError(
                f"Failed to download all hours for {day.isoformat()}: "
                + ", ".join(f"{item['hour']}={item['error']}" for item in failed_hours)
            )

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


def execute_monitor_once(payload: dict[str, Any]) -> dict[str, Any]:
    data_root = Path(payload.get("data_root", "data"))
    symbol = _required(payload, "symbol")
    timeframe = str(payload.get("timeframe", "5m"))
    day = parse_date(_required(payload, "date"))
    report = build_monitor_report(
        symbol=symbol,
        timeframe=timeframe,
        bar_files=[bar_path(data_root, symbol, timeframe, day)],
        proximity_points=float(payload.get("proximity_points", 2.0)),
    )
    outputs = write_monitor_outputs(Path(payload.get("output_dir", "data/runtime/reports")), report)
    report["outputs"] = outputs
    return report


def execute_research_propose(payload: dict[str, Any]) -> dict[str, Any]:
    seed_spec = load_strategy_spec(Path(_required(payload, "spec")))
    experiments_root = Path(payload.get("experiments_root", "experiments"))
    experiment_id = str(payload.get("experiment_id") or f"{seed_spec.name}_proposal")
    llm_parameters = payload.get("llm_parameters", {})
    if not isinstance(llm_parameters, dict):
        raise ValueError("llm_parameters must be an object")
    feedback = None
    feedback_root = payload.get("feedback_experiments_root")
    if feedback_root:
        feedback = load_train_validation_feedback(
            Path(str(feedback_root)),
            experiment_id=payload.get("feedback_experiment_id"),
            limit=int(payload.get("feedback_limit", 10)),
        )
    proposal = create_llm_adapter(
        str(payload.get("model", "local-deterministic-template")),
        llm_parameters,
    ).propose(seed_spec, feedback=feedback)
    output_path = (
        Path(str(payload["output"]))
        if payload.get("output")
        else Path("strategies/generated") / f"{proposal.strategy.name}.json"
    )
    write_json(output_path, proposal.strategy.raw)
    audit_path = (
        Path(str(payload["audit_log"]))
        if payload.get("audit_log")
        else experiments_root / experiment_id / "llm_audit.jsonl"
    )
    audit_record = proposal.audit_record()
    append_audit_log(audit_path, audit_record)

    experiment_db = Path(payload.get("experiment_db", "experiments/research.sqlite3"))
    record_experiment(
        experiment_db,
        experiment_id=experiment_id,
        symbol=proposal.strategy.symbol,
        status="proposed",
        metadata={
            "seed_spec": str(Path(_required(payload, "spec"))),
            "output": str(output_path),
            "feedback_count": len(feedback or []),
            "feedback_experiment_id": payload.get("feedback_experiment_id"),
        },
    )
    record_audit_event(
        experiment_db,
        experiment_id=experiment_id,
        event_type="llm_strategy_proposal",
        payload=audit_record,
    )
    return {
        "strategy_spec": str(output_path),
        "audit_log": str(audit_path),
        "experiment_id": experiment_id,
        "prompt_hash": proposal.prompt_hash,
        "response_hash": proposal.response_hash,
        "feedback_count": len(feedback or []),
        "strategy_name": proposal.strategy.name,
        "strategy_family": proposal.strategy.strategy_family,
    }


def execute_research_iterate(payload: dict[str, Any]) -> dict[str, Any]:
    seed_spec = load_strategy_spec(Path(_required(payload, "spec")))
    experiments_root = Path(payload.get("experiments_root", "experiments"))
    experiment_id = str(payload.get("experiment_id") or f"{seed_spec.name}_iteration")
    proposal_payload = dict(payload)
    proposal_payload["experiment_id"] = experiment_id
    proposal_payload.setdefault(
        "output",
        str(experiments_root / experiment_id / "proposed_strategy.json"),
    )
    proposal_payload.setdefault(
        "audit_log",
        str(experiments_root / experiment_id / "llm_audit.jsonl"),
    )
    if "model" not in proposal_payload and payload.get("llm_model"):
        proposal_payload["model"] = payload["llm_model"]
    proposal = execute_research_propose(proposal_payload)

    research_payload = dict(payload)
    research_payload["experiment_id"] = experiment_id
    research_payload["spec"] = proposal["strategy_spec"]
    research_payload.pop("specs", None)
    if "llm_model" not in research_payload and payload.get("model"):
        research_payload["llm_model"] = payload["model"]
    research = execute_research_run(research_payload)
    return {
        "experiment_id": experiment_id,
        "proposal": proposal,
        "research": research,
    }


def execute_research_discover_target(payload: dict[str, Any]) -> dict[str, Any]:
    config_dir = Path(payload.get("config_dir", "configs"))
    data_root = Path(payload.get("data_root", "data"))
    experiments_root = Path(payload.get("experiments_root", "experiments"))
    experiment_db = Path(payload.get("experiment_db", "experiments/research.sqlite3"))
    date_from = parse_date(_required(payload, "date_from", "from"))
    date_to = parse_date(_required(payload, "date_to", "to"))
    seed_specs, seed_selection_report = strategy_discovery_seed_specs(payload)
    if not seed_specs:
        raise ValueError("No strategy seed specs matched the discovery filters")
    seed_spec = seed_specs[0]
    discovery_id = str(payload.get("experiment_id") or f"{seed_spec.name}_target_discovery_{date_from}_{date_to}")
    llm_parameters = payload.get("llm_parameters", {})
    if not isinstance(llm_parameters, dict):
        raise ValueError("llm_parameters must be an object")
    target = StrategyTargetCriteria(
        min_annual_trades=float(payload.get("min_annual_trades", 1000)),
        min_sharpe=float(payload.get("min_sharpe", 2)),
        min_win_probability=float(payload.get("min_win_probability", 0.53)),
        min_profit_factor=float(payload.get("min_profit_factor", 1.2)),
        max_drawdown=float(payload.get("max_drawdown", 10_000)),
        min_positive_year_ratio=float(payload.get("min_positive_year_ratio", 0.6)),
        max_final_holdout_sharpe_decay=float(payload.get("max_final_holdout_sharpe_decay", 0.5)),
        max_parameter_combinations=int(payload.get("max_target_parameter_combinations", 200)),
        min_non_overlap_test_folds=int(payload.get("min_non_overlap_test_folds", 1)),
    )
    symbol = get_symbol(payload.get("symbol", seed_spec.symbol), config_dir)
    cost_model = get_cost_model(seed_spec.cost_model, config_dir)
    record_experiment(
        experiment_db,
        experiment_id=discovery_id,
        symbol=str(payload.get("symbol", seed_spec.symbol)),
        status="running",
        metadata={
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "target": target.to_dict(),
            "max_rounds": int(payload.get("max_rounds", 10)),
            "trials_per_round": int(payload.get("trials_per_round", 1)),
            "target_count": int(payload.get("target_count", 1)),
            "execution_mode": payload.get("execution_mode", "bar"),
            "llm_model": payload.get("llm_model", "local-deterministic-template"),
            "llm_parameters": llm_parameters,
        },
    )
    discovery_kwargs = {
        "symbol_config": symbol,
        "data_root": data_root,
        "discovery_id": discovery_id,
        "date_from": date_from,
        "date_to": date_to,
        "max_rounds": int(payload.get("max_rounds", 10)),
        "trials_per_round": int(payload.get("trials_per_round", 1)),
        "target_count": int(payload.get("target_count", 1)),
        "target": target,
        "starting_equity": float(payload.get("starting_equity", 100_000)),
        "train_days": int(payload.get("train_days", 730)),
        "validation_days": int(payload.get("validation_days", 182)),
        "test_days": int(payload.get("test_days", 182)),
        "step_days": int(payload.get("step_days", 91)),
        "embargo_days": int(payload.get("embargo_days", 5)),
        "final_holdout_days": int(payload.get("final_holdout_days", 365)),
        "min_folds": int(payload.get("min_folds", 1)),
        "indicator_warmup_days": optional_int(payload.get("indicator_warmup_days")),
        "max_parameter_combinations": int(payload.get("max_parameter_combinations", 50)),
        "allow_high_parameter_budget": bool(payload.get("allow_high_parameter_budget", False)),
        "execution_mode": str(payload.get("execution_mode", "bar")),
        "cost_model": cost_model,
        "config_dir": config_dir,
        "random_seed": int(payload.get("random_seed", 0)),
        "llm_model": str(payload.get("llm_model", "local-deterministic-template")),
        "llm_parameters": llm_parameters,
    }
    if len(seed_specs) == 1 and payload.get("spec"):
        discovery = run_llm_target_discovery(seed_spec=seed_spec, **discovery_kwargs)
    else:
        discovery = run_llm_seed_pool_target_discovery(
            seed_specs=seed_specs,
            seed_selection_report=seed_selection_report,
            **discovery_kwargs,
        )
    result_paths = []
    for audit in discovery.proposal_audits:
        append_audit_log(experiments_root / discovery_id / "llm_audit.jsonl", audit)
        record_audit_event(
            experiment_db,
            experiment_id=discovery_id,
            event_type="llm_target_discovery_proposal",
            payload=audit,
        )
    for result in discovery.results:
        output_path = experiments_root / result.experiment_id / "leaderboard.json"
        write_research_result(output_path, result)
        record_trial(experiment_db, discovery_id, result)
        evaluation = next(
            attempt for attempt in discovery.attempts if attempt.experiment_id == result.experiment_id
        )
        record_audit_event(
            experiment_db,
            experiment_id=discovery_id,
            trial_id=result.experiment_id,
            event_type="target_discovery_trial_completed",
            payload={
                "trial_id": result.experiment_id,
                "strategy_spec_hash": result.strategy_spec_hash,
                "prompt_hash": result.prompt_hash,
                "target_evaluation": evaluation.to_dict(),
            },
        )
        result_paths.append(str(output_path))
    summary_path = experiments_root / discovery_id / "target_discovery.json"
    write_strategy_discovery_result(summary_path, discovery)
    result_payload = {
        **discovery.to_dict(),
        "result_paths": result_paths,
        "summary_path": str(summary_path),
        "seed_selection_report": seed_selection_report,
    }
    record_experiment(
        experiment_db,
        experiment_id=discovery_id,
        symbol=str(payload.get("symbol", seed_spec.symbol)),
        status="completed",
        metadata=result_payload,
    )
    return result_payload


def strategy_discovery_seed_specs(payload: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    if payload.get("spec"):
        path = Path(_required(payload, "spec"))
        spec = load_strategy_spec(path)
        return [spec], {
            "mode": "explicit_spec",
            "candidate_count": 1,
            "selected_count": 1,
            "selected_paths": [str(path)],
            "selected": [
                {
                    "strategy_name": spec.name,
                    "strategy_family": spec.strategy_family,
                    "symbol": spec.symbol,
                    "timeframe": spec.timeframe,
                }
            ],
            "skipped": [],
        }
    return discover_strategy_seed_specs(
        strategies_root=Path(payload.get("strategies_root", "strategies")),
        spec_paths=optional_string_list(payload.get("specs")),
        symbol=payload.get("symbol"),
        timeframe=payload.get("timeframe"),
        strategy_families=optional_string_list(payload.get("strategy_families")),
        limit=optional_int(payload.get("max_seed_strategies")),
    )


def execute_research_run(payload: dict[str, Any]) -> dict[str, Any]:
    specs, deduplication_report = deduplicate_research_specs(_research_specs(payload))
    config_dir = Path(payload.get("config_dir", "configs"))
    data_root = Path(payload.get("data_root", "data"))
    experiments_root = Path(payload.get("experiments_root", "experiments"))
    experiment_db = Path(payload.get("experiment_db", "experiments/research.sqlite3"))
    date_from = parse_date(_required(payload, "date_from", "from"))
    date_to = parse_date(_required(payload, "date_to", "to"))
    experiment_id = payload.get("experiment_id") or f"research_{date_from}_{date_to}"
    llm_parameters = payload.get("llm_parameters", {})
    if not isinstance(llm_parameters, dict):
        raise ValueError("llm_parameters must be an object")
    record_experiment(
        experiment_db,
        experiment_id=experiment_id,
        symbol=str(payload.get("symbol", specs[0].symbol if specs else "unknown")),
        status="running",
        metadata={
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "execution_mode": payload.get("execution_mode", "bar"),
            "max_trials": payload.get("max_trials"),
            "max_trials_per_family": payload.get("max_trials_per_family"),
            "family_weights": payload.get("family_weights"),
            "llm_model": payload.get("llm_model", "local-deterministic-template"),
            "llm_parameters": llm_parameters,
            "deduplication_report": deduplication_report,
        },
    )
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
            record_trial(experiment_db, experiment_id, result)
            record_audit_event(
                experiment_db,
                experiment_id=experiment_id,
                trial_id=result.experiment_id,
                event_type="research_trial_completed",
                payload={
                    "trial_id": result.experiment_id,
                    "strategy_spec_hash": result.strategy_spec_hash,
                    "prompt_hash": result.prompt_hash,
                    "gates": result.gates,
                },
            )
            result_paths.append(str(output_path))
    result_payload = {
        "trials": sum(by_family.values()),
        "by_family": by_family,
        "family_weight_report": build_family_weight_report(family_outcomes),
        "result_paths": result_paths,
        "deduplication_report": deduplication_report,
    }
    record_experiment(
        experiment_db,
        experiment_id=experiment_id,
        symbol=str(payload.get("symbol", specs[0].symbol if specs else "unknown")),
        status="completed",
        metadata={
            "trials": result_payload["trials"],
            "execution_mode": payload.get("execution_mode", "bar"),
            "by_family": by_family,
            "family_weight_report": result_payload["family_weight_report"],
            "deduplication_report": deduplication_report,
        },
    )
    return result_payload


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


def optional_string_list(value: Any) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item) for item in value]
    raise ValueError("value must be a string or list of strings")


def _required(payload: dict[str, Any], key: str, *aliases: str) -> str:
    for candidate in (key, *aliases):
        value = payload.get(candidate)
        if value:
            return str(value)
    raise ValueError(f"{key} is required")
