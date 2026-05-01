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
    parser = argparse.ArgumentParser(description="Sweep low-R basket params for high-frequency annual trade floors.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--date-from", default="2019-01-01")
    parser.add_argument("--date-to", default="2026-04-27")
    parser.add_argument("--min-year-trades", type=int, default=1000)
    parser.add_argument("--max-results", type=int, default=80)
    parser.add_argument("--max-holds", default="60,120,300")
    parser.add_argument("--max-positions", default="99")
    parser.add_argument("--stop-multiples", default="4,6,10")
    parser.add_argument("--min-stops", default="4,8")
    parser.add_argument("--max-stops", default="30,90")
    parser.add_argument("--tp-scales", default="0.5,0.75,1.0")
    parser.add_argument(
        "--edge-sets",
        default="top_net_2019,top_net_plus_annual_low_volume,low_volume_union",
        help="Comma-separated edge set names to evaluate.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/profit_mining/low_r_high_frequency_sweep.json"),
    )
    args = parser.parse_args()
    search_grid = {
        "max_holds": parse_int_csv(args.max_holds),
        "max_positions": parse_int_csv(args.max_positions),
        "stop_multiples": parse_float_csv(args.stop_multiples),
        "min_stops": parse_float_csv(args.min_stops),
        "max_stops": parse_float_csv(args.max_stops),
        "tp_scales": parse_float_csv(args.tp_scales),
    }

    base_config = LowRRegimeBasketConfig(
        data_root=args.data_root,
        symbol=args.symbol,
        date_from=args.date_from,
        date_to=args.date_to,
        preset="top_net_2019_low_r",
    )
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
        results = []
        selected_edge_sets = {name: edges for name, edges in edge_sets().items() if name in set(parse_str_csv(args.edge_sets))}
        if not selected_edge_sets:
            raise ValueError("--edge-sets did not match any available edge set")
        for edge_set_name, edges in selected_edge_sets.items():
            print(f"loading signals for {edge_set_name} ({len(edges)} edges)", flush=True)
            signals = _load_signals(con, edges, base_config)
            print(f"{edge_set_name}: {len(signals)} signals", flush=True)
            results.extend(
                evaluate_edge_set(
                    edge_set_name,
                    edges,
                    signals,
                    bars,
                    base_config,
                    args.min_year_trades,
                    coverage_days,
                    search_grid,
                )
            )
    finally:
        con.close()

    results.sort(key=lambda row: sort_key(row, args.min_year_trades), reverse=True)
    payload = {
        "schema_version": 1,
        "artifact": "low_r_high_frequency_sweep",
        "date_from": args.date_from,
        "date_to": args.date_to,
        "min_year_trades": args.min_year_trades,
        "candidate_count": len(results),
        "search_grid": search_grid,
        "best_full_or_annualized_years": next(
            (row for row in results if row["constraints"]["full_or_annualized_years_gt_min_trades"]),
            None,
        ),
        "top_results": results[: args.max_results],
        "notes": [
            "Uses the existing low_r_regime_basket engine and its intrabar stop/target model.",
            "The hard trade-count check is actual trades per calendar year inside the evaluated date range.",
            "For incomplete start/end years, the constraint also reports annualized trades based on covered bar dates.",
            "Average hold is measured in 1-minute bars held.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")
    print(args.output)
    best = payload["best_full_or_annualized_years"] or (results[0] if results else None)
    if best:
        metrics = best["metrics"]
        print(
            "best",
            "full_or_annualized=",
            best["constraints"]["full_or_annualized_years_gt_min_trades"],
            "net=",
            round(metrics["net_pnl"], 2),
            "trades=",
            metrics["trade_count"],
            "min_annualized_year_trades=",
            round(best["constraints"]["min_annualized_year_trades"], 1),
            "pf=",
            round(metrics["profit_factor"], 3),
            "dd=",
            round(metrics["max_drawdown"], 2),
            "avg_hold=",
            round(best["avg_hold_bars"], 2),
            "params=",
            best["params"],
        )
    return 0


def edge_sets() -> dict[str, tuple[RegimeEdge, ...]]:
    top_net = tuple(TOP_NET_2019_LOW_R_EDGES)
    annual_low_volume = tuple(ANNUAL_2023_LOW_VOLUME_LOW_R_EDGES)
    union = tuple(dict.fromkeys((*top_net, *annual_low_volume)))
    low_volume_union = tuple(edge for edge in union if edge.scan_type == "low_volume_drift")
    return {
        "top_net_2019": top_net,
        "top_net_plus_annual_low_volume": union,
        "low_volume_union": low_volume_union,
    }


def evaluate_edge_set(
    edge_set_name: str,
    base_edges: Sequence[RegimeEdge],
    signals: Sequence[dict],
    bars: Sequence[dict],
    base_config: LowRRegimeBasketConfig,
    min_year_trades: int,
    coverage_days: dict[int, int],
    search_grid: dict[str, list[int] | list[float]],
) -> list[dict[str, Any]]:
    rows = []
    for max_hold, max_positions, stop_multiple, min_stop, max_stop, tp_scale, allow_overnight in product(
        search_grid["max_holds"],
        search_grid["max_positions"],
        search_grid["stop_multiples"],
        search_grid["min_stops"],
        search_grid["max_stops"],
        search_grid["tp_scales"],
        (False,),
    ):
        if min_stop > max_stop:
            continue
        config = replace(
            base_config,
            stop_range_multiple=stop_multiple,
            min_stop_points=min_stop,
            max_stop_points=max_stop,
            max_hold_minutes=max_hold,
            max_concurrent_positions=max_positions,
            flatten_on_date_change=not allow_overnight,
        )
        edges = scaled_edges(base_edges, tp_scale)
        trades = _replay_low_r_exits(bars, signals, edges, config)
        result = _build_result(bars, signals, trades, edges, config)
        yearly = result["yearly_results"]
        if not yearly:
            continue
        hold_values = [trade.bars_held for trade in trades]
        row = {
            "edge_set": edge_set_name,
            "edge_count": len(edges),
            "params": {
                "max_hold_minutes": max_hold,
                "max_concurrent_positions": max_positions,
                "stop_range_multiple": stop_multiple,
                "min_stop_points": min_stop,
                "max_stop_points": max_stop,
                "take_profit_scale": tp_scale,
                "allow_overnight": allow_overnight,
            },
            "metrics": result["metrics"],
            "avg_hold_bars": sum(hold_values) / len(hold_values) if hold_values else 0.0,
            "median_hold_bars": sorted(hold_values)[len(hold_values) // 2] if hold_values else 0,
            "edge_summary": result["edge_summary"],
            "exit_reasons": result["exit_reasons"],
            "yearly_results": yearly,
            "constraints": constraint_summary(yearly, min_year_trades, coverage_days),
            "config": asdict(config),
            "edges": [asdict(edge) for edge in edges],
        }
        rows.append(row)
    return rows


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
    trade_counts = [int(row.get("trade_count") or 0) for row in yearly]
    pnl_values = [float(row.get("net_pnl") or 0.0) for row in yearly]
    annualized_counts = [
        annualized_trade_count(int(row.get("trade_count") or 0), coverage_days.get(int(row["year"]), 365))
        for row in yearly
    ]
    full_year_actual = [
        int(row.get("trade_count") or 0)
        for row in yearly
        if coverage_days.get(int(row["year"]), 365) >= 330
    ]
    return {
        "min_year_trades": min(trade_counts) if trade_counts else 0,
        "min_annualized_year_trades": min(annualized_counts) if annualized_counts else 0.0,
        "min_full_year_actual_trades": min(full_year_actual) if full_year_actual else 0,
        "actual_all_years_gt_min_trades": all(value > min_year_trades for value in trade_counts),
        "full_or_annualized_years_gt_min_trades": all(value > min_year_trades for value in annualized_counts),
        "positive_years": sum(1 for value in pnl_values if value > 0),
        "worst_year_pnl": min(pnl_values) if pnl_values else 0.0,
    }


def sort_key(row: dict[str, Any], min_year_trades: int) -> tuple[float, ...]:
    constraints = row["constraints"]
    metrics = row["metrics"]
    trade_deficit = min(0.0, float(constraints["min_annualized_year_trades"]) - min_year_trades)
    return (
        1.0 if constraints["full_or_annualized_years_gt_min_trades"] else 0.0,
        float(trade_deficit),
        float(metrics.get("net_pnl") or 0.0),
        float(metrics.get("profit_factor") or 0.0),
        float(metrics.get("net_pnl_to_max_drawdown") or 0.0),
        -float(row["avg_hold_bars"]),
    )


def annualized_trade_count(trade_count: int, covered_days: int) -> float:
    if covered_days <= 0:
        return 0.0
    return trade_count * 365.0 / min(covered_days, 365)


def parse_int_csv(raw: str) -> list[int]:
    values = [int(value.strip()) for value in raw.split(",") if value.strip()]
    if not values:
        raise ValueError("integer CSV argument must include at least one value")
    return values


def parse_float_csv(raw: str) -> list[float]:
    values = [float(value.strip()) for value in raw.split(",") if value.strip()]
    if not values:
        raise ValueError("float CSV argument must include at least one value")
    return values


def parse_str_csv(raw: str) -> list[str]:
    values = [value.strip() for value in raw.split(",") if value.strip()]
    if not values:
        raise ValueError("string CSV argument must include at least one value")
    return values


if __name__ == "__main__":
    raise SystemExit(main())
