#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
import sys
from typing import Any, Sequence

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.search_low_r_15r_subsets import (
    coverage_days_by_year,
    enumerate_subsets,
    exact_result_for_subset,
    exact_sort_key,
    objective_passed,
    positive_year_ratio,
    single_sort_key,
)
from tlm.low_r_regime_basket import (
    LowRRegimeBasketConfig,
    RegimeEdge,
    _ensure_feature_table,
    _json_default,
    _load_feature_bars,
    _load_signals,
)


DEFAULT_FOLDS = (
    ((2019, 2020), 2021),
    ((2019, 2020, 2021), 2022),
    ((2019, 2020, 2021, 2022), 2023),
    ((2019, 2020, 2021, 2022, 2023), 2024),
    ((2019, 2020, 2021, 2022, 2023, 2024), 2025),
    ((2019, 2020, 2021, 2022, 2023, 2024, 2025), 2026),
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk-forward validate forced-1.5R low-R edge subsets.")
    parser.add_argument("--baseline", type=Path, default=Path("experiments/profit_mining/low_r_high_frequency_15r_forced_baseline_2019_2026.json"))
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--date-from", default="2019-01-01")
    parser.add_argument("--date-to", default="2026-04-27")
    parser.add_argument("--min-win-rate", type=float, default=0.55)
    parser.add_argument("--min-train-win-rate", type=float, default=0.55)
    parser.add_argument("--min-year-trades", type=int, default=1000)
    parser.add_argument("--min-positive-year-ratio", type=float, default=1.0)
    parser.add_argument("--max-subset-size", type=int, default=10)
    parser.add_argument("--top-approx", type=int, default=80)
    parser.add_argument("--output", type=Path, default=Path("reports/low_r_15r_subset_walk_forward_55wr_2026-05-03.json"))
    args = parser.parse_args(argv)

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    baseline_row = baseline["best_full_or_annualized_years"]
    edges = tuple(RegimeEdge(**edge) for edge in baseline_row["edges"])
    params = baseline_row["params"]
    config = LowRRegimeBasketConfig(
        data_root=args.data_root,
        symbol=args.symbol,
        date_from=args.date_from,
        date_to=args.date_to,
        preset="low_r_15r_subset_walk_forward",
        stop_range_multiple=float(params["stop_range_multiple"]),
        min_stop_points=float(params["min_stop_points"]),
        max_stop_points=float(params["max_stop_points"]),
        max_hold_minutes=int(params["max_hold_minutes"]),
        max_concurrent_positions=int(params["max_concurrent_positions"]),
        flatten_on_date_change=not bool(params.get("allow_overnight", False)),
    )
    pattern = str(Path(args.data_root) / "bars" / config.timeframe / args.symbol / "date=*" / "part-000.parquet")
    con = duckdb.connect(":memory:")
    try:
        print("building feature table", flush=True)
        _ensure_feature_table(con, pattern, config)
        print("loading bars", flush=True)
        bars = _load_feature_bars(con)
        coverage_days = coverage_days_by_year(bars)
        print(f"loading signals for {len(edges)} edges", flush=True)
        all_signals = _load_signals(con, edges, config)
    finally:
        con.close()

    folds = []
    previous_indexes: tuple[int, ...] = ()
    for train_years, test_year in DEFAULT_FOLDS:
        print(f"running fold train={train_years} test={test_year}", flush=True)
        fold = run_fold(
            bars=bars,
            all_signals=all_signals,
            edges=edges,
            config=config,
            coverage_days=coverage_days,
            train_years=train_years,
            test_year=test_year,
            min_win_rate=args.min_win_rate,
            min_train_win_rate=args.min_train_win_rate,
            min_year_trades=args.min_year_trades,
            min_positive_year_ratio=args.min_positive_year_ratio,
            max_subset_size=args.max_subset_size,
            top_approx=args.top_approx,
            previous_indexes=previous_indexes,
        )
        previous_indexes = tuple(fold.get("selected_edge_indexes") or previous_indexes)
        folds.append(fold)
        print(
            f"finished fold test={test_year} status={fold['status']} "
            f"selected={fold.get('selected_edge_indexes')}",
            flush=True,
        )

    summary = summarize_walk_forward(
        folds,
        min_win_rate=args.min_win_rate,
        min_year_trades=args.min_year_trades,
    )
    payload = {
        "artifact": "low_r_15r_subset_walk_forward_55wr",
        "schema_version": 1,
        "baseline": str(args.baseline),
        "date_from": args.date_from,
        "date_to": args.date_to,
        "target": {
            "min_win_rate": args.min_win_rate,
            "min_train_win_rate": args.min_train_win_rate,
            "min_year_trades": args.min_year_trades,
            "min_positive_year_ratio": args.min_positive_year_ratio,
            "forced_take_profit_r": 1.5,
        },
        "method": {
            "folds": [{"train_years": list(train_years), "test_year": test_year} for train_years, test_year in DEFAULT_FOLDS],
            "selection": "Each fold evaluates and selects low-R edge subsets only on train years, then exact-replays the selected subset on the next unseen test year.",
            "edge_count": len(edges),
            "max_subset_size": args.max_subset_size,
            "top_approx": args.top_approx,
            "approximation_note": "Train-only subset prefilter sums independently replayed single-edge train stats; selected candidates are exact-replayed with original concurrency rules before the next-year test replay.",
        },
        "summary": summary,
        "folds": folds,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), **summary["decision"]}, indent=2))
    return 0


def run_fold(
    *,
    bars: Sequence[dict[str, Any]],
    all_signals: Sequence[dict[str, Any]],
    edges: Sequence[RegimeEdge],
    config: LowRRegimeBasketConfig,
    coverage_days: dict[int, int],
    train_years: Sequence[int],
    test_year: int,
    min_win_rate: float,
    min_train_win_rate: float,
    min_year_trades: int,
    min_positive_year_ratio: float,
    max_subset_size: int,
    top_approx: int,
    previous_indexes: Sequence[int],
) -> dict[str, Any]:
    train_signals = filter_signals_by_entry_years(all_signals, train_years)
    test_signals = filter_signals_by_entry_years(all_signals, (test_year,))
    train_bars = filter_bars_by_years(bars, train_years)
    test_bars = filter_bars_by_years(bars, (test_year,))
    single_rows = []
    for index, _edge in enumerate(edges):
        result = exact_result_for_subset(
            bars=train_bars,
            all_signals=train_signals,
            all_edges=edges,
            indexes=(index,),
            config=replace(config, max_concurrent_positions=1),
            coverage_days=coverage_days,
            min_year_trades=min_year_trades,
        )
        result["source_edge_index"] = index
        single_rows.append(result)
    approximate = enumerate_subsets(
        single_rows,
        min_win_rate=min_train_win_rate,
        min_year_trades=min_year_trades,
        min_positive_year_ratio=min_positive_year_ratio,
        max_subset_size=max_subset_size,
        top_n=top_approx,
    )
    exact_train_rows = []
    for row in approximate:
        exact_train_rows.append(
            exact_result_for_subset(
                bars=train_bars,
                all_signals=train_signals,
                all_edges=edges,
                indexes=tuple(row["edge_indexes"]),
                config=config,
                coverage_days=coverage_days,
                min_year_trades=min_year_trades,
            )
        )
    exact_train_rows.sort(key=lambda row: train_sort_key(row, min_train_win_rate, train_years), reverse=True)
    selected_train = next(
        (row for row in exact_train_rows if train_gate_passed(row, min_train_win_rate, min_positive_year_ratio, train_years)),
        exact_train_rows[0] if exact_train_rows else None,
    )
    if selected_train is None:
        return {
            "train_years": list(train_years),
            "test_year": test_year,
            "status": "no_train_subset",
            "train_signal_count": len(train_signals),
            "test_signal_count": len(test_signals),
            "single_edge_count": len(single_rows),
            "approximate_candidate_count": 0,
            "exact_train_candidate_count": 0,
        }

    selected_indexes = tuple(int(index) for index in selected_train["edge_indexes"])
    test_result = exact_result_for_subset(
        bars=test_bars,
        all_signals=test_signals,
        all_edges=edges,
        indexes=selected_indexes,
        config=config,
        coverage_days=coverage_days,
        min_year_trades=min_year_trades,
    )
    test_gate = test_year_gate(test_result, test_year, min_win_rate, min_year_trades, coverage_days)
    return {
        "train_years": list(train_years),
        "test_year": test_year,
        "status": "ok",
        "train_signal_count": len(train_signals),
        "test_signal_count": len(test_signals),
        "single_edge_count": len(single_rows),
        "approximate_candidate_count": len(approximate),
        "exact_train_candidate_count": len(exact_train_rows),
        "selected_edge_indexes": list(selected_indexes),
        "selected_edge_turnover": selected_edge_turnover(previous_indexes, selected_indexes),
        "selected_edges": [edge_to_dict(edges[index]) for index in selected_indexes],
        "train_metrics": compact_result(selected_train),
        "train_gate_report": train_gate_report(selected_train, train_years, min_train_win_rate, min_positive_year_ratio),
        "test_metrics": compact_result(test_result),
        "test_yearly_result": compact_year(test_result, test_year),
        "test_gate_report": test_gate,
        "top_train_exact": [compact_candidate(row) for row in exact_train_rows[:10]],
        "top_train_single_edges": [compact_single(row) for row in sorted(single_rows, key=single_sort_key, reverse=True)[:10]],
    }


def filter_signals_by_entry_years(signals: Sequence[dict[str, Any]], years: Sequence[int]) -> list[dict[str, Any]]:
    year_set = {int(year) for year in years}
    return [signal for signal in signals if signal["entry_time"].year in year_set]


def filter_bars_by_years(bars: Sequence[dict[str, Any]], years: Sequence[int]) -> list[dict[str, Any]]:
    year_set = {int(year) for year in years}
    return [bar for bar in bars if bar["timestamp"].year in year_set]


def train_gate_passed(
    row: dict[str, Any],
    min_train_win_rate: float,
    min_positive_year_ratio: float,
    train_years: Sequence[int],
) -> bool:
    years_with_trades = {int(year["year"]) for year in row.get("yearly_results", [])}
    return (
        set(train_years).issubset(years_with_trades)
        and objective_passed(row, min_train_win_rate, min_positive_year_ratio)
    )


def train_sort_key(row: dict[str, Any], min_train_win_rate: float, train_years: Sequence[int]) -> tuple[float, ...]:
    metrics = row["metrics"]
    years_with_trades = {int(year["year"]) for year in row.get("yearly_results", [])}
    return (
        1.0 if set(train_years).issubset(years_with_trades) else 0.0,
        *exact_sort_key(row, min_train_win_rate),
        float(metrics.get("trade_count") or 0.0),
    )


def train_gate_report(
    row: dict[str, Any],
    train_years: Sequence[int],
    min_train_win_rate: float,
    min_positive_year_ratio: float,
) -> dict[str, Any]:
    years = {int(year["year"]): year for year in row.get("yearly_results", [])}
    missing_years = [int(year) for year in train_years if int(year) not in years]
    return {
        "passed": train_gate_passed(row, min_train_win_rate, min_positive_year_ratio, train_years),
        "missing_trade_years": missing_years,
        "win_rate": row["metrics"].get("win_rate"),
        "net_pnl": row["metrics"].get("net_pnl"),
        "positive_year_ratio": positive_year_ratio(row.get("yearly_results", [])),
        "constraints": row.get("constraints", {}),
    }


def test_year_gate(
    row: dict[str, Any],
    test_year: int,
    min_win_rate: float,
    min_year_trades: int,
    coverage_days: dict[int, int],
) -> dict[str, Any]:
    year = compact_year(row, test_year)
    trade_count = int(year.get("trade_count") or 0)
    covered_days = coverage_days.get(test_year, 365)
    annualized_trades = trade_count * 365.0 / min(covered_days, 365) if covered_days > 0 else 0.0
    win_rate = float(year.get("win_rate") or 0.0)
    net_pnl = float(year.get("net_pnl") or 0.0)
    return {
        "passed": bool(trade_count) and annualized_trades > min_year_trades and win_rate >= min_win_rate and net_pnl > 0,
        "test_year": int(test_year),
        "trade_count": trade_count,
        "annualized_trade_count": annualized_trades,
        "win_rate": win_rate,
        "net_pnl": net_pnl,
        "min_win_rate": min_win_rate,
        "min_year_trades": min_year_trades,
    }


def summarize_walk_forward(
    folds: Sequence[dict[str, Any]],
    *,
    min_win_rate: float,
    min_year_trades: int,
) -> dict[str, Any]:
    ok_folds = [fold for fold in folds if fold.get("status") == "ok"]
    test_gates = [fold["test_gate_report"] for fold in ok_folds]
    total_trades = sum(int(fold["test_metrics"].get("trade_count") or 0) for fold in ok_folds)
    winning_trades = sum(int(fold["test_metrics"].get("winning_trade_count") or 0) for fold in ok_folds)
    total_net = sum(float(fold["test_metrics"].get("net_pnl") or 0.0) for fold in ok_folds)
    min_test_win_rate = min((float(gate.get("win_rate") or 0.0) for gate in test_gates), default=0.0)
    min_test_pnl = min((float(gate.get("net_pnl") or 0.0) for gate in test_gates), default=0.0)
    passed = len(ok_folds) == len(folds) and bool(test_gates) and all(gate["passed"] for gate in test_gates) and total_net > 0
    return {
        "oos_total_trades": total_trades,
        "oos_winning_trades": winning_trades,
        "oos_win_rate": winning_trades / total_trades if total_trades else 0.0,
        "oos_total_net_pnl": total_net,
        "oos_min_year_win_rate": min_test_win_rate,
        "oos_min_year_pnl": min_test_pnl,
        "ok_fold_count": len(ok_folds),
        "positive_test_years": sum(1 for gate in test_gates if float(gate.get("net_pnl") or 0.0) > 0),
        "win_rate_gate_years": sum(1 for gate in test_gates if float(gate.get("win_rate") or 0.0) >= min_win_rate),
        "trade_floor_years": sum(1 for gate in test_gates if float(gate.get("annualized_trade_count") or 0.0) > min_year_trades),
        "decision": {
            "passed": passed,
            "reason": None if passed else "No train-only selected forced-1.5R low-R subset passed every next-year 55% win-rate, trade-floor, and positive-PnL gate.",
            "failed_test_years": [gate["test_year"] for gate in test_gates if not gate["passed"]],
        },
    }


def compact_result(row: dict[str, Any]) -> dict[str, Any]:
    metrics = row.get("metrics", {})
    return {
        "trade_count": metrics.get("trade_count"),
        "winning_trade_count": metrics.get("winning_trade_count"),
        "win_rate": metrics.get("win_rate"),
        "net_pnl": metrics.get("net_pnl"),
        "gross_profit": metrics.get("gross_profit"),
        "gross_loss": metrics.get("gross_loss"),
        "profit_factor": metrics.get("profit_factor"),
        "max_drawdown": metrics.get("max_drawdown"),
    }


def compact_year(row: dict[str, Any], year: int) -> dict[str, Any]:
    for year_row in row.get("yearly_results", []):
        if int(year_row["year"]) == int(year):
            return {
                "year": int(year),
                "trade_count": year_row.get("trade_count"),
                "winning_trade_count": year_row.get("winning_trade_count"),
                "win_rate": year_row.get("win_rate"),
                "net_pnl": year_row.get("net_pnl"),
                "profit_factor": year_row.get("profit_factor"),
                "max_drawdown": year_row.get("max_drawdown"),
            }
    return {"year": int(year), "trade_count": 0, "winning_trade_count": 0, "win_rate": 0.0, "net_pnl": 0.0}


def compact_candidate(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "edge_indexes": row.get("edge_indexes"),
        "edge_count": row.get("edge_count"),
        "metrics": compact_result(row),
        "objective_gate": row.get("objective_gate"),
        "constraints": row.get("constraints"),
    }


def compact_single(row: dict[str, Any]) -> dict[str, Any]:
    candidate = compact_candidate(row)
    candidate["source_edge_index"] = row.get("source_edge_index")
    return candidate


def selected_edge_turnover(previous: Sequence[int], current: Sequence[int]) -> dict[str, Any]:
    previous_set = set(previous)
    current_set = set(current)
    return {
        "added": sorted(current_set - previous_set),
        "removed": sorted(previous_set - current_set),
        "retained": sorted(previous_set & current_set),
    }


def edge_to_dict(edge: RegimeEdge) -> dict[str, Any]:
    return {
        "scan_type": edge.scan_type,
        "direction_label": edge.direction_label,
        "horizon_minutes": edge.horizon_minutes,
        "session_bucket": edge.session_bucket,
        "dow": edge.dow,
        "trend_bin": edge.trend_bin,
        "volume_bin": edge.volume_bin,
        "range_bin": edge.range_bin,
        "take_profit_r": edge.take_profit_r,
    }


if __name__ == "__main__":
    raise SystemExit(main())
