#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.search_expanded_high_edge_strategy import (
    attach_entry_indexes,
    build_feature_table,
    build_signal_table,
    combine_signals,
    coverage_days_by_year,
    load_bars,
    load_signals_by_candidate,
    replay_result,
)
from scripts.validate_expanded_high_edge_execution_stress import (
    _candidate_id,
    _decision,
    _to_regime_edge,
)
from tlm.expanded_high_edge import EXPANDED_HIGH_EDGE_CAP24_PRESET, ExpandedHighEdge, expanded_high_edge_preset_spec
from tlm.low_r_regime_basket import LowRRegimeBasketConfig, RegimeEdge, _json_default


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Optimize expanded_high_edge_cap24 for lower risk while retaining high return.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--date-from", default="2019-01-01")
    parser.add_argument("--date-to", default="2026-04-27")
    parser.add_argument("--min-full-year-trades", type=int, default=1000)
    parser.add_argument("--min-net-retention", type=float, default=0.75)
    parser.add_argument("--stress-top", type=int, default=20)
    parser.add_argument("--output", type=Path, default=Path("reports/nq_expanded_high_edge_cap24_risk_optimization_2026-05-02.json"))
    args = parser.parse_args(argv)

    spec = expanded_high_edge_preset_spec(EXPANDED_HIGH_EDGE_CAP24_PRESET)
    pattern = str(Path(args.data_root) / "bars" / "1m" / args.symbol / "date=*" / "part-000.parquet")
    candidate_ids = [_candidate_id(edge) for edge in spec.edges]

    con = duckdb.connect(":memory:")
    try:
        build_feature_table(con, pattern, args.date_from, args.date_to)
        bars = load_bars(con)
        coverage_days = coverage_days_by_year(bars)
        build_signal_table(con)
        signals_by_id = load_signals_by_candidate(con, sorted(set(candidate_ids)))
        attach_entry_indexes(signals_by_id, bars)
    finally:
        con.close()

    baseline = evaluate_variant(
        label="cap24_current",
        source_label=spec.source_label,
        edges=spec.edges,
        bars=bars,
        signals_by_id=signals_by_id,
        coverage_days=coverage_days,
        args=args,
        max_hold_minutes=spec.max_hold_minutes,
        stop_range_multiple=spec.stop_range_multiple,
        max_concurrent_positions=spec.max_concurrent_positions,
        slippage_ticks_per_side=1.0,
    )
    baseline_net = float(baseline["metrics"]["net_pnl"])
    baseline_drawdown = float(baseline["metrics"]["max_drawdown"])

    one_x_results = []
    seen: set[tuple[Any, ...]] = set()
    for max_prior_day_edges in (6, 8, 10, 12, 14):
        selected_edges = cap_prior_day_edges(spec.edges, max_prior_day_edges)
        for max_concurrent_positions in (6, 9, 12, 18, 24):
            for max_hold_minutes in (120, 180, 240, 300):
                for stop_range_multiple in (4.0, 6.0, 8.0, 10.0):
                    key = (
                        tuple(_candidate_id(edge) for edge in selected_edges),
                        max_concurrent_positions,
                        max_hold_minutes,
                        stop_range_multiple,
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    result = evaluate_variant(
                        label=(
                            f"cap24_priorcap{max_prior_day_edges}_pos{max_concurrent_positions}"
                            f"_hold{max_hold_minutes}_stop{stop_range_multiple:g}"
                        ),
                        source_label=spec.source_label,
                        edges=selected_edges,
                        bars=bars,
                        signals_by_id=signals_by_id,
                        coverage_days=coverage_days,
                        args=args,
                        max_hold_minutes=max_hold_minutes,
                        stop_range_multiple=stop_range_multiple,
                        max_concurrent_positions=max_concurrent_positions,
                        slippage_ticks_per_side=1.0,
                    )
                    annotate_candidate(
                        result,
                        baseline_net=baseline_net,
                        baseline_drawdown=baseline_drawdown,
                        min_net_retention=args.min_net_retention,
                    )
                    one_x_results.append(result)

    one_x_results.sort(key=candidate_sort_key, reverse=True)
    stress_candidates = [row for row in one_x_results if row["risk_gate"]["passed"]][: args.stress_top]
    if len(stress_candidates) < args.stress_top:
        stress_candidates = one_x_results[: args.stress_top]

    stressed = []
    for candidate in stress_candidates:
        stress_rows = [candidate]
        candidate_edges = [edge_from_dict(edge) for edge in candidate["edges"]]
        for slippage_ticks_per_side in (2.0, 3.0):
            result = evaluate_variant(
                label=f"{candidate['label']}_slip{slippage_ticks_per_side:g}x",
                source_label=spec.source_label,
                edges=candidate_edges,
                bars=bars,
                signals_by_id=signals_by_id,
                coverage_days=coverage_days,
                args=args,
                max_hold_minutes=int(candidate["params"]["max_hold_minutes"]),
                stop_range_multiple=float(candidate["params"]["stop_range_multiple"]),
                max_concurrent_positions=int(candidate["params"]["max_concurrent_positions"]),
                slippage_ticks_per_side=slippage_ticks_per_side,
            )
            annotate_candidate(
                result,
                baseline_net=baseline_net,
                baseline_drawdown=baseline_drawdown,
                min_net_retention=args.min_net_retention,
            )
            stress_rows.append(result)
        stressed.append(
            {
                "label": candidate["label"],
                "stress_passed": all(row["risk_gate"]["passed"] for row in stress_rows),
                "stress_results": stress_rows,
            }
        )

    payload = {
        "artifact": "expanded_high_edge_cap24_risk_optimization",
        "date_from": args.date_from,
        "date_to": args.date_to,
        "symbol": args.symbol,
        "method": {
            "base_preset": EXPANDED_HIGH_EDGE_CAP24_PRESET,
            "objective": "Retain high cap24 net PnL while reducing max drawdown and preserving annual positivity/frequency.",
            "risk_gate": {
                "positive_all_years": True,
                "trade_floor": args.min_full_year_trades,
                "min_net_retention": args.min_net_retention,
                "max_drawdown_below_baseline": True,
            },
            "searched_dimensions": {
                "max_prior_day_edges": [6, 8, 10, 12, 14],
                "max_concurrent_positions": [6, 9, 12, 18, 24],
                "max_hold_minutes": [120, 180, 240, 300],
                "stop_range_multiple": [4.0, 6.0, 8.0, 10.0],
            },
        },
        "baseline": baseline,
        "best_one_x": one_x_results[:25],
        "stress_candidates": stressed,
        "decision": best_decision(stressed, baseline),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), **payload["decision"]}, indent=2))
    return 0


def evaluate_variant(
    *,
    label: str,
    source_label: str,
    edges: Sequence[ExpandedHighEdge],
    bars: Sequence[dict[str, Any]],
    signals_by_id: dict[str, list[dict[str, Any]]],
    coverage_days: dict[int, int],
    args: argparse.Namespace,
    max_hold_minutes: int,
    stop_range_multiple: float,
    max_concurrent_positions: int,
    slippage_ticks_per_side: float,
) -> dict[str, Any]:
    candidate_ids = [_candidate_id(edge) for edge in edges]
    edge_order = {candidate_id: index for index, candidate_id in enumerate(candidate_ids)}
    signals = combine_signals(
        [signals_by_id[candidate_id] for candidate_id in candidate_ids if candidate_id in signals_by_id],
        edge_order=edge_order,
    )
    regime_edges = tuple(_to_regime_edge(edge) for edge in edges)
    config = LowRRegimeBasketConfig(
        data_root=args.data_root,
        symbol=args.symbol,
        date_from=args.date_from,
        date_to=args.date_to,
        preset=EXPANDED_HIGH_EDGE_CAP24_PRESET,
        stop_range_multiple=stop_range_multiple,
        min_stop_points=8.0,
        max_stop_points=90.0,
        max_hold_minutes=max_hold_minutes,
        max_concurrent_positions=max_concurrent_positions,
        slippage_ticks_per_side=slippage_ticks_per_side,
        flatten_on_date_change=True,
    )
    result = replay_result(
        label=label,
        spec={"name": label, "source_label": source_label, "candidate_ids": candidate_ids},
        bars=bars,
        signals=signals,
        edges=regime_edges,
        config=config,
        coverage_days=coverage_days,
        min_full_year_trades=args.min_full_year_trades,
        params={
            "max_hold_minutes": max_hold_minutes,
            "stop_range_multiple": stop_range_multiple,
            "min_stop_points": 8.0,
            "max_stop_points": 90.0,
            "flatten_on_date_change": True,
            "max_concurrent_positions": max_concurrent_positions,
            "slippage_ticks_per_side": slippage_ticks_per_side,
        },
    )
    result["decision"] = _decision(result, coverage_days, args.min_full_year_trades)
    result["scan_type_counts"] = result["edge_summary"]["scan_types"]
    return result


def cap_prior_day_edges(edges: Sequence[ExpandedHighEdge], max_prior_day_edges: int) -> tuple[ExpandedHighEdge, ...]:
    selected = []
    prior_count = 0
    for edge in edges:
        if edge.scan_type == "prior_day_breakout":
            if prior_count >= max_prior_day_edges:
                continue
            prior_count += 1
        selected.append(edge)
    return tuple(selected)


def annotate_candidate(
    result: dict[str, Any],
    *,
    baseline_net: float,
    baseline_drawdown: float,
    min_net_retention: float,
) -> None:
    metrics = result["metrics"]
    net_pnl = float(metrics.get("net_pnl") or 0.0)
    max_drawdown = float(metrics.get("max_drawdown") or 0.0)
    net_retention = net_pnl / baseline_net if baseline_net else 0.0
    drawdown_ratio = max_drawdown / baseline_drawdown if baseline_drawdown else 0.0
    result["risk_gate"] = {
        "passed": bool(
            result["decision"]["passed"]
            and net_retention >= min_net_retention
            and max_drawdown < baseline_drawdown
        ),
        "net_retention": net_retention,
        "drawdown_ratio": drawdown_ratio,
        "drawdown_reduction": 1.0 - drawdown_ratio,
        "net_pnl_to_max_drawdown": net_pnl / max_drawdown if max_drawdown > 0 else None,
    }


def candidate_sort_key(row: dict[str, Any]) -> tuple[float, ...]:
    risk = row["risk_gate"]
    metrics = row["metrics"]
    return (
        1.0 if risk["passed"] else 0.0,
        float(risk.get("net_pnl_to_max_drawdown") or 0.0),
        float(risk["net_retention"]),
        float(risk["drawdown_reduction"]),
        float(metrics.get("net_pnl") or 0.0),
    )


def best_decision(stressed: Sequence[dict[str, Any]], baseline: dict[str, Any]) -> dict[str, Any]:
    passed = [row for row in stressed if row["stress_passed"]]
    if not passed:
        return {
            "passed": False,
            "selected": None,
            "reason": "No candidate passed 1x/2x/3x annual positivity, frequency, net-retention, and drawdown gates.",
        }
    selected = max(
        passed,
        key=lambda row: candidate_sort_key(row["stress_results"][0]),
    )
    first = selected["stress_results"][0]
    return {
        "passed": True,
        "selected": selected["label"],
        "baseline_net_pnl": baseline["metrics"]["net_pnl"],
        "baseline_max_drawdown": baseline["metrics"]["max_drawdown"],
        "selected_net_pnl": first["metrics"]["net_pnl"],
        "selected_max_drawdown": first["metrics"]["max_drawdown"],
        "selected_net_retention": first["risk_gate"]["net_retention"],
        "selected_drawdown_reduction": first["risk_gate"]["drawdown_reduction"],
    }


def edge_from_dict(edge: dict[str, Any]) -> ExpandedHighEdge:
    return ExpandedHighEdge(
        scan_type=str(edge["scan_type"]),
        direction_label=str(edge["direction_label"]),
        session_bucket=str(edge["session_bucket"]),
        take_profit_r=float(edge["take_profit_r"]),
        dow=None if int(edge["dow"]) == -99 else int(edge["dow"]),
        trend_bin=int(edge["trend_bin"]),
        volume_bin=int(edge["volume_bin"]),
        range_bin=int(edge["range_bin"]),
    )


if __name__ == "__main__":
    raise SystemExit(main())
