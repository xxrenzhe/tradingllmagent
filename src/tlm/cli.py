from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from .bars import build_minute_bars_from_parquet
from .backtest import result_to_json, run_bar_backtest
from .cli_dates import iter_dates
from .config import ConfigError, get_symbol, load_symbols
from .dukascopy import download_hour, iter_hours, parse_bi5_file
from .experiments import (
    load_experiment_summary,
    record_audit_event,
    record_experiment,
    record_trial,
)
from .llm import DeterministicLocalLLM, append_audit_log
from .quality import build_quality_report
from .research import load_leaderboard, run_budgeted_research, write_research_result
from .storage import (
    bar_path,
    compute_data_version_hash,
    normalized_tick_path,
    quality_path,
    write_json,
    write_ticks_parquet,
)
from .strategy import StrategySpecError, load_strategy_spec


def parse_date(value: str) -> date:
    if value == "today":
        return datetime.now(UTC).date()
    return date.fromisoformat(value)


def day_bounds(value: date) -> tuple[datetime, datetime]:
    start = datetime.combine(value, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def tick_parquet_files(data_root: Path, symbol: str, start: date, end: date) -> list[Path]:
    return [normalized_tick_path(data_root, symbol, day) for day in iter_dates(start, end)]


def cmd_data_discover(args: argparse.Namespace) -> int:
    symbols = load_symbols(Path(args.config_dir))
    query = args.query.lower()
    for alias, symbol in sorted(symbols.items()):
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
    raw_paths: list[Path] = []
    for day in iter_dates(date_from, date_to):
        start, end = day_bounds(day)
        day_ticks = []
        for hour in iter_hours(start, end):
            result = download_hour(symbol, hour, data_root)
            if result.status in {"downloaded", "cached"}:
                raw_paths.append(result.path)
                ticks = parse_bi5_file(result.path, hour, symbol.price_scale)
                day_ticks.extend(ticks)
            print(f"{hour.isoformat()}\t{result.status}\t{result.bytes_written}\t{result.url}")

        output = normalized_tick_path(data_root, args.symbol, day)
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
    if args.timeframe != "1m":
        raise SystemExit("Only --timeframe 1m is supported in Phase 1")
    data_root = Path(args.data_root)
    date_from = parse_date(args.date_from)
    date_to = parse_date(args.date_to)
    total = 0
    for day in iter_dates(date_from, date_to):
        tick_file = normalized_tick_path(data_root, args.symbol, day)
        output = bar_path(data_root, args.symbol, args.timeframe, day)
        count = build_minute_bars_from_parquet([tick_file], output)
        total += count
        print(f"wrote\t{output}\trows={count}")
    print(f"bar build complete: rows={total}")
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


def bar_parquet_files(data_root: Path, symbol: str, timeframe: str, start: date, end: date) -> list[Path]:
    return [bar_path(data_root, symbol, timeframe, day) for day in iter_dates(start, end)]


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


def cmd_backtest_bar(args: argparse.Namespace) -> int:
    spec = load_strategy_spec(Path(args.spec))
    symbol = get_symbol(args.symbol or spec.symbol, Path(args.config_dir))
    data_root = Path(args.data_root)
    date_from = parse_date(args.date_from)
    date_to = parse_date(args.date_to)
    files = bar_parquet_files(data_root, spec.symbol, spec.timeframe, date_from, date_to)
    result = run_bar_backtest(spec, symbol, files, starting_equity=args.starting_equity)
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
            "max_trials": args.max_trials,
            "seed_spec": str(Path(args.spec)),
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
        max_parameter_combinations=args.max_parameter_combinations,
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
        metadata={"trials": len(results)},
    )
    print(json.dumps({"trials": len(results)}, indent=2, sort_keys=True))
    return 0


def cmd_research_propose(args: argparse.Namespace) -> int:
    seed_spec = load_strategy_spec(Path(args.spec))
    experiment_id = args.experiment_id or f"{seed_spec.name}_proposal"
    proposal = DeterministicLocalLLM(model=args.model).propose(seed_spec)
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
        metadata={"seed_spec": str(Path(args.spec)), "output": str(output_path)},
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
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_report_leaderboard(args: argparse.Namespace) -> int:
    rows = load_leaderboard(Path(args.experiments_root))
    print(json.dumps({"rows": rows}, indent=2, sort_keys=True))
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
    download.set_defaults(func=cmd_data_download)

    build_bars = data_subparsers.add_parser("build-bars")
    build_bars.add_argument("--symbol", required=True)
    build_bars.add_argument("--from", dest="date_from", required=True)
    build_bars.add_argument("--to", dest="date_to", required=True)
    build_bars.add_argument("--timeframe", default="1m")
    build_bars.set_defaults(func=cmd_data_build_bars)

    quality = data_subparsers.add_parser("quality")
    quality.add_argument("--symbol", required=True)
    quality.add_argument("--from", dest="date_from", required=True)
    quality.add_argument("--to", dest="date_to", required=True)
    quality.set_defaults(func=cmd_data_quality)

    strategy = subparsers.add_parser("strategy")
    strategy_subparsers = strategy.add_subparsers(dest="strategy_command", required=True)
    validate = strategy_subparsers.add_parser("validate")
    validate.add_argument("--spec", required=True)
    validate.set_defaults(func=cmd_strategy_validate)

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
    research_run.add_argument("--max-parameter-combinations", type=int, default=500)
    research_run.add_argument("--starting-equity", type=float, default=100_000)
    research_run.add_argument("--train-days", type=int, default=730)
    research_run.add_argument("--validation-days", type=int, default=182)
    research_run.add_argument("--test-days", type=int, default=182)
    research_run.add_argument("--step-days", type=int, default=91)
    research_run.add_argument("--embargo-days", type=int, default=5)
    research_run.add_argument("--final-holdout-days", type=int, default=365)
    research_run.add_argument("--min-folds", type=int, default=1)
    research_run.set_defaults(func=cmd_research_run)

    propose = research_subparsers.add_parser("propose")
    propose.add_argument("--spec", required=True)
    propose.add_argument("--model", default="local-deterministic-template")
    propose.add_argument("--experiment-id")
    propose.add_argument("--experiments-root", default="experiments")
    propose.add_argument("--experiment-db", default="experiments/research.sqlite3")
    propose.add_argument("--audit-log")
    propose.add_argument("--output")
    propose.set_defaults(func=cmd_research_propose)

    report = subparsers.add_parser("report")
    report_subparsers = report.add_subparsers(dest="report_command", required=True)
    leaderboard = report_subparsers.add_parser("leaderboard")
    leaderboard.add_argument("--experiments-root", default="experiments")
    leaderboard.set_defaults(func=cmd_report_leaderboard)

    experiment = report_subparsers.add_parser("experiment")
    experiment.add_argument("--experiment-id", required=True)
    experiment.add_argument("--experiment-db", default="experiments/research.sqlite3")
    experiment.set_defaults(func=cmd_report_experiment)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, StrategySpecError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
