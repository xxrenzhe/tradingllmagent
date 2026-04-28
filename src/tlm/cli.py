from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from .bars import (
    build_minute_bars_from_parquet,
    build_timeframe_bars_from_1m_parquet,
    timeframe_minutes,
)
from .backtest import result_to_json, run_bar_backtest, run_tick_backtest
from .cli_dates import iter_dates
from .config import ConfigError, get_cost_model, get_symbol, load_symbols
from .dukascopy import download_hour, iter_hours, parse_bi5_file
from .experiments import (
    load_experiment_summary,
    record_audit_event,
    record_experiment,
    record_trial,
)
from .events import (
    build_event_context_rows,
    load_event_calendar,
    resolve_event_calendar_path,
    validate_event_calendar,
    write_event_context_parquet,
)
from .feature_catalog import feature_readiness_report
from .firstrate import import_firstrate_bars
from .llm import append_audit_log, create_llm_adapter, load_train_validation_feedback
from .monitor import build_monitor_report, write_monitor_outputs
from .modules import (
    build_target_frequency_pool,
    discover_module_memory_files,
    load_module_performance_memory,
    strategy_module_catalog,
    summarize_module_performance,
)
from .paper import build_paper_replay_attribution, export_ninjatrader_signals, load_backtest_result, replay_trades
from .quality import build_bar_quality_report, build_quality_report
from .quotes import build_quote_execution_report, import_databento_quotes
from .research import (
    StrategyTargetCriteria,
    discover_strategy_seed_specs,
    load_leaderboard_report,
    run_budgeted_research,
    run_llm_seed_pool_target_discovery,
    run_llm_target_discovery,
    write_research_result,
    write_strategy_discovery_result,
)
from .storage import (
    bar_path,
    compute_data_version_hash,
    event_context_path,
    normalized_quote_path,
    normalized_tick_path,
    quality_path,
    write_json,
    write_ticks_parquet,
)
from .strategy import StrategySpecError, load_strategy_spec, with_strategy_symbol
from .strategy_generation import write_feature_combo_strategy_specs
from .trigger_gate import (
    append_trigger_gate_outcome_from_payload,
    build_forward_test_schedule,
    build_trigger_gate_memory_view,
    load_trigger_gate_forward_report,
    run_trigger_gate_simulation,
)
from .variants import DEFAULT_PARAMETER_BUDGET, ParameterBudgetError
from .validation import generate_rolling_folds


def parse_date(value: str) -> date:
    if value == "today":
        return datetime.now(UTC).date()
    return date.fromisoformat(value)


def parse_json_object(value: str) -> dict:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("value must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise argparse.ArgumentTypeError("value must be a JSON object")
    return payload


def day_bounds(value: date) -> tuple[datetime, datetime]:
    start = datetime.combine(value, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def tick_parquet_files(data_root: Path, symbol: str, start: date, end: date) -> list[Path]:
    return [normalized_tick_path(data_root, symbol, day) for day in iter_dates(start, end)]


def quote_parquet_files(data_root: Path, symbol: str, start: date, end: date) -> list[Path]:
    return [normalized_quote_path(data_root, symbol, day) for day in iter_dates(start, end)]


def cmd_data_discover(args: argparse.Namespace) -> int:
    symbols = load_symbols(Path(args.config_dir))
    query = args.query.lower()
    provider = args.provider.lower()
    for alias, symbol in sorted(symbols.items()):
        if provider not in {"", "all"} and symbol.provider.lower() != provider:
            continue
        haystack = f"{alias} {symbol.instrument} {symbol.description}".lower()
        if query in haystack:
            print(f"{alias}\t{symbol.provider}\t{symbol.instrument}\t{symbol.description}")
    return 0


def cmd_data_download(args: argparse.Namespace) -> int:
    if args.granularity != "tick":
        raise SystemExit("Only --granularity tick is supported in Phase 1")
    symbol = get_symbol(args.symbol, Path(args.config_dir))
    data_root = Path(args.data_root)
    date_from = parse_date(args.date_from)
    date_to = parse_date(args.date_to)

    total_ticks = 0
    for day in iter_dates(date_from, date_to):
        output = normalized_tick_path(data_root, args.symbol, day)
        if output.exists() and output.stat().st_size > 0 and not args.force:
            print(f"skipped_existing_day\t{output}")
            continue
        start, end = day_bounds(day)
        day_ticks = []
        raw_paths: list[Path] = []
        failed_hours: list[tuple[datetime, str]] = []
        for hour in iter_hours(start, end):
            try:
                result = download_hour(
                    symbol,
                    hour,
                    data_root,
                    retries=args.hour_retries,
                    timeout_seconds=args.hour_timeout_seconds,
                )
            except Exception as exc:
                failed_hours.append((hour, str(exc)))
                print(f"{hour.isoformat()}\tfailed\t0\t{exc}")
                continue
            if result.status in {"downloaded", "cached"}:
                raw_paths.append(result.path)
                ticks = parse_bi5_file(result.path, hour, symbol.price_scale)
                day_ticks.extend(ticks)
            print(f"{hour.isoformat()}\t{result.status}\t{result.bytes_written}\t{result.url}")

        if failed_hours:
            failures = ", ".join(f"{hour.isoformat()}={error}" for hour, error in failed_hours)
            raise RuntimeError(f"Failed to download all hours for {day.isoformat()}: {failures}")

        write_ticks_parquet(output, args.symbol, day_ticks)
        total_ticks += len(day_ticks)
        metadata = {
            "symbol": args.symbol,
            "instrument": symbol.instrument,
            "price_scale": symbol.price_scale,
            "day": day.isoformat(),
        }
        print(
            f"wrote\t{output}\trows={len(day_ticks)}\t"
            f"data_version_hash={compute_data_version_hash(raw_paths, metadata)}"
        )

    print(f"download complete: rows={total_ticks}")
    return 0


def cmd_data_build_bars(args: argparse.Namespace) -> int:
    minutes = timeframe_minutes(args.timeframe)
    data_root = Path(args.data_root)
    date_from = parse_date(args.date_from)
    date_to = parse_date(args.date_to)
    total = 0
    for day in iter_dates(date_from, date_to):
        output = bar_path(data_root, args.symbol, args.timeframe, day)
        if minutes == 1:
            tick_file = normalized_tick_path(data_root, args.symbol, day)
            count = build_minute_bars_from_parquet([tick_file], output)
            source_files = [tick_file]
        else:
            one_minute_file = bar_path(data_root, args.symbol, "1m", day)
            count = build_timeframe_bars_from_1m_parquet([one_minute_file], output, args.timeframe)
            source_files = [one_minute_file]
        total += count
        data_version_hash = compute_data_version_hash(
            [*source_files, output],
            {
                "artifact": "bars",
                "symbol": args.symbol,
                "timeframe": args.timeframe,
                "day": day.isoformat(),
                "source_files": [str(path) for path in source_files],
            },
        )
        print(f"wrote\t{output}\trows={count}\tdata_version_hash={data_version_hash}")
    print(f"bar build complete: rows={total}")
    return 0


def cmd_data_import_firstrate(args: argparse.Namespace) -> int:
    symbol = get_symbol(args.symbol, Path(args.config_dir))
    if symbol.provider.lower() != "firstratedata":
        raise SystemExit(f"Symbol {args.symbol} provider must be firstratedata, got {symbol.provider}")
    outputs = import_firstrate_bars(
        csv_paths=[Path(path) for path in args.input],
        data_root=Path(args.data_root),
        symbol=args.symbol,
        source_timezone=args.source_timezone,
        force=args.force,
    )
    print(json.dumps({"symbol": args.symbol, "outputs": outputs}, indent=2, sort_keys=True))
    return 0


def cmd_data_import_databento_quotes(args: argparse.Namespace) -> int:
    symbol = get_symbol(args.symbol, Path(args.config_dir))
    if symbol.provider.lower() != "databento":
        raise SystemExit(f"Symbol {args.symbol} provider must be databento, got {symbol.provider}")
    outputs = import_databento_quotes(
        csv_paths=[Path(path) for path in args.input],
        data_root=Path(args.data_root),
        symbol=args.symbol,
        source_timezone=args.source_timezone,
        force=args.force,
    )
    print(json.dumps({"symbol": args.symbol, "outputs": outputs}, indent=2, sort_keys=True))
    return 0


def cmd_data_quality(args: argparse.Namespace) -> int:
    data_root = Path(args.data_root)
    date_from = parse_date(args.date_from)
    date_to = parse_date(args.date_to)
    files = tick_parquet_files(data_root, args.symbol, date_from, date_to)
    report = build_quality_report(args.symbol, files)
    output = quality_path(data_root, args.symbol, date_from.isoformat(), date_to.isoformat())
    write_json(output, report.to_dict())
    print(output)
    print(report.to_dict())
    return 0


def cmd_data_bar_quality(args: argparse.Namespace) -> int:
    data_root = Path(args.data_root)
    date_from = parse_date(args.date_from)
    date_to = parse_date(args.date_to)
    files = bar_parquet_files(data_root, args.symbol, args.timeframe, date_from, date_to)
    minutes = timeframe_minutes(args.timeframe)
    report = build_bar_quality_report(
        args.symbol,
        args.timeframe,
        files,
        expected_minutes=minutes,
        max_normal_price_jump=args.max_normal_price_jump,
    )
    output = (
        Path(args.output)
        if args.output
        else data_root / "quality" / args.symbol / f"{date_from.isoformat()}_{date_to.isoformat()}_{args.timeframe}_bars.json"
    )
    write_json(output, report.to_dict())
    print(output)
    print(report.to_dict())
    return 0


def bar_parquet_files(data_root: Path, symbol: str, timeframe: str, start: date, end: date) -> list[Path]:
    return [bar_path(data_root, symbol, timeframe, day) for day in iter_dates(start, end)]


def cmd_data_split_manifest(args: argparse.Namespace) -> int:
    date_from = parse_date(args.date_from)
    date_to = parse_date(args.date_to)
    data_root = Path(args.data_root)
    plan = generate_rolling_folds(
        date_from,
        date_to,
        train_days=args.train_days,
        validation_days=args.validation_days,
        test_days=args.test_days,
        step_days=args.step_days,
        embargo_days=args.embargo_days,
        final_holdout_days=args.final_holdout_days,
        min_folds=args.min_folds,
        indicator_warmup_days=args.indicator_warmup_days,
    )
    files = bar_parquet_files(data_root, args.symbol, args.timeframe, date_from, date_to)
    payload = {
        "schema_version": 1,
        "artifact": "dataset_split_manifest",
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "data_version_hash": compute_data_version_hash(
            files,
            {
                "artifact": "dataset_split_manifest",
                "symbol": args.symbol,
                "timeframe": args.timeframe,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
            },
        ),
        "plan": plan.to_dict(),
        "final_holdout_policy": {
            "llm_feedback_includes_final_holdout": False,
            "llm_visible_splits": ["train", "validation"],
            "llm_hidden_splits": ["test", "final_holdout"],
        },
        "source_files": [str(path) for path in files if path.exists()],
        "missing_files": [str(path) for path in files if not path.exists()],
    }
    output = (
        Path(args.output)
        if args.output
        else data_root / "splits" / args.symbol / args.timeframe / f"{date_from.isoformat()}_{date_to.isoformat()}.json"
    )
    write_json(output, payload)
    print(output)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_data_quote_replay(args: argparse.Namespace) -> int:
    data_root = Path(args.data_root)
    date_from = parse_date(args.date_from)
    date_to = parse_date(args.date_to)
    symbol = get_symbol(args.symbol, Path(args.config_dir))
    output = Path(args.output) if args.output else None
    report = build_quote_execution_report(
        backtest_result_path=Path(args.backtest_result),
        quote_files=quote_parquet_files(data_root, args.symbol, date_from, date_to),
        symbol_config=symbol,
        output_path=output,
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


def cmd_strategy_validate(args: argparse.Namespace) -> int:
    spec = load_strategy_spec(Path(args.spec))
    print(
        json.dumps(
            {
                "status": "valid",
                "name": spec.name,
                "symbol": spec.symbol,
                "strategy_family": spec.strategy_family,
                "timeframe": spec.timeframe,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_modules_list(args: argparse.Namespace) -> int:
    print(json.dumps({"modules": strategy_module_catalog()}, indent=2, sort_keys=True))
    return 0


def cmd_modules_summary(args: argparse.Namespace) -> int:
    if args.memory:
        memory_paths = [Path(path) for path in args.memory]
    else:
        memory_paths = discover_module_memory_files(Path(args.experiments_root))
    records = load_module_performance_memory(memory_paths)
    summary = summarize_module_performance(records)
    summary["target_frequency_pool"] = build_target_frequency_pool(
        records,
        target_min_per_day=args.target_min_per_day,
        target_max_per_day=args.target_max_per_day,
        min_proxy_win_rate=args.min_proxy_win_rate,
        lookback_days=args.lookback_days,
        require_passed=not args.include_rejected,
    )
    print(
        json.dumps(
            {
                "memory_files": [str(path) for path in memory_paths],
                **summary,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_trigger_gate_simulate(args: argparse.Namespace) -> int:
    if args.pool:
        target_frequency_pool = json.loads(Path(args.pool).read_text(encoding="utf-8"))
    else:
        if args.memory:
            memory_paths = [Path(path) for path in args.memory]
        else:
            memory_paths = discover_module_memory_files(Path(args.experiments_root))
        target_frequency_pool = build_target_frequency_pool(
            load_module_performance_memory(memory_paths),
            target_min_per_day=args.target_min_per_day,
            target_max_per_day=args.target_max_per_day,
            min_proxy_win_rate=args.min_proxy_win_rate,
            lookback_days=args.lookback_days,
            require_passed=not args.include_rejected,
        )
    manifest = run_trigger_gate_simulation(
        target_frequency_pool=target_frequency_pool,
        output_dir=Path(args.output_dir),
        replay_start=args.date_from,
        replay_end=args.date_to,
        enable_llm=args.enable_llm,
        step_minutes=args.step_minutes,
        model=args.model,
        daily_token_budget=args.daily_token_budget,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True, default=str))
    return 0


def cmd_trigger_gate_report(args: argparse.Namespace) -> int:
    previous_pool = json.loads(Path(args.previous_pool).read_text(encoding="utf-8")) if args.previous_pool else None
    report = load_trigger_gate_forward_report(Path(args.output_dir), previous_pool=previous_pool)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


def cmd_trigger_gate_schedule(args: argparse.Namespace) -> int:
    if args.pool:
        target_frequency_pool = json.loads(Path(args.pool).read_text(encoding="utf-8"))
    else:
        records = load_module_performance_memory(discover_module_memory_files(Path(args.experiments_root)))
        target_frequency_pool = build_target_frequency_pool(
            records,
            target_min_per_day=args.target_min_per_day,
            target_max_per_day=args.target_max_per_day,
            min_proxy_win_rate=args.min_proxy_win_rate,
            lookback_days=args.lookback_days,
            require_passed=not args.include_rejected,
        )
    schedule = build_forward_test_schedule(
        target_frequency_pool=target_frequency_pool,
        as_of=args.as_of,
        output_root=Path(args.output_root),
        enable_llm=args.enable_llm,
        daily_token_budget=args.daily_token_budget,
    )
    if args.output:
        write_json(Path(args.output), schedule)
    print(json.dumps(schedule, indent=2, sort_keys=True, default=str))
    return 0


def cmd_trigger_gate_memory(args: argparse.Namespace) -> int:
    view = build_trigger_gate_memory_view(
        Path(args.output_dir),
        strategy_spec_hash=args.strategy_spec_hash,
        module_id=args.module_id,
        decision=args.decision,
        risk_level=args.risk_level,
        outcome_label=args.outcome_label,
        limit=args.limit,
    )
    print(json.dumps(view, indent=2, sort_keys=True, default=str))
    return 0


def cmd_trigger_gate_outcome(args: argparse.Namespace) -> int:
    payload = {
        "decision_id": args.decision_id,
        "outcome_window": args.outcome_window,
        "net_pnl": args.net_pnl,
        "mfe": args.mfe,
        "mae": args.mae,
        "paper_fill_id": args.paper_fill_id,
        "would_have_hit_target": args.would_have_hit_target,
        "would_have_hit_stop": args.would_have_hit_stop,
        "final_label": args.final_label,
        "notes": args.notes,
    }
    outcome = append_trigger_gate_outcome_from_payload(Path(args.output_dir), payload)
    print(json.dumps(outcome, indent=2, sort_keys=True, default=str))
    return 0


def cmd_backtest_bar(args: argparse.Namespace) -> int:
    spec = load_strategy_spec(Path(args.spec))
    symbol = get_symbol(args.symbol or spec.symbol, Path(args.config_dir))
    spec = with_strategy_symbol(spec, symbol.alias)
    cost_model = get_cost_model(spec.cost_model, Path(args.config_dir))
    data_root = Path(args.data_root)
    date_from = parse_date(args.date_from)
    date_to = parse_date(args.date_to)
    files = bar_parquet_files(data_root, spec.symbol, spec.timeframe, date_from, date_to)
    result = run_bar_backtest(
        spec,
        symbol,
        files,
        starting_equity=args.starting_equity,
        cost_model=cost_model,
    )
    output = result_to_json(result)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
        print(output_path)
    else:
        print(output)
    return 0


def cmd_backtest_tick(args: argparse.Namespace) -> int:
    spec = load_strategy_spec(Path(args.spec))
    symbol = get_symbol(args.symbol or spec.symbol, Path(args.config_dir))
    spec = with_strategy_symbol(spec, symbol.alias)
    cost_model = get_cost_model(spec.cost_model, Path(args.config_dir))
    data_root = Path(args.data_root)
    date_from = parse_date(args.date_from)
    date_to = parse_date(args.date_to)
    files = tick_parquet_files(data_root, spec.symbol, date_from, date_to)
    result = run_tick_backtest(
        spec,
        symbol,
        files,
        starting_equity=args.starting_equity,
        cost_model=cost_model,
    )
    output = result_to_json(result)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
        print(output_path)
    else:
        print(output)
    return 0


def cmd_research_run(args: argparse.Namespace) -> int:
    spec = load_strategy_spec(Path(args.spec))
    symbol = get_symbol(args.symbol or spec.symbol, Path(args.config_dir))
    cost_model = get_cost_model(spec.cost_model, Path(args.config_dir))
    data_root = Path(args.data_root)
    date_from = parse_date(args.date_from)
    date_to = parse_date(args.date_to)
    experiment_id = args.experiment_id or f"{spec.name}_{date_from.isoformat()}_{date_to.isoformat()}"
    experiment_db = Path(args.experiment_db)
    record_experiment(
        experiment_db,
        experiment_id=experiment_id,
        symbol=args.symbol or spec.symbol,
        status="running",
        metadata={
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "execution_mode": args.execution_mode,
            "max_trials": args.max_trials,
            "llm_model": args.llm_model,
            "llm_parameters": args.llm_parameters,
            "seed_spec": str(Path(args.spec)) if args.spec else None,
        },
    )
    results = run_budgeted_research(
        seed_spec=spec,
        symbol_config=symbol,
        data_root=data_root,
        experiment_id=experiment_id,
        date_from=date_from,
        date_to=date_to,
        max_trials=args.max_trials,
        starting_equity=args.starting_equity,
        train_days=args.train_days,
        validation_days=args.validation_days,
        test_days=args.test_days,
        step_days=args.step_days,
        embargo_days=args.embargo_days,
        final_holdout_days=args.final_holdout_days,
        min_folds=args.min_folds,
        indicator_warmup_days=args.indicator_warmup_days,
        max_parameter_combinations=args.max_parameter_combinations,
        allow_high_parameter_budget=args.allow_high_parameter_budget,
        execution_mode=args.execution_mode,
        cost_model=cost_model,
        config_dir=Path(args.config_dir),
        random_seed=args.random_seed,
        llm_model=args.llm_model,
        llm_parameters=args.llm_parameters,
    )
    for result in results:
        output_path = Path(args.experiments_root) / result.experiment_id / "leaderboard.json"
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
        print(output_path)
    record_experiment(
        experiment_db,
        experiment_id=experiment_id,
        symbol=args.symbol or spec.symbol,
        status="completed",
        metadata={"trials": len(results), "execution_mode": args.execution_mode},
    )
    print(json.dumps({"trials": len(results)}, indent=2, sort_keys=True))
    return 0


def cmd_research_propose(args: argparse.Namespace) -> int:
    seed_spec = load_strategy_spec(Path(args.spec))
    experiment_id = args.experiment_id or f"{seed_spec.name}_proposal"
    feedback = load_train_validation_feedback(
        Path(args.feedback_experiments_root),
        experiment_id=args.feedback_experiment_id,
        limit=args.feedback_limit,
    ) if args.feedback_experiments_root else None
    proposal = create_llm_adapter(args.model, args.llm_parameters).propose(seed_spec, feedback=feedback)
    output_path = Path(args.output) if args.output else Path("strategies/generated") / f"{proposal.strategy.name}.json"
    write_json(output_path, proposal.strategy.raw)

    audit_path = (
        Path(args.audit_log)
        if args.audit_log
        else Path(args.experiments_root) / experiment_id / "llm_audit.jsonl"
    )
    audit_record = proposal.audit_record()
    append_audit_log(audit_path, audit_record)

    experiment_db = Path(args.experiment_db)
    record_experiment(
        experiment_db,
        experiment_id=experiment_id,
        symbol=proposal.strategy.symbol,
        status="proposed",
        metadata={
            "seed_spec": str(Path(args.spec)),
            "output": str(output_path),
            "feedback_count": len(feedback or []),
            "feedback_experiment_id": args.feedback_experiment_id,
        },
    )
    record_audit_event(
        experiment_db,
        experiment_id=experiment_id,
        event_type="llm_strategy_proposal",
        payload=audit_record,
    )

    print(
        json.dumps(
            {
                "strategy_spec": str(output_path),
                "audit_log": str(audit_path),
                "experiment_id": experiment_id,
                "prompt_hash": proposal.prompt_hash,
                "response_hash": proposal.response_hash,
                "feedback_count": len(feedback or []),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_research_generate_feature_seeds(args: argparse.Namespace) -> int:
    paths = write_feature_combo_strategy_specs(
        output_dir=Path(args.output_dir),
        count=args.count,
        random_seed=args.random_seed,
        symbol=args.symbol,
        timeframe=args.timeframe,
        prefix=args.prefix,
        manifest_path=Path(args.manifest_output) if args.manifest_output else None,
    )
    print(
        json.dumps(
            {
                "count": len(paths),
                "output_dir": str(Path(args.output_dir)),
                "strategy_specs": [str(path) for path in paths],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_research_feature_readiness(args: argparse.Namespace) -> int:
    report = feature_readiness_report()
    output = Path(args.output) if args.output else None
    if output:
        write_json(output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def cmd_research_discover_target(args: argparse.Namespace) -> int:
    if args.spec:
        spec = load_strategy_spec(Path(args.spec))
        seed_specs = [spec]
        seed_selection_report = {
            "mode": "explicit_spec",
            "candidate_count": 1,
            "selected_count": 1,
            "selected_paths": [str(Path(args.spec))],
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
    else:
        seed_specs, seed_selection_report = discover_strategy_seed_specs(
            strategies_root=Path(args.strategies_root),
            spec_paths=args.specs,
            symbol=args.symbol,
            timeframe=args.timeframe,
            strategy_families=args.strategy_families,
            limit=args.max_seed_strategies,
        )
        if not seed_specs:
            raise SystemExit("No strategy seed specs matched the discovery filters")
        spec = seed_specs[0]
    symbol = get_symbol(args.symbol or spec.symbol, Path(args.config_dir))
    cost_model = get_cost_model(spec.cost_model, Path(args.config_dir))
    date_from = parse_date(args.date_from)
    date_to = parse_date(args.date_to)
    discovery_id = args.experiment_id or f"{spec.name}_target_discovery_{date_from.isoformat()}_{date_to.isoformat()}"
    target = StrategyTargetCriteria(
        min_annual_trades=args.min_annual_trades,
        min_sharpe=args.min_sharpe,
        min_win_probability=args.min_win_probability,
        min_profit_factor=args.min_profit_factor,
        max_drawdown=args.max_drawdown,
        min_positive_year_ratio=args.min_positive_year_ratio,
        max_final_holdout_sharpe_decay=args.max_final_holdout_sharpe_decay,
        max_parameter_combinations=args.max_target_parameter_combinations,
        min_non_overlap_test_folds=args.min_non_overlap_test_folds,
    )
    discovery_kwargs = {
        "symbol_config": symbol,
        "data_root": Path(args.data_root),
        "discovery_id": discovery_id,
        "date_from": date_from,
        "date_to": date_to,
        "max_rounds": args.max_rounds,
        "trials_per_round": args.trials_per_round,
        "target_count": args.target_count,
        "target": target,
        "starting_equity": args.starting_equity,
        "train_days": args.train_days,
        "validation_days": args.validation_days,
        "test_days": args.test_days,
        "step_days": args.step_days,
        "embargo_days": args.embargo_days,
        "final_holdout_days": args.final_holdout_days,
        "min_folds": args.min_folds,
        "indicator_warmup_days": args.indicator_warmup_days,
        "max_parameter_combinations": args.max_parameter_combinations,
        "allow_high_parameter_budget": args.allow_high_parameter_budget,
        "execution_mode": args.execution_mode,
        "cost_model": cost_model,
        "config_dir": Path(args.config_dir),
        "random_seed": args.random_seed,
        "llm_model": args.llm_model,
        "llm_parameters": args.llm_parameters,
        "include_mutations": args.include_mutations,
    }
    if args.spec:
        discovery = run_llm_target_discovery(seed_spec=spec, **discovery_kwargs)
    else:
        discovery = run_llm_seed_pool_target_discovery(
            seed_specs=seed_specs,
            seed_selection_report=seed_selection_report,
            **discovery_kwargs,
        )
    experiments_root = Path(args.experiments_root)
    experiment_db = Path(args.experiment_db)
    record_experiment(
        experiment_db,
        experiment_id=discovery_id,
        symbol=args.symbol or spec.symbol,
        status="running",
        metadata={
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "execution_mode": args.execution_mode,
            "target": target.to_dict(),
            "max_rounds": args.max_rounds,
            "trials_per_round": args.trials_per_round,
            "target_count": args.target_count,
            "llm_model": args.llm_model,
            "llm_parameters": args.llm_parameters,
            "include_mutations": args.include_mutations,
            "seed_spec": str(Path(args.spec)) if args.spec else None,
            "seed_selection_report": seed_selection_report,
        },
    )
    for audit in discovery.proposal_audits:
        append_audit_log(experiments_root / discovery_id / "llm_audit.jsonl", audit)
        record_audit_event(
            experiment_db,
            experiment_id=discovery_id,
            event_type="llm_target_discovery_proposal",
            payload=audit,
        )
    result_paths = []
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
    record_experiment(
        experiment_db,
        experiment_id=discovery_id,
        symbol=args.symbol or spec.symbol,
        status="completed",
        metadata={
            **discovery.to_dict(),
            "result_paths": result_paths,
            "summary_path": str(summary_path),
        },
    )
    print(
        json.dumps(
            {
                **discovery.to_dict(),
                "result_paths": result_paths,
                "summary_path": str(summary_path),
                "seed_selection_report": seed_selection_report,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_report_leaderboard(args: argparse.Namespace) -> int:
    report = load_leaderboard_report(Path(args.experiments_root))
    if args.experiment_id:
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
                if row["experiment_id"] == args.experiment_id
                or row["experiment_id"].startswith(f"{args.experiment_id}_")
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
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def cmd_report_experiment(args: argparse.Namespace) -> int:
    print(
        json.dumps(
            load_experiment_summary(Path(args.experiment_db), args.experiment_id),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_paper_replay(args: argparse.Namespace) -> int:
    result = load_backtest_result(Path(args.strategy_id))
    replay = replay_trades(result["trades"], starting_equity=args.starting_equity)
    payload = replay.to_dict()
    payload["replay_attribution"] = build_paper_replay_attribution(result, replay)
    output = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
        print(output_path)
    else:
        print(output)
    return 0


def cmd_nt_export_signal(args: argparse.Namespace) -> int:
    result = load_backtest_result(Path(args.strategy_id))
    output = export_ninjatrader_signals(
        result["trades"],
        export_format=args.format,
        account=args.account,
        instrument=args.instrument,
    )
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output, encoding="utf-8")
        print(output_path)
    else:
        print(output, end="")
    return 0


def cmd_events_validate(args: argparse.Namespace) -> int:
    calendar_path = resolve_event_calendar_path(args.calendar, Path(args.config_dir))
    print(json.dumps(validate_event_calendar(calendar_path), indent=2, sort_keys=True))
    return 0


def cmd_events_list(args: argparse.Namespace) -> int:
    calendar_path = resolve_event_calendar_path(args.calendar, Path(args.config_dir))
    calendar = load_event_calendar(calendar_path)
    date_from = parse_date(args.date_from) if args.date_from else None
    date_to = parse_date(args.date_to) if args.date_to else None
    rows = []
    for event in calendar["events"]:
        event_day = event.timestamp_utc.date()
        if date_from and event_day < date_from:
            continue
        if date_to and event_day > date_to:
            continue
        if args.symbol and args.symbol not in event.affected_symbols and "*" not in event.affected_symbols:
            continue
        rows.append(event.to_dict())
    print(
        json.dumps(
            {
                "calendar_id": calendar["calendar_id"],
                "event_calendar_hash": calendar["event_calendar_hash"],
                "events": rows,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_events_build_context(args: argparse.Namespace) -> int:
    calendar_path = resolve_event_calendar_path(args.calendar, Path(args.config_dir))
    calendar = load_event_calendar(calendar_path)
    data_root = Path(args.data_root)
    date_from = parse_date(args.date_from)
    date_to = parse_date(args.date_to)
    outputs = []
    total_rows = 0
    for day in iter_dates(date_from, date_to):
        files = [bar_path(data_root, args.symbol, args.timeframe, day)]
        contexts = build_event_context_rows(args.symbol, files, calendar["events"])
        output = Path(args.output) if args.output else event_context_path(data_root, args.symbol, day)
        write_event_context_parquet(output, contexts)
        total_rows += len(contexts)
        outputs.append({"path": str(output), "rows": len(contexts), "day": day.isoformat()})
    print(
        json.dumps(
            {
                "calendar_id": calendar["calendar_id"],
                "event_calendar_hash": calendar["event_calendar_hash"],
                "rows": total_rows,
                "outputs": outputs,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_monitor_once(args: argparse.Namespace) -> int:
    data_root = Path(args.data_root)
    day = parse_date(args.date)
    calendar_events = []
    if args.calendar:
        calendar_path = resolve_event_calendar_path(args.calendar, Path(args.config_dir))
        calendar_events = load_event_calendar(calendar_path)["events"]
    payload = build_monitor_report(
        symbol=args.symbol,
        timeframe=args.timeframe,
        bar_files=[bar_path(data_root, args.symbol, args.timeframe, day)],
        events=calendar_events,
        proximity_points=args.proximity_points,
    )
    outputs = write_monitor_outputs(Path(args.output_dir), payload)
    payload["outputs"] = outputs
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tlm")
    parser.add_argument("--config-dir", default="configs")
    parser.add_argument("--data-root", default="data")
    subparsers = parser.add_subparsers(dest="command", required=True)

    data = subparsers.add_parser("data")
    data_subparsers = data.add_subparsers(dest="data_command", required=True)

    discover = data_subparsers.add_parser("discover")
    discover.add_argument("--provider", default="dukascopy")
    discover.add_argument("--query", default="")
    discover.set_defaults(func=cmd_data_discover)

    download = data_subparsers.add_parser("download")
    download.add_argument("--symbol", required=True)
    download.add_argument("--from", dest="date_from", required=True)
    download.add_argument("--to", dest="date_to", required=True)
    download.add_argument("--granularity", default="tick")
    download.add_argument("--hour-retries", type=int, default=3)
    download.add_argument("--hour-timeout-seconds", type=int, default=30)
    download.add_argument("--force", action="store_true", help="rebuild existing non-empty normalized day outputs")
    download.set_defaults(func=cmd_data_download)

    build_bars = data_subparsers.add_parser("build-bars")
    build_bars.add_argument("--symbol", required=True)
    build_bars.add_argument("--from", dest="date_from", required=True)
    build_bars.add_argument("--to", dest="date_to", required=True)
    build_bars.add_argument("--timeframe", default="1m")
    build_bars.set_defaults(func=cmd_data_build_bars)

    import_firstrate = data_subparsers.add_parser("import-firstrate")
    import_firstrate.add_argument("--symbol", required=True)
    import_firstrate.add_argument("--input", action="append", required=True)
    import_firstrate.add_argument("--source-timezone", default="UTC")
    import_firstrate.add_argument("--force", action="store_true")
    import_firstrate.set_defaults(func=cmd_data_import_firstrate)

    import_databento_quotes = data_subparsers.add_parser("import-databento-quotes")
    import_databento_quotes.add_argument("--symbol", required=True)
    import_databento_quotes.add_argument("--input", action="append", required=True)
    import_databento_quotes.add_argument("--source-timezone", default="UTC")
    import_databento_quotes.add_argument("--force", action="store_true")
    import_databento_quotes.set_defaults(func=cmd_data_import_databento_quotes)

    quality = data_subparsers.add_parser("quality")
    quality.add_argument("--symbol", required=True)
    quality.add_argument("--from", dest="date_from", required=True)
    quality.add_argument("--to", dest="date_to", required=True)
    quality.set_defaults(func=cmd_data_quality)

    bar_quality = data_subparsers.add_parser("bar-quality")
    bar_quality.add_argument("--symbol", required=True)
    bar_quality.add_argument("--from", dest="date_from", required=True)
    bar_quality.add_argument("--to", dest="date_to", required=True)
    bar_quality.add_argument("--timeframe", default="1m")
    bar_quality.add_argument("--max-normal-price-jump", type=float, default=100.0)
    bar_quality.add_argument("--output")
    bar_quality.set_defaults(func=cmd_data_bar_quality)

    split_manifest = data_subparsers.add_parser("split-manifest")
    split_manifest.add_argument("--symbol", required=True)
    split_manifest.add_argument("--from", dest="date_from", required=True)
    split_manifest.add_argument("--to", dest="date_to", required=True)
    split_manifest.add_argument("--timeframe", default="1m")
    split_manifest.add_argument("--train-days", type=int, default=730)
    split_manifest.add_argument("--validation-days", type=int, default=182)
    split_manifest.add_argument("--test-days", type=int, default=182)
    split_manifest.add_argument("--step-days", type=int, default=91)
    split_manifest.add_argument("--embargo-days", type=int, default=5)
    split_manifest.add_argument("--final-holdout-days", type=int, default=365)
    split_manifest.add_argument("--min-folds", type=int, default=1)
    split_manifest.add_argument("--indicator-warmup-days", type=int, default=0)
    split_manifest.add_argument("--output")
    split_manifest.set_defaults(func=cmd_data_split_manifest)

    quote_replay = data_subparsers.add_parser("quote-replay")
    quote_replay.add_argument("--symbol", required=True)
    quote_replay.add_argument("--from", dest="date_from", required=True)
    quote_replay.add_argument("--to", dest="date_to", required=True)
    quote_replay.add_argument("--backtest-result", required=True)
    quote_replay.add_argument("--output")
    quote_replay.set_defaults(func=cmd_data_quote_replay)

    strategy = subparsers.add_parser("strategy")
    strategy_subparsers = strategy.add_subparsers(dest="strategy_command", required=True)
    validate = strategy_subparsers.add_parser("validate")
    validate.add_argument("--spec", required=True)
    validate.set_defaults(func=cmd_strategy_validate)

    modules = subparsers.add_parser("modules")
    modules_subparsers = modules.add_subparsers(dest="modules_command", required=True)
    modules_list = modules_subparsers.add_parser("list")
    modules_list.set_defaults(func=cmd_modules_list)
    modules_summary = modules_subparsers.add_parser("summary")
    modules_summary.add_argument("--experiments-root", default="experiments")
    modules_summary.add_argument("--memory", action="append")
    modules_summary.add_argument("--target-min-per-day", type=float, default=2.0)
    modules_summary.add_argument("--target-max-per-day", type=float, default=3.0)
    modules_summary.add_argument("--min-proxy-win-rate", type=float, default=0.53)
    modules_summary.add_argument("--lookback-days", type=int, default=90)
    modules_summary.add_argument("--include-rejected", action="store_true")
    modules_summary.set_defaults(func=cmd_modules_summary)

    trigger_gate = subparsers.add_parser("trigger-gate")
    trigger_gate_subparsers = trigger_gate.add_subparsers(dest="trigger_gate_command", required=True)
    trigger_gate_simulate = trigger_gate_subparsers.add_parser("simulate")
    trigger_gate_simulate.add_argument("--pool")
    trigger_gate_simulate.add_argument("--experiments-root", default="experiments")
    trigger_gate_simulate.add_argument("--memory", action="append")
    trigger_gate_simulate.add_argument("--from", dest="date_from", required=True)
    trigger_gate_simulate.add_argument("--to", dest="date_to", required=True)
    trigger_gate_simulate.add_argument("--output-dir", required=True)
    trigger_gate_simulate.add_argument("--enable-llm", action="store_true")
    trigger_gate_simulate.add_argument("--model", default="local-trigger-gate")
    trigger_gate_simulate.add_argument("--daily-token-budget", type=int)
    trigger_gate_simulate.add_argument("--step-minutes", type=int, default=15)
    trigger_gate_simulate.add_argument("--target-min-per-day", type=float, default=2.0)
    trigger_gate_simulate.add_argument("--target-max-per-day", type=float, default=3.0)
    trigger_gate_simulate.add_argument("--min-proxy-win-rate", type=float, default=0.53)
    trigger_gate_simulate.add_argument("--lookback-days", type=int, default=90)
    trigger_gate_simulate.add_argument("--include-rejected", action="store_true")
    trigger_gate_simulate.set_defaults(func=cmd_trigger_gate_simulate)
    trigger_gate_report = trigger_gate_subparsers.add_parser("report")
    trigger_gate_report.add_argument("--output-dir", required=True)
    trigger_gate_report.add_argument("--previous-pool")
    trigger_gate_report.set_defaults(func=cmd_trigger_gate_report)
    trigger_gate_schedule = trigger_gate_subparsers.add_parser("schedule")
    trigger_gate_schedule.add_argument("--pool")
    trigger_gate_schedule.add_argument("--experiments-root", default="experiments")
    trigger_gate_schedule.add_argument("--as-of", required=True)
    trigger_gate_schedule.add_argument("--output-root", required=True)
    trigger_gate_schedule.add_argument("--output")
    trigger_gate_schedule.add_argument("--enable-llm", action="store_true")
    trigger_gate_schedule.add_argument("--daily-token-budget", type=int)
    trigger_gate_schedule.add_argument("--target-min-per-day", type=float, default=2.0)
    trigger_gate_schedule.add_argument("--target-max-per-day", type=float, default=3.0)
    trigger_gate_schedule.add_argument("--min-proxy-win-rate", type=float, default=0.53)
    trigger_gate_schedule.add_argument("--lookback-days", type=int, default=90)
    trigger_gate_schedule.add_argument("--include-rejected", action="store_true")
    trigger_gate_schedule.set_defaults(func=cmd_trigger_gate_schedule)
    trigger_gate_memory = trigger_gate_subparsers.add_parser("memory")
    trigger_gate_memory.add_argument("--output-dir", required=True)
    trigger_gate_memory.add_argument("--strategy-spec-hash")
    trigger_gate_memory.add_argument("--module-id")
    trigger_gate_memory.add_argument("--decision", choices=["allow", "block", "observe"])
    trigger_gate_memory.add_argument("--risk-level", choices=["low", "medium", "high"])
    trigger_gate_memory.add_argument("--outcome-label")
    trigger_gate_memory.add_argument("--limit", type=int, default=100)
    trigger_gate_memory.set_defaults(func=cmd_trigger_gate_memory)
    trigger_gate_outcome = trigger_gate_subparsers.add_parser("outcome")
    trigger_gate_outcome.add_argument("--output-dir", required=True)
    trigger_gate_outcome.add_argument("--decision-id", required=True)
    trigger_gate_outcome.add_argument("--outcome-window", required=True)
    trigger_gate_outcome.add_argument("--net-pnl", type=float)
    trigger_gate_outcome.add_argument("--mfe", type=float)
    trigger_gate_outcome.add_argument("--mae", type=float)
    trigger_gate_outcome.add_argument("--paper-fill-id")
    trigger_gate_outcome.add_argument("--would-have-hit-target", action=argparse.BooleanOptionalAction, default=None)
    trigger_gate_outcome.add_argument("--would-have-hit-stop", action=argparse.BooleanOptionalAction, default=None)
    trigger_gate_outcome.add_argument("--final-label")
    trigger_gate_outcome.add_argument("--notes")
    trigger_gate_outcome.set_defaults(func=cmd_trigger_gate_outcome)

    backtest = subparsers.add_parser("backtest")
    backtest_subparsers = backtest.add_subparsers(dest="backtest_command", required=True)
    bar = backtest_subparsers.add_parser("bar")
    bar.add_argument("--spec", required=True)
    bar.add_argument("--symbol")
    bar.add_argument("--from", dest="date_from", required=True)
    bar.add_argument("--to", dest="date_to", required=True)
    bar.add_argument("--starting-equity", type=float, default=100_000)
    bar.add_argument("--output")
    bar.set_defaults(func=cmd_backtest_bar)

    tick = backtest_subparsers.add_parser("tick")
    tick.add_argument("--spec", required=True)
    tick.add_argument("--symbol")
    tick.add_argument("--from", dest="date_from", required=True)
    tick.add_argument("--to", dest="date_to", required=True)
    tick.add_argument("--starting-equity", type=float, default=100_000)
    tick.add_argument("--output")
    tick.set_defaults(func=cmd_backtest_tick)

    research = subparsers.add_parser("research")
    research_subparsers = research.add_subparsers(dest="research_command", required=True)
    research_run = research_subparsers.add_parser("run")
    research_run.add_argument("--spec", required=True)
    research_run.add_argument("--symbol")
    research_run.add_argument("--from", dest="date_from", required=True)
    research_run.add_argument("--to", dest="date_to", required=True)
    research_run.add_argument("--experiment-id")
    research_run.add_argument("--experiments-root", default="experiments")
    research_run.add_argument("--experiment-db", default="experiments/research.sqlite3")
    research_run.add_argument("--max-trials", type=int, default=1)
    research_run.add_argument("--execution-mode", choices=["bar", "tick", "bar_then_tick"], default="bar")
    research_run.add_argument("--random-seed", type=int, default=0)
    research_run.add_argument("--llm-model", default="local-deterministic-template")
    research_run.add_argument("--llm-parameters", type=parse_json_object, default={})
    research_run.add_argument(
        "--max-parameter-combinations",
        type=int,
        default=DEFAULT_PARAMETER_BUDGET,
    )
    research_run.add_argument("--allow-high-parameter-budget", action="store_true")
    research_run.add_argument("--starting-equity", type=float, default=100_000)
    research_run.add_argument("--train-days", type=int, default=730)
    research_run.add_argument("--validation-days", type=int, default=182)
    research_run.add_argument("--test-days", type=int, default=182)
    research_run.add_argument("--step-days", type=int, default=91)
    research_run.add_argument("--embargo-days", type=int, default=5)
    research_run.add_argument("--final-holdout-days", type=int, default=365)
    research_run.add_argument("--min-folds", type=int, default=1)
    research_run.add_argument("--indicator-warmup-days", type=int)
    research_run.set_defaults(func=cmd_research_run)

    propose = research_subparsers.add_parser("propose")
    propose.add_argument("--spec", required=True)
    propose.add_argument("--model", default="local-deterministic-template")
    propose.add_argument("--llm-parameters", type=parse_json_object, default={})
    propose.add_argument("--feedback-experiments-root")
    propose.add_argument("--feedback-experiment-id")
    propose.add_argument("--feedback-limit", type=int, default=10)
    propose.add_argument("--experiment-id")
    propose.add_argument("--experiments-root", default="experiments")
    propose.add_argument("--experiment-db", default="experiments/research.sqlite3")
    propose.add_argument("--audit-log")
    propose.add_argument("--output")
    propose.set_defaults(func=cmd_research_propose)

    generate_feature_seeds = research_subparsers.add_parser("generate-feature-seeds")
    generate_feature_seeds.add_argument("--count", type=int, default=28)
    generate_feature_seeds.add_argument("--random-seed", type=int, default=0)
    generate_feature_seeds.add_argument("--output-dir", default="strategies")
    generate_feature_seeds.add_argument("--symbol", default="NQmain")
    generate_feature_seeds.add_argument("--timeframe", default="1m")
    generate_feature_seeds.add_argument("--prefix", default="generated_feature_combo")
    generate_feature_seeds.add_argument("--manifest-output")
    generate_feature_seeds.set_defaults(func=cmd_research_generate_feature_seeds)

    feature_readiness = research_subparsers.add_parser("feature-readiness")
    feature_readiness.add_argument("--output")
    feature_readiness.set_defaults(func=cmd_research_feature_readiness)

    discover = research_subparsers.add_parser("discover-target")
    discover.add_argument("--spec")
    discover.add_argument("--specs", nargs="*")
    discover.add_argument("--strategies-root", default="strategies")
    discover.add_argument("--strategy-families", nargs="*")
    discover.add_argument("--max-seed-strategies", type=int)
    discover.add_argument("--symbol")
    discover.add_argument("--timeframe")
    discover.add_argument("--from", dest="date_from", required=True)
    discover.add_argument("--to", dest="date_to", required=True)
    discover.add_argument("--experiment-id")
    discover.add_argument("--experiments-root", default="experiments")
    discover.add_argument("--experiment-db", default="experiments/research.sqlite3")
    discover.add_argument("--max-rounds", type=int, default=10)
    discover.add_argument("--trials-per-round", type=int, default=1)
    discover.add_argument("--target-count", type=int, default=1)
    discover.add_argument("--min-annual-trades", type=float, default=1000)
    discover.add_argument("--min-sharpe", type=float, default=2)
    discover.add_argument("--min-win-probability", type=float, default=0.53)
    discover.add_argument("--min-profit-factor", type=float, default=1.2)
    discover.add_argument("--max-drawdown", type=float, default=10_000)
    discover.add_argument("--min-positive-year-ratio", type=float, default=0.6)
    discover.add_argument("--max-final-holdout-sharpe-decay", type=float, default=0.5)
    discover.add_argument("--max-target-parameter-combinations", type=int, default=200)
    discover.add_argument("--min-non-overlap-test-folds", type=int, default=1)
    discover.add_argument("--execution-mode", choices=["bar", "tick", "bar_then_tick"], default="bar")
    discover.add_argument("--random-seed", type=int, default=0)
    discover.add_argument("--llm-model", default="local-deterministic-template")
    discover.add_argument("--llm-parameters", type=parse_json_object, default={})
    discover.add_argument("--include-mutations", action="store_true")
    discover.add_argument(
        "--max-parameter-combinations",
        type=int,
        default=DEFAULT_PARAMETER_BUDGET,
    )
    discover.add_argument("--allow-high-parameter-budget", action="store_true")
    discover.add_argument("--starting-equity", type=float, default=100_000)
    discover.add_argument("--train-days", type=int, default=730)
    discover.add_argument("--validation-days", type=int, default=182)
    discover.add_argument("--test-days", type=int, default=182)
    discover.add_argument("--step-days", type=int, default=91)
    discover.add_argument("--embargo-days", type=int, default=5)
    discover.add_argument("--final-holdout-days", type=int, default=365)
    discover.add_argument("--min-folds", type=int, default=1)
    discover.add_argument("--indicator-warmup-days", type=int)
    discover.set_defaults(func=cmd_research_discover_target)

    report = subparsers.add_parser("report")
    report_subparsers = report.add_subparsers(dest="report_command", required=True)
    leaderboard = report_subparsers.add_parser("leaderboard")
    leaderboard.add_argument("--experiments-root", default="experiments")
    leaderboard.add_argument("--experiment-id")
    leaderboard.set_defaults(func=cmd_report_leaderboard)

    experiment = report_subparsers.add_parser("experiment")
    experiment.add_argument("--experiment-id", required=True)
    experiment.add_argument("--experiment-db", default="experiments/research.sqlite3")
    experiment.set_defaults(func=cmd_report_experiment)

    paper = subparsers.add_parser("paper")
    paper_subparsers = paper.add_subparsers(dest="paper_command", required=True)
    paper_replay = paper_subparsers.add_parser("replay")
    paper_replay.add_argument("--strategy-id", required=True)
    paper_replay.add_argument("--starting-equity", type=float, default=100_000)
    paper_replay.add_argument("--output")
    paper_replay.set_defaults(func=cmd_paper_replay)

    nt = subparsers.add_parser("nt")
    nt_subparsers = nt.add_subparsers(dest="nt_command", required=True)
    export_signal = nt_subparsers.add_parser("export-signal")
    export_signal.add_argument("--strategy-id", required=True)
    export_signal.add_argument("--format", choices=["csv", "oif"], required=True)
    export_signal.add_argument("--account", required=True)
    export_signal.add_argument("--instrument", required=True)
    export_signal.add_argument("--output")
    export_signal.set_defaults(func=cmd_nt_export_signal)

    events = subparsers.add_parser("events")
    events_subparsers = events.add_subparsers(dest="events_command", required=True)
    events_validate = events_subparsers.add_parser("validate")
    events_validate.add_argument("--calendar", required=True)
    events_validate.set_defaults(func=cmd_events_validate)

    events_list = events_subparsers.add_parser("list")
    events_list.add_argument("--calendar", required=True)
    events_list.add_argument("--symbol")
    events_list.add_argument("--from", dest="date_from")
    events_list.add_argument("--to", dest="date_to")
    events_list.set_defaults(func=cmd_events_list)

    events_context = events_subparsers.add_parser("build-context")
    events_context.add_argument("--calendar", required=True)
    events_context.add_argument("--symbol", required=True)
    events_context.add_argument("--from", dest="date_from", required=True)
    events_context.add_argument("--to", dest="date_to", required=True)
    events_context.add_argument("--timeframe", default="5m")
    events_context.add_argument("--output")
    events_context.set_defaults(func=cmd_events_build_context)

    monitor = subparsers.add_parser("monitor")
    monitor_subparsers = monitor.add_subparsers(dest="monitor_command", required=True)
    monitor_once = monitor_subparsers.add_parser("once")
    monitor_once.add_argument("--symbol", required=True)
    monitor_once.add_argument("--date", required=True)
    monitor_once.add_argument("--timeframe", default="5m")
    monitor_once.add_argument("--calendar")
    monitor_once.add_argument("--proximity-points", type=float, default=2.0)
    monitor_once.add_argument("--output-dir", default="data/runtime/reports")
    monitor_once.set_defaults(func=cmd_monitor_once)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, StrategySpecError, ParameterBudgetError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
