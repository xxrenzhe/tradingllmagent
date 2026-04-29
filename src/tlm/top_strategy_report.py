from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

import duckdb

from .bars import timeframe_minutes
from .profit_mining import (
    _cost_stress_metrics,
    _duckdb_glob,
    _fetch_dicts,
    _one_trade_per_timestamp,
    _period_replay_metrics,
    _replay_regime_edges_for_horizon,
    _signal_metrics,
    _signal_timestamp,
    _signals_covered_days,
    _yearly_signal_results,
)
from .storage import write_json
from .variants import stable_hash


def generate_top_strategy_html_report(
    *,
    mining_report_path: Path | None = None,
    mining_report_paths: Sequence[Path] | None = None,
    data_root: Path,
    output_html: Path,
    top_n: int = 3,
    sample_trade_count: int = 3,
    objective: str = "annualized_quality",
    comparison_objectives: Sequence[str] | None = None,
) -> dict[str, Any]:
    report_paths = [Path(path) for path in (mining_report_paths or ([] if mining_report_path is None else [mining_report_path]))]
    if not report_paths:
        raise ValueError("At least one mining report path is required")
    mining_reports = [
        {
            **json.loads(path.read_text(encoding="utf-8")),
            "_source_report_path": str(path),
        }
        for path in report_paths
    ]
    capital_base_usd = float(next((report.get("starting_equity") for report in mining_reports if report.get("starting_equity") is not None), 100_000.0))
    objective_order = _objective_order(objective, comparison_objectives)
    round_trip_cost_usd = max(float(report.get("round_trip_cost_usd") or 0.0) for report in mining_reports)

    con = duckdb.connect(":memory:")
    try:
        strategy_sets = []
        for objective_index, objective_name in enumerate(objective_order, start=1):
            strategies = _select_top_yearly_strategies(mining_reports, top_n=top_n, objective=objective_name)
            enriched = []
            for strategy_index, strategy in enumerate(strategies, start=1):
                source_report = mining_reports[int(strategy["source_report_index"])]
                context = _replay_context(source_report, data_root)
                strategy_round_trip_cost_usd = float(source_report.get("round_trip_cost_usd") or 0.0)
                bar_minutes = int(source_report.get("timeframe_minutes") or timeframe_minutes(str(source_report["timeframe"])))
                signals = _replay_strategy_signals(con, context, strategy, strategy_round_trip_cost_usd)
                samples = _sample_trades(signals, limit=sample_trade_count)
                enriched.append(
                    _enrich_strategy(
                        con=con,
                        context=context,
                        strategy={**strategy, "display_id": f"O{objective_index}-S{strategy_index}"},
                        signals=signals,
                        sample_trades=samples,
                        bar_minutes=bar_minutes,
                        capital_base_usd=capital_base_usd,
                    )
                )
            strategy_sets.append(
                {
                    "objective": objective_name,
                    "selection_policy": _selection_policy(objective_name),
                    "strategy_count": len(enriched),
                    "strategies": enriched,
                    "total_net_pnl": sum(float(item["replayed_metrics"].get("net_pnl") or 0.0) for item in enriched),
                    "total_excess_net_pnl": sum(float(item["excess_summary"].get("excess_net_pnl") or 0.0) for item in enriched),
                }
            )
    finally:
        con.close()

    source_reports = [_report_summary(report) for report in mining_reports]
    symbols = sorted({str(report.get("symbol")) for report in mining_reports})
    timeframes = sorted({str(report.get("timeframe")) for report in mining_reports}, key=timeframe_minutes)
    date_from = min(str(report.get("date_from")) for report in mining_reports)
    date_to = max(str(report.get("date_to")) for report in mining_reports)

    payload = {
        "artifact": "top_strategy_html_report",
        "source_report": str(report_paths[0]) if len(report_paths) == 1 else None,
        "source_reports": source_reports,
        "symbol": symbols[0] if len(symbols) == 1 else symbols,
        "timeframe": timeframes[0] if len(timeframes) == 1 else ", ".join(timeframes),
        "date_from": date_from,
        "date_to": date_to,
        "round_trip_cost_usd": round_trip_cost_usd,
        "capital_base_usd": capital_base_usd,
        "selection_objective": objective,
        "selection_policy": _selection_policy(objective),
        "strategy_count": len(strategy_sets[0]["strategies"]) if strategy_sets else 0,
        "strategies": strategy_sets[0]["strategies"] if strategy_sets else [],
        "strategy_sets": strategy_sets,
    }
    payload["report_hash"] = stable_hash(_json_ready(payload))

    output_html.parent.mkdir(parents=True, exist_ok=True)
    data_path = output_html.with_suffix(".data.json")
    write_json(data_path, _json_ready(_export_payload(payload)))
    output_html.write_text(_render_html(payload, data_path.name), encoding="utf-8")
    return {
        "html": str(output_html),
        "data": str(data_path),
        "selection_objective": objective,
        "strategy_count": len(strategy_sets[0]["strategies"]) if strategy_sets else 0,
        "strategy_set_count": len(strategy_sets),
        "report_hash": payload["report_hash"],
    }


def generate_top_strategy_comparison_html(
    *,
    report_data_paths: Sequence[Path],
    output_html: Path,
) -> dict[str, Any]:
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in report_data_paths]
    strategy_sets = []
    source_reports_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    symbols = set()
    timeframes = set()
    date_from_values = []
    date_to_values = []
    round_trip_cost_usd = 0.0
    capital_base_usd = 100_000.0
    for report in reports:
        strategies = report.get("strategies", [])
        strategy_sets.append(
            {
                "objective": report.get("selection_objective"),
                "selection_policy": report.get("selection_policy"),
                "strategy_count": len(strategies),
                "strategies": strategies,
                "total_net_pnl": sum(float(strategy["replayed_metrics"].get("net_pnl") or 0.0) for strategy in strategies),
                "total_excess_net_pnl": sum(float(strategy["excess_summary"].get("excess_net_pnl") or 0.0) for strategy in strategies),
            }
        )
        for source_report in report.get("source_reports") or []:
            key = (str(source_report.get("path")), str(source_report.get("timeframe")))
            source_reports_by_key[key] = source_report
        symbols.add(str(report.get("symbol")))
        for timeframe in str(report.get("timeframe") or "").split(", "):
            if timeframe:
                timeframes.add(timeframe)
        if report.get("date_from"):
            date_from_values.append(str(report.get("date_from")))
        if report.get("date_to"):
            date_to_values.append(str(report.get("date_to")))
        round_trip_cost_usd = max(round_trip_cost_usd, float(report.get("round_trip_cost_usd") or 0.0))
        capital_base_usd = float(report.get("capital_base_usd") or capital_base_usd)
    primary = strategy_sets[0] if strategy_sets else {"objective": "annualized_quality", "selection_policy": _selection_policy("annualized_quality"), "strategies": []}
    payload = {
        "artifact": "top_strategy_html_report",
        "source_report": None,
        "source_reports": list(source_reports_by_key.values()),
        "symbol": sorted(symbols)[0] if len(symbols) == 1 else sorted(symbols),
        "timeframe": ", ".join(sorted(timeframes, key=timeframe_minutes)),
        "date_from": min(date_from_values) if date_from_values else None,
        "date_to": max(date_to_values) if date_to_values else None,
        "round_trip_cost_usd": round_trip_cost_usd,
        "capital_base_usd": capital_base_usd,
        "selection_objective": primary.get("objective"),
        "selection_policy": primary.get("selection_policy"),
        "strategy_count": len(primary.get("strategies") or []),
        "strategies": primary.get("strategies") or [],
        "strategy_sets": strategy_sets,
    }
    payload["report_hash"] = stable_hash(_json_ready(payload))
    output_html.parent.mkdir(parents=True, exist_ok=True)
    data_path = output_html.with_suffix(".data.json")
    write_json(data_path, _json_ready(_export_payload(payload)))
    output_html.write_text(_render_html(payload, data_path.name), encoding="utf-8")
    return {
        "html": str(output_html),
        "data": str(data_path),
        "report_count": len(strategy_sets),
        "report_hash": payload["report_hash"],
    }


def _objective_order(primary_objective: str, comparison_objectives: Sequence[str] | None) -> list[str]:
    preferred = [primary_objective]
    if comparison_objectives:
        preferred.extend(str(objective) for objective in comparison_objectives)
    else:
        preferred.extend(["net_pnl", "stability_first"])
    ordered = []
    seen = set()
    for objective in preferred:
        if objective in seen:
            continue
        seen.add(objective)
        ordered.append(objective)
    return ordered


def _select_top_yearly_strategies(
    report: dict[str, Any] | Sequence[dict[str, Any]],
    top_n: int,
    objective: str = "annualized_quality",
) -> list[dict[str, Any]]:
    candidates = []
    reports = [report] if isinstance(report, dict) else list(report)
    for report_index, report_item in enumerate(reports):
        for replay in report_item.get("regime_basket_replays", []):
            for candidate in replay.get("yearly_profitable_candidates", []):
                enriched_candidate = {
                    **candidate,
                    "basket_id": replay.get("basket_id"),
                    "basket_hash": replay.get("basket_hash"),
                    "source_report_index": report_index,
                    "source_report_path": report_item.get("_source_report_path"),
                    "source_symbol": report_item.get("symbol"),
                    "source_timeframe": report_item.get("timeframe"),
                    "source_date_from": report_item.get("date_from"),
                    "source_date_to": report_item.get("date_to"),
                }
                enriched_candidate["evaluation_summary"] = _evaluation_summary(enriched_candidate)
                candidates.append(enriched_candidate)
    candidates.sort(key=lambda row: _selection_key(row, objective), reverse=True)
    selected = []
    seen = set()
    for candidate in candidates:
        signature = stable_hash(
            {
                "selection_rule": candidate.get("selection_rule"),
                "constituent_edges": candidate.get("constituent_edges"),
            }
        )
        if signature in seen:
            continue
        seen.add(signature)
        selected.append({**candidate, "strategy_signature": signature})
        if len(selected) >= top_n:
            break
    return selected


def _selection_key(candidate: dict[str, Any], objective: str) -> tuple[float, ...]:
    full = candidate.get("full_after_activation") or {}
    test = candidate.get("test") or {}
    train_period = candidate.get("train_period") or {}
    test_period = candidate.get("test_period") or {}
    full_pf = float(full.get("profit_factor") or 0.0)
    test_pf = float(test.get("profit_factor") or 0.0)
    full_net = float(full.get("net_pnl") or 0.0)
    test_net = float(test.get("net_pnl") or 0.0)
    annual_trades = float(full.get("annual_trades") or 0.0)
    covered_days = max(1.0, float(train_period.get("covered_days") or 0.0) + float(test_period.get("covered_days") or 0.0))
    annualized_net = full_net / covered_days * 365.0
    evaluation = candidate.get("evaluation_summary") or _evaluation_summary(candidate)
    gate_pass_count = float(evaluation.get("passed_gate_count") or 0.0)
    fully_qualified = float(evaluation.get("fully_qualified") or 0.0)
    positive_year_ratio = float(evaluation.get("positive_year_ratio") or 0.0)
    return_to_drawdown = float(full.get("return_to_drawdown") or 0.0)
    max_drawdown = float(full.get("max_drawdown") or 0.0)
    min_pf = min(full_pf, test_pf) if full_pf and test_pf else max(full_pf, test_pf)
    if objective == "profit_factor":
        return (full_pf, test_pf, full_net, annual_trades)
    if objective == "test_profit_factor":
        return (test_pf, full_pf, test_net, annual_trades)
    if objective == "balanced":
        return (annualized_net, min(full_pf, test_pf), full_net, annual_trades)
    if objective == "stability_first":
        return (fully_qualified, positive_year_ratio, return_to_drawdown, min_pf, test_net, annualized_net, -max_drawdown, annual_trades, full_net)
    if objective == "annualized_quality":
        return (fully_qualified, annualized_net, gate_pass_count, positive_year_ratio, min_pf, test_net, return_to_drawdown, annual_trades, full_net)
    if objective == "net_pnl":
        return (full_net, test_net, annual_trades, full_pf)
    if objective == "annualized_net_pnl":
        return (annualized_net, full_net, test_net, full_pf)
    return (full_net, test_net, annual_trades, full_pf)


def _selection_policy(objective: str) -> str:
    policies = {
        "net_pnl": "top yearly-profitable strategies by net PnL, de-duplicated by edge composition",
        "annualized_net_pnl": "top yearly-profitable strategies by annualized net PnL per 1 contract, de-duplicated by edge composition",
        "annualized_quality": "top yearly-profitable strategies by hard-gate qualification first, then annualized net PnL with stability/cost-quality tiebreakers, de-duplicated by edge composition",
        "stability_first": "top yearly-profitable strategies by hard-gate qualification first, then positive-year ratio, return-to-drawdown, PF, and test-period resilience, de-duplicated by edge composition",
        "profit_factor": "top yearly-profitable strategies by full-period profit factor, de-duplicated by edge composition",
        "test_profit_factor": "top yearly-profitable strategies by recent test-period profit factor, de-duplicated by edge composition",
        "balanced": "top yearly-profitable strategies by annualized net PnL, then the weaker of full-period and test-period profit factor",
    }
    return policies.get(objective, policies["annualized_quality"])


def _objective_label(objective: str) -> str:
    labels = {
        "annualized_quality": "年化质量优先",
        "annualized_net_pnl": "年化净收益优先",
        "net_pnl": "总净收益优先",
        "stability_first": "稳定性优先",
        "profit_factor": "PF 优先",
        "test_profit_factor": "测试期 PF 优先",
        "balanced": "平衡优先",
    }
    return labels.get(objective, objective)


def _replay_context(report: dict[str, Any], data_root: Path) -> dict[str, Any]:
    cost_model = report.get("cost_model") or {}
    return {
        "parquet_glob": _duckdb_glob(data_root, str(report["symbol"]), str(report["timeframe"])),
        "date_from": str(report["date_from"]),
        "date_to_exclusive": (date.fromisoformat(str(report["date_to"])) + timedelta(days=1)).isoformat(),
        "point_value": float(cost_model.get("point_value") or 20.0),
        "timeframe_minutes": int(report.get("timeframe_minutes") or timeframe_minutes(str(report["timeframe"]))),
        "continuity_tolerance_minutes": max(2, int(report.get("timeframe_minutes") or 1) * 2),
        "min_annual_trades": float((report.get("target") or {}).get("min_annual_trades") or 1000),
        "min_win_probability": float((report.get("target") or {}).get("min_win_probability") or 0.53),
        "cost_stress_usd_per_trade": [
            {"label": "configured_cost", "additional_round_trip_ticks": 0, "extra_usd_per_trade": 0.0},
            {
                "label": "configured_cost_plus_1_tick",
                "additional_round_trip_ticks": 1,
                "extra_usd_per_trade": float(cost_model.get("tick_value") or 5.0),
            },
            {
                "label": "configured_cost_plus_2_ticks",
                "additional_round_trip_ticks": 2,
                "extra_usd_per_trade": float(cost_model.get("tick_value") or 5.0) * 2.0,
            },
        ],
    }


def _report_summary(report: dict[str, Any]) -> dict[str, Any]:
    candidate_count = sum(len(replay.get("yearly_profitable_candidates", [])) for replay in report.get("regime_basket_replays", []))
    return {
        "path": report.get("_source_report_path"),
        "symbol": report.get("symbol"),
        "timeframe": report.get("timeframe"),
        "date_from": report.get("date_from"),
        "date_to": report.get("date_to"),
        "round_trip_cost_usd": float(report.get("round_trip_cost_usd") or 0.0),
        "yearly_profitable_candidate_count": candidate_count,
    }


def _evaluation_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    full = candidate.get("full_after_activation") or {}
    test = candidate.get("test") or {}
    full_net = float(full.get("net_pnl") or 0.0)
    test_net = float(test.get("net_pnl") or 0.0)
    annual_trades = float(full.get("annual_trades") or 0.0)
    full_pf = float(full.get("profit_factor") or 0.0)
    full_win = float(full.get("win_probability") or 0.0)
    return_to_drawdown = float(full.get("return_to_drawdown") or 0.0)
    positive_year_ratio = _positive_year_ratio(full.get("yearly_results") or [])
    plus_two_tick_net = _stress_metric(full, "configured_cost_plus_2_ticks", "net_pnl")
    gates = [
        ("full_net_positive", full_net > 0.0),
        ("test_net_positive", test_net > 0.0),
        ("annual_trades_ge_1000", annual_trades >= 1000.0),
        ("win_probability_ge_53bp", full_win >= 0.53),
        ("profit_factor_ge_1_15", full_pf >= 1.15),
        ("cost_plus_2_ticks_positive", plus_two_tick_net > 0.0),
        ("positive_year_ratio_ge_75pct", positive_year_ratio >= 0.75),
        ("return_to_drawdown_gt_1", return_to_drawdown > 1.0),
    ]
    return {
        "passed_gates": [name for name, passed in gates if passed],
        "failed_gates": [name for name, passed in gates if not passed],
        "passed_gate_count": sum(1 for _, passed in gates if passed),
        "total_gate_count": len(gates),
        "positive_year_ratio": positive_year_ratio,
        "fully_qualified": all(passed for _, passed in gates),
    }


def _positive_year_ratio(rows: Sequence[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    positive_years = sum(1 for row in rows if float(row.get("net_pnl") or 0.0) > 0.0)
    return positive_years / len(rows)


def _stress_metric(metrics: dict[str, Any], label: str, field: str) -> float:
    for row in metrics.get("cost_stress") or []:
        if str(row.get("label")) == label:
            return float(row.get(field) or 0.0)
    return 0.0


def _replay_strategy_signals(
    con: duckdb.DuckDBPyConnection,
    context: dict[str, Any],
    strategy: dict[str, Any],
    round_trip_cost_usd: float,
) -> list[dict[str, Any]]:
    edges = strategy.get("constituent_edges") or []
    signals = []
    for horizon_minutes in sorted({int(edge["horizon_minutes"]) for edge in edges}):
        indexed_edges = [
            (index, edge)
            for index, edge in enumerate(edges)
            if int(edge["horizon_minutes"]) == horizon_minutes
        ]
        signals.extend(
            _replay_regime_edges_for_horizon(
                con,
                context,
                horizon_minutes=horizon_minutes,
                indexed_edges=indexed_edges,
                round_trip_cost_usd=round_trip_cost_usd,
            )
        )
    activation_year = int(strategy["activation_start_year"])
    active = [
        signal
        for signal in signals
        if _signal_timestamp(signal).year >= activation_year
    ]
    return sorted(_one_trade_per_timestamp(active), key=lambda signal: signal["timestamp"])


def _enrich_strategy(
    *,
    con: duckdb.DuckDBPyConnection,
    context: dict[str, Any],
    strategy: dict[str, Any],
    signals: Sequence[dict[str, Any]],
    sample_trades: Sequence[dict[str, Any]],
    bar_minutes: int,
    capital_base_usd: float,
) -> dict[str, Any]:
    day_count = _signals_covered_days(signals)
    replay_metrics = _period_replay_metrics(signals, context)
    replay_metrics["annualized_net_pnl"] = float(replay_metrics.get("net_pnl") or 0.0) / max(1, day_count) * 365.0
    equity_curve = _equity_curve(signals)
    benchmark = _benchmark_bundle(con, context, signals, capital_base_usd=capital_base_usd)
    notional_base_usd = float((benchmark.get("metrics") or {}).get("notional_base_usd") or 1.0)
    replay_metrics = _with_return_metrics(replay_metrics, capital_base_usd, notional_base_usd=notional_base_usd)
    monthly = [_with_return_metrics(row, capital_base_usd, notional_base_usd=notional_base_usd) for row in _monthly_signal_results(signals)]
    excess_summary = _excess_summary(replay_metrics, benchmark["metrics"])
    annual_results = [_with_return_metrics(row, capital_base_usd, notional_base_usd=notional_base_usd) for row in _yearly_signal_results(signals)]
    annual_excess = _merge_period_results(annual_results, benchmark["annual_results"], "year")
    monthly_excess = _merge_period_results(monthly, benchmark["monthly_results"], "period")
    samples = []
    for sample in sample_trades:
        bars = _load_trade_window_bars(con, context, sample, bar_minutes=bar_minutes)
        samples.append(
            {
                "label": sample["sample_label"],
                "trade": _trade_marker(sample),
                "bars": bars,
                "chart_svg": _candlestick_svg(bars, sample),
            }
        )
    return {
        **strategy,
        "replayed_metrics": replay_metrics,
        "annual_results": annual_results,
        "monthly_results": monthly,
        "equity_curve": equity_curve,
        "equity_curve_sampled": _sample_series(equity_curve, max_points=1200),
        "benchmark": benchmark,
        "excess_summary": excess_summary,
        "annual_excess_results": annual_excess,
        "monthly_excess_results": monthly_excess,
        "cost_stress": [_with_return_metrics(row, capital_base_usd, notional_base_usd=notional_base_usd) for row in _cost_stress_metrics(signals, day_count, context)],
        "trade_markers": [_trade_marker(signal) for signal in signals],
        "sample_trade_charts": samples,
        "signal_count": len(signals),
    }


def _monthly_signal_results(signals: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_month: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        timestamp = _signal_timestamp(signal)
        by_month[(timestamp.year, timestamp.month)].append(signal)
    rows = []
    for (year, month), month_signals in sorted(by_month.items()):
        ordered = sorted(month_signals, key=lambda signal: signal["timestamp"])
        first_day = _signal_timestamp(ordered[0]).date()
        last_day = _signal_timestamp(ordered[-1]).date()
        metrics = _signal_metrics(ordered, max(1, (last_day - first_day).days + 1))
        rows.append(
            {
                "year": year,
                "month": month,
                "period": f"{year}-{month:02d}",
                "period_from": first_day.isoformat(),
                "period_to": last_day.isoformat(),
                **metrics,
            }
        )
    return rows


def _equity_curve(signals: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    equity = 0.0
    rows = []
    for index, signal in enumerate(sorted(signals, key=lambda item: item["timestamp"]), start=1):
        equity += float(signal["pnl"])
        rows.append(
            {
                "trade": index,
                "timestamp": _signal_timestamp(signal).isoformat(sep=" "),
                "equity": equity,
                "pnl": float(signal["pnl"]),
            }
        )
    return rows


def _benchmark_bundle(
    con: duckdb.DuckDBPyConnection,
    context: dict[str, Any],
    signals: Sequence[dict[str, Any]],
    *,
    capital_base_usd: float,
) -> dict[str, Any]:
    if not signals:
        return {
            "metrics": {"net_pnl": 0.0, "annual_trades": 0.0},
            "equity_curve": [],
            "equity_curve_sampled": [],
            "annual_results": [],
            "monthly_results": [],
        }
    start_ts = _signal_timestamp(signals[0])
    end_ts = _signal_timestamp(signals[-1])
    bars = _fetch_dicts(
        con,
        f"""
        SELECT timestamp, close
        FROM read_parquet('{context["parquet_glob"]}')
        WHERE timestamp >= '{start_ts.isoformat(sep=" ")}'
          AND timestamp <= '{end_ts.isoformat(sep=" ")}'
        ORDER BY timestamp
        """,
    )
    if not bars:
        return {
            "metrics": {"net_pnl": 0.0, "annual_trades": 0.0},
            "equity_curve": [],
            "equity_curve_sampled": [],
            "annual_results": [],
            "monthly_results": [],
        }
    start_close = float(bars[0]["close"])
    point_value = float(context["point_value"])
    curve = []
    for index, bar in enumerate(bars, start=1):
        pnl = (float(bar["close"]) - start_close) * point_value
        curve.append(
            {
                "trade": index,
                "timestamp": _to_iso(bar["timestamp"]),
                "close": float(bar["close"]),
                "equity": pnl,
                "pnl": pnl,
            }
        )
    metrics = _benchmark_metrics(curve)
    notional_base_usd = start_close * point_value
    metrics = _with_return_metrics(metrics, capital_base_usd, notional_base_usd=notional_base_usd)
    return {
        "metrics": metrics,
        "equity_curve": curve,
        "equity_curve_sampled": _sample_series(curve, max_points=1200),
        "annual_results": [_with_return_metrics(row, capital_base_usd, notional_base_usd=notional_base_usd) for row in _benchmark_period_results(curve, "year", point_value)],
        "monthly_results": [_with_return_metrics(row, capital_base_usd, notional_base_usd=notional_base_usd) for row in _benchmark_period_results(curve, "month", point_value)],
    }


def _benchmark_metrics(curve: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not curve:
        return {"trades": 0, "annual_trades": 0.0, "net_pnl": 0.0, "win_probability": None, "avg_pnl": None, "profit_factor": None, "max_drawdown": 0.0, "return_to_drawdown": None}
    net_pnl = float(curve[-1]["equity"])
    peak = float("-inf")
    max_drawdown = 0.0
    for row in curve:
        equity = float(row["equity"])
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    first_ts = datetime.fromisoformat(str(curve[0]["timestamp"]))
    last_ts = datetime.fromisoformat(str(curve[-1]["timestamp"]))
    covered_days = max(1, (last_ts.date() - first_ts.date()).days + 1)
    return {
        "trades": len(curve),
        "annual_trades": len(curve) / covered_days * 365,
        "net_pnl": net_pnl,
        "annualized_net_pnl": net_pnl / covered_days * 365,
        "win_probability": None,
        "avg_pnl": net_pnl / len(curve),
        "profit_factor": None,
        "max_drawdown": max_drawdown,
        "return_to_drawdown": net_pnl / max_drawdown if max_drawdown else None,
    }


def _benchmark_period_results(curve: Sequence[dict[str, Any]], mode: str, point_value: float) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, int] | int, list[dict[str, Any]]] = defaultdict(list)
    for row in curve:
        ts = datetime.fromisoformat(str(row["timestamp"]))
        key = ts.year if mode == "year" else (ts.year, ts.month)
        grouped[key].append(row)
    results = []
    previous_period_end_equity = 0.0
    for key, rows in sorted(grouped.items()):
        first = rows[0]
        last = rows[-1]
        first_ts = datetime.fromisoformat(str(first["timestamp"]))
        last_ts = datetime.fromisoformat(str(last["timestamp"]))
        period_end_equity = float(last["equity"])
        period_pnl = period_end_equity - previous_period_end_equity
        previous_period_end_equity = period_end_equity
        payload = {
            "period_from": first_ts.date().isoformat(),
            "period_to": last_ts.date().isoformat(),
            "net_pnl": period_pnl,
            "profit_factor": None,
            "win_probability": None,
            "max_drawdown": None,
            "trades": len(rows),
            "annual_trades": 0.0,
        }
        if mode == "year":
            payload["year"] = int(key)
        else:
            year, month = key
            payload["year"] = year
            payload["month"] = month
            payload["period"] = f"{year}-{month:02d}"
        results.append(payload)
    return results


def _merge_period_results(
    strategy_rows: Sequence[dict[str, Any]],
    benchmark_rows: Sequence[dict[str, Any]],
    label_key: str,
) -> list[dict[str, Any]]:
    bench_by_key = {row[label_key]: row for row in benchmark_rows if label_key in row}
    merged = []
    for row in strategy_rows:
        bench = bench_by_key.get(row[label_key], {})
        merged.append(
            {
                **row,
                "benchmark_net_pnl": float(bench.get("net_pnl") or 0.0),
                "benchmark_net_return": float(bench.get("net_return") or 0.0),
                "benchmark_net_return_on_notional": float(bench.get("net_return_on_notional") or 0.0),
                "excess_net_pnl": float(row.get("net_pnl") or 0.0) - float(bench.get("net_pnl") or 0.0),
                "excess_net_return": float(row.get("net_return") or 0.0) - float(bench.get("net_return") or 0.0),
                "excess_net_return_on_notional": float(row.get("net_return_on_notional") or 0.0) - float(bench.get("net_return_on_notional") or 0.0),
            }
        )
    return merged


def _excess_summary(strategy_metrics: dict[str, Any], benchmark_metrics: dict[str, Any]) -> dict[str, Any]:
    strategy_net = float(strategy_metrics.get("net_pnl") or 0.0)
    strategy_annualized = float(strategy_metrics.get("annualized_net_pnl") or 0.0)
    benchmark_net = float(benchmark_metrics.get("net_pnl") or 0.0)
    benchmark_annualized = float(benchmark_metrics.get("annualized_net_pnl") or 0.0)
    return {
        "strategy_net_pnl": strategy_net,
        "strategy_annualized_net_pnl": strategy_annualized,
        "strategy_net_return": float(strategy_metrics.get("net_return") or 0.0),
        "strategy_annualized_net_return": float(strategy_metrics.get("annualized_net_return") or 0.0),
        "strategy_net_return_on_notional": float(strategy_metrics.get("net_return_on_notional") or 0.0),
        "strategy_annualized_net_return_on_notional": float(strategy_metrics.get("annualized_net_return_on_notional") or 0.0),
        "benchmark_net_pnl": benchmark_net,
        "benchmark_annualized_net_pnl": benchmark_annualized,
        "benchmark_net_return": float(benchmark_metrics.get("net_return") or 0.0),
        "benchmark_annualized_net_return": float(benchmark_metrics.get("annualized_net_return") or 0.0),
        "benchmark_net_return_on_notional": float(benchmark_metrics.get("net_return_on_notional") or 0.0),
        "benchmark_annualized_net_return_on_notional": float(benchmark_metrics.get("annualized_net_return_on_notional") or 0.0),
        "excess_net_pnl": strategy_net - benchmark_net,
        "excess_annualized_net_pnl": strategy_annualized - benchmark_annualized,
        "excess_net_return": float(strategy_metrics.get("net_return") or 0.0) - float(benchmark_metrics.get("net_return") or 0.0),
        "excess_annualized_net_return": float(strategy_metrics.get("annualized_net_return") or 0.0) - float(benchmark_metrics.get("annualized_net_return") or 0.0),
        "excess_net_return_on_notional": float(strategy_metrics.get("net_return_on_notional") or 0.0) - float(benchmark_metrics.get("net_return_on_notional") or 0.0),
        "excess_annualized_net_return_on_notional": float(strategy_metrics.get("annualized_net_return_on_notional") or 0.0) - float(benchmark_metrics.get("annualized_net_return_on_notional") or 0.0),
    }


def _with_return_metrics(metrics: dict[str, Any], capital_base_usd: float, *, notional_base_usd: float | None = None) -> dict[str, Any]:
    enriched = dict(metrics)
    base = max(1.0, float(capital_base_usd))
    notional_base = max(1.0, float(notional_base_usd if notional_base_usd is not None else base))
    enriched["capital_base_usd"] = base
    enriched["notional_base_usd"] = notional_base
    enriched["net_return"] = float(enriched.get("net_pnl") or 0.0) / base
    enriched["net_return_on_notional"] = float(enriched.get("net_pnl") or 0.0) / notional_base
    annualized_net_pnl = enriched.get("annualized_net_pnl")
    if annualized_net_pnl is not None:
        enriched["annualized_net_return"] = float(annualized_net_pnl) / base
        enriched["annualized_net_return_on_notional"] = float(annualized_net_pnl) / notional_base
    max_drawdown = enriched.get("max_drawdown")
    if max_drawdown is not None:
        enriched["max_drawdown_return"] = float(max_drawdown) / base
        enriched["max_drawdown_return_on_notional"] = float(max_drawdown) / notional_base
    return enriched


def _sample_trades(signals: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if not signals:
        return []
    candidates = [
        ("best_trade", max(signals, key=lambda row: float(row["pnl"]))),
        ("worst_trade", min(signals, key=lambda row: float(row["pnl"]))),
        ("latest_trade", max(signals, key=lambda row: _signal_timestamp(row))),
    ]
    selected = []
    seen = set()
    for label, signal in candidates:
        key = (_signal_timestamp(signal), signal.get("rule_index"))
        if key in seen:
            continue
        seen.add(key)
        selected.append({**signal, "sample_label": label})
        if len(selected) >= limit:
            break
    return selected


def _load_trade_window_bars(
    con: duckdb.DuckDBPyConnection,
    context: dict[str, Any],
    trade: dict[str, Any],
    *,
    bar_minutes: int,
) -> list[dict[str, Any]]:
    entry = _signal_timestamp(trade)
    exit_value = trade.get("exit_timestamp")
    exit_ts = datetime.fromisoformat(str(exit_value)) if isinstance(exit_value, str) else exit_value
    if not isinstance(exit_ts, datetime):
        exit_ts = entry + timedelta(minutes=int(trade.get("horizon_minutes") or bar_minutes))
    pad = timedelta(minutes=bar_minutes * 40)
    rows = _fetch_dicts(
        con,
        f"""
        SELECT timestamp, open, high, low, close, tick_count
        FROM read_parquet('{context["parquet_glob"]}')
        WHERE timestamp >= '{(entry - pad).isoformat(sep=" ")}'
          AND timestamp <= '{(exit_ts + pad).isoformat(sep=" ")}'
        ORDER BY timestamp
        """,
    )
    return [
        {
            "timestamp": _to_iso(row["timestamp"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "bar_volume": int(row.get("tick_count") or 0),
        }
        for row in rows
    ]


def _trade_marker(signal: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_timestamp": _to_iso(signal["timestamp"]),
        "exit_timestamp": _to_iso(signal.get("exit_timestamp")),
        "entry_price": float(signal.get("entry_price") or 0.0),
        "exit_price": float(signal.get("exit_price") or 0.0),
        "direction": signal.get("direction_label"),
        "pnl": float(signal.get("pnl") or 0.0),
        "horizon_minutes": int(signal.get("horizon_minutes") or 0),
        "scan_type": signal.get("scan_type"),
        "session_bucket": signal.get("session_bucket"),
        "volume_bin": signal.get("volume_bin"),
        "range_bin": signal.get("range_bin"),
    }


def _render_html(payload: dict[str, Any], data_filename: str) -> str:
    strategy_sets = payload.get("strategy_sets") or [
        {
            "objective": payload.get("selection_objective"),
            "selection_policy": payload.get("selection_policy"),
            "strategy_count": len(payload["strategies"]),
            "total_net_pnl": sum(float(strategy["replayed_metrics"].get("net_pnl") or 0.0) for strategy in payload["strategies"]),
            "total_excess_net_pnl": sum(float(strategy["excess_summary"].get("excess_net_pnl") or 0.0) for strategy in payload["strategies"]),
            "strategies": payload["strategies"],
        }
    ]
    objective_rows = "\n".join(_objective_overview_row(report) for report in strategy_sets)
    overview_sections = "\n".join(_strategy_set_section(report) for report in strategy_sets)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NQ_CME Top 3 策略表现报告</title>
  <style>
    :root {{
      --bg: #f6f8fb;
      --surface: #ffffff;
      --ink: #172033;
      --muted: #667085;
      --line: #d9e0ea;
      --accent: #1769aa;
      --good: #117a4c;
      --bad: #b42318;
      --warn: #a15c00;
      --soft: #eaf2fb;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: var(--bg); line-height: 1.5; }}
    header {{ background: #102033; color: white; padding: 28px 32px; }}
    main {{ max-width: 1320px; margin: 0 auto; padding: 24px; }}
    h1, h2, h3 {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 30px; }}
    h2 {{ font-size: 22px; margin-bottom: 14px; }}
    h3 {{ font-size: 17px; margin: 18px 0 10px; }}
    p {{ margin: 8px 0; }}
    .meta {{ color: #c9d6e6; margin-top: 10px; }}
    .section {{ background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 20px; margin-bottom: 18px; }}
    .grid {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    .metric {{ border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #fbfcfe; }}
    .metric span {{ display: block; color: var(--muted); font-size: 12px; }}
    .metric strong {{ display: block; margin-top: 4px; font-size: 20px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 8px; text-align: right; vertical-align: top; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ color: var(--muted); font-weight: 600; background: #f8fafc; }}
    .pill {{ display: inline-block; border: 1px solid var(--line); border-radius: 999px; padding: 3px 8px; margin: 2px; background: var(--soft); font-size: 12px; }}
    .good {{ color: var(--good); }}
    .bad {{ color: var(--bad); }}
    .warn {{ color: var(--warn); }}
    .chart {{ width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; background: white; padding: 10px; position: relative; }}
    .chart svg {{ display: block; width: 100%; height: auto; }}
    .chart-tooltip {{
      position: absolute;
      min-width: 180px;
      max-width: 280px;
      pointer-events: none;
      background: rgba(15, 23, 40, 0.94);
      color: white;
      border: 1px solid rgba(255,255,255,0.14);
      border-radius: 8px;
      padding: 8px 10px;
      font-size: 12px;
      line-height: 1.45;
      box-shadow: 0 10px 24px rgba(15, 23, 40, 0.18);
      opacity: 0;
      transform: translateY(4px);
      transition: opacity 120ms ease, transform 120ms ease;
      z-index: 5;
      white-space: pre-line;
    }}
    .chart-tooltip.is-visible {{ opacity: 1; transform: translateY(0); }}
    .chart-hover-line {{ stroke: rgba(23, 105, 170, 0.45); stroke-width: 1.2; stroke-dasharray: 4 4; visibility: hidden; }}
    .kline-grid {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); }}
    .note {{ color: var(--muted); font-size: 13px; }}
    .heat td {{ min-width: 68px; }}
    details {{ border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; margin-top: 10px; background: #fbfcfe; }}
    summary {{ cursor: pointer; font-weight: 600; }}
    @media (max-width: 760px) {{
      header {{ padding: 22px 18px; }}
      main {{ padding: 14px; }}
      h1 {{ font-size: 24px; }}
      table {{ font-size: 12px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>NQ_CME Top 策略综合报告</h1>
    <div class="meta">数据: {html.escape(str(payload["symbol"]))} {html.escape(str(payload["timeframe"]))} OHLCV, {html.escape(str(payload["date_from"]))} 至 {html.escape(str(payload["date_to"]))}; 成本: {fmt_usd(payload["round_trip_cost_usd"])} / trade; 收益率基准资金: {fmt_usd(payload["capital_base_usd"])}; 名义收益率按各策略激活窗口首根 NQ close × point_value 的 1手名义价值计算</div>
  </header>
  <main>
    <section class="section">
      <h2>总览</h2>
      <p>本报告将不同排序口径收敛为一份总报告：同一份 HTML 内同时展示“年化质量优先”“总净收益优先”“稳定性优先”的 Top 策略结果，并按策略构成去重。所有交易均基于 OHLCV bar 级重放，不能证明真实 bid/ask、限价成交率或排队成本。</p>
      <p class="note">来源报告: {'; '.join(f"{html.escape(str(row['timeframe']))} -> {html.escape(str(row['path']))} (候选 {row['yearly_profitable_candidate_count']})" for row in payload.get('source_reports', []))}</p>
      <table>
        <thead><tr><th>口径</th><th>策略数</th><th>Top合计净收益</th><th>Top合计超额收益</th><th>选择规则</th></tr></thead>
        <tbody>{objective_rows}</tbody>
      </table>
      <p class="note">配套结构化数据: {html.escape(data_filename)}</p>
    </section>
    {overview_sections}
  </main>
  <script>
    (() => {{
      const charts = document.querySelectorAll('.chart[data-interactive-chart="true"]');
      charts.forEach((chart) => {{
        const svg = chart.querySelector('svg');
        if (!svg) return;
        const hoverTargets = svg.querySelectorAll('[data-tooltip]');
        if (!hoverTargets.length) return;
        const crosshair = svg.querySelector('.chart-hover-line');
        const tooltip = document.createElement('div');
        tooltip.className = 'chart-tooltip';
        chart.appendChild(tooltip);

        const hideTooltip = () => {{
          tooltip.classList.remove('is-visible');
          if (crosshair) crosshair.style.visibility = 'hidden';
        }};

        const placeTooltip = (event) => {{
          const rect = chart.getBoundingClientRect();
          const tooltipRect = tooltip.getBoundingClientRect();
          let left = event.clientX - rect.left + 14;
          let top = event.clientY - rect.top - tooltipRect.height - 12;
          if (left + tooltipRect.width > rect.width - 8) {{
            left = rect.width - tooltipRect.width - 8;
          }}
          if (left < 8) left = 8;
          if (top < 8) {{
            top = event.clientY - rect.left + 14;
            top = event.clientY - rect.top + 14;
          }}
          tooltip.style.left = `${{left}}px`;
          tooltip.style.top = `${{top}}px`;
        }};

        hoverTargets.forEach((target) => {{
          const show = (event) => {{
            tooltip.textContent = target.dataset.tooltip || '';
            tooltip.classList.add('is-visible');
            if (crosshair && target.dataset.crosshairX) {{
              crosshair.setAttribute('x1', target.dataset.crosshairX);
              crosshair.setAttribute('x2', target.dataset.crosshairX);
              crosshair.style.visibility = 'visible';
            }}
            placeTooltip(event);
          }};
          target.addEventListener('mouseenter', show);
          target.addEventListener('mousemove', show);
          target.addEventListener('mouseleave', hideTooltip);
        }});

        chart.addEventListener('mouseleave', hideTooltip);
      }});
    }})();
  </script>
</body>
</html>
"""


def _render_comparison_html(payload: dict[str, Any]) -> str:
    report_rows = "".join(_comparison_overview_row(report) for report in payload["reports"])
    sections = "".join(_comparison_report_section(report) for report in payload["reports"])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>NQ Top3 策略报告对比</title>
  <style>
    :root {{ color-scheme: light; --bg:#f4f7fb; --surface:#ffffff; --line:#d9e2ec; --text:#172033; --muted:#667085; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    header {{ background:#0f1728; color:#fff; padding:24px 28px; }}
    main {{ padding:18px; max-width:1400px; margin:0 auto; }}
    .section {{ background:var(--surface); border:1px solid var(--line); border-radius:8px; padding:18px; margin-bottom:16px; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    th, td {{ border-bottom:1px solid var(--line); padding:8px; text-align:right; vertical-align:top; }}
    th:first-child, td:first-child {{ text-align:left; }}
    th {{ color:var(--muted); background:#f8fafc; }}
    .note {{ color:var(--muted); font-size:13px; }}
  </style>
</head>
<body>
  <header>
    <h1>NQ Top3 策略报告对比</h1>
    <p class="note">对比总收益优先、年化收益优先、稳健性优先三种选股口径。</p>
  </header>
  <main>
    <section class="section">
      <h2>总览</h2>
      <table>
        <thead><tr><th>报告</th><th>目标</th><th>策略数</th><th>Top3 总净收益</th><th>Top3 总超额收益</th></tr></thead>
        <tbody>{report_rows}</tbody>
      </table>
    </section>
    {sections}
  </main>
</body>
</html>"""


def _comparison_overview_row(report: dict[str, Any]) -> str:
    return (
        f"<tr><td>{html.escape(str(report['name']))}</td>"
        f"<td>{html.escape(str(report['objective']))}</td>"
        f"<td>{int(report['strategy_count'])}</td>"
        f"<td>{fmt_usd(report['total_net_pnl'])}</td>"
        f"<td>{fmt_usd(report['total_excess_net_pnl'])}</td></tr>"
    )


def _comparison_report_section(report: dict[str, Any]) -> str:
    rows = []
    for strategy in report["strategies"]:
        rows.append(
            f"<tr><td>{html.escape(str(strategy['display_id']))}</td>"
            f"<td>{html.escape(str(strategy['source_timeframe']))}</td>"
            f"<td>{html.escape(str(strategy['selection_rule']))}</td>"
            f"<td>{fmt_usd(strategy['net_pnl'])}</td>"
            f"<td>{fmt_usd(strategy['annualized_net_pnl'])}</td>"
            f"<td>{fmt_num(strategy['profit_factor'], 3)}</td>"
            f"<td>{fmt_pct(strategy['win_probability'])}</td>"
            f"<td>{strategy['passed_gate_count']}/{strategy['total_gate_count']}</td></tr>"
        )
    source_reports = "; ".join(
        f"{html.escape(str(source.get('timeframe')))} -> {html.escape(str(source.get('path')))}"
        for source in report.get("source_reports", [])
    )
    return f"""
    <section class="section">
      <h2>{html.escape(str(report['name']))}</h2>
      <p class="note">目标: {html.escape(str(report['objective']))} | 规则: {html.escape(str(report['policy']))}</p>
      <p class="note">来源: {source_reports}</p>
      <table>
        <thead><tr><th>策略</th><th>周期</th><th>选择规则</th><th>净收益</th><th>年化净收益</th><th>PF</th><th>胜率</th><th>门槛</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
    """


def _objective_overview_row(report: dict[str, Any]) -> str:
    return (
        f"<tr><td>{html.escape(_objective_label(str(report.get('objective'))))}</td>"
        f"<td>{int(report.get('strategy_count') or 0)}</td>"
        f"<td>{fmt_usd(report.get('total_net_pnl'))}</td>"
        f"<td>{fmt_usd(report.get('total_excess_net_pnl'))}</td>"
        f"<td>{html.escape(str(report.get('selection_policy')))}</td></tr>"
    )


def _strategy_set_section(report: dict[str, Any]) -> str:
    strategies = report.get("strategies") or []
    overview_rows = "\n".join(_overview_row(strategy) for strategy in strategies)
    strategy_sections = "\n".join(_strategy_section(strategy) for strategy in strategies)
    objective = str(report.get("objective"))
    return f"""
    <section class="section">
      <h2>{html.escape(_objective_label(objective))}</h2>
      <p class="note">选择目标: {html.escape(objective)} | 选择规则: {html.escape(str(report.get("selection_policy")))}</p>
      <table>
        <thead><tr><th>策略</th><th>周期</th><th>激活年份</th><th>边数量</th><th>净收益</th><th>收益率(10万)</th><th>收益率(1手名义)</th><th>年化净收益</th><th>年化收益率(10万)</th><th>年化收益率(1手名义)</th><th>PF</th><th>胜率</th><th>门槛</th></tr></thead>
        <tbody>{overview_rows}</tbody>
      </table>
    </section>
    {strategy_sections}
    """


def _strategy_section(strategy: dict[str, Any]) -> str:
    metrics = strategy["replayed_metrics"]
    benchmark = strategy["benchmark"]
    benchmark_metrics = benchmark["metrics"]
    excess = strategy["excess_summary"]
    analysis = strategy.get("strategy_analysis") or {}
    profile = analysis.get("strategy_profile") or {}
    evaluation = strategy.get("evaluation_summary") or {}
    strategy_curve = strategy.get("equity_curve") or strategy.get("equity_curve_sampled") or []
    benchmark_curve = benchmark.get("equity_curve") or benchmark.get("equity_curve_sampled") or []
    return f"""
    <section class="section">
      <h2>{html.escape(strategy["display_id"])} · {html.escape(str(strategy["selection_rule"]))}</h2>
      <p class="note">来源: {html.escape(str(strategy.get("source_timeframe")))} | 激活年份: {html.escape(str(strategy.get("activation_start_year")))} | 候选来源: {html.escape(str(strategy.get("source_report_path")))} | 硬门槛通过: {evaluation.get("passed_gate_count", 0)}/{evaluation.get("total_gate_count", 0)} | 年度盈利占比: {fmt_pct(evaluation.get("positive_year_ratio"))}</p>
      <div class="grid">
        {_metric("净收益", fmt_usd(metrics["net_pnl"]))}
        {_metric("净收益率(10万资金)", fmt_pct(metrics.get("net_return")))}
        {_metric("净收益率(1手名义)", fmt_pct(metrics.get("net_return_on_notional")))}
        {_metric("Profit Factor", fmt_num(metrics["profit_factor"], 3))}
        {_metric("胜率", fmt_pct(metrics["win_probability"]))}
        {_metric("年化交易", fmt_num(metrics["annual_trades"], 1))}
        {_metric("年化净收益", fmt_usd(metrics.get("annualized_net_pnl")))}
        {_metric("年化收益率(10万资金)", fmt_pct(metrics.get("annualized_net_return")))}
        {_metric("年化收益率(1手名义)", fmt_pct(metrics.get("annualized_net_return_on_notional")))}
        {_metric("最大回撤", fmt_usd(metrics["max_drawdown"]))}
        {_metric("最大回撤率(10万资金)", fmt_pct(metrics.get("max_drawdown_return")))}
        {_metric("最大回撤率(1手名义)", fmt_pct(metrics.get("max_drawdown_return_on_notional")))}
        {_metric("交易数", fmt_int(metrics["trades"]))}
      </div>
      <h3>基准与超额收益</h3>
      <div class="grid">
        {_metric("策略净收益", fmt_usd(excess["strategy_net_pnl"]))}
        {_metric("策略净收益率(10万资金)", fmt_pct(excess["strategy_net_return"]))}
        {_metric("策略净收益率(1手名义)", fmt_pct(excess["strategy_net_return_on_notional"]))}
        {_metric("策略年化净收益", fmt_usd(excess["strategy_annualized_net_pnl"]))}
        {_metric("策略年化收益率(10万资金)", fmt_pct(excess["strategy_annualized_net_return"]))}
        {_metric("策略年化收益率(1手名义)", fmt_pct(excess["strategy_annualized_net_return_on_notional"]))}
        {_metric("NQ 持有净收益", fmt_usd(excess["benchmark_net_pnl"]))}
        {_metric("NQ 持有净收益率(10万资金)", fmt_pct(excess["benchmark_net_return"]))}
        {_metric("NQ 持有净收益率(1手名义)", fmt_pct(excess["benchmark_net_return_on_notional"]))}
        {_metric("NQ 持有年化净收益", fmt_usd(excess["benchmark_annualized_net_pnl"]))}
        {_metric("NQ 持有年化收益率(10万资金)", fmt_pct(excess["benchmark_annualized_net_return"]))}
        {_metric("NQ 持有年化收益率(1手名义)", fmt_pct(excess["benchmark_annualized_net_return_on_notional"]))}
        {_metric("超额收益", fmt_usd(excess["excess_net_pnl"]))}
        {_metric("超额收益率(10万资金)", fmt_pct(excess["excess_net_return"]))}
        {_metric("超额收益率(1手名义)", fmt_pct(excess["excess_net_return_on_notional"]))}
        {_metric("年化超额收益", fmt_usd(excess["excess_annualized_net_pnl"]))}
        {_metric("年化超额收益率(10万资金)", fmt_pct(excess["excess_annualized_net_return"]))}
        {_metric("年化超额收益率(1手名义)", fmt_pct(excess["excess_annualized_net_return_on_notional"]))}
        {_metric("基准最大回撤", fmt_usd(benchmark_metrics["max_drawdown"]))}
        {_metric("基准最大回撤率(10万资金)", fmt_pct(benchmark_metrics.get("max_drawdown_return")))}
        {_metric("基准最大回撤率(1手名义)", fmt_pct(benchmark_metrics.get("max_drawdown_return_on_notional")))}
      </div>
      <h3>策略构成</h3>
      <p>{_profile_pills(profile)}</p>
      <table>{_edge_rows(strategy.get("constituent_edges") or [])}</table>
      <h3>资金曲线</h3>
      <div class="chart" data-interactive-chart="true">{_line_svg(strategy["equity_curve_sampled"], title="累计净收益")}</div>
      <div class="chart" data-interactive-chart="true">{_comparison_line_svg(strategy_curve, benchmark_curve)}</div>
      <h3>年度表现</h3>
      <div class="chart">{_bar_svg(strategy["annual_results"], "year", "net_pnl")}</div>
      <table>{_period_rows(strategy["annual_results"], ["year"])}</table>
      <details><summary>展开年度基准与超额收益</summary><table>{_excess_period_rows(strategy["annual_excess_results"], ["year"])}</table></details>
      <h3>月度表现</h3>
      {_monthly_heatmap(strategy["monthly_results"])}
      <details><summary>展开月度明细</summary><table>{_period_rows(strategy["monthly_results"], ["period"])}</table></details>
      <details><summary>展开月度基准与超额收益</summary><table>{_excess_period_rows(strategy["monthly_excess_results"], ["period"])}</table></details>
      <h3>进场/出场 K 线位置</h3>
      <div class="kline-grid">{''.join(_sample_chart(sample) for sample in strategy["sample_trade_charts"])}</div>
      <h3>优点与缺点</h3>
      <div class="grid">
        <div>{_bullet_list("优点", analysis.get("strengths") or [])}</div>
        <div>{_bullet_list("缺点", analysis.get("weaknesses") or [])}</div>
      </div>
      <h3>成本压力</h3>
      <table>{_cost_stress_rows(strategy["cost_stress"])}</table>
    </section>
    """


def _overview_row(strategy: dict[str, Any]) -> str:
    metrics = strategy["replayed_metrics"]
    evaluation = strategy.get("evaluation_summary") or {}
    return (
        f"<tr><td>{html.escape(strategy['display_id'])}</td>"
        f"<td>{html.escape(str(strategy.get('source_timeframe')))}</td>"
        f"<td>{strategy['activation_start_year']}</td>"
        f"<td>{strategy['edge_count']}</td>"
        f"<td>{fmt_usd(metrics['net_pnl'])}</td>"
        f"<td>{fmt_pct(metrics.get('net_return'))}</td>"
        f"<td>{fmt_pct(metrics.get('net_return_on_notional'))}</td>"
        f"<td>{fmt_usd(metrics.get('annualized_net_pnl'))}</td>"
        f"<td>{fmt_pct(metrics.get('annualized_net_return'))}</td>"
        f"<td>{fmt_pct(metrics.get('annualized_net_return_on_notional'))}</td>"
        f"<td>{fmt_num(metrics['profit_factor'], 3)}</td>"
        f"<td>{fmt_pct(metrics['win_probability'])}</td>"
        f"<td>{int(evaluation.get('passed_gate_count') or 0)}/{int(evaluation.get('total_gate_count') or 0)}</td></tr>"
    )


def _metric(label: str, value: str) -> str:
    return f"<div class=\"metric\"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>"


def _profile_pills(profile: dict[str, Any]) -> str:
    parts = [
        ("主导 VOL", profile.get("dominant_volume_profile")),
        ("主导时段", profile.get("dominant_session_bucket")),
        ("家族", profile.get("families")),
        ("方向", profile.get("directions")),
    ]
    return " ".join(f"<span class=\"pill\">{html.escape(label)}: {html.escape(str(value))}</span>" for label, value in parts)


def _edge_rows(edges: Sequence[dict[str, Any]]) -> str:
    rows = [
        "<thead><tr><th>scan_type</th><th>方向</th><th>周期</th><th>时段</th><th>DOW</th><th>trend</th><th>volume</th><th>range</th></tr></thead><tbody>"
    ]
    for edge in edges:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(edge.get('scan_type')))}</td>"
            f"<td>{html.escape(str(edge.get('direction_label')))}</td>"
            f"<td>{edge.get('horizon_minutes')}</td>"
            f"<td>{html.escape(str(edge.get('session_bucket')))}</td>"
            f"<td>{edge.get('dow')}</td>"
            f"<td>{edge.get('trend_bin')}</td>"
            f"<td>{edge.get('volume_bin')}</td>"
            f"<td>{edge.get('range_bin')}</td>"
            "</tr>"
        )
    rows.append("</tbody>")
    return "".join(rows)


def _period_rows(rows: Sequence[dict[str, Any]], label_keys: Sequence[str]) -> str:
    out = ["<thead><tr><th>周期</th><th>交易数</th><th>净收益</th><th>收益率(10万)</th><th>收益率(1手名义)</th><th>PF</th><th>胜率</th><th>最大回撤</th><th>回撤率(10万)</th><th>回撤率(1手名义)</th></tr></thead><tbody>"]
    for row in rows:
        label = " ".join(str(row.get(key)) for key in label_keys)
        css = "good" if float(row.get("net_pnl") or 0) >= 0 else "bad"
        out.append(
            f"<tr><td>{html.escape(label)}</td><td>{fmt_int(row.get('trades'))}</td>"
            f"<td class=\"{css}\">{fmt_usd(row.get('net_pnl'))}</td>"
            f"<td class=\"{css}\">{fmt_pct(row.get('net_return'))}</td>"
            f"<td class=\"{css}\">{fmt_pct(row.get('net_return_on_notional'))}</td>"
            f"<td>{fmt_num(row.get('profit_factor'), 3)}</td>"
            f"<td>{fmt_pct(row.get('win_probability'))}</td>"
            f"<td>{fmt_usd(row.get('max_drawdown'))}</td>"
            f"<td>{fmt_pct(row.get('max_drawdown_return'))}</td>"
            f"<td>{fmt_pct(row.get('max_drawdown_return_on_notional'))}</td></tr>"
        )
    out.append("</tbody>")
    return "".join(out)


def _cost_stress_rows(rows: Sequence[dict[str, Any]]) -> str:
    out = ["<thead><tr><th>场景</th><th>额外成本</th><th>净收益</th><th>收益率(10万)</th><th>收益率(1手名义)</th><th>PF</th><th>胜率</th></tr></thead><tbody>"]
    for row in rows:
        out.append(
            f"<tr><td>{html.escape(str(row.get('label')))}</td><td>{fmt_usd(row.get('extra_usd_per_trade'))}</td>"
            f"<td>{fmt_usd(row.get('net_pnl'))}</td><td>{fmt_pct(row.get('net_return'))}</td><td>{fmt_pct(row.get('net_return_on_notional'))}</td><td>{fmt_num(row.get('profit_factor'), 3)}</td>"
            f"<td>{fmt_pct(row.get('win_probability'))}</td></tr>"
        )
    out.append("</tbody>")
    return "".join(out)


def _excess_period_rows(rows: Sequence[dict[str, Any]], label_keys: Sequence[str]) -> str:
    out = ["<thead><tr><th>周期</th><th>策略净收益</th><th>策略收益率(10万)</th><th>策略收益率(1手名义)</th><th>基准净收益</th><th>基准收益率(10万)</th><th>基准收益率(1手名义)</th><th>超额收益</th><th>超额收益率(10万)</th><th>超额收益率(1手名义)</th><th>策略PF</th><th>策略胜率</th></tr></thead><tbody>"]
    for row in rows:
        label = " ".join(str(row.get(key)) for key in label_keys)
        excess_css = "good" if float(row.get("excess_net_pnl") or 0) >= 0 else "bad"
        out.append(
            f"<tr><td>{html.escape(label)}</td>"
            f"<td>{fmt_usd(row.get('net_pnl'))}</td>"
            f"<td>{fmt_pct(row.get('net_return'))}</td>"
            f"<td>{fmt_pct(row.get('net_return_on_notional'))}</td>"
            f"<td>{fmt_usd(row.get('benchmark_net_pnl'))}</td>"
            f"<td>{fmt_pct(row.get('benchmark_net_return'))}</td>"
            f"<td>{fmt_pct(row.get('benchmark_net_return_on_notional'))}</td>"
            f"<td class=\"{excess_css}\">{fmt_usd(row.get('excess_net_pnl'))}</td>"
            f"<td class=\"{excess_css}\">{fmt_pct(row.get('excess_net_return'))}</td>"
            f"<td class=\"{excess_css}\">{fmt_pct(row.get('excess_net_return_on_notional'))}</td>"
            f"<td>{fmt_num(row.get('profit_factor'), 3)}</td>"
            f"<td>{fmt_pct(row.get('win_probability'))}</td></tr>"
        )
    out.append("</tbody>")
    return "".join(out)


def _monthly_heatmap(rows: Sequence[dict[str, Any]]) -> str:
    by_year_month = {(row["year"], row["month"]): row for row in rows}
    years = sorted({row["year"] for row in rows})
    max_abs = max((abs(float(row.get("net_pnl") or 0.0)) for row in rows), default=1.0)
    out = ["<table class=\"heat\"><thead><tr><th>年</th>"]
    out.extend(f"<th>{month:02d}</th>" for month in range(1, 13))
    out.append("</tr></thead><tbody>")
    for year in years:
        out.append(f"<tr><td>{year}</td>")
        for month in range(1, 13):
            row = by_year_month.get((year, month))
            if not row:
                out.append("<td></td>")
                continue
            pnl = float(row.get("net_pnl") or 0.0)
            alpha = 0.12 + 0.72 * min(1.0, abs(pnl) / max_abs)
            color = f"rgba(17,122,76,{alpha:.2f})" if pnl >= 0 else f"rgba(180,35,24,{alpha:.2f})"
            out.append(f"<td style=\"background:{color}\">{fmt_usd(pnl)}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _sample_chart(sample: dict[str, Any]) -> str:
    trade = sample["trade"]
    return (
        "<div>"
        f"<h3>{html.escape(sample['label'])}: {html.escape(str(trade['entry_timestamp']))} "
        f"{html.escape(str(trade['direction']))} PnL {fmt_usd(trade['pnl'])}</h3>"
        f"<div class=\"chart\">{sample['chart_svg']}</div>"
        "</div>"
    )


def _bullet_list(title: str, items: Sequence[str]) -> str:
    body = "".join(f"<li>{html.escape(str(item))}</li>" for item in items)
    return f"<h3>{html.escape(title)}</h3><ul>{body}</ul>"


def _line_svg(points: Sequence[dict[str, Any]], *, title: str) -> str:
    width, height = 900, 260
    pad = 38
    if not points:
        return "<svg viewBox=\"0 0 900 260\"></svg>"
    values = [float(point["equity"]) for point in points]
    low, high = min(values), max(values)
    if high == low:
        high += 1.0
        low -= 1.0
    def xy(index: int, value: float) -> tuple[float, float]:
        x = pad + index / max(1, len(points) - 1) * (width - pad * 2)
        y = height - pad - (value - low) / (high - low) * (height - pad * 2)
        return x, y
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, float(point["equity"])) for i, point in enumerate(points)))
    zero_y = xy(0, 0.0)[1] if low <= 0 <= high else height - pad
    hover_targets = _hover_targets_svg(
        width=width,
        height=height,
        pad=pad,
        point_count=len(points),
        labels=[
            f"{str(point['timestamp'])} | 累计净收益 {fmt_usd(point['equity'])} | 单笔变动 {fmt_usd(point.get('pnl'))}"
            for point in points
        ],
    )
    return (
        f"<svg viewBox=\"0 0 {width} {height}\" role=\"img\" aria-label=\"{html.escape(title)}\">"
        f"<text x=\"{pad}\" y=\"22\" fill=\"#667085\" font-size=\"13\">{html.escape(title)}</text>"
        f"<line x1=\"{pad}\" y1=\"{zero_y:.1f}\" x2=\"{width-pad}\" y2=\"{zero_y:.1f}\" stroke=\"#d9e0ea\"/>"
        f"<line class=\"chart-hover-line\" x1=\"{pad}\" y1=\"{pad}\" x2=\"{pad}\" y2=\"{height-pad}\"/>"
        f"<polyline fill=\"none\" stroke=\"#1769aa\" stroke-width=\"2.2\" points=\"{poly}\"/>"
        f"{hover_targets}"
        f"<text x=\"{pad}\" y=\"{height-8}\" fill=\"#667085\" font-size=\"12\">{html.escape(str(points[0]['timestamp'])[:10])}</text>"
        f"<text x=\"{width-pad-86}\" y=\"{height-8}\" fill=\"#667085\" font-size=\"12\">{html.escape(str(points[-1]['timestamp'])[:10])}</text>"
        f"<text x=\"{width-pad-112}\" y=\"22\" fill=\"#172033\" font-size=\"12\">max {fmt_usd(high)}</text>"
        f"</svg>"
    )


def _comparison_line_svg(strategy_points: Sequence[dict[str, Any]], benchmark_points: Sequence[dict[str, Any]]) -> str:
    width, height = 900, 260
    pad = 38
    if not strategy_points or not benchmark_points:
        return "<svg viewBox=\"0 0 900 260\"></svg>"
    aligned = _aligned_equity_curves(strategy_points, benchmark_points)
    values = [row["strategy"] for row in aligned] + [row["benchmark"] for row in aligned]
    low, high = min(values), max(values)
    if high == low:
        high += 1.0
        low -= 1.0
    def xy(index: int, value: float) -> tuple[float, float]:
        x = pad + index / max(1, len(aligned) - 1) * (width - pad * 2)
        y = height - pad - (value - low) / (high - low) * (height - pad * 2)
        return x, y
    strategy_poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, row["strategy"]) for i, row in enumerate(aligned)))
    benchmark_poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, row["benchmark"]) for i, row in enumerate(aligned)))
    hover_targets = _hover_targets_svg(
        width=width,
        height=height,
        pad=pad,
        point_count=len(aligned),
        labels=[
            f"{row['timestamp']} | 策略累计净收益 {fmt_usd(row['strategy'])} | NQ累计净收益 {fmt_usd(row['benchmark'])} | NQ指数 {fmt_price(row.get('benchmark_close'))}"
            for row in aligned
        ],
    )
    return (
        f"<svg viewBox=\"0 0 {width} {height}\" role=\"img\" aria-label=\"策略与基准对比曲线\">"
        f"<text x=\"{pad}\" y=\"22\" fill=\"#667085\" font-size=\"13\">策略 vs NQ 持有累计收益</text>"
        f"<line class=\"chart-hover-line\" x1=\"{pad}\" y1=\"{pad}\" x2=\"{pad}\" y2=\"{height-pad}\"/>"
        f"<polyline fill=\"none\" stroke=\"#1769aa\" stroke-width=\"2.2\" points=\"{strategy_poly}\"/>"
        f"<polyline fill=\"none\" stroke=\"#a15c00\" stroke-width=\"2.2\" points=\"{benchmark_poly}\"/>"
        f"{hover_targets}"
        f"<text x=\"{pad}\" y=\"{height-8}\" fill=\"#1769aa\" font-size=\"12\">策略</text>"
        f"<text x=\"{pad+48}\" y=\"{height-8}\" fill=\"#a15c00\" font-size=\"12\">基准</text>"
        f"</svg>"
    )


def _aligned_equity_curves(
    strategy_points: Sequence[dict[str, Any]],
    benchmark_points: Sequence[dict[str, Any]],
    *,
    max_points: int = 1200,
) -> list[dict[str, Any]]:
    if not strategy_points or not benchmark_points:
        return []
    ordered_strategy = sorted(strategy_points, key=lambda row: str(row["timestamp"]))
    ordered_benchmark = sorted(benchmark_points, key=lambda row: str(row["timestamp"]))
    benchmark_index = 0
    last_benchmark_equity = float(ordered_benchmark[0]["equity"])
    last_benchmark_close = float(ordered_benchmark[0].get("close") or 0.0)
    aligned = []
    for point in ordered_strategy:
        strategy_ts = str(point["timestamp"])
        while benchmark_index < len(ordered_benchmark) and str(ordered_benchmark[benchmark_index]["timestamp"]) <= strategy_ts:
            last_benchmark_equity = float(ordered_benchmark[benchmark_index]["equity"])
            last_benchmark_close = float(ordered_benchmark[benchmark_index].get("close") or last_benchmark_close)
            benchmark_index += 1
        aligned.append(
            {
                "timestamp": strategy_ts,
                "strategy": float(point["equity"]),
                "benchmark": last_benchmark_equity,
                "benchmark_close": last_benchmark_close,
            }
        )
    return _sample_series(aligned, max_points=max_points)


def _hover_targets_svg(
    *,
    width: int,
    height: int,
    pad: int,
    point_count: int,
    labels: Sequence[str],
) -> str:
    if point_count <= 0:
        return ""
    segments = []
    chart_width = width - pad * 2
    for index, label in enumerate(labels):
        if point_count == 1:
            x = pad
            rect_width = chart_width
        else:
            left_ratio = (index - 0.5) / (point_count - 1)
            right_ratio = (index + 0.5) / (point_count - 1)
            left = pad + max(0.0, left_ratio) * chart_width
            right = pad + min(1.0, right_ratio) * chart_width
            x = left
            rect_width = max(1.0, right - left)
        center_x = pad if point_count == 1 else pad + index / (point_count - 1) * chart_width
        segments.append(
            f"<rect x=\"{x:.1f}\" y=\"{pad:.1f}\" width=\"{rect_width:.1f}\" height=\"{height - pad * 2:.1f}\" fill=\"rgba(0,0,0,0)\" pointer-events=\"all\" style=\"cursor: crosshair;\" data-crosshair-x=\"{center_x:.1f}\" data-tooltip=\"{html.escape(label, quote=True)}\"></rect>"
        )
    return "".join(segments)


def _bar_svg(rows: Sequence[dict[str, Any]], label_key: str, value_key: str) -> str:
    width, height = 900, 260
    pad = 38
    values = [float(row.get(value_key) or 0.0) for row in rows]
    if not values:
        return "<svg viewBox=\"0 0 900 260\"></svg>"
    low, high = min(0.0, min(values)), max(0.0, max(values))
    if high == low:
        high += 1.0
    zero_y = height - pad - (0.0 - low) / (high - low) * (height - pad * 2)
    bar_w = (width - pad * 2) / max(1, len(rows))
    bars = []
    for index, row in enumerate(rows):
        value = float(row.get(value_key) or 0.0)
        x = pad + index * bar_w + 3
        y = height - pad - (value - low) / (high - low) * (height - pad * 2)
        top = min(y, zero_y)
        h = max(1, abs(zero_y - y))
        color = "#117a4c" if value >= 0 else "#b42318"
        bars.append(f"<rect x=\"{x:.1f}\" y=\"{top:.1f}\" width=\"{max(2, bar_w-6):.1f}\" height=\"{h:.1f}\" fill=\"{color}\"/>")
        bars.append(f"<text x=\"{x:.1f}\" y=\"{height-8}\" fill=\"#667085\" font-size=\"10\">{html.escape(str(row.get(label_key)))}</text>")
    return (
        f"<svg viewBox=\"0 0 {width} {height}\" role=\"img\" aria-label=\"年度净收益柱状图\">"
        f"<line x1=\"{pad}\" y1=\"{zero_y:.1f}\" x2=\"{width-pad}\" y2=\"{zero_y:.1f}\" stroke=\"#667085\"/>"
        f"{''.join(bars)}</svg>"
    )


def _candlestick_svg(bars: Sequence[dict[str, Any]], trade: dict[str, Any]) -> str:
    width, height = 720, 320
    pad = 42
    if not bars:
        return f"<svg viewBox=\"0 0 {width} {height}\"></svg>"
    lows = [float(bar["low"]) for bar in bars]
    highs = [float(bar["high"]) for bar in bars]
    low, high = min(lows), max(highs)
    if high == low:
        high += 1.0
    x_step = (width - pad * 2) / max(1, len(bars))
    def y(price: float) -> float:
        return height - pad - (price - low) / (high - low) * (height - pad * 2)
    by_ts = {str(bar["timestamp"]): index for index, bar in enumerate(bars)}
    candles = []
    for index, bar in enumerate(bars):
        x = pad + index * x_step + x_step / 2
        open_y = y(float(bar["open"]))
        close_y = y(float(bar["close"]))
        high_y = y(float(bar["high"]))
        low_y = y(float(bar["low"]))
        color = "#117a4c" if float(bar["close"]) >= float(bar["open"]) else "#b42318"
        candles.append(f"<line x1=\"{x:.1f}\" y1=\"{high_y:.1f}\" x2=\"{x:.1f}\" y2=\"{low_y:.1f}\" stroke=\"{color}\" stroke-width=\"1\"/>")
        candles.append(f"<rect x=\"{x - max(2, x_step*0.32):.1f}\" y=\"{min(open_y, close_y):.1f}\" width=\"{max(2, x_step*0.64):.1f}\" height=\"{max(1, abs(close_y-open_y)):.1f}\" fill=\"{color}\" opacity=\"0.85\"/>")
    markers = []
    for label, ts_key, price_key, color in (
        ("ENTRY", "timestamp", "entry_price", "#1769aa"),
        ("EXIT", "exit_timestamp", "exit_price", "#a15c00"),
    ):
        timestamp = _to_iso(trade.get(ts_key))
        if timestamp not in by_ts:
            continue
        x = pad + by_ts[timestamp] * x_step + x_step / 2
        marker_y = y(float(trade.get(price_key) or 0.0))
        markers.append(f"<circle cx=\"{x:.1f}\" cy=\"{marker_y:.1f}\" r=\"5\" fill=\"{color}\" stroke=\"white\" stroke-width=\"2\"/>")
        markers.append(f"<text x=\"{x + 7:.1f}\" y=\"{marker_y - 7:.1f}\" fill=\"{color}\" font-size=\"11\" font-weight=\"700\">{label}</text>")
    return (
        f"<svg viewBox=\"0 0 {width} {height}\" role=\"img\" aria-label=\"K线入场出场位置\">"
        f"<text x=\"{pad}\" y=\"22\" fill=\"#667085\" font-size=\"12\">{html.escape(str(bars[0]['timestamp']))} 到 {html.escape(str(bars[-1]['timestamp']))}</text>"
        f"<text x=\"{width-pad-90}\" y=\"22\" fill=\"#667085\" font-size=\"12\">{high:.2f}</text>"
        f"<text x=\"{width-pad-90}\" y=\"{height-12}\" fill=\"#667085\" font-size=\"12\">{low:.2f}</text>"
        f"<line x1=\"{pad}\" y1=\"{pad}\" x2=\"{pad}\" y2=\"{height-pad}\" stroke=\"#d9e0ea\"/>"
        f"<line x1=\"{pad}\" y1=\"{height-pad}\" x2=\"{width-pad}\" y2=\"{height-pad}\" stroke=\"#d9e0ea\"/>"
        f"{''.join(candles)}{''.join(markers)}</svg>"
    )


def _sample_series(rows: Sequence[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    if len(rows) <= max_points:
        return list(rows)
    step = max(1, len(rows) // max_points)
    sampled = [row for index, row in enumerate(rows) if index % step == 0]
    if sampled[-1] is not rows[-1]:
        sampled.append(rows[-1])
    return sampled


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _export_payload(payload: dict[str, Any]) -> dict[str, Any]:
    exported = {**payload}
    exported["strategies"] = [_export_strategy(strategy) for strategy in payload.get("strategies", [])]
    return exported


def _export_strategy(strategy: dict[str, Any]) -> dict[str, Any]:
    exported = dict(strategy)
    exported.pop("equity_curve", None)
    benchmark = dict(exported.get("benchmark") or {})
    benchmark.pop("equity_curve", None)
    exported["benchmark"] = benchmark
    return exported


def _to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return datetime.fromisoformat(str(value)).isoformat(sep=" ")


def fmt_usd(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"${float(value):,.0f}"


def fmt_pct(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.2f}%"


def fmt_num(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):,.{digits}f}"


def fmt_int(value: Any) -> str:
    if value is None:
        return "0"
    return f"{int(value):,}"


def fmt_price(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):,.2f}"
