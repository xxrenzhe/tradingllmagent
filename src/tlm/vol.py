from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from .backtest import (
    _close_position,
    _evaluate_grammar_node,
    _grammar_exit_points,
    _open_position,
    _selected_feature_values,
    load_bar_rows,
    parse_clock,
    parse_session_range,
    run_bar_strategy,
)
from .cli_dates import iter_dates
from .config import CostModelConfig, SymbolConfig
from .feature_catalog import feature_readiness_report
from .features import compute_executable_features, feature_value
from .metrics import calculate_metrics
from .research import load_leaderboard_report
from .storage import bar_path, compute_data_version_hash, write_json
from .strategy import StrategySpec, load_strategy_spec
from .strategy_generation import VOL_EXECUTION_AWARE_FAMILIES, vol_strategy_generation_manifest
from .variants import stable_hash, strategy_spec_hash


VOL_ARTIFACT_FILENAMES = (
    "vol_feature_readiness.json",
    "vol_strategy_leaderboard.json",
    "vol_cost_stress_report.json",
    "vol_quote_replay_report.json",
    "vol_paper_shadow_review.json",
    "vol_mutation_memory.json",
)

VOL_REQUIRED_FEATURES = (
    "bar_volume",
    "volume_ma_5",
    "volume_ma_20",
    "relative_volume_5",
    "relative_volume_20",
    "volume_percentile_session",
    "volume_spike_flag",
    "multi_timeframe_volume_confirm",
    "volume_price_confirm",
    "volume_absorption_flag",
    "low_volume_filter",
)

VOL_STRONG_CANDIDATE_TARGET = {
    "min_annual_trades": 1000,
    "min_sharpe": 1,
    "min_win_probability": 0.53,
    "min_profit_factor": 1.2,
}

VOL_FINAL_TARGET = {
    "min_annual_trades": 1000,
    "min_sharpe": 2,
    "min_win_probability": 0.53,
}


def build_vol_feature_readiness() -> dict[str, Any]:
    readiness = feature_readiness_report()
    executable = set(readiness.get("executable_features", []))
    missing = [name for name in VOL_REQUIRED_FEATURES if name not in executable]
    return {
        "schema_version": 1,
        "artifact": "vol_feature_readiness",
        "status": "ready" if not missing else "blocked",
        "bar_volume_semantics": {
            "canonical_name": "bar_volume",
            "databento_ohlcv_mapping": "tick_count",
            "runtime_aliases": ["volume", "volume_1m", "tick_count_1m"],
        },
        "required_features": list(VOL_REQUIRED_FEATURES),
        "missing_features": missing,
        "feature_readiness": readiness,
        "execution_data_gap": {
            "ohlcv_has_real_spread": False,
            "required_for_quote_validation": ["TBBO"],
            "required_for_limit_fill_validation": ["MBP-1"],
        },
    }


def build_vol_strategy_leaderboard(experiments_root: Path, specs: Sequence[dict[str, Any]] | None = None) -> dict[str, Any]:
    report = load_leaderboard_report(experiments_root)
    rows = [_is_vol_row(row) for row in report.get("rows", [])]
    rows = [row for row in rows if row is not None]
    candidate_rows = [_is_vol_row(row) for row in report.get("candidate_leaderboard", [])]
    candidate_rows = [row for row in candidate_rows if row is not None]
    freeze_rows = [_is_vol_row(row) for row in report.get("freeze_confirmed_leaderboard", [])]
    freeze_rows = [row for row in freeze_rows if row is not None]
    manifest = vol_strategy_generation_manifest(list(specs or [])) if specs is not None else None
    return {
        "schema_version": 1,
        "artifact": "vol_strategy_leaderboard",
        "experiments_root": str(experiments_root),
        "target": {
            "min_annual_trades": 1000,
            "min_sharpe": 2,
            "min_win_probability": 0.53,
            "requires_quote_replay": True,
            "requires_paper_shadow": True,
        },
        "summary": {
            "total_vol_rows": len(rows),
            "candidate_vol_rows": len(candidate_rows),
            "freeze_confirmed_vol_rows": len(freeze_rows),
            "generated_seed_count": len(specs or []),
        },
        "seed_manifest": manifest,
        "candidate_leaderboard": candidate_rows,
        "freeze_confirmed_leaderboard": freeze_rows,
        "rejected": [_is_vol_row(row) for row in report.get("rejected", []) if _is_vol_row(row) is not None],
    }


def run_vol_prescreen(
    *,
    strategy_paths: Sequence[Path],
    data_root: Path,
    symbol_config: SymbolConfig,
    cost_model: CostModelConfig,
    date_from,
    date_to,
    timeframe: str = "1m",
    output_dir: Path | None = None,
    starting_equity: float = 100_000,
) -> dict[str, Any]:
    specs = [load_strategy_spec(path) for path in strategy_paths]
    if not specs:
        raise ValueError("At least one VOL strategy spec is required")
    invalid = [spec.name for spec in specs if spec.strategy_family not in VOL_EXECUTION_AWARE_FAMILIES]
    if invalid:
        raise ValueError(f"Non-VOL strategy specs are not allowed in VOL pre-screen: {', '.join(invalid)}")
    bar_files = [
        bar_path(data_root, symbol_config.alias, timeframe, day)
        for day in iter_dates(date_from, date_to)
    ]
    existing_files = [path for path in bar_files if path.exists()]
    if not existing_files:
        raise ValueError(f"No bar files found for {symbol_config.alias} {timeframe} between {date_from} and {date_to}")

    trades_by_strategy: dict[str, list] = {spec.name: [] for spec in specs}
    feature_hashes = []
    calendar_days = 0
    yearly_rows = []
    for year in range(date_from.year, date_to.year + 1):
        year_files = [path for path in existing_files if f"date={year}-" in str(path)]
        if not year_files:
            continue
        bars = load_bar_rows(year_files)
        if not bars:
            continue
        calendar_days += len({bar["timestamp"].date() for bar in bars})
        feature_bars = compute_executable_features(
            bars,
            session_trade=specs[0].session.trade,
            flatten=specs[0].session.flatten,
            tick_size=cost_model.tick_size,
        )
        feature_hashes.append(_feature_year_summary_hash(year, feature_bars))
        year_trades_by_strategy = _run_vol_strategy_batch(specs, symbol_config, feature_bars, cost_model)
        for spec in specs:
            trades = year_trades_by_strategy[spec.name]
            trades_by_strategy[spec.name].extend(trades)
            yearly_metrics = _metrics_for_trades(trades, starting_equity, len({bar["timestamp"].date() for bar in bars}))
            yearly_rows.append(
                {
                    "year": year,
                    "strategy_name": spec.name,
                    "strategy_family": spec.strategy_family,
                    **yearly_metrics.to_dict(),
                    "win_probability": _win_probability(trades),
                }
            )

    rows = []
    for spec in specs:
        trades = sorted(trades_by_strategy[spec.name], key=lambda trade: trade.exit_time)
        metrics = _metrics_for_trades(trades, starting_equity, calendar_days)
        win_probability = _win_probability(trades)
        gate = _vol_prescreen_gate(metrics.to_dict(), win_probability)
        row = {
            "experiment_id": f"vol_prescreen_{spec.name}",
            "execution_mode": "bar",
            "strategy_name": spec.name,
            "strategy_family": spec.strategy_family,
            "strategy_spec_hash": strategy_spec_hash(spec),
            "strategy_card": _vol_strategy_card(spec),
            "cost_model": cost_model.to_dict(),
            "cost_model_hash": stable_hash(cost_model.to_dict()),
            "data_version_hash": compute_data_version_hash(
                existing_files,
                {
                    "artifact": "vol_prescreen",
                    "symbol": symbol_config.alias,
                    "timeframe": timeframe,
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                    "strategy_name": spec.name,
                },
            ),
            "feature_snapshot_hash": stable_hash(feature_hashes),
            "trade_count": metrics.trade_count,
            "net_pnl_test": metrics.net_pnl,
            "sharpe_test": metrics.sharpe,
            "annual_trades_test": metrics.annual_trades,
            "win_probability_test": win_probability,
            "profit_factor_test": metrics.profit_factor,
            "max_drawdown_test": metrics.max_drawdown,
            "avg_trade_net_pnl": metrics.avg_trade_net_pnl,
            "passed": gate["strong_candidate"],
            "final_target_passed": gate["final_target"],
            "reasons": gate["reasons"],
            "next_round_suggestions": _vol_next_round_suggestions(gate["reasons"]),
        }
        rows.append(row)

    rows = sorted(
        rows,
        key=lambda row: (
            not row["final_target_passed"],
            not row["passed"],
            -(row["sharpe_test"] or -999),
            -row["net_pnl_test"],
        ),
    )
    candidate_rows = [row for row in rows if row["passed"]]
    final_rows = [row for row in rows if row["final_target_passed"]]
    rejected_rows = [row for row in rows if not row["passed"]]
    seed_manifest = vol_strategy_generation_manifest([spec.raw for spec in specs], output_paths=list(strategy_paths))
    leaderboard = {
        "schema_version": 1,
        "artifact": "vol_strategy_leaderboard",
        "mode": "ohlcv_prescreen",
        "symbol": symbol_config.alias,
        "timeframe": timeframe,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "target": {
            **VOL_FINAL_TARGET,
            "requires_quote_replay": True,
            "requires_paper_shadow": True,
        },
        "summary": {
            "total_vol_rows": len(rows),
            "candidate_vol_rows": len(candidate_rows),
            "freeze_confirmed_vol_rows": 0,
            "final_target_rows": len(final_rows),
            "generated_seed_count": len(specs),
        },
        "seed_manifest": seed_manifest,
        "candidate_leaderboard": candidate_rows,
        "freeze_confirmed_leaderboard": [],
        "final_target_leaderboard": final_rows,
        "rejected": rejected_rows,
        "rows": rows,
        "yearly_results": yearly_rows,
        "family_attribution": _vol_family_attribution(rows),
        "artifact_hashes": {
            "data_version_hash": stable_hash([row["data_version_hash"] for row in rows]),
            "feature_snapshot_hash": stable_hash(feature_hashes),
            "cost_model_hash": stable_hash(cost_model.to_dict()),
        },
    }
    report = {
        "schema_version": 1,
        "artifact": "vol_prescreen_run",
        "symbol": symbol_config.alias,
        "timeframe": timeframe,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "strategy_count": len(specs),
        "source_file_count": len(existing_files),
        "calendar_days": calendar_days,
        "leaderboard": leaderboard,
    }
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        write_json(output_dir / "vol_prescreen_report.json", report)
        write_json(output_dir / "vol_strategy_leaderboard.json", leaderboard)
        write_json(output_dir / "vol_cost_stress_report.json", build_vol_cost_stress_report(leaderboard, symbol_config))
        write_json(output_dir / "vol_mutation_memory.json", build_vol_mutation_memory(leaderboard))
    return report


def build_vol_cost_stress_report(
    leaderboard_report: dict[str, Any],
    symbol_config: SymbolConfig,
    extra_round_trip_ticks: Sequence[int] = (0, 1, 2, 4),
) -> dict[str, Any]:
    rows = list(leaderboard_report.get("candidate_leaderboard", []))
    tick_value = symbol_config.tick_size * symbol_config.point_value
    stressed = []
    for row in rows:
        annual_trades = float(row.get("annual_trades_test") or 0.0)
        net_pnl = float(row.get("net_pnl_test") or 0.0)
        stress_rows = []
        for ticks in extra_round_trip_ticks:
            annual_cost_drag = annual_trades * ticks * tick_value
            stress_rows.append(
                {
                    "extra_round_trip_ticks": ticks,
                    "extra_cost_per_trade_usd": ticks * tick_value,
                    "annual_cost_drag_usd": annual_cost_drag,
                    "stressed_net_pnl_proxy": net_pnl - annual_cost_drag,
                    "survives_net_pnl_proxy": net_pnl - annual_cost_drag > 0,
                }
            )
        stressed.append(
            {
                "experiment_id": row.get("experiment_id"),
                "strategy_name": row.get("strategy_name"),
                "strategy_family": row.get("strategy_card", {}).get("strategy_family"),
                "annual_trades_test": annual_trades,
                "net_pnl_test": net_pnl,
                "sharpe_test": row.get("sharpe_test"),
                "stress": stress_rows,
            }
        )
    return {
        "schema_version": 1,
        "artifact": "vol_cost_stress_report",
        "symbol": symbol_config.alias,
        "tick_size": symbol_config.tick_size,
        "point_value": symbol_config.point_value,
        "tick_value_usd": tick_value,
        "assumption": "Proxy stress subtracts extra round-trip ticks from annualized test PnL; quote replay is required before promotion.",
        "candidate_count": len(rows),
        "stressed_candidates": stressed,
    }


def build_vol_quote_replay_report(
    *,
    quote_files: Sequence[Path],
    existing_reports: Sequence[Path] | None = None,
) -> dict[str, Any]:
    existing_quote_files = [str(path) for path in quote_files if path.exists()]
    replay_reports = [str(path) for path in (existing_reports or []) if path.exists()]
    report_summaries = [_quote_report_summary(Path(path)) for path in replay_reports]
    ready_reports = [summary for summary in report_summaries if summary.get("has_required_execution_models")]
    return {
        "schema_version": 1,
        "artifact": "vol_quote_replay_report",
        "status": "ready_for_review" if ready_reports else "blocked",
        "quote_files": existing_quote_files,
        "quote_replay_reports": replay_reports,
        "execution_report_summaries": report_summaries,
        "missing_requirements": [] if ready_reports else ["quote_replay_report_with_market_limit_and_adverse_selection"],
        "required_checks": [
            "market_order_bid_ask_replay",
            "spread_distribution",
            "limit_fill_rate",
            "missed_fill_opportunity_cost",
            "adverse_selection_1m_3m_5m_15m",
        ],
    }


def _quote_report_summary(path: Path) -> dict[str, Any]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {"path": str(path), "status": "unreadable", "error": str(error), "has_required_execution_models": False}
    models = report.get("execution_models") or {}
    required = {
        "market_order_bid_ask_replay",
        "fixed_conservative",
        "limit_missed_fill",
        "adverse_selection",
    }
    return {
        "path": str(path),
        "artifact": report.get("artifact"),
        "trade_count": report.get("trade_count"),
        "validated_trade_count": report.get("validated_trade_count"),
        "avg_bid_ask_cost_usd": report.get("avg_bid_ask_cost_usd"),
        "limit_fill_rate": (models.get("limit_missed_fill") or {}).get("fill_rate"),
        "avg_adverse_selection_ticks_5m": ((models.get("adverse_selection") or {}).get("5m") or {}).get("avg_ticks"),
        "has_required_execution_models": required.issubset(models),
    }


def build_vol_paper_shadow_review(paper_reports: Sequence[Path] | None = None) -> dict[str, Any]:
    reports = [str(path) for path in (paper_reports or []) if path.exists()]
    return {
        "schema_version": 1,
        "artifact": "vol_paper_shadow_review",
        "status": "ready_for_review" if reports else "blocked",
        "paper_reports": reports,
        "missing_requirements": [] if reports else ["paper_shadow_trade_log"],
        "required_fields": [
            "signal_id",
            "expected_edge",
            "actual_fill_mode",
            "spread_ticks",
            "adverse_selection_ticks_5m",
            "regime",
            "llm_diagnosis",
            "allowed_mutations",
            "blocked_mutations",
        ],
    }


def build_vol_mutation_memory(leaderboard_report: dict[str, Any]) -> dict[str, Any]:
    rows = list(leaderboard_report.get("candidate_leaderboard", [])) + [
        row for row in leaderboard_report.get("rejected", []) if row
    ]
    records = []
    for row in rows:
        records.append(
            {
                "experiment_id": row.get("experiment_id"),
                "strategy_name": row.get("strategy_name"),
                "strategy_spec_hash": row.get("strategy_spec_hash"),
                "allowed_mutations": _allowed_mutations(row),
                "blocked_mutations": [
                    "read_final_holdout_before_freeze",
                    "remove_cost_model",
                    "promote_without_quote_replay",
                    "promote_without_paper_shadow",
                ],
                "diagnostic_inputs": {
                    "reasons": row.get("reasons", []),
                    "next_round_suggestions": row.get("next_round_suggestions", []),
                    "cost_sensitivity_report": row.get("cost_sensitivity_report", {}),
                },
            }
        )
    return {
        "schema_version": 1,
        "artifact": "vol_mutation_memory",
        "record_count": len(records),
        "records": records,
    }


def write_vol_research_artifacts(
    output_dir: Path,
    *,
    experiments_root: Path,
    symbol_config: SymbolConfig,
    specs: Sequence[dict[str, Any]] | None = None,
    quote_files: Sequence[Path] = (),
    quote_reports: Sequence[Path] = (),
    paper_reports: Sequence[Path] = (),
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    feature_readiness = build_vol_feature_readiness()
    leaderboard = build_vol_strategy_leaderboard(experiments_root, specs=specs)
    artifacts = {
        "vol_feature_readiness.json": feature_readiness,
        "vol_strategy_leaderboard.json": leaderboard,
        "vol_cost_stress_report.json": build_vol_cost_stress_report(leaderboard, symbol_config),
        "vol_quote_replay_report.json": build_vol_quote_replay_report(
            quote_files=quote_files,
            existing_reports=quote_reports,
        ),
        "vol_paper_shadow_review.json": build_vol_paper_shadow_review(paper_reports),
        "vol_mutation_memory.json": build_vol_mutation_memory(leaderboard),
    }
    paths = {}
    for filename, payload in artifacts.items():
        path = output_dir / filename
        write_json(path, payload)
        paths[filename] = str(path)
    return paths


def _is_vol_row(row: dict[str, Any]) -> dict[str, Any] | None:
    family = row.get("strategy_card", {}).get("strategy_family") or row.get("strategy_family")
    name = str(row.get("strategy_name", ""))
    if family in VOL_EXECUTION_AWARE_FAMILIES or name.startswith("nq_vol_execution_"):
        return row
    return None


def _allowed_mutations(row: dict[str, Any]) -> list[str]:
    reasons = set(row.get("reasons", []))
    suggestions = {str(item).lower() for item in row.get("next_round_suggestions", [])}
    mutations = ["adjust_volume_threshold", "tighten_time_window", "stress_extra_slippage"]
    if "annual_trades_test" in reasons:
        mutations.append("relax_entry_frequency_without_adding_complex_features")
    if "avg_trade_net_pnl" in reasons or any("cost" in item for item in suggestions):
        mutations.append("raise_min_expected_edge")
    if "sharpe_test_aggregate" in reasons:
        mutations.append("add_regime_filter")
    return mutations


def _vol_family_attribution(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    families = sorted({str(row.get("strategy_family")) for row in rows})
    attribution = []
    for family in families:
        family_rows = [row for row in rows if row.get("strategy_family") == family]
        profitable = [row for row in family_rows if float(row.get("net_pnl_test") or 0.0) > 0]
        best = max(
            family_rows,
            key=lambda row: row.get("sharpe_test") if row.get("sharpe_test") is not None else -999,
        )
        attribution.append(
            {
                "strategy_family": family,
                "strategy_count": len(family_rows),
                "candidate_count": sum(1 for row in family_rows if row.get("passed")),
                "final_target_count": sum(1 for row in family_rows if row.get("final_target_passed")),
                "profitable_count": len(profitable),
                "best_strategy_name": best.get("strategy_name"),
                "best_sharpe_test": best.get("sharpe_test"),
                "best_net_pnl_test": best.get("net_pnl_test"),
                "best_win_probability_test": best.get("win_probability_test"),
                "median_annual_trades_test": _median(
                    [float(row.get("annual_trades_test") or 0.0) for row in family_rows]
                ),
            }
        )
    return attribution


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[middle]
    return (sorted_values[middle - 1] + sorted_values[middle]) / 2


def _run_vol_strategy_batch(
    specs: Sequence[StrategySpec],
    symbol_config: SymbolConfig,
    bars: Sequence[dict[str, Any]],
    cost_model: CostModelConfig,
) -> dict[str, list]:
    results = {spec.name: [] for spec in specs}
    grammar_runtimes = []
    runtime_by_signature: dict[str, dict[str, Any]] = {}
    for spec in specs:
        grammar = spec.raw.get("signal_grammar")
        if not isinstance(grammar, dict):
            results[spec.name] = run_bar_strategy(spec, symbol_config, bars, cost_model)
            continue
        entry = grammar.get("entry")
        if not isinstance(entry, dict):
            raise ValueError(f"{spec.name} signal_grammar.entry must be an object")
        trade_start, trade_end = parse_session_range(spec.session.trade)
        flatten_time = parse_clock(spec.session.flatten)
        signature = stable_hash(
            {
                "strategy_family": spec.strategy_family,
                "direction": spec.direction,
                "session": spec.session.__dict__,
                "risk": {
                    "position_sizing": spec.risk.get("position_sizing", {}),
                    "max_trades_per_day": spec.risk.get("max_trades_per_day", 999_999),
                },
                "signal_grammar": grammar,
                "exit": spec.exit,
            }
        )
        if signature in runtime_by_signature:
            runtime_by_signature[signature]["aliases"].append(spec.name)
            continue
        runtime = {
                "spec": spec,
                "aliases": [spec.name],
                "entry": entry,
                "filters": grammar.get("filters", {}),
                "exit": grammar.get("exit", {}),
                "contracts": int(spec.risk.get("position_sizing", {}).get("contracts", 1)),
                "max_trades_per_day": int(spec.risk.get("max_trades_per_day", 999_999)),
                "trade_start": trade_start,
                "trade_end": trade_end,
                "flatten_time": flatten_time,
            }
        runtime_by_signature[signature] = runtime
        grammar_runtimes.append(runtime)
    if not grammar_runtimes or not bars:
        return results

    by_day: dict[object, list[dict[str, Any]]] = {}
    for bar in bars:
        by_day.setdefault(bar["timestamp"].date(), []).append(bar)

    grouped_runtimes: dict[tuple, list[dict[str, Any]]] = {}
    for runtime in grammar_runtimes:
        key = (runtime["trade_start"], runtime["trade_end"], runtime["flatten_time"])
        grouped_runtimes.setdefault(key, []).append(runtime)

    for _, day_bars in sorted(by_day.items(), key=lambda item: item[0]):
        for (trade_start, _trade_end, flatten_time), runtimes in grouped_runtimes.items():
            session_bars = [
                bar
                for bar in day_bars
                if trade_start <= bar["timestamp"].time() <= flatten_time
            ]
            if not session_bars:
                continue
            positions = {runtime["spec"].name: None for runtime in runtimes}
            trades_today = {runtime["spec"].name: 0 for runtime in runtimes}
            for session_index, bar in enumerate(session_bars):
                bar_time = bar["timestamp"].time()
                for runtime in runtimes:
                    spec = runtime["spec"]
                    strategy_name = spec.name
                    position = positions[strategy_name]
                    if bar_time > runtime["trade_end"] and position is None:
                        continue
                    if position is None and trades_today[strategy_name] < runtime["max_trades_per_day"]:
                        if _grammar_pass(runtime["filters"], bar):
                            for side in ("long", "short"):
                                if side == "long" and spec.direction not in {"long", "long_short"}:
                                    continue
                                if side == "short" and spec.direction not in {"short", "long_short"}:
                                    continue
                                side_rule = runtime["entry"].get(side)
                                if _grammar_pass(side_rule, bar):
                                    _filter_passed, filter_audit = _evaluate_grammar_node(runtime["filters"], bar)
                                    _entry_passed, predicate_audit = _evaluate_grammar_node(side_rule, bar)
                                    position = _open_position(
                                        side,
                                        bar,
                                        runtime["contracts"],
                                        session_index,
                                        f"signal_grammar_{side}",
                                        feature_values=_selected_feature_values(bar, predicate_audit + filter_audit),
                                        predicate_evaluation=predicate_audit + filter_audit,
                                    )
                                    position.update(_grammar_exit_points(runtime["exit"], bar, spec))
                                    positions[strategy_name] = position
                                    trades_today[strategy_name] += 1
                                    break
                        if positions[strategy_name] is not None:
                            continue

                    position = positions[strategy_name]
                    if position is not None:
                        exit_reason, exit_price = _vol_position_exit(position, bar, session_index, flatten_time)
                        if exit_reason and exit_price is not None:
                            results[strategy_name].append(
                                _close_position(position, bar, exit_price, exit_reason, cost_model)
                            )
                            positions[strategy_name] = None

            last_bar = session_bars[-1]
            for runtime in runtimes:
                strategy_name = runtime["spec"].name
                position = positions[strategy_name]
                if position is None:
                    continue
                exit_price = last_bar["bid_close"] if position["side"] == "long" else last_bar["ask_close"]
                results[strategy_name].append(
                    _close_position(position, last_bar, exit_price, "end_of_data", cost_model)
                )
    for runtime in grammar_runtimes:
        primary_name = runtime["spec"].name
        for alias in runtime["aliases"][1:]:
            results[alias] = list(results[primary_name])
    return results


def _grammar_pass(node, bar: dict[str, Any]) -> bool:
    if node in (None, {}):
        return True
    if not isinstance(node, dict):
        raise ValueError("signal_grammar nodes must be objects")
    if "all" in node:
        return all(_grammar_pass(child, bar) for child in node["all"])
    if "any" in node:
        return any(_grammar_pass(child, bar) for child in node["any"])
    if "not" in node:
        return not _grammar_pass(node["not"], bar)
    return _predicate_pass(node, bar)


def _predicate_pass(predicate: dict[str, Any], bar: dict[str, Any]) -> bool:
    left_name = str(predicate.get("feature", predicate.get("left", "")))
    operator = str(predicate.get("op", "=="))
    right_value = predicate.get("value", predicate.get("right"))
    left_value = feature_value(bar, left_name)
    resolved_right = (
        feature_value(bar, str(right_value))
        if isinstance(right_value, str) and _vol_looks_like_feature(right_value)
        else right_value
    )
    if left_value is None or resolved_right is None:
        return False
    left_value = _vol_coerce_scalar(left_value)
    resolved_right = _vol_coerce_scalar(resolved_right)
    if operator == ">":
        return left_value > resolved_right
    if operator == ">=":
        return left_value >= resolved_right
    if operator == "<":
        return left_value < resolved_right
    if operator == "<=":
        return left_value <= resolved_right
    if operator == "==":
        return left_value == resolved_right
    if operator == "!=":
        return left_value != resolved_right
    raise ValueError(f"Unsupported signal_grammar operator: {operator}")


def _vol_looks_like_feature(value: str) -> bool:
    if value.lower() in {"true", "false"}:
        return False
    try:
        float(value)
        return False
    except ValueError:
        return True


def _vol_coerce_scalar(value):
    if isinstance(value, str):
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        try:
            return float(value)
        except ValueError:
            return value
    return value


def _vol_position_exit(position: dict, bar: dict[str, Any], session_index: int, flatten_time) -> tuple[str | None, float | None]:
    holding_minutes = session_index - position["entry_index"]
    stop_points = float(position["stop_points"])
    take_profit_points = float(position["take_profit_points"])
    max_holding_minutes = int(position["max_holding_minutes"])
    if position["side"] == "long":
        stop_price = position["entry_price"] - stop_points
        take_price = position["entry_price"] + take_profit_points
        if bar["low"] <= stop_price:
            return "stop_loss", stop_price
        if bar["high"] >= take_price:
            return "take_profit", take_price
        if holding_minutes >= max_holding_minutes:
            return "max_holding", bar["bid_close"]
        if bar["timestamp"].time() >= flatten_time:
            return "session_flatten", bar["bid_close"]
    else:
        stop_price = position["entry_price"] + stop_points
        take_price = position["entry_price"] - take_profit_points
        if bar["high"] >= stop_price:
            return "stop_loss", stop_price
        if bar["low"] <= take_price:
            return "take_profit", take_price
        if holding_minutes >= max_holding_minutes:
            return "max_holding", bar["ask_close"]
        if bar["timestamp"].time() >= flatten_time:
            return "session_flatten", bar["ask_close"]
    return None, None


def _metrics_for_trades(trades, starting_equity: float, calendar_days: int):
    trade_pnls = [trade.net_pnl for trade in trades]
    equity = [starting_equity]
    for pnl in trade_pnls:
        equity.append(equity[-1] + pnl)
    return calculate_metrics(trade_pnls, equity, starting_equity, calendar_days)


def _feature_year_summary_hash(year: int, feature_bars: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not feature_bars:
        return {"year": year, "bar_count": 0, "feature_snapshot_hash": stable_hash({"year": year, "bar_count": 0})}
    sample_indexes = sorted({0, len(feature_bars) // 2, len(feature_bars) - 1})
    sample = []
    for index in sample_indexes:
        bar = feature_bars[index]
        features = bar.get("features") or {}
        sample.append(
            {
                "timestamp": bar["timestamp"].isoformat(),
                "bar_volume": features.get("bar_volume"),
                "relative_volume_20": features.get("relative_volume_20"),
                "volume_spike_flag": features.get("volume_spike_flag"),
                "low_volume_filter": features.get("low_volume_filter"),
            }
        )
    payload = {
        "year": year,
        "bar_count": len(feature_bars),
        "first_timestamp": feature_bars[0]["timestamp"].isoformat(),
        "last_timestamp": feature_bars[-1]["timestamp"].isoformat(),
        "sample": sample,
    }
    return {"year": year, "bar_count": len(feature_bars), "feature_snapshot_hash": stable_hash(payload)}


def _win_probability(trades) -> float | None:
    if not trades:
        return None
    return sum(1 for trade in trades if trade.net_pnl > 0) / len(trades)


def _vol_prescreen_gate(metrics: dict[str, Any], win_probability: float | None) -> dict[str, Any]:
    reasons = []
    annual_trades = float(metrics.get("annual_trades") or 0.0)
    sharpe = metrics.get("sharpe")
    profit_factor = metrics.get("profit_factor")
    if annual_trades < VOL_STRONG_CANDIDATE_TARGET["min_annual_trades"]:
        reasons.append("annual_trades_below_1000")
    if sharpe is None or sharpe < VOL_STRONG_CANDIDATE_TARGET["min_sharpe"]:
        reasons.append("sharpe_below_1")
    if (win_probability is None or win_probability < VOL_STRONG_CANDIDATE_TARGET["min_win_probability"]) and (
        profit_factor is None or profit_factor < VOL_STRONG_CANDIDATE_TARGET["min_profit_factor"]
    ):
        reasons.append("win_probability_or_profit_factor")
    if metrics.get("net_pnl", 0.0) <= 0:
        reasons.append("net_pnl_not_positive")
    final_reasons = list(reasons)
    if sharpe is None or sharpe <= VOL_FINAL_TARGET["min_sharpe"]:
        final_reasons.append("final_sharpe_below_2")
    if win_probability is None or win_probability <= VOL_FINAL_TARGET["min_win_probability"]:
        final_reasons.append("final_win_probability_below_53pct")
    return {
        "strong_candidate": not reasons,
        "final_target": not final_reasons,
        "reasons": final_reasons,
    }


def _vol_next_round_suggestions(reasons: Sequence[str]) -> list[str]:
    suggestions = []
    if "annual_trades_below_1000" in reasons:
        suggestions.append("Relax the most restrictive volume or time filter while keeping spread and low-volume gates.")
    if "sharpe_below_1" in reasons or "final_sharpe_below_2" in reasons:
        suggestions.append("Segment by session window and volatility regime before widening the strategy family.")
    if "win_probability_or_profit_factor" in reasons or "final_win_probability_below_53pct" in reasons:
        suggestions.append("Stress test inverse direction and tighten stop/take-profit asymmetry.")
    if "net_pnl_not_positive" in reasons:
        suggestions.append("Reject or invert the hypothesis unless cost stress shows a clear execution-mode dependency.")
    return suggestions or ["Promote to quote replay before any paper shadow decision."]


def _vol_strategy_card(spec: StrategySpec) -> dict[str, Any]:
    regime = "trend" if spec.strategy_family in {"vol_breakout_trend", "ma_pullback_volume_confirm", "macd_ma_volume_confirm"} else "mean_reversion"
    return {
        "name": spec.name,
        "strategy_family": spec.strategy_family,
        "symbol": spec.symbol,
        "timeframe": spec.timeframe,
        "regime": regime,
        "market_hypothesis": spec.market_hypothesis,
        "event_window_policy": spec.raw.get("generation", {}).get("event_window_policy"),
        "execution_assumption": spec.raw.get("generation", {}).get("execution_assumption"),
        "core_feature_count": len(spec.raw.get("feature_set", [])),
        "parameter_keys": sorted((spec.raw.get("parameters") or {}).keys()),
    }
