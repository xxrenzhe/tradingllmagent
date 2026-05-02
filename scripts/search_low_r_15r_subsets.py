#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace
from itertools import combinations
from pathlib import Path
import sys
from typing import Any, Sequence

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tlm.low_r_regime_basket import (
    LowRRegimeBasketConfig,
    RegimeEdge,
    _build_result,
    _ensure_feature_table,
    _json_default,
    _load_feature_bars,
    _load_signals,
    _replay_low_r_exits,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Search forced-1.5R low-R edge subsets for 55%+ win rate.")
    parser.add_argument("--baseline", type=Path, default=Path("experiments/profit_mining/low_r_high_frequency_15r_forced_baseline_2019_2026.json"))
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--date-from", default="2019-01-01")
    parser.add_argument("--date-to", default="2026-04-27")
    parser.add_argument("--min-win-rate", type=float, default=0.55)
    parser.add_argument("--min-year-trades", type=int, default=1000)
    parser.add_argument("--min-positive-year-ratio", type=float, default=1.0)
    parser.add_argument("--max-subset-size", type=int, default=10)
    parser.add_argument("--top-approx", type=int, default=80)
    parser.add_argument("--output", type=Path, default=Path("reports/low_r_15r_subset_search_55wr_2026-05-03.json"))
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
        preset="low_r_15r_subset_search",
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

    print("evaluating single edges", flush=True)
    single_rows = []
    for index, edge in enumerate(edges):
        result = exact_result_for_subset(
            bars=bars,
            all_signals=all_signals,
            all_edges=edges,
            indexes=(index,),
            config=replace(config, max_concurrent_positions=1),
            coverage_days=coverage_days,
            min_year_trades=args.min_year_trades,
        )
        single_rows.append({**result, "source_edge_index": index})

    print("enumerating approximate subsets", flush=True)
    approximate = enumerate_subsets(
        single_rows,
        min_win_rate=args.min_win_rate,
        min_year_trades=args.min_year_trades,
        min_positive_year_ratio=args.min_positive_year_ratio,
        max_subset_size=args.max_subset_size,
        top_n=args.top_approx,
    )
    print(f"exact replay for {len(approximate)} subsets", flush=True)
    exact_rows = []
    for row in approximate:
        exact_rows.append(
            exact_result_for_subset(
                bars=bars,
                all_signals=all_signals,
                all_edges=edges,
                indexes=tuple(row["edge_indexes"]),
                config=config,
                coverage_days=coverage_days,
                min_year_trades=args.min_year_trades,
            )
        )
    exact_rows.sort(key=lambda row: exact_sort_key(row, args.min_win_rate), reverse=True)
    passing = [row for row in exact_rows if objective_passed(row, args.min_win_rate, args.min_positive_year_ratio)]
    payload = {
        "artifact": "low_r_15r_subset_search_55wr",
        "schema_version": 1,
        "baseline": str(args.baseline),
        "date_from": args.date_from,
        "date_to": args.date_to,
        "target": {
            "min_win_rate": args.min_win_rate,
            "min_year_trades": args.min_year_trades,
            "min_positive_year_ratio": args.min_positive_year_ratio,
            "forced_take_profit_r": 1.5,
        },
        "search": {
            "edge_count": len(edges),
            "max_subset_size": args.max_subset_size,
            "approximate_candidate_count": len(approximate),
            "exact_candidate_count": len(exact_rows),
            "approximation_note": "Subset prefilter sums independently replayed single-edge stats, then exact-replays top subsets with original concurrency rules.",
        },
        "decision": {
            "passed": bool(passing),
            "reason": None if passing else "No exact-replayed forced-1.5R low-R subset reached all 55% win-rate, trade-floor, and positive-year gates.",
            "passing_count": len(passing),
        },
        "best_exact": exact_rows[0] if exact_rows else None,
        "passing_exact": passing[:20],
        "top_exact": exact_rows[:20],
        "top_single_edges": sorted(single_rows, key=lambda row: single_sort_key(row), reverse=True),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), **payload["decision"]}, indent=2))
    return 0


def exact_result_for_subset(
    *,
    bars: Sequence[dict[str, Any]],
    all_signals: Sequence[dict[str, Any]],
    all_edges: Sequence[RegimeEdge],
    indexes: Sequence[int],
    config: LowRRegimeBasketConfig,
    coverage_days: dict[int, int],
    min_year_trades: int,
) -> dict[str, Any]:
    index_map = {old: new for new, old in enumerate(indexes)}
    signals = [
        {**signal, "edge_index": index_map[int(signal["edge_index"])]}
        for signal in all_signals
        if int(signal["edge_index"]) in index_map
    ]
    edges = tuple(all_edges[index] for index in indexes)
    trades = _replay_low_r_exits(bars, signals, edges, config)
    result = _build_result(bars, signals, trades, edges, config)
    result.pop("trades", None)
    result["edge_indexes"] = list(indexes)
    result["constraints"] = constraint_summary(result["yearly_results"], min_year_trades, coverage_days)
    result["objective_gate"] = {
        "win_rate": result["metrics"].get("win_rate"),
        "trade_floor_passed": result["constraints"]["full_or_annualized_years_gt_min_trades"],
        "positive_year_ratio": positive_year_ratio(result["yearly_results"]),
    }
    return result


def enumerate_subsets(
    single_rows: Sequence[dict[str, Any]],
    *,
    min_win_rate: float,
    min_year_trades: int,
    min_positive_year_ratio: float,
    max_subset_size: int,
    top_n: int,
) -> list[dict[str, Any]]:
    rows = []
    indexes = [int(row["source_edge_index"]) for row in single_rows]
    by_index = {int(row["source_edge_index"]): row for row in single_rows}
    for size in range(1, min(max_subset_size, len(indexes)) + 1):
        for subset in combinations(indexes, size):
            row = aggregate_subset(subset, by_index, min_year_trades)
            row["objective_passed"] = (
                row["win_rate"] >= min_win_rate
                and row["constraints"]["full_or_annualized_years_gt_min_trades"]
                and row["positive_year_ratio"] >= min_positive_year_ratio
                and row["net_pnl"] > 0
            )
            rows.append(row)
    rows.sort(
        key=lambda row: (
            1.0 if row["objective_passed"] else 0.0,
            row["win_rate"],
            row["positive_year_ratio"],
            row["net_pnl"],
            -row["edge_count"],
        ),
        reverse=True,
    )
    return rows[:top_n]


def aggregate_subset(subset: Sequence[int], by_index: dict[int, dict[str, Any]], min_year_trades: int) -> dict[str, Any]:
    trade_count = 0
    winning_count = 0
    net_pnl = 0.0
    yearly: dict[int, dict[str, float]] = {}
    for index in subset:
        row = by_index[index]
        metrics = row["metrics"]
        trade_count += int(metrics.get("trade_count") or 0)
        winning_count += int(metrics.get("winning_trade_count") or 0)
        net_pnl += float(metrics.get("net_pnl") or 0.0)
        for year_row in row.get("yearly_results", []):
            year = int(year_row["year"])
            bucket = yearly.setdefault(year, {"trade_count": 0.0, "net_pnl": 0.0, "winning_trade_count": 0.0})
            bucket["trade_count"] += float(year_row.get("trade_count") or 0.0)
            bucket["net_pnl"] += float(year_row.get("net_pnl") or 0.0)
            bucket["winning_trade_count"] += float(year_row.get("winning_trade_count") or 0.0)
    yearly_rows = [
        {"year": year, **values, "win_rate": values["winning_trade_count"] / values["trade_count"] if values["trade_count"] else 0.0}
        for year, values in sorted(yearly.items())
    ]
    return {
        "edge_indexes": list(subset),
        "edge_count": len(subset),
        "trade_count": trade_count,
        "winning_trade_count": winning_count,
        "win_rate": winning_count / trade_count if trade_count else 0.0,
        "net_pnl": net_pnl,
        "yearly_results": yearly_rows,
        "positive_year_ratio": positive_year_ratio(yearly_rows),
        "constraints": constraint_summary(yearly_rows, min_year_trades, {int(row["year"]): 365 for row in yearly_rows}),
    }


def objective_passed(row: dict[str, Any], min_win_rate: float, min_positive_year_ratio: float) -> bool:
    return (
        float(row["metrics"].get("win_rate") or 0.0) >= min_win_rate
        and float(row["metrics"].get("net_pnl") or 0.0) > 0
        and row["constraints"]["full_or_annualized_years_gt_min_trades"]
        and positive_year_ratio(row["yearly_results"]) >= min_positive_year_ratio
    )


def positive_year_ratio(yearly: Sequence[dict[str, Any]]) -> float:
    return sum(1 for row in yearly if float(row.get("net_pnl") or 0.0) > 0) / len(yearly) if yearly else 0.0


def coverage_days_by_year(bars: Sequence[dict[str, Any]]) -> dict[int, int]:
    by_year: dict[int, set] = {}
    for bar in bars:
        timestamp = bar["timestamp"]
        by_year.setdefault(timestamp.year, set()).add(timestamp.date())
    return {year: len(days) for year, days in by_year.items()}


def constraint_summary(
    yearly: Sequence[dict[str, Any]],
    min_year_trades: int,
    coverage_days: dict[int, int],
) -> dict[str, Any]:
    trade_counts = [int(row.get("trade_count") or 0) for row in yearly]
    pnl_values = [float(row.get("net_pnl") or 0.0) for row in yearly]
    annualized_counts = [
        annualized_trade_count(int(row.get("trade_count") or 0), coverage_days.get(int(row["year"]), 365))
        for row in yearly
    ]
    return {
        "min_year_trades": min(trade_counts) if trade_counts else 0,
        "min_annualized_year_trades": min(annualized_counts) if annualized_counts else 0.0,
        "full_or_annualized_years_gt_min_trades": bool(annualized_counts) and all(value > min_year_trades for value in annualized_counts),
        "positive_years": sum(1 for value in pnl_values if value > 0),
        "worst_year_pnl": min(pnl_values) if pnl_values else 0.0,
    }


def annualized_trade_count(trade_count: int, covered_days: int) -> float:
    if covered_days <= 0:
        return 0.0
    return trade_count * 365.0 / min(covered_days, 365)


def exact_sort_key(row: dict[str, Any], min_win_rate: float) -> tuple[float, ...]:
    metrics = row["metrics"]
    return (
        1.0 if objective_passed(row, min_win_rate, 1.0) else 0.0,
        float(metrics.get("win_rate") or 0.0),
        positive_year_ratio(row["yearly_results"]),
        float(metrics.get("net_pnl") or 0.0),
        float(metrics.get("profit_factor") or 0.0),
        -float(row["edge_count"]),
    )


def single_sort_key(row: dict[str, Any]) -> tuple[float, ...]:
    metrics = row["metrics"]
    return (
        float(metrics.get("win_rate") or 0.0),
        positive_year_ratio(row["yearly_results"]),
        float(metrics.get("net_pnl") or 0.0),
        float(metrics.get("trade_count") or 0.0),
    )


if __name__ == "__main__":
    raise SystemExit(main())
