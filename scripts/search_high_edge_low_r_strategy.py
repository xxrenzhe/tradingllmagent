#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace
from itertools import product
from pathlib import Path
from typing import Any, Sequence

import duckdb

from tlm.low_r_regime_basket import (
    ANNUAL_2023_LOW_VOLUME_LOW_R_EDGES,
    TOP_NET_2019_LOW_R_EDGES,
    LowRRegimeBasketConfig,
    RegimeEdge,
    _build_result,
    _ensure_feature_table,
    _json_default,
    _load_feature_bars,
    _load_signals,
    _replay_low_r_exits,
)


FULL_YEARS = tuple(range(2019, 2026))
PARTIAL_YEARS = (2026,)


def main() -> int:
    parser = argparse.ArgumentParser(description="Search high-edge NQ low-R strategies without low-margin fillers.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--date-from", default="2019-01-01")
    parser.add_argument("--date-to", default="2026-04-27")
    parser.add_argument("--min-full-year-trades", type=int, default=1000)
    parser.add_argument("--max-results", type=int, default=80)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/profit_mining/high_edge_low_r_strategy_search.json"),
    )
    args = parser.parse_args()

    base_config = LowRRegimeBasketConfig(
        data_root=args.data_root,
        symbol=args.symbol,
        date_from=args.date_from,
        date_to=args.date_to,
        preset="top_net_2019_low_r",
        flatten_on_date_change=True,
    )
    base_edges = union_edges()
    pattern = str(
        Path(base_config.data_root)
        / "bars"
        / base_config.timeframe
        / base_config.symbol
        / "date=*"
        / "part-000.parquet"
    )

    con = duckdb.connect(":memory:")
    try:
        print("building feature table", flush=True)
        _ensure_feature_table(con, pattern, base_config)
        print("loading bars", flush=True)
        bars = _load_feature_bars(con)
        coverage_days = coverage_days_by_year(bars)
        all_results = []
        single_edge_snapshots = []
        for params in signal_parameter_grid():
            print(f"evaluating params {params}", flush=True)
            config = config_from_params(base_config, {**params, "max_concurrent_positions": 1})
            edges = scaled_edges(base_edges, float(params["take_profit_scale"]))
            all_signals = _load_signals(con, edges, config)
            single_edges = evaluate_single_edges(
                bars=bars,
                all_signals=all_signals,
                edges=edges,
                config=config,
                coverage_days=coverage_days,
                min_full_year_trades=args.min_full_year_trades,
                params=params,
            )
            single_edge_snapshots.extend(single_edges)
            specs = build_high_edge_specs(single_edges)
            for max_positions in (1, 2, 3, 6, 12, 99):
                concurrent_params = {**params, "max_concurrent_positions": max_positions}
                all_results.extend(
                    evaluate_specs(
                        specs=specs,
                        bars=bars,
                        all_signals=all_signals,
                        edges=edges,
                        base_config=config_from_params(base_config, concurrent_params),
                        coverage_days=coverage_days,
                        min_full_year_trades=args.min_full_year_trades,
                        params=concurrent_params,
                    )
                )
    finally:
        con.close()

    all_results.sort(key=sort_key, reverse=True)
    qualified = [row for row in all_results if row["constraints"]["trade_floor_pass"] and row["metrics"]["net_pnl"] > 0]
    stable = sorted(qualified, key=stable_sort_key, reverse=True)
    payload = {
        "schema_version": 1,
        "artifact": "high_edge_low_r_strategy_search",
        "date_from": args.date_from,
        "date_to": args.date_to,
        "constraints": {
            "full_years": list(FULL_YEARS),
            "partial_years": list(PARTIAL_YEARS),
            "min_full_year_trades": args.min_full_year_trades,
            "partial_years_use_annualized_trade_count": True,
            "high_edge_filter": "candidate specs are built only from individually positive/high-PF/high-avg edge groups",
        },
        "candidate_count": len(all_results),
        "qualified_positive_count": len(qualified),
        "best_net": qualified[0] if qualified else (all_results[0] if all_results else None),
        "best_stable": stable[0] if stable else (qualified[0] if qualified else None),
        "top_results": all_results[: args.max_results],
        "top_single_edges": sorted(single_edge_snapshots, key=single_edge_sort_key, reverse=True)[: args.max_results],
        "notes": [
            "No candidate is allowed to include intentionally negative or low-margin filler edges.",
            "Concurrency is selected by backtest over max positions 1, 2, 3, 6, 12, and 99.",
            "2026 is incomplete, so its trade floor uses annualized count.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")
    print(args.output)
    best = payload["best_net"]
    if best:
        metrics = best["metrics"]
        print(
            "best_net",
            "pass=",
            best["constraints"]["trade_floor_pass"],
            "net=",
            round(metrics["net_pnl"], 2),
            "trades=",
            metrics["trade_count"],
            "pf=",
            round(metrics["profit_factor"], 3),
            "dd=",
            round(metrics["max_drawdown"], 2),
            "avg_hold=",
            round(best["avg_hold_bars"], 2),
            "max_pos=",
            best["params"]["max_concurrent_positions"],
            "edges=",
            best["edge_count"],
            "spec=",
            best["spec"]["name"],
        )
    return 0


def union_edges() -> tuple[RegimeEdge, ...]:
    return tuple(dict.fromkeys((*TOP_NET_2019_LOW_R_EDGES, *ANNUAL_2023_LOW_VOLUME_LOW_R_EDGES)))


def signal_parameter_grid() -> list[dict[str, Any]]:
    rows = []
    for max_hold, stop_multiple, max_stop, tp_scale in product(
        (60, 120, 300),
        (6.0, 10.0),
        (90.0,),
        (0.5, 0.75, 1.0),
    ):
        rows.append(
            {
                "max_hold_minutes": max_hold,
                "stop_range_multiple": stop_multiple,
                "min_stop_points": 8.0,
                "max_stop_points": max_stop,
                "take_profit_scale": tp_scale,
                "flatten_on_date_change": True,
            }
        )
    return rows


def config_from_params(config: LowRRegimeBasketConfig, params: dict[str, Any]) -> LowRRegimeBasketConfig:
    return replace(
        config,
        max_hold_minutes=int(params["max_hold_minutes"]),
        stop_range_multiple=float(params["stop_range_multiple"]),
        min_stop_points=float(params["min_stop_points"]),
        max_stop_points=float(params["max_stop_points"]),
        max_concurrent_positions=int(params["max_concurrent_positions"]),
        flatten_on_date_change=bool(params["flatten_on_date_change"]),
    )


def evaluate_single_edges(
    bars: Sequence[dict],
    all_signals: Sequence[dict],
    edges: Sequence[RegimeEdge],
    config: LowRRegimeBasketConfig,
    coverage_days: dict[int, int],
    min_full_year_trades: int,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = []
    single_config = replace(config, max_concurrent_positions=1)
    for index, edge in enumerate(edges):
        signals = remap_signals(all_signals, (index,))
        result = replay_result(
            label=f"single_{index}",
            spec={"name": f"single_{index}", "edge_indexes": (index,)},
            bars=bars,
            signals=signals,
            edges=(edge,),
            config=single_config,
            coverage_days=coverage_days,
            min_full_year_trades=min_full_year_trades,
            params={**params, "max_concurrent_positions": 1},
        )
        result["source_edge_index"] = index
        rows.append(result)
    return rows


def build_high_edge_specs(single_edges: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    strict = [row for row in single_edges if is_high_edge(row, min_avg=80.0, min_pf=1.08)]
    medium = [row for row in single_edges if is_high_edge(row, min_avg=50.0, min_pf=1.05)]
    positive = [row for row in single_edges if is_high_edge(row, min_avg=25.0, min_pf=1.02)]
    robust = [
        row
        for row in single_edges
        if row["metrics"]["net_pnl"] > 0
        and row["constraints"]["positive_years"] >= 5
        and float(row["metrics"].get("profit_factor") or 0.0) >= 1.02
    ]
    groups = {
        "strict_high_edge": strict,
        "medium_high_edge": medium,
        "positive_edge": positive,
        "robust_positive_edge": robust,
    }
    specs: dict[tuple[int, ...], dict[str, Any]] = {}
    for group_name, rows in groups.items():
        ranked = sorted(rows, key=single_edge_sort_key, reverse=True)
        if not ranked:
            continue
        for size in (1, 2, 3, 5, 8, 12, 16, 19):
            prefix = ranked[:size]
            indexes = tuple(int(row["source_edge_index"]) for row in prefix)
            if indexes:
                specs[indexes] = {"name": f"{group_name}_top_{len(indexes)}", "edge_indexes": indexes}
        for scan_type in sorted({row["edges"][0]["scan_type"] for row in ranked}):
            scan_rows = [row for row in ranked if row["edges"][0]["scan_type"] == scan_type]
            indexes = tuple(int(row["source_edge_index"]) for row in scan_rows)
            if indexes:
                specs[indexes] = {"name": f"{group_name}_{scan_type}", "edge_indexes": indexes}
    return list(specs.values())


def is_high_edge(row: dict[str, Any], min_avg: float, min_pf: float) -> bool:
    metrics = row["metrics"]
    return (
        float(metrics["net_pnl"]) > 0
        and float(metrics["avg_trade_net_pnl"]) >= min_avg
        and float(metrics.get("profit_factor") or 0.0) >= min_pf
    )


def evaluate_specs(
    specs: Sequence[dict[str, Any]],
    bars: Sequence[dict],
    all_signals: Sequence[dict],
    edges: Sequence[RegimeEdge],
    base_config: LowRRegimeBasketConfig,
    coverage_days: dict[int, int],
    min_full_year_trades: int,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = []
    for spec in specs:
        edge_indexes = tuple(int(index) for index in spec["edge_indexes"])
        selected_edges = tuple(edges[index] for index in edge_indexes)
        selected_signals = remap_signals(all_signals, edge_indexes)
        rows.append(
            replay_result(
                label=spec["name"],
                spec=spec,
                bars=bars,
                signals=selected_signals,
                edges=selected_edges,
                config=base_config,
                coverage_days=coverage_days,
                min_full_year_trades=min_full_year_trades,
                params=params,
            )
        )
    return rows


def remap_signals(signals: Sequence[dict], edge_indexes: Sequence[int]) -> list[dict]:
    index_map = {old_index: new_index for new_index, old_index in enumerate(edge_indexes)}
    return [
        {**signal, "edge_index": index_map[int(signal["edge_index"])]}
        for signal in signals
        if int(signal["edge_index"]) in index_map
    ]


def replay_result(
    label: str,
    spec: dict[str, Any],
    bars: Sequence[dict],
    signals: Sequence[dict],
    edges: Sequence[RegimeEdge],
    config: LowRRegimeBasketConfig,
    coverage_days: dict[int, int],
    min_full_year_trades: int,
    params: dict[str, Any],
) -> dict[str, Any]:
    trades = _replay_low_r_exits(bars, signals, edges, config)
    result = _build_result(bars, signals, trades, edges, config)
    hold_values = [trade.bars_held for trade in trades]
    yearly = result["yearly_results"]
    return {
        "label": label,
        "spec": spec,
        "edge_count": len(edges),
        "params": dict(params),
        "metrics": result["metrics"],
        "avg_hold_bars": sum(hold_values) / len(hold_values) if hold_values else 0.0,
        "median_hold_bars": sorted(hold_values)[len(hold_values) // 2] if hold_values else 0,
        "edge_summary": result["edge_summary"],
        "exit_reasons": result["exit_reasons"],
        "yearly_results": yearly,
        "constraints": constraint_summary(yearly, min_full_year_trades, coverage_days),
        "edges": [asdict(edge) for edge in edges],
    }


def scaled_edges(edges: Sequence[RegimeEdge], scale: float) -> tuple[RegimeEdge, ...]:
    return tuple(replace(edge, take_profit_r=max(0.25, min(edge.take_profit_r * scale, 1.5))) for edge in edges)


def coverage_days_by_year(bars: Sequence[dict]) -> dict[int, int]:
    by_year: dict[int, set] = {}
    for bar in bars:
        timestamp = bar["timestamp"]
        by_year.setdefault(timestamp.year, set()).add(timestamp.date())
    return {year: len(days) for year, days in by_year.items()}


def constraint_summary(
    yearly: Sequence[dict],
    min_full_year_trades: int,
    coverage_days: dict[int, int],
) -> dict[str, Any]:
    by_year = {int(row["year"]): row for row in yearly}
    full_counts = [int(by_year.get(year, {}).get("trade_count") or 0) for year in FULL_YEARS]
    partial_annualized = [
        annualized_trade_count(
            int(by_year.get(year, {}).get("trade_count") or 0),
            coverage_days.get(year, 365),
        )
        for year in PARTIAL_YEARS
    ]
    pnl_values = [float(row.get("net_pnl") or 0.0) for row in yearly]
    return {
        "full_year_min_trades": min(full_counts) if full_counts else 0,
        "partial_year_min_annualized_trades": min(partial_annualized) if partial_annualized else 0.0,
        "trade_floor_pass": all(value > min_full_year_trades for value in full_counts)
        and all(value > min_full_year_trades for value in partial_annualized),
        "positive_years": sum(1 for value in pnl_values if value > 0),
        "worst_year_pnl": min(pnl_values) if pnl_values else 0.0,
    }


def annualized_trade_count(trade_count: int, covered_days: int) -> float:
    if covered_days <= 0:
        return 0.0
    return trade_count * 365.0 / min(covered_days, 365)


def single_edge_sort_key(row: dict[str, Any]) -> tuple[float, ...]:
    metrics = row["metrics"]
    return (
        float(metrics["avg_trade_net_pnl"]),
        float(metrics.get("profit_factor") or 0.0),
        float(metrics["net_pnl"]),
        float(row["constraints"]["positive_years"]),
        float(row["constraints"]["full_year_min_trades"]),
    )


def sort_key(row: dict[str, Any]) -> tuple[float, ...]:
    metrics = row["metrics"]
    constraints = row["constraints"]
    return (
        1.0 if constraints["trade_floor_pass"] and metrics["net_pnl"] > 0 else 0.0,
        float(metrics["net_pnl"]),
        float(constraints["positive_years"]),
        float(metrics.get("net_pnl_to_max_drawdown") or 0.0),
        float(metrics.get("profit_factor") or 0.0),
        -float(row["avg_hold_bars"]),
    )


def stable_sort_key(row: dict[str, Any]) -> tuple[float, ...]:
    metrics = row["metrics"]
    constraints = row["constraints"]
    return (
        1.0 if constraints["trade_floor_pass"] and metrics["net_pnl"] > 0 else 0.0,
        float(constraints["positive_years"]),
        float(constraints["worst_year_pnl"]),
        float(metrics.get("net_pnl_to_max_drawdown") or 0.0),
        float(metrics.get("profit_factor") or 0.0),
        float(metrics["net_pnl"]),
        -float(row["avg_hold_bars"]),
    )


if __name__ == "__main__":
    raise SystemExit(main())
