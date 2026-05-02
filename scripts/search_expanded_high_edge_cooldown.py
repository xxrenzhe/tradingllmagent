#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import asdict, replace
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
    config_from_params,
    coverage_days_by_year,
    load_bars,
    load_signals_by_candidate,
)
from scripts.walk_forward_expanded_high_edge import (
    compact_year,
    constraint_summary_for_years,
    fixed_horizon_cost_adjustment_usd,
    summarize_walk_forward,
    yearly_results_from_trades,
)
from tlm.low_r_regime_basket import LowRRegimeBasketConfig, RegimeEdge, _json_default
from tlm.metrics import calculate_metrics
from tlm.low_r_regime_basket import _open_trade, _simulate_trade_exit


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Search same-regime cooldown windows on locked walk-forward selections.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--date-from", default="2019-01-01")
    parser.add_argument("--date-to", default="2026-04-27")
    parser.add_argument(
        "--walk-forward-report",
        type=Path,
        default=Path("reports/nq_expanded_high_edge_walk_forward_no_prior_highvol_donchian_min13_2026-05-01.json"),
    )
    parser.add_argument("--cooldown-minutes-grid", default="0,5,15,30,60,120,240")
    parser.add_argument("--same-scan-only", action="store_true")
    parser.add_argument("--min-full-year-trades", type=int, default=1000)
    parser.add_argument("--output", type=Path, default=Path("reports/nq_expanded_high_edge_cooldown_search_2026-05-02.json"))
    args = parser.parse_args(argv)

    cooldown_grid = parse_int_grid(args.cooldown_minutes_grid, option_name="--cooldown-minutes-grid")
    source_report = json.loads(args.walk_forward_report.read_text(encoding="utf-8"))
    pattern = str(Path(args.data_root) / "bars" / "1m" / args.symbol / "date=*" / "part-000.parquet")
    base_config = LowRRegimeBasketConfig(
        data_root=args.data_root,
        symbol=args.symbol,
        date_from=args.date_from,
        date_to=args.date_to,
        preset="expanded_high_edge_cooldown_search",
        flatten_on_date_change=True,
    )

    con = duckdb.connect(":memory:")
    try:
        build_feature_table(con, pattern, args.date_from, args.date_to)
        bars = load_bars(con)
        coverage_days = coverage_days_by_year(bars)
        build_signal_table(con)
        candidate_ids = sorted(set(_candidate_ids_for_report(source_report)))
        signals_by_id = load_signals_by_candidate(con, candidate_ids)
        attach_entry_indexes(signals_by_id, bars)
        rows = []
        for cooldown_minutes in cooldown_grid:
            folds = []
            previous_ids: tuple[str, ...] = ()
            for fold in source_report.get("folds", []):
                if fold.get("status") != "ok":
                    continue
                folds.append(
                    run_cooldown_fold(
                        source_fold=fold,
                        bars=bars,
                        signals_by_id=signals_by_id,
                        base_config=base_config,
                        coverage_days=coverage_days,
                        cooldown_minutes=cooldown_minutes,
                        same_scan_only=args.same_scan_only,
                        min_full_year_trades=args.min_full_year_trades,
                        previous_ids=previous_ids,
                    )
                )
                previous_ids = tuple(folds[-1].get("selected_candidate_ids") or ())
            summary = summarize_walk_forward(folds, [], args.min_full_year_trades)
            rows.append(
                {
                    "cooldown_minutes": cooldown_minutes,
                    "same_scan_only": args.same_scan_only,
                    "summary": summary,
                    "folds": folds,
                }
            )
    finally:
        con.close()

    payload = {
        "artifact": "expanded_high_edge_cooldown_search",
        "source_walk_forward_report": str(args.walk_forward_report),
        "date_from": args.date_from,
        "date_to": args.date_to,
        "symbol": args.symbol,
        "method": {
            "cooldown_minutes_grid": list(cooldown_grid),
            "same_scan_only": args.same_scan_only,
            "selection": "Uses selected candidate ids, edges, and params from the source walk-forward report; only same-regime cooldown is varied.",
        },
        "leaderboard": sorted(rows, key=cooldown_sort_key, reverse=True),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "best": best_summary(payload["leaderboard"][0])}, indent=2))
    return 0


def run_cooldown_fold(
    *,
    source_fold: dict[str, Any],
    bars: Sequence[dict[str, Any]],
    signals_by_id: dict[str, list[dict[str, Any]]],
    base_config: LowRRegimeBasketConfig,
    coverage_days: dict[int, int],
    cooldown_minutes: int,
    same_scan_only: bool,
    min_full_year_trades: int,
    previous_ids: Sequence[str],
) -> dict[str, Any]:
    test_year = int(source_fold["test_year"])
    selected_ids = tuple(str(candidate_id) for candidate_id in source_fold.get("selected_candidate_ids") or [])
    selected_edges = tuple(_edge_from_dict(edge) for edge in source_fold.get("selected_edges") or [])
    edge_order = {candidate_id: index for index, candidate_id in enumerate(selected_ids)}
    signals = combine_signals(
        [
            _filter_signals_by_year(signals_by_id.get(candidate_id, ()), test_year)
            for candidate_id in selected_ids
        ],
        edge_order=edge_order,
    )
    params = dict(source_fold["selection"]["params"])
    config = config_from_params(base_config, params)
    trades = replay_with_regime_cooldown(
        bars=bars,
        signals=signals,
        edges=selected_edges,
        config=config,
        cooldown_minutes=cooldown_minutes,
        same_scan_only=same_scan_only,
    )
    result = build_result(
        label=f"{source_fold['selection']['label']}_cooldown_{cooldown_minutes}m",
        spec={"edge_candidate_ids": selected_ids},
        bars=bars,
        trades=trades,
        edges=selected_edges,
        config=config,
        coverage_days=coverage_days,
        min_full_year_trades=min_full_year_trades,
        params={**params, "signal_regime_cooldown_minutes": cooldown_minutes, "same_scan_only": same_scan_only},
    )
    return {
        "train_years": source_fold.get("train_years", []),
        "test_year": test_year,
        "status": "ok",
        "candidate_group_count": source_fold.get("candidate_group_count", len(selected_ids)),
        "evaluated_single_count": source_fold.get("evaluated_single_count", 0),
        "evaluated_combo_count": source_fold.get("evaluated_combo_count", 0),
        "selection": {
            "label": result["label"],
            "edge_count": len(selected_edges),
            "params": result["params"],
            "edge_summary": result["edge_summary"],
        },
        "selected_candidate_ids": list(selected_ids),
        "selected_edges": [asdict(edge) for edge in selected_edges],
        "selected_edge_turnover": selected_edge_turnover(previous_ids, selected_ids),
        "train_metrics": source_fold.get("train_metrics", {}),
        "test_metrics": compact_metrics(result),
        "test_yearly_result": compact_year(result, test_year, coverage_days),
    }


def replay_with_regime_cooldown(
    *,
    bars: Sequence[dict[str, Any]],
    signals: Sequence[dict[str, Any]],
    edges: Sequence[RegimeEdge],
    config: LowRRegimeBasketConfig,
    cooldown_minutes: int,
    same_scan_only: bool,
) -> list[Any]:
    closed = []
    open_exit_indexes: list[tuple[int, int]] = []
    last_entry_index_by_regime: dict[str, int] = {}
    serial = 0
    cooldown_bars = max(int(cooldown_minutes), 0)
    for signal in signals:
        entry_index = signal.get("entry_index")
        if entry_index is None:
            continue
        entry_index = int(entry_index)
        while open_exit_indexes and open_exit_indexes[0][0] <= entry_index:
            open_exit_indexes.pop(0)
        if len(open_exit_indexes) >= config.max_concurrent_positions:
            continue
        edge = edges[int(signal["edge_index"])]
        regime_key = signal_regime_key(signal, edge, same_scan_only=same_scan_only)
        last_entry_index = last_entry_index_by_regime.get(regime_key)
        if last_entry_index is not None and entry_index - last_entry_index < cooldown_bars:
            continue
        trade = _open_trade(signal, edge, entry_index, config)
        closed_trade = _simulate_trade_exit(trade, bars, config)
        closed.append(closed_trade)
        last_entry_index_by_regime[regime_key] = entry_index
        insert_sorted_exit(open_exit_indexes, (trade.entry_index + closed_trade.bars_held, serial))
        serial += 1
    closed.sort(key=lambda trade: (trade.exit_time, trade.entry_time, trade.edge_index))
    return closed


def signal_regime_key(signal: dict[str, Any], edge: RegimeEdge, *, same_scan_only: bool) -> str:
    if same_scan_only:
        return f"{edge.scan_type}|{edge.direction_label}"
    return "|".join(
        [
            edge.scan_type,
            edge.direction_label,
            edge.session_bucket,
            str(edge.dow),
            str(edge.trend_bin),
            str(edge.volume_bin),
            str(edge.range_bin),
        ]
    )


def insert_sorted_exit(rows: list[tuple[int, int]], item: tuple[int, int]) -> None:
    rows.append(item)
    rows.sort()


def build_result(
    *,
    label: str,
    spec: dict[str, Any],
    bars: Sequence[dict[str, Any]],
    trades: Sequence[Any],
    edges: Sequence[RegimeEdge],
    config: LowRRegimeBasketConfig,
    coverage_days: dict[int, int],
    min_full_year_trades: int,
    params: dict[str, Any],
) -> dict[str, Any]:
    pnls = [trade.net_pnl for trade in trades]
    equity = [config.starting_equity]
    for pnl in pnls:
        equity.append(equity[-1] + pnl)
    metrics = calculate_metrics(pnls, equity, config.starting_equity, len({bar["timestamp"].date() for bar in bars}) or 1).to_dict()
    yearly = yearly_results_from_trades(trades)
    return {
        "label": label,
        "spec": spec,
        "edge_count": len(edges),
        "params": params,
        "metrics": metrics,
        "edge_summary": {
            "scan_types": dict(sorted(defaultdict(int, _count(edge.scan_type for edge in edges)).items())),
            "take_profit_r": dict(sorted(defaultdict(int, _count(edge.take_profit_r for edge in edges)).items())),
            "sessions": dict(sorted(defaultdict(int, _count(edge.session_bucket for edge in edges)).items())),
        },
        "yearly_results": yearly,
        "constraints": constraint_summary_for_years(yearly, min_full_year_trades, coverage_days),
    }


def _count(values: Sequence[Any]) -> dict[Any, int]:
    counts: dict[Any, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def compact_metrics(result: dict[str, Any]) -> dict[str, Any]:
    metrics = result["metrics"]
    return {
        "trade_count": metrics.get("trade_count"),
        "net_pnl": metrics.get("net_pnl"),
        "profit_factor": metrics.get("profit_factor"),
        "avg_trade_net_pnl": metrics.get("avg_trade_net_pnl"),
        "max_drawdown": metrics.get("max_drawdown"),
        "net_pnl_to_max_drawdown": metrics.get("net_pnl_to_max_drawdown"),
        "win_rate": metrics.get("win_rate"),
    }


def cooldown_sort_key(row: dict[str, Any]) -> tuple[float, ...]:
    summary = row["summary"]
    decision = summary["decision"]
    return (
        1.0 if decision["passed"] else 0.0,
        float(decision["positive_test_years"]),
        float(decision["trade_floor_years"]),
        float(summary["oos_min_year_pnl"]),
        float(summary["oos_total_net_pnl"]),
        float(summary["oos_min_year_trade_floor_count"]),
    )


def best_summary(row: dict[str, Any]) -> dict[str, Any]:
    summary = row["summary"]
    return {
        "cooldown_minutes": row["cooldown_minutes"],
        "same_scan_only": row["same_scan_only"],
        "decision": summary["decision"],
        "oos_total_net_pnl": summary["oos_total_net_pnl"],
        "oos_min_year_pnl": summary["oos_min_year_pnl"],
        "oos_total_trades": summary["oos_total_trades"],
    }


def parse_int_grid(raw: str, *, option_name: str) -> tuple[int, ...]:
    values = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = int(item)
        except ValueError as exc:
            raise SystemExit(f"{option_name} expects comma-separated integers, got {raw!r}") from exc
        if value < 0:
            raise SystemExit(f"{option_name} values must be non-negative, got {raw!r}")
        values.append(value)
    if not values:
        raise SystemExit(f"{option_name} cannot be empty")
    return tuple(values)


def _candidate_ids_for_report(report: dict[str, Any]) -> list[str]:
    ids = []
    for fold in report.get("folds", []):
        ids.extend(str(candidate_id) for candidate_id in fold.get("selected_candidate_ids") or [])
    return ids


def _filter_signals_by_year(signals: Sequence[dict[str, Any]], year: int) -> list[dict[str, Any]]:
    return [signal for signal in signals if signal["entry_time"].year == year]


def _edge_from_dict(edge: dict[str, Any]) -> RegimeEdge:
    return RegimeEdge(
        scan_type=str(edge["scan_type"]),
        direction_label=str(edge["direction_label"]),
        horizon_minutes=int(edge.get("horizon_minutes") or 120),
        session_bucket=str(edge["session_bucket"]),
        dow=int(edge.get("dow", -99)),
        trend_bin=int(edge.get("trend_bin", 0)),
        volume_bin=int(edge.get("volume_bin", 0)),
        range_bin=int(edge.get("range_bin", 0)),
        take_profit_r=float(edge["take_profit_r"]),
    )


def selected_edge_turnover(previous_ids: Sequence[str], current_ids: Sequence[str]) -> dict[str, float | None]:
    if not previous_ids:
        return {"jaccard_similarity": None, "added_count": None, "removed_count": None}
    previous = set(previous_ids)
    current = set(current_ids)
    union = previous | current
    return {
        "jaccard_similarity": len(previous & current) / len(union) if union else 1.0,
        "added_count": len(current - previous),
        "removed_count": len(previous - current),
    }


if __name__ == "__main__":
    raise SystemExit(main())
