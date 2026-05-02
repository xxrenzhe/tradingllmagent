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
    build_feature_table,
    build_signal_table,
    coverage_days_by_year,
    load_bars,
)
from scripts.walk_forward_expanded_high_edge import (
    DEFAULT_FOLDS,
    fixed_horizon_cost_adjustment_usd,
    load_candidate_stats_for_years,
    low_r_grid,
    parse_float_grid,
    parse_scan_type_counts,
    run_fold,
    summarize_walk_forward,
)
from tlm.low_r_regime_basket import LowRRegimeBasketConfig, _json_default


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run cached expanded high-edge walk-forward grids and emit a comparable leaderboard."
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--date-from", default="2019-01-01")
    parser.add_argument("--date-to", default="2026-04-27")
    parser.add_argument("--max-preselect-grid", default="80,120")
    parser.add_argument("--max-specs-grid", default="8,12")
    parser.add_argument("--min-edge-count-grid", default="13")
    parser.add_argument("--selection-profile-grid", default="stress,floor,defensive")
    parser.add_argument("--slippage-ticks-grid", default="1,2,3")
    parser.add_argument("--take-profit-r-grid", default="0.75,1.0,1.25,1.5")
    parser.add_argument("--two-r-only", action="store_true")
    parser.add_argument("--full-low-r-grid", action="store_true")
    parser.add_argument("--max-scan-type-count", action="append", default=[])
    parser.add_argument("--min-scan-type-count", action="append", default=[])
    parser.add_argument("--exclude-scan-type", action="append", default=[])
    parser.add_argument("--min-full-year-trades", type=int, default=1000)
    parser.add_argument("--output", type=Path, default=Path("reports/nq_expanded_high_edge_cached_walk_forward_grid_2026-05-02.json"))
    args = parser.parse_args(argv)

    max_preselect_grid = parse_int_grid(args.max_preselect_grid, option_name="--max-preselect-grid")
    max_specs_grid = parse_int_grid(args.max_specs_grid, option_name="--max-specs-grid")
    min_edge_count_grid = parse_int_grid(args.min_edge_count_grid, option_name="--min-edge-count-grid")
    selection_profile_grid = parse_text_grid(args.selection_profile_grid, option_name="--selection-profile-grid")
    slippage_grid = parse_float_grid(args.slippage_ticks_grid, option_name="--slippage-ticks-grid")
    take_profit_r_grid = (2.0,) if args.two_r_only else parse_float_grid(args.take_profit_r_grid, option_name="--take-profit-r-grid")
    max_scan_type_counts = parse_scan_type_counts(args.max_scan_type_count, option_name="--max-scan-type-count")
    min_scan_type_counts = parse_scan_type_counts(args.min_scan_type_count, option_name="--min-scan-type-count")

    pattern = str(Path(args.data_root) / "bars" / "1m" / args.symbol / "date=*" / "part-000.parquet")
    con = duckdb.connect(":memory:")
    try:
        print("building expanded feature table", flush=True)
        build_feature_table(con, pattern, args.date_from, args.date_to)
        print("loading bars", flush=True)
        bars = load_bars(con)
        coverage_days = coverage_days_by_year(bars)
        print("building expanded signal table", flush=True)
        build_signal_table(con)
        rows = []
        for slippage_ticks in slippage_grid:
            base_config = LowRRegimeBasketConfig(
                data_root=args.data_root,
                symbol=args.symbol,
                date_from=args.date_from,
                date_to=args.date_to,
                preset="cached_walk_forward_expanded_high_edge",
                slippage_ticks_per_side=float(slippage_ticks),
                flatten_on_date_change=True,
            )
            cost_adjustment_usd = fixed_horizon_cost_adjustment_usd(base_config)
            all_candidate_stats = load_candidate_stats_for_years(
                con,
                tuple(range(2019, 2027)),
                cost_adjustment_usd=cost_adjustment_usd,
            )
            single_replay_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
            for profile in selection_profile_grid:
                for max_preselect in max_preselect_grid:
                    for max_specs in max_specs_grid:
                        for min_edge_count in min_edge_count_grid:
                            print(
                                "running config",
                                {
                                    "slippage": slippage_ticks,
                                    "profile": profile,
                                    "max_preselect": max_preselect,
                                    "max_specs": max_specs,
                                    "min_edge_count": min_edge_count,
                                },
                                flush=True,
                            )
                            folds = run_walk_forward_config(
                                con=con,
                                bars=bars,
                                coverage_days=coverage_days,
                                base_config=base_config,
                                max_preselect=max_preselect,
                                max_specs=max_specs,
                                min_edge_count=min_edge_count,
                                selection_profile=profile,
                                excluded_scan_types=tuple(args.exclude_scan_type),
                                max_scan_type_counts=max_scan_type_counts,
                                min_scan_type_counts=min_scan_type_counts,
                                take_profit_r_grid=take_profit_r_grid,
                                parameter_grid=low_r_grid(full_grid=args.full_low_r_grid),
                                max_positions_grid=(1, 2, 3, 6, 12, 24, 99) if args.full_low_r_grid else (6, 12, 24, 99),
                                min_full_year_trades=args.min_full_year_trades,
                                cost_adjustment_usd=cost_adjustment_usd,
                                single_replay_cache=single_replay_cache,
                            )
                            summary = summarize_walk_forward(folds, all_candidate_stats, args.min_full_year_trades)
                            rows.append(
                                {
                                    "config": {
                                        "slippage_ticks_per_side": slippage_ticks,
                                        "selection_profile": profile,
                                        "max_preselect": max_preselect,
                                        "max_specs": max_specs,
                                        "min_edge_count": min_edge_count,
                                        "take_profit_r_grid": list(take_profit_r_grid),
                                        "full_low_r_grid": args.full_low_r_grid,
                                        "max_scan_type_counts": max_scan_type_counts,
                                        "min_scan_type_counts": min_scan_type_counts,
                                        "excluded_scan_types": list(args.exclude_scan_type),
                                    },
                                    "summary": summary,
                                    "folds": folds,
                                }
                            )
        payload = {
            "artifact": "cached_expanded_high_edge_walk_forward_grid",
            "date_from": args.date_from,
            "date_to": args.date_to,
            "symbol": args.symbol,
            "leaderboard": sorted(rows, key=leaderboard_sort_key, reverse=True),
        }
    finally:
        con.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")
    best = payload["leaderboard"][0] if payload["leaderboard"] else None
    print(json.dumps({"output": str(args.output), "best": best_summary(best)}, indent=2, default=_json_default))
    return 0


def run_walk_forward_config(
    *,
    con: duckdb.DuckDBPyConnection,
    bars: Sequence[dict[str, Any]],
    coverage_days: dict[int, int],
    base_config: LowRRegimeBasketConfig,
    max_preselect: int,
    max_specs: int,
    min_edge_count: int,
    selection_profile: str,
    excluded_scan_types: Sequence[str],
    max_scan_type_counts: dict[str, int],
    min_scan_type_counts: dict[str, int],
    take_profit_r_grid: Sequence[float],
    parameter_grid: Sequence[dict[str, Any]],
    max_positions_grid: Sequence[int],
    min_full_year_trades: int,
    cost_adjustment_usd: float,
    single_replay_cache: dict[tuple[Any, ...], dict[str, Any]],
) -> list[dict[str, Any]]:
    folds = []
    previous_ids: tuple[str, ...] = ()
    for train_years, test_year in DEFAULT_FOLDS:
        fold = run_fold(
            con=con,
            bars=bars,
            coverage_days=coverage_days,
            base_config=base_config,
            train_years=train_years,
            test_year=test_year,
            max_preselect=max_preselect,
            max_specs=max_specs,
            min_edge_count=min_edge_count,
            selection_profile=selection_profile,
            excluded_scan_types=excluded_scan_types,
            max_scan_type_counts=max_scan_type_counts,
            min_scan_type_counts=min_scan_type_counts,
            take_profit_r_grid=take_profit_r_grid,
            parameter_grid=parameter_grid,
            max_positions_grid=max_positions_grid,
            min_full_year_trades=min_full_year_trades,
            previous_ids=previous_ids,
            cost_adjustment_usd=cost_adjustment_usd,
            single_replay_cache=single_replay_cache,
        )
        previous_ids = tuple(fold.get("selected_candidate_ids") or ())
        folds.append(fold)
    return folds


def leaderboard_sort_key(row: dict[str, Any]) -> tuple[float, ...]:
    summary = row["summary"]
    decision = summary["decision"]
    return (
        1.0 if decision["passed"] else 0.0,
        float(decision["positive_test_years"]),
        float(decision["trade_floor_years"]),
        float(summary["oos_min_year_pnl"]),
        float(summary["oos_total_net_pnl"]),
        float(summary["oos_min_year_trade_floor_count"]),
        -float(summary["multiple_testing"]["effective_trial_count_floor"]),
    )


def best_summary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    summary = row["summary"]
    return {
        "config": row["config"],
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
        if value <= 0:
            raise SystemExit(f"{option_name} values must be positive, got {raw!r}")
        values.append(value)
    if not values:
        raise SystemExit(f"{option_name} cannot be empty")
    return tuple(values)


def parse_text_grid(raw: str, *, option_name: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    allowed = {"net", "stable", "defensive", "stress", "floor"}
    invalid = [value for value in values if value not in allowed]
    if invalid:
        raise SystemExit(f"{option_name} contains invalid profiles: {', '.join(invalid)}")
    if not values:
        raise SystemExit(f"{option_name} cannot be empty")
    return values


if __name__ == "__main__":
    raise SystemExit(main())
