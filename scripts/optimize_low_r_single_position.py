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


def main() -> int:
    parser = argparse.ArgumentParser(description="Optimize low-R NQ basket for one concurrent position.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--date-from", default="2019-01-01")
    parser.add_argument("--date-to", default="2026-04-27")
    parser.add_argument("--min-year-trades", type=int, default=1000)
    parser.add_argument("--max-results", type=int, default=80)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/profit_mining/low_r_single_position_optimized.json"),
    )
    args = parser.parse_args()

    base_config = LowRRegimeBasketConfig(
        data_root=args.data_root,
        symbol=args.symbol,
        date_from=args.date_from,
        date_to=args.date_to,
        preset="top_net_2019_low_r",
        max_concurrent_positions=1,
        flatten_on_date_change=True,
    )
    edges = union_edges()
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
        for params in parameter_grid():
            print(f"evaluating {params}", flush=True)
            single_edge_results = evaluate_single_edges(
                con,
                edges,
                bars,
                base_config,
                coverage_days,
                args.min_year_trades,
                params,
            )
            candidate_specs = build_candidate_specs(single_edge_results)
            all_results.extend(
                evaluate_candidate_specs(
                    con,
                    candidate_specs,
                    edges,
                    bars,
                    base_config,
                    coverage_days,
                    args.min_year_trades,
                    params,
                )
            )
    finally:
        con.close()

    all_results.sort(key=lambda row: sort_key(row, args.min_year_trades), reverse=True)
    payload = {
        "schema_version": 1,
        "artifact": "low_r_single_position_optimized",
        "date_from": args.date_from,
        "date_to": args.date_to,
        "min_year_trades": args.min_year_trades,
        "candidate_count": len(all_results),
        "best": next((row for row in all_results if row["constraints"]["full_or_annualized_years_gt_min_trades"]), None),
        "top_results": all_results[: args.max_results],
        "notes": [
            "All candidates use max_concurrent_positions=1.",
            "Candidate edge order is optimized by ranking individual edge results for the same stop/target parameters.",
            "Incomplete years are checked by annualized trade count.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")
    print(args.output)
    best = payload["best"] or (all_results[0] if all_results else None)
    if best:
        metrics = best["metrics"]
        print(
            "best",
            "pass=",
            best["constraints"]["full_or_annualized_years_gt_min_trades"],
            "net=",
            round(metrics["net_pnl"], 2),
            "trades=",
            metrics["trade_count"],
            "min_ann=",
            round(best["constraints"]["min_annualized_year_trades"], 1),
            "pf=",
            round(metrics["profit_factor"], 3),
            "dd=",
            round(metrics["max_drawdown"], 2),
            "avg_hold=",
            round(best["avg_hold_bars"], 2),
            "edge_count=",
            best["edge_count"],
            "spec=",
            best["candidate_spec"],
        )
    return 0


def union_edges() -> tuple[RegimeEdge, ...]:
    return tuple(dict.fromkeys((*TOP_NET_2019_LOW_R_EDGES, *ANNUAL_2023_LOW_VOLUME_LOW_R_EDGES)))


def parameter_grid() -> list[dict[str, Any]]:
    rows = []
    for max_hold, stop_multiple, max_stop, tp_scale in product(
        (45, 60, 90),
        (10.0,),
        (90.0,),
        (0.75, 1.0),
    ):
        rows.append(
            {
                "stop_range_multiple": stop_multiple,
                "min_stop_points": 8.0,
                "max_stop_points": max_stop,
                "max_hold_minutes": max_hold,
                "max_concurrent_positions": 1,
                "flatten_on_date_change": True,
                "take_profit_scale": tp_scale,
            }
        )
    return rows


def evaluate_single_edges(
    con: duckdb.DuckDBPyConnection,
    edges: Sequence[RegimeEdge],
    bars: Sequence[dict],
    base_config: LowRRegimeBasketConfig,
    coverage_days: dict[int, int],
    min_year_trades: int,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    results = []
    for index, edge in enumerate(edges):
        row = evaluate_edges(
            label=f"single_{index}",
            edges=(edge,),
            bars=bars,
            base_config=base_config,
            coverage_days=coverage_days,
            min_year_trades=min_year_trades,
            params=params,
            con=con,
        )
        row["source_edge_index"] = index
        results.append(row)
    return results


def build_candidate_specs(single_edge_results: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked_all = sorted(
        single_edge_results,
        key=lambda row: (
            row["metrics"]["net_pnl"],
            row["constraints"]["min_annualized_year_trades"],
            row["metrics"].get("profit_factor") or 0.0,
        ),
        reverse=True,
    )
    ranked_positive = [row for row in ranked_all if row["metrics"]["net_pnl"] > 0]
    ranked_frequency = sorted(
        single_edge_results,
        key=lambda row: (
            row["constraints"]["min_annualized_year_trades"],
            row["metrics"]["net_pnl"],
        ),
        reverse=True,
    )
    specs: dict[str, dict[str, Any]] = {}

    def add(name: str, rows: Sequence[dict[str, Any]]) -> None:
        indexes = tuple(int(row["source_edge_index"]) for row in rows)
        if indexes:
            specs[name] = {"name": name, "edge_indexes": indexes}

    for size in (1, 2, 3, 5, 8, 12, 16, 19):
        add(f"top_net_prefix_{size}", ranked_all[:size])
        add(f"positive_net_prefix_{size}", ranked_positive[:size])
        add(f"top_frequency_prefix_{size}", ranked_frequency[:size])
    scan_types = sorted({row["edges"][0]["scan_type"] for row in single_edge_results})
    for scan_type in scan_types:
        rows = [row for row in ranked_all if row["edges"][0]["scan_type"] == scan_type]
        add(f"scan_type_{scan_type}", rows)
    return list(specs.values())


def evaluate_candidate_specs(
    con: duckdb.DuckDBPyConnection,
    specs: Sequence[dict[str, Any]],
    base_edges: Sequence[RegimeEdge],
    bars: Sequence[dict],
    base_config: LowRRegimeBasketConfig,
    coverage_days: dict[int, int],
    min_year_trades: int,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = []
    for spec in specs:
        edges = tuple(base_edges[index] for index in spec["edge_indexes"])
        rows.append(
            evaluate_edges(
                label=spec["name"],
                edges=edges,
                bars=bars,
                base_config=base_config,
                coverage_days=coverage_days,
                min_year_trades=min_year_trades,
                params=params,
                candidate_spec=spec,
                con=con,
            )
        )
    return rows


def evaluate_edges(
    label: str,
    edges: Sequence[RegimeEdge],
    bars: Sequence[dict],
    base_config: LowRRegimeBasketConfig,
    coverage_days: dict[int, int],
    min_year_trades: int,
    params: dict[str, Any],
    candidate_spec: dict[str, Any] | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, Any]:
    take_profit_scale = float(params["take_profit_scale"])
    config_params = {key: value for key, value in params.items() if key != "take_profit_scale"}
    config = replace(base_config, **config_params)
    scaled = scaled_edges(edges, take_profit_scale)
    if con is None:
        pattern_signals = load_signals_for_edges(scaled, config)
    else:
        pattern_signals = _load_signals(con, scaled, config)
    trades = _replay_low_r_exits(bars, pattern_signals, scaled, config)
    result = _build_result(bars, pattern_signals, trades, scaled, config)
    hold_values = [trade.bars_held for trade in trades]
    yearly = result["yearly_results"]
    return {
        "label": label,
        "candidate_spec": candidate_spec or {"name": label, "edge_indexes": tuple(range(len(edges)))},
        "edge_count": len(edges),
        "params": params,
        "metrics": result["metrics"],
        "avg_hold_bars": sum(hold_values) / len(hold_values) if hold_values else 0.0,
        "median_hold_bars": sorted(hold_values)[len(hold_values) // 2] if hold_values else 0,
        "edge_summary": result["edge_summary"],
        "exit_reasons": result["exit_reasons"],
        "yearly_results": yearly,
        "constraints": constraint_summary(yearly, min_year_trades, coverage_days),
        "edges": [asdict(edge) for edge in scaled],
    }


def load_signals_for_edges(edges: Sequence[RegimeEdge], config: LowRRegimeBasketConfig) -> list[dict]:
    pattern = str(
        Path(config.data_root)
        / "bars"
        / config.timeframe
        / config.symbol
        / "date=*"
        / "part-000.parquet"
    )
    con = duckdb.connect(":memory:")
    try:
        _ensure_feature_table(con, pattern, config)
        return _load_signals(con, edges, config)
    finally:
        con.close()


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
    min_year_trades: int,
    coverage_days: dict[int, int],
) -> dict[str, Any]:
    if not yearly:
        return {
            "min_year_trades": 0,
            "min_annualized_year_trades": 0.0,
            "full_or_annualized_years_gt_min_trades": False,
            "positive_years": 0,
            "worst_year_pnl": 0.0,
        }
    trade_counts = [int(row.get("trade_count") or 0) for row in yearly]
    annualized_counts = [
        annualized_trade_count(int(row.get("trade_count") or 0), coverage_days.get(int(row["year"]), 365))
        for row in yearly
    ]
    pnl_values = [float(row.get("net_pnl") or 0.0) for row in yearly]
    return {
        "min_year_trades": min(trade_counts),
        "min_annualized_year_trades": min(annualized_counts),
        "full_or_annualized_years_gt_min_trades": all(value > min_year_trades for value in annualized_counts),
        "positive_years": sum(1 for value in pnl_values if value > 0),
        "worst_year_pnl": min(pnl_values),
    }


def sort_key(row: dict[str, Any], min_year_trades: int) -> tuple[float, ...]:
    metrics = row["metrics"]
    constraints = row["constraints"]
    trade_deficit = min(0.0, float(constraints["min_annualized_year_trades"]) - min_year_trades)
    return (
        1.0 if constraints["full_or_annualized_years_gt_min_trades"] else 0.0,
        trade_deficit,
        float(metrics["net_pnl"]),
        float(metrics.get("net_pnl_to_max_drawdown") or 0.0),
        float(metrics.get("profit_factor") or 0.0),
        -float(row["avg_hold_bars"]),
        -float(row["edge_count"]),
    )


def annualized_trade_count(trade_count: int, covered_days: int) -> float:
    if covered_days <= 0:
        return 0.0
    return trade_count * 365.0 / min(covered_days, 365)


if __name__ == "__main__":
    raise SystemExit(main())
