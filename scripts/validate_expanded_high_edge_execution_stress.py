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
from tlm.expanded_high_edge import (
    EXPANDED_HIGH_EDGE_WF_ORB_2026_PRESET,
    expanded_high_edge_preset_spec,
)
from tlm.low_r_regime_basket import LowRRegimeBasketConfig, RegimeEdge, _json_default


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate expanded high-edge execution with quote availability and slippage stress."
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--date-from", default="2019-01-01")
    parser.add_argument("--date-to", default="2026-04-27")
    parser.add_argument("--preset", default=EXPANDED_HIGH_EDGE_WF_ORB_2026_PRESET)
    parser.add_argument(
        "--walk-forward-report",
        type=Path,
        default=Path("reports/nq_expanded_high_edge_walk_forward_no_prior_highvol_donchian_min13_2026-05-01.json"),
    )
    parser.add_argument("--min-full-year-trades", type=int, default=1000)
    parser.add_argument("--output", type=Path, default=Path("reports/nq_expanded_high_edge_execution_stress_2026-05-01.json"))
    parser.add_argument("--fail-on-cost-stress", action="store_true")
    args = parser.parse_args(argv)

    spec = expanded_high_edge_preset_spec(args.preset)
    pattern = str(Path(args.data_root) / "bars" / "1m" / args.symbol / "date=*" / "part-000.parquet")
    walk_forward = json.loads(args.walk_forward_report.read_text(encoding="utf-8")) if args.walk_forward_report.exists() else None

    con = duckdb.connect(":memory:")
    try:
        build_feature_table(con, pattern, args.date_from, args.date_to)
        bars = load_bars(con)
        coverage_days = coverage_days_by_year(bars)
        build_signal_table(con)
        candidate_ids = _candidate_ids_for_report(walk_forward) if walk_forward else [_candidate_id(edge) for edge in spec.edges]
        signals_by_id = load_signals_by_candidate(con, sorted(set(candidate_ids)))
        attach_entry_indexes(signals_by_id, bars)
    finally:
        con.close()

    stress_results = []
    for stress_multiple in (1, 2, 3):
        if walk_forward:
            stress_results.append(
                _run_walk_forward_stress(
                    stress_multiple=stress_multiple,
                    report=walk_forward,
                    spec=spec,
                    bars=bars,
                    signals_by_id=signals_by_id,
                    coverage_days=coverage_days,
                    min_full_year_trades=args.min_full_year_trades,
                    data_root=args.data_root,
                    symbol=args.symbol,
                    date_from=args.date_from,
                    date_to=args.date_to,
                )
            )
        else:
            edge_order = {candidate_id: index for index, candidate_id in enumerate(candidate_ids)}
            signals = combine_signals(
                [signals_by_id[candidate_id] for candidate_id in candidate_ids if candidate_id in signals_by_id],
                edge_order=edge_order,
            )
            edges = tuple(_to_regime_edge(edge) for edge in spec.edges)
            stress_results.append(
                _run_fixed_preset_stress(
                    stress_multiple=stress_multiple,
                    spec=spec,
                    bars=bars,
                    signals=signals,
                    edges=edges,
                    coverage_days=coverage_days,
                    min_full_year_trades=args.min_full_year_trades,
                    data_root=args.data_root,
                    symbol=args.symbol,
                    date_from=args.date_from,
                    date_to=args.date_to,
                )
            )

    quote_files = _execution_files(Path(args.data_root), "quotes", args.symbol)
    tick_files = _execution_files(Path(args.data_root), "ticks", args.symbol)
    quote_status = {
        "status": "blocked_no_quote_or_tick_files" if not quote_files and not tick_files else "available_not_consumed_by_this_stress_script",
        "quote_file_count": len(quote_files),
        "tick_file_count": len(tick_files),
        "quote_sample": [str(path) for path in quote_files[:5]],
        "tick_sample": [str(path) for path in tick_files[:5]],
    }
    cost_stress_passed = all(row["decision"]["passed"] for row in stress_results)
    payload = {
        "artifact": "expanded_high_edge_execution_stress",
        "preset": spec.preset,
        "source_label": spec.source_label,
        "source_report": spec.source_report,
        "walk_forward_report": str(args.walk_forward_report) if walk_forward else None,
        "date_from": args.date_from,
        "date_to": args.date_to,
        "symbol": args.symbol,
        "signal_count": sum(len(rows) for rows in signals_by_id.values()),
        "quote_replay": quote_status,
        "cost_stress_passed": cost_stress_passed,
        "stress_results": stress_results,
        "decision": {
            "passed": cost_stress_passed and quote_status["status"] != "blocked_no_quote_or_tick_files",
            "cost_stress_passed": cost_stress_passed,
            "quote_replay_passed": quote_status["status"] != "blocked_no_quote_or_tick_files",
            "reason": None if quote_status["status"] != "blocked_no_quote_or_tick_files" else "No normalized quote/tick parquet files are available locally.",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), **payload["decision"]}, indent=2))
    return 1 if args.fail_on_cost_stress and not cost_stress_passed else 0


def _run_walk_forward_stress(
    *,
    stress_multiple: int,
    report: dict[str, Any],
    spec: Any,
    bars: Sequence[dict[str, Any]],
    signals_by_id: dict[str, list[dict[str, Any]]],
    coverage_days: dict[int, int],
    min_full_year_trades: int,
    data_root: str,
    symbol: str,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    fold_results = []
    for fold in report.get("folds", []):
        if fold.get("status") != "ok":
            continue
        test_year = int(fold["test_year"])
        edge_dicts = list(fold.get("selected_edges") or [])
        edges = tuple(_to_regime_edge_dict(edge) for edge in edge_dicts)
        candidate_ids = list(fold.get("selected_candidate_ids") or [_candidate_id_from_mapping(edge) for edge in edge_dicts])
        edge_order = {candidate_id: index for index, candidate_id in enumerate(candidate_ids)}
        signals = combine_signals(
            [
                _filter_signals_by_year(signals_by_id[candidate_id], test_year)
                for candidate_id in candidate_ids
                if candidate_id in signals_by_id
            ],
            edge_order=edge_order,
        )
        params = dict(fold["selection"]["params"])
        config = LowRRegimeBasketConfig(
            data_root=data_root,
            symbol=symbol,
            date_from=date_from,
            date_to=date_to,
            preset=spec.preset,
            stop_range_multiple=float(params["stop_range_multiple"]),
            min_stop_points=float(params["min_stop_points"]),
            max_stop_points=float(params["max_stop_points"]),
            max_hold_minutes=int(params["max_hold_minutes"]),
            max_concurrent_positions=int(params["max_concurrent_positions"]),
            slippage_ticks_per_side=float(stress_multiple),
            flatten_on_date_change=bool(params["flatten_on_date_change"]),
        )
        result = replay_result(
            label=f"{spec.preset}_wf_{test_year}_slippage_{stress_multiple}x",
            spec={
                "name": fold["selection"]["label"],
                "source_label": spec.source_label,
                "candidate_ids": candidate_ids,
            },
            bars=bars,
            signals=signals,
            edges=edges,
            config=config,
            coverage_days=coverage_days,
            min_full_year_trades=min_full_year_trades,
            params={**params, "slippage_ticks_per_side": float(stress_multiple)},
        )
        row = _year_row(result, test_year)
        fold_results.append(
            {
                "test_year": test_year,
                "selection": fold["selection"],
                "trade_count": int(row.get("trade_count") or 0),
                "trade_floor_count": _trade_floor_count(test_year, int(row.get("trade_count") or 0), coverage_days),
                "net_pnl": float(row.get("net_pnl") or 0.0),
                "profit_factor": row.get("profit_factor"),
                "max_drawdown": row.get("max_drawdown"),
                "yearly_result": row,
            }
        )
    decision = _fold_decision(fold_results, min_full_year_trades)
    return {
        "stress_multiple": stress_multiple,
        "mode": "walk_forward_oos",
        "slippage_ticks_per_side": float(stress_multiple),
        "fold_results": fold_results,
        "decision": decision,
        "metrics": {
            "net_pnl": sum(row["net_pnl"] for row in fold_results),
            "trade_count": sum(row["trade_count"] for row in fold_results),
            "min_year_pnl": min((row["net_pnl"] for row in fold_results), default=None),
            "min_trade_floor_count": min((row["trade_floor_count"] for row in fold_results), default=None),
        },
    }


def _run_fixed_preset_stress(
    *,
    stress_multiple: int,
    spec: Any,
    bars: Sequence[dict[str, Any]],
    signals: Sequence[dict[str, Any]],
    edges: Sequence[RegimeEdge],
    coverage_days: dict[int, int],
    min_full_year_trades: int,
    data_root: str,
    symbol: str,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    config = LowRRegimeBasketConfig(
        data_root=data_root,
        symbol=symbol,
        date_from=date_from,
        date_to=date_to,
        preset=spec.preset,
        stop_range_multiple=spec.stop_range_multiple,
        min_stop_points=spec.min_stop_points,
        max_stop_points=spec.max_stop_points,
        max_hold_minutes=spec.max_hold_minutes,
        max_concurrent_positions=spec.max_concurrent_positions,
        slippage_ticks_per_side=float(stress_multiple),
        flatten_on_date_change=True,
    )
    result = replay_result(
        label=f"{spec.preset}_slippage_{stress_multiple}x",
        spec={
            "name": spec.preset,
            "source_label": spec.source_label,
            "candidate_ids": [_candidate_id(edge) for edge in spec.edges],
        },
        bars=bars,
        signals=signals,
        edges=edges,
        config=config,
        coverage_days=coverage_days,
        min_full_year_trades=min_full_year_trades,
        params={
            "max_hold_minutes": spec.max_hold_minutes,
            "stop_range_multiple": spec.stop_range_multiple,
            "min_stop_points": spec.min_stop_points,
            "max_stop_points": spec.max_stop_points,
            "flatten_on_date_change": True,
            "max_concurrent_positions": spec.max_concurrent_positions,
            "slippage_ticks_per_side": float(stress_multiple),
        },
    )
    decision = _decision(result, coverage_days, min_full_year_trades)
    return {
        "stress_multiple": stress_multiple,
        "mode": "fixed_preset_full_history",
        "slippage_ticks_per_side": float(stress_multiple),
        "metrics": result["metrics"],
        "constraints": result["constraints"],
        "edge_summary": result["edge_summary"],
        "exit_reasons": result["exit_reasons"],
        "yearly_results": result["yearly_results"],
        "decision": decision,
    }


def _decision(result: dict[str, Any], coverage_days: dict[int, int], min_full_year_trades: int) -> dict[str, Any]:
    yearly = {int(row["year"]): row for row in result["yearly_results"]}
    years = sorted(yearly)
    failed_positive_years = [year for year in years if float(yearly[year].get("net_pnl") or 0.0) <= 0]
    failed_trade_years = [
        year
        for year in years
        if _trade_floor_count(year, int(yearly[year].get("trade_count") or 0), coverage_days) <= min_full_year_trades
    ]
    return {
        "passed": not failed_positive_years and not failed_trade_years,
        "positive_years": len(years) - len(failed_positive_years),
        "checked_years": years,
        "trade_floor_years": len(years) - len(failed_trade_years),
        "failed_positive_years": failed_positive_years,
        "failed_trade_floor_years": failed_trade_years,
        "min_trade_floor_count": min(
            (_trade_floor_count(year, int(yearly[year].get("trade_count") or 0), coverage_days) for year in years),
            default=None,
        ),
    }


def _fold_decision(fold_results: Sequence[dict[str, Any]], min_full_year_trades: int) -> dict[str, Any]:
    failed_positive_years = [int(row["test_year"]) for row in fold_results if float(row["net_pnl"]) <= 0]
    failed_trade_years = [
        int(row["test_year"])
        for row in fold_results
        if float(row["trade_floor_count"]) <= min_full_year_trades
    ]
    return {
        "passed": not failed_positive_years and not failed_trade_years,
        "positive_years": len(fold_results) - len(failed_positive_years),
        "checked_years": [int(row["test_year"]) for row in fold_results],
        "trade_floor_years": len(fold_results) - len(failed_trade_years),
        "failed_positive_years": failed_positive_years,
        "failed_trade_floor_years": failed_trade_years,
        "min_trade_floor_count": min((float(row["trade_floor_count"]) for row in fold_results), default=None),
    }


def _trade_floor_count(year: int, trade_count: int, coverage_days: dict[int, int]) -> float:
    if year == 2026:
        days = min(max(coverage_days.get(year, 0), 1), 365)
        return trade_count * 365.0 / days
    return float(trade_count)


def _candidate_id(edge: Any) -> str:
    parts = [
        edge.scan_type,
        f"dir={edge.direction_label}",
        f"sess={edge.session_bucket}",
    ]
    if edge.dow is not None:
        parts.append(f"dow={edge.dow}")
    if edge.trend_bin is not None:
        parts.append(f"trend={edge.trend_bin}")
    if edge.volume_bin is not None:
        parts.append(f"vol={edge.volume_bin}")
    if edge.range_bin is not None:
        parts.append(f"range={edge.range_bin}")
    return "|".join(parts)


def _candidate_id_from_mapping(edge: dict[str, Any]) -> str:
    parts = [
        str(edge["scan_type"]),
        f"dir={edge['direction_label']}",
        f"sess={edge['session_bucket']}",
    ]
    if edge.get("dow") is not None:
        parts.append(f"dow={int(edge['dow'])}")
    if edge.get("trend_bin") is not None:
        parts.append(f"trend={int(edge['trend_bin'])}")
    if edge.get("volume_bin") is not None:
        parts.append(f"vol={int(edge['volume_bin'])}")
    if edge.get("range_bin") is not None:
        parts.append(f"range={int(edge['range_bin'])}")
    return "|".join(parts)


def _to_regime_edge(edge: Any) -> RegimeEdge:
    return RegimeEdge(
        scan_type=edge.scan_type,
        direction_label=edge.direction_label,
        horizon_minutes=120,
        session_bucket=edge.session_bucket,
        dow=-99 if edge.dow is None else int(edge.dow),
        trend_bin=0 if edge.trend_bin is None else int(edge.trend_bin),
        volume_bin=0 if edge.volume_bin is None else int(edge.volume_bin),
        range_bin=0 if edge.range_bin is None else int(edge.range_bin),
        take_profit_r=edge.take_profit_r,
    )


def _to_regime_edge_dict(edge: dict[str, Any]) -> RegimeEdge:
    return RegimeEdge(
        scan_type=str(edge["scan_type"]),
        direction_label=str(edge["direction_label"]),
        horizon_minutes=int(edge.get("horizon_minutes") or 120),
        session_bucket=str(edge["session_bucket"]),
        dow=-99 if edge.get("dow") is None else int(edge["dow"]),
        trend_bin=0 if edge.get("trend_bin") is None else int(edge["trend_bin"]),
        volume_bin=0 if edge.get("volume_bin") is None else int(edge["volume_bin"]),
        range_bin=0 if edge.get("range_bin") is None else int(edge["range_bin"]),
        take_profit_r=float(edge["take_profit_r"]),
    )


def _candidate_ids_for_report(report: dict[str, Any] | None) -> list[str]:
    if not report:
        return []
    ids = []
    for fold in report.get("folds", []):
        if fold.get("selected_candidate_ids"):
            ids.extend(str(candidate_id) for candidate_id in fold["selected_candidate_ids"])
            continue
        for edge in fold.get("selected_edges") or []:
            ids.append(_candidate_id_from_mapping(edge))
    return sorted(set(ids))


def _filter_signals_by_year(signals: Sequence[dict[str, Any]], year: int) -> list[dict[str, Any]]:
    return [signal for signal in signals if signal["entry_time"].year == year]


def _year_row(result: dict[str, Any], year: int) -> dict[str, Any]:
    for row in result.get("yearly_results", []):
        if int(row["year"]) == year:
            return row
    return {"year": year, "trade_count": 0, "net_pnl": 0.0, "profit_factor": None, "max_drawdown": 0.0}


def _execution_files(data_root: Path, kind: str, symbol: str) -> list[Path]:
    root = data_root / "normalized" / kind / symbol
    if not root.exists():
        return []
    return sorted(path for path in root.glob("date=*/part-000.parquet") if path.is_file())


if __name__ == "__main__":
    raise SystemExit(main())
