#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
import sys
from typing import Any, Sequence

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.search_expanded_high_edge_strategy import (
    CandidateStats,
    attach_entry_indexes,
    build_feature_table,
    build_signal_table,
    combine_signals,
    config_from_params,
    coverage_days_by_year,
    edge_from_stats,
    load_bars,
    load_signals_by_candidate,
    replay_low_r_exits_fast,
    select_candidate_stats,
    single_edge_sort_key,
)
from tlm.low_r_regime_basket import LowRRegimeBasketConfig, RegimeEdge, _json_default
from tlm.metrics import calculate_metrics


DEFAULT_FOLDS = (
    ((2019, 2020), 2021),
    ((2019, 2020, 2021), 2022),
    ((2019, 2020, 2021, 2022), 2023),
    ((2019, 2020, 2021, 2022, 2023), 2024),
    ((2019, 2020, 2021, 2022, 2023, 2024), 2025),
    ((2019, 2020, 2021, 2022, 2023, 2024, 2025), 2026),
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk-forward validate expanded high-edge NQ baskets.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--date-from", default="2019-01-01")
    parser.add_argument("--date-to", default="2026-04-27")
    parser.add_argument("--max-preselect", type=int, default=40)
    parser.add_argument("--max-specs", type=int, default=6)
    parser.add_argument("--min-edge-count", type=int, default=1)
    parser.add_argument("--selection-profile", choices=("net", "stable", "defensive", "stress", "floor"), default="net")
    parser.add_argument("--exclude-scan-type", action="append", default=[])
    parser.add_argument(
        "--max-scan-type-count",
        action="append",
        default=[],
        metavar="SCAN_TYPE=N",
        help="Limit selected combo edges from a scan family, e.g. prior_day_breakout=2.",
    )
    parser.add_argument(
        "--min-scan-type-count",
        action="append",
        default=[],
        metavar="SCAN_TYPE=N",
        help="Require selected combo edges from a scan family, e.g. prior_day_breakout=1.",
    )
    parser.add_argument("--slippage-ticks-per-side", type=float, default=1.0)
    parser.add_argument(
        "--take-profit-r-grid",
        default="0.5,0.75,1.0,1.25,1.5",
        help="Comma-separated take-profit R values tested for each single edge.",
    )
    parser.add_argument("--full-grid", action="store_true")
    parser.add_argument("--min-full-year-trades", type=int, default=1000)
    parser.add_argument("--output", type=Path, default=Path("reports/nq_expanded_high_edge_walk_forward_2026-05-01.json"))
    args = parser.parse_args(argv)
    max_scan_type_counts = parse_scan_type_counts(args.max_scan_type_count, option_name="--max-scan-type-count")
    min_scan_type_counts = parse_scan_type_counts(args.min_scan_type_count, option_name="--min-scan-type-count")
    take_profit_r_grid = parse_float_grid(args.take_profit_r_grid, option_name="--take-profit-r-grid")

    config = LowRRegimeBasketConfig(
        data_root=args.data_root,
        symbol=args.symbol,
        date_from=args.date_from,
        date_to=args.date_to,
        preset="walk_forward_expanded_high_edge",
        slippage_ticks_per_side=args.slippage_ticks_per_side,
        flatten_on_date_change=True,
    )
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
        print("loading global candidate stats", flush=True)
        cost_adjustment_usd = fixed_horizon_cost_adjustment_usd(config)
        all_candidate_stats = load_candidate_stats_for_years(
            con,
            tuple(range(2019, 2027)),
            cost_adjustment_usd=cost_adjustment_usd,
        )
        folds = []
        previous_ids: tuple[str, ...] = ()
        parameter_grid = low_r_grid(full_grid=args.full_grid)
        max_positions_grid = (1, 2, 3, 6, 12, 24, 99) if args.full_grid else (6, 12, 24, 99)
        for train_years, test_year in DEFAULT_FOLDS:
            print(f"running fold train={train_years} test={test_year}", flush=True)
            fold = run_fold(
                con=con,
                bars=bars,
                coverage_days=coverage_days,
                base_config=config,
                train_years=train_years,
                test_year=test_year,
                max_preselect=args.max_preselect,
                max_specs=args.max_specs,
                min_edge_count=args.min_edge_count,
                selection_profile=args.selection_profile,
                excluded_scan_types=tuple(args.exclude_scan_type),
                max_scan_type_counts=max_scan_type_counts,
                min_scan_type_counts=min_scan_type_counts,
                take_profit_r_grid=take_profit_r_grid,
                parameter_grid=parameter_grid,
                max_positions_grid=max_positions_grid,
                min_full_year_trades=args.min_full_year_trades,
                previous_ids=previous_ids,
                cost_adjustment_usd=cost_adjustment_usd,
            )
            previous_ids = tuple(fold["selected_candidate_ids"])
            folds.append(fold)
            print(
                f"finished fold test={test_year} status={fold['status']} "
                f"combos={fold.get('evaluated_combo_count', 0)}",
                flush=True,
            )
    finally:
        con.close()

    summary = summarize_walk_forward(folds, all_candidate_stats, args.min_full_year_trades)
    payload = {
        "artifact": "expanded_high_edge_walk_forward_validation",
        "date_from": args.date_from,
        "date_to": args.date_to,
        "symbol": args.symbol,
        "method": {
            "folds": [
                {"train_years": list(train_years), "test_year": test_year}
                for train_years, test_year in DEFAULT_FOLDS
            ],
            "selection": "For each fold, candidate groups and combo specs are selected only on train years, then replayed on the next test year.",
            "max_preselect": args.max_preselect,
            "max_specs": args.max_specs,
            "min_edge_count": args.min_edge_count,
            "selection_profile": args.selection_profile,
            "excluded_scan_types": list(args.exclude_scan_type),
            "max_scan_type_counts": max_scan_type_counts,
            "min_scan_type_counts": min_scan_type_counts,
            "slippage_ticks_per_side": args.slippage_ticks_per_side,
            "take_profit_r_grid": list(take_profit_r_grid),
            "full_grid": args.full_grid,
            "min_full_year_trades": args.min_full_year_trades,
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
    con: duckdb.DuckDBPyConnection,
    bars: Sequence[dict[str, Any]],
    coverage_days: dict[int, int],
    base_config: LowRRegimeBasketConfig,
    train_years: Sequence[int],
    test_year: int,
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
    previous_ids: Sequence[str],
    cost_adjustment_usd: float,
) -> dict[str, Any]:
    excluded = set(excluded_scan_types)
    raw_candidate_stats = [
        row
        for row in load_candidate_stats_for_years(con, train_years, cost_adjustment_usd=cost_adjustment_usd)
        if row.scan_type not in excluded
    ]
    candidate_stats = select_candidate_stats_for_profile(
        raw_candidate_stats,
        max_preselect,
        selection_profile,
    )
    signals_by_id = load_signals_by_candidate(con, [row.candidate_id for row in candidate_stats])
    attach_entry_indexes(signals_by_id, bars)
    train_signals_by_id = {
        stats.candidate_id: filter_signals_by_years(signals_by_id.get(stats.candidate_id, ()), train_years)
        for stats in candidate_stats
    }

    best_singles_by_param: dict[tuple[tuple[str, Any], ...], list[dict[str, Any]]] = {}
    evaluated_single_count = 0
    evaluated_combo_count = 0
    best_train_combo = None
    for params in parameter_grid:
        param_key = tuple(sorted(params.items()))
        singles = []
        for stats in candidate_stats:
            train_signals = train_signals_by_id.get(stats.candidate_id, [])
            if len(train_signals) < 20:
                continue
            best_single = None
            for take_profit_r in take_profit_r_grid:
                edge = edge_from_stats(stats, take_profit_r)
                result = replay_summary_result(
                    label=stats.candidate_id,
                    spec={"name": "walk_forward_single", "candidate_ids": (stats.candidate_id,)},
                    bars=bars,
                    signals=train_signals,
                    edges=(edge,),
                    config=config_from_params(base_config, {**params, "max_concurrent_positions": 1}),
                    coverage_days=coverage_days,
                    min_full_year_trades=min_full_year_trades,
                    params={**params, "max_concurrent_positions": 1},
                )
                evaluated_single_count += 1
                result["source_candidate_id"] = stats.candidate_id
                result["source_stats"] = asdict(stats)
                if best_single is None or single_train_sort_key(result, train_years, selection_profile) > single_train_sort_key(best_single, train_years, selection_profile):
                    best_single = result
            if best_single is not None:
                singles.append(best_single)
        best_singles_by_param[param_key] = singles
        single_by_id = {row["source_candidate_id"]: row for row in singles}
        specs = build_walk_forward_specs(
            singles,
            train_years,
            selection_profile,
            min_edge_count,
            max_scan_type_counts=max_scan_type_counts,
            min_scan_type_counts=min_scan_type_counts,
        )[:max_specs]
        for max_positions in max_positions_grid:
            combo_config = config_from_params(base_config, {**params, "max_concurrent_positions": max_positions})
            for spec in specs:
                selected = [single_by_id[candidate_id] for candidate_id in spec["candidate_ids"] if candidate_id in single_by_id]
                if len(selected) != len(spec["candidate_ids"]):
                    continue
                selected_edges = tuple(RegimeEdge(**row["edges"][0]) for row in selected)
                selected_signals = combine_signals(
                    [train_signals_by_id[row["source_candidate_id"]] for row in selected],
                    edge_order={row["source_candidate_id"]: index for index, row in enumerate(selected)},
                )
                train_result = replay_summary_result(
                    label=spec["name"],
                    spec={**spec, "edge_candidate_ids": tuple(row["source_candidate_id"] for row in selected)},
                    bars=bars,
                    signals=selected_signals,
                    edges=selected_edges,
                    config=combo_config,
                    coverage_days=coverage_days,
                    min_full_year_trades=min_full_year_trades,
                    params={**params, "max_concurrent_positions": max_positions},
                )
                evaluated_combo_count += 1
                if best_train_combo is None or train_combo_sort_key(
                    train_result,
                    train_years,
                    selection_profile,
                    min_full_year_trades,
                ) > train_combo_sort_key(
                    best_train_combo,
                    train_years,
                    selection_profile,
                    min_full_year_trades,
                ):
                    best_train_combo = train_result

    if best_train_combo is None:
        return {
            "train_years": list(train_years),
            "test_year": test_year,
            "status": "no_train_combo",
            "candidate_group_count": len(candidate_stats),
            "evaluated_single_count": evaluated_single_count,
            "evaluated_combo_count": evaluated_combo_count,
        }

    selected_ids = tuple(best_train_combo["spec"]["edge_candidate_ids"])
    param_key = tuple(sorted({key: value for key, value in best_train_combo["params"].items() if key != "max_concurrent_positions"}.items()))
    single_by_id = {row["source_candidate_id"]: row for row in best_singles_by_param[param_key]}
    selected = [single_by_id[candidate_id] for candidate_id in selected_ids]
    selected_edges = tuple(RegimeEdge(**row["edges"][0]) for row in selected)
    test_signals = combine_signals(
        [filter_signals_by_years(signals_by_id[candidate_id], (test_year,)) for candidate_id in selected_ids],
        edge_order={candidate_id: index for index, candidate_id in enumerate(selected_ids)},
    )
    test_config = config_from_params(base_config, best_train_combo["params"])
    test_result = replay_summary_result(
        label=best_train_combo["label"],
        spec=best_train_combo["spec"],
        bars=bars,
        signals=test_signals,
        edges=selected_edges,
        config=test_config,
        coverage_days=coverage_days,
        min_full_year_trades=min_full_year_trades,
        params=best_train_combo["params"],
    )
    return {
        "train_years": list(train_years),
        "test_year": test_year,
        "status": "ok",
        "candidate_group_count": len(candidate_stats),
        "evaluated_single_count": evaluated_single_count,
        "evaluated_combo_count": evaluated_combo_count,
        "selection": {
            "label": best_train_combo["label"],
            "edge_count": best_train_combo["edge_count"],
            "params": best_train_combo["params"],
            "edge_summary": best_train_combo["edge_summary"],
        },
        "selected_candidate_ids": list(selected_ids),
        "selected_edges": [asdict(edge) for edge in selected_edges],
        "selected_edge_turnover": selected_edge_turnover(previous_ids, selected_ids),
        "train_metrics": compact_metrics(best_train_combo),
        "test_metrics": compact_metrics(test_result),
        "test_yearly_result": compact_year(test_result, test_year, coverage_days),
    }


def load_candidate_stats_for_years(
    con: duckdb.DuckDBPyConnection,
    years: Sequence[int],
    *,
    cost_adjustment_usd: float = 0.0,
) -> list[CandidateStats]:
    year_values = ", ".join(str(int(year)) for year in years)
    rows = con.execute(
        f"""
        SELECT candidate_id,
               any_value(group_level) AS group_level,
               any_value(scan_type) AS scan_type,
               any_value(direction_label) AS direction_label,
               any_value(session_bucket) AS session_bucket,
               CASE WHEN contains(candidate_id, '|dow=') THEN any_value(dow) ELSE NULL END AS dow,
               CASE WHEN contains(candidate_id, '|trend=') THEN any_value(trend_bin) ELSE NULL END AS trend_bin,
               CASE WHEN contains(candidate_id, '|vol=') THEN any_value(volume_bin) ELSE NULL END AS volume_bin,
               CASE WHEN contains(candidate_id, '|range=') THEN any_value(range_bin) ELSE NULL END AS range_bin,
               year,
               count(*) AS trades,
               sum(fixed_pnl - ?) AS net_pnl,
               sum(CASE WHEN fixed_pnl - ? > 0 THEN fixed_pnl - ? ELSE 0 END) AS gross_profit,
               sum(CASE WHEN fixed_pnl - ? < 0 THEN fixed_pnl - ? ELSE 0 END) AS gross_loss
        FROM candidate_signals
        WHERE year IN ({year_values})
        GROUP BY candidate_id, year
        ORDER BY candidate_id, year
        """,
        [
            cost_adjustment_usd,
            cost_adjustment_usd,
            cost_adjustment_usd,
            cost_adjustment_usd,
            cost_adjustment_usd,
        ],
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    year_set = tuple(int(year) for year in years)
    for row in rows:
        candidate_id = str(row[0])
        item = grouped.setdefault(
            candidate_id,
            {
                "candidate_id": candidate_id,
                "group_level": str(row[1]),
                "scan_type": str(row[2]),
                "direction_label": str(row[3]),
                "session_bucket": str(row[4]),
                "dow": None if row[5] is None else int(row[5]),
                "trend_bin": None if row[6] is None else int(row[6]),
                "volume_bin": None if row[7] is None else int(row[7]),
                "range_bin": None if row[8] is None else int(row[8]),
                "yearly_pnl": {year: 0.0 for year in year_set},
                "yearly_trades": {year: 0 for year in year_set},
                "gross_profit": 0.0,
                "gross_loss": 0.0,
            },
        )
        year = int(row[9])
        if year not in item["yearly_pnl"]:
            continue
        item["yearly_trades"][year] = int(row[10])
        item["yearly_pnl"][year] = float(row[11] or 0.0)
        item["gross_profit"] += float(row[12] or 0.0)
        item["gross_loss"] += float(row[13] or 0.0)
    stats = []
    for item in grouped.values():
        total_trades = sum(item["yearly_trades"].values())
        fixed_net = sum(item["yearly_pnl"].values())
        gross_loss = float(item["gross_loss"])
        stats.append(
            CandidateStats(
                candidate_id=str(item["candidate_id"]),
                group_level=str(item["group_level"]),
                scan_type=str(item["scan_type"]),
                direction_label=str(item["direction_label"]),
                session_bucket=str(item["session_bucket"]),
                dow=item["dow"],
                trend_bin=item["trend_bin"],
                volume_bin=item["volume_bin"],
                range_bin=item["range_bin"],
                total_trades=total_trades,
                fixed_net_pnl=fixed_net,
                fixed_avg_pnl=fixed_net / total_trades if total_trades else 0.0,
                fixed_profit_factor=float(item["gross_profit"]) / abs(gross_loss) if gross_loss else None,
                fixed_positive_years=sum(1 for value in item["yearly_pnl"].values() if value > 0),
                fixed_worst_year_pnl=min(item["yearly_pnl"].values()),
                fixed_min_full_year_trades=min(item["yearly_trades"].values()),
            )
        )
    return stats


def parse_scan_type_counts(values: Sequence[str], *, option_name: str) -> dict[str, int]:
    parsed: dict[str, int] = {}
    for raw in values:
        if "=" not in raw:
            raise SystemExit(f"{option_name} expects SCAN_TYPE=N, got {raw!r}")
        scan_type, count_text = raw.split("=", 1)
        scan_type = scan_type.strip()
        if not scan_type:
            raise SystemExit(f"{option_name} scan type cannot be empty")
        try:
            count = int(count_text)
        except ValueError as exc:
            raise SystemExit(f"{option_name} count must be an integer, got {raw!r}") from exc
        if count < 0:
            raise SystemExit(f"{option_name} count must be non-negative, got {raw!r}")
        parsed[scan_type] = count
    return parsed


def parse_float_grid(raw: str, *, option_name: str) -> tuple[float, ...]:
    values: list[float] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = float(item)
        except ValueError as exc:
            raise SystemExit(f"{option_name} expects comma-separated floats, got {raw!r}") from exc
        if value <= 0:
            raise SystemExit(f"{option_name} values must be positive, got {raw!r}")
        values.append(value)
    if not values:
        raise SystemExit(f"{option_name} cannot be empty")
    return tuple(values)


def select_candidate_stats_for_profile(
    stats: Sequence[CandidateStats],
    max_preselect: int,
    selection_profile: str,
) -> list[CandidateStats]:
    if selection_profile == "net":
        return select_candidate_stats(stats, max_preselect)
    if selection_profile in {"stress", "floor"}:
        viable = [
            row
            for row in stats
            if row.total_trades >= 250
            and row.fixed_net_pnl > 0
            and row.fixed_avg_pnl >= 0.0
            and (row.fixed_profit_factor or 0.0) >= 1.0
            and row.fixed_min_full_year_trades >= 15
        ]

        def stress_rank(row: CandidateStats) -> tuple[float, ...]:
            return (
                float(row.fixed_positive_years),
                float(row.fixed_worst_year_pnl),
                float(row.fixed_min_full_year_trades),
                float(row.fixed_net_pnl),
                float(row.fixed_avg_pnl),
                float(row.fixed_profit_factor or 0.0),
                -float(row.total_trades),
            )

        return sorted({row.candidate_id: row for row in viable}.values(), key=stress_rank, reverse=True)[:max_preselect]
    viable = [
        row
        for row in stats
        if row.total_trades >= 250
        and row.fixed_net_pnl > 0
        and row.fixed_avg_pnl >= 5.0
        and (row.fixed_profit_factor or 0.0) >= 1.02
        and row.fixed_min_full_year_trades >= 15
    ]

    def stable_rank(row: CandidateStats) -> tuple[float, ...]:
        return (
            float(row.fixed_positive_years),
            float(row.fixed_worst_year_pnl),
            float(row.fixed_min_full_year_trades),
            float(row.fixed_profit_factor or 0.0),
            float(row.fixed_avg_pnl),
            float(row.fixed_net_pnl),
            float(row.total_trades),
        )

    def defensive_rank(row: CandidateStats) -> tuple[float, ...]:
        return (
            float(row.fixed_worst_year_pnl),
            float(row.fixed_positive_years),
            float(row.fixed_profit_factor or 0.0),
            float(row.fixed_min_full_year_trades),
            float(row.fixed_avg_pnl),
            float(row.fixed_net_pnl),
            float(row.total_trades),
        )

    rank = defensive_rank if selection_profile == "defensive" else stable_rank
    return sorted({row.candidate_id: row for row in viable}.values(), key=rank, reverse=True)[:max_preselect]


def replay_summary_result(
    label: str,
    spec: dict[str, Any],
    bars: Sequence[dict[str, Any]],
    signals: Sequence[dict[str, Any]],
    edges: Sequence[RegimeEdge],
    config: LowRRegimeBasketConfig,
    coverage_days: dict[int, int],
    min_full_year_trades: int,
    params: dict[str, Any],
) -> dict[str, Any]:
    trades = replay_low_r_exits_fast(bars, signals, edges, config)
    trade_pnls = [trade.net_pnl for trade in trades]
    equity = [config.starting_equity]
    for pnl in trade_pnls:
        equity.append(equity[-1] + pnl)
    days = len({bar["timestamp"].date() for bar in bars}) or 1
    metrics = calculate_metrics(trade_pnls, equity, config.starting_equity, days).to_dict()
    yearly = yearly_results_from_trades(trades)
    hold_values = [trade.bars_held for trade in trades]
    return {
        "label": label,
        "spec": spec,
        "edge_count": len(edges),
        "params": dict(params),
        "metrics": metrics,
        "avg_hold_bars": sum(hold_values) / len(hold_values) if hold_values else 0.0,
        "median_hold_bars": sorted(hold_values)[len(hold_values) // 2] if hold_values else 0,
        "edge_summary": edge_summary(edges),
        "exit_reasons": dict(sorted(Counter(trade.exit_reason for trade in trades).items())),
        "yearly_results": yearly,
        "constraints": constraint_summary_for_years(yearly, min_full_year_trades, coverage_days),
        "edges": [asdict(edge) for edge in edges],
    }


def yearly_results_from_trades(trades: Sequence[Any]) -> list[dict[str, Any]]:
    by_year: dict[int, list[float]] = {}
    for trade in trades:
        by_year.setdefault(trade.exit_time.year, []).append(trade.net_pnl)
    rows = []
    for year, pnls in sorted(by_year.items()):
        equity = [0.0]
        for pnl in pnls:
            equity.append(equity[-1] + pnl)
        rows.append({"year": year, **calculate_metrics(pnls, equity, 100_000.0, 365).to_dict()})
    return rows


def edge_summary(edges: Sequence[RegimeEdge]) -> dict[str, dict[Any, int]]:
    return {
        "scan_types": dict(sorted(Counter(edge.scan_type for edge in edges).items())),
        "take_profit_r": dict(sorted(Counter(edge.take_profit_r for edge in edges).items())),
        "sessions": dict(sorted(Counter(edge.session_bucket for edge in edges).items())),
    }


def constraint_summary_for_years(
    yearly: Sequence[dict[str, Any]],
    min_full_year_trades: int,
    coverage_days: dict[int, int],
) -> dict[str, Any]:
    by_year = {int(row["year"]): row for row in yearly}
    full_years = tuple(range(2019, 2026))
    partial_years = (2026,)
    full_counts = [int(by_year.get(year, {}).get("trade_count") or 0) for year in full_years]
    partial_annualized = [
        annualized_trade_count(
            int(by_year.get(year, {}).get("trade_count") or 0),
            coverage_days.get(year, 365),
        )
        for year in partial_years
    ]
    pnl_values = [float(by_year.get(year, {}).get("net_pnl") or 0.0) for year in (*full_years, *partial_years)]
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


def filter_signals_by_years(signals: Sequence[dict[str, Any]], years: Sequence[int]) -> list[dict[str, Any]]:
    year_set = {int(year) for year in years}
    return [signal for signal in signals if signal["entry_time"].year in year_set]


def rank_specs(specs: Sequence[dict[str, Any]], single_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    def key(spec: dict[str, Any]) -> tuple[float, ...]:
        singles = [single_by_id[candidate_id] for candidate_id in spec["candidate_ids"] if candidate_id in single_by_id]
        return (
            float(len(singles)),
            sum(float(row["metrics"].get("net_pnl") or 0.0) for row in singles),
            sum(float(row["metrics"].get("avg_trade_net_pnl") or 0.0) for row in singles),
        )

    return sorted(specs, key=key, reverse=True)


def low_r_grid(*, full_grid: bool) -> list[dict[str, Any]]:
    if full_grid:
        rows = []
        for max_hold_minutes in (60, 120, 300):
            for stop_range_multiple in (6.0, 10.0):
                rows.append(
                    {
                        "max_hold_minutes": max_hold_minutes,
                        "stop_range_multiple": stop_range_multiple,
                        "min_stop_points": 8.0,
                        "max_stop_points": 90.0,
                        "flatten_on_date_change": True,
                    }
                )
        return rows
    return [
        {
            "max_hold_minutes": 120,
            "stop_range_multiple": 6.0,
            "min_stop_points": 8.0,
            "max_stop_points": 90.0,
            "flatten_on_date_change": True,
        }
    ]


def build_walk_forward_specs(
    single_edges: Sequence[dict[str, Any]],
    train_years: Sequence[int],
    selection_profile: str,
    min_edge_count: int,
    *,
    max_scan_type_counts: dict[str, int] | None = None,
    min_scan_type_counts: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    max_scan_type_counts = max_scan_type_counts or {}
    min_scan_type_counts = min_scan_type_counts or {}
    ranked = sorted(
        single_edges,
        key=lambda row: single_train_sort_key(row, train_years, selection_profile),
        reverse=True,
    )
    specs: list[dict[str, Any]] = []
    for size in (4, 6, 8, 10, 12, 13, 14, 16, 20, 24, 32):
        if size < min_edge_count:
            continue
        prefix = select_ranked_with_scan_type_limits(
            ranked,
            target_size=size,
            max_scan_type_counts=max_scan_type_counts,
        )
        if len(prefix) < size or not scan_type_minimums_pass(prefix, min_scan_type_counts):
            continue
        ids = tuple(row["source_candidate_id"] for row in prefix)
        specs.append({"name": f"walk_forward_{selection_profile}_top_{len(ids)}", "candidate_ids": ids})
    return specs


def select_ranked_with_scan_type_limits(
    ranked: Sequence[dict[str, Any]],
    *,
    target_size: int,
    max_scan_type_counts: dict[str, int],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for row in ranked:
        scan_type = str(row["edges"][0]["scan_type"])
        max_count = max_scan_type_counts.get(scan_type)
        if max_count is not None and counts[scan_type] >= max_count:
            continue
        selected.append(row)
        counts[scan_type] += 1
        if len(selected) >= target_size:
            break
    return selected


def scan_type_minimums_pass(rows: Sequence[dict[str, Any]], minimums: dict[str, int]) -> bool:
    if not minimums:
        return True
    counts = Counter(str(row["edges"][0]["scan_type"]) for row in rows)
    return all(counts[scan_type] >= minimum for scan_type, minimum in minimums.items())


def single_train_sort_key(
    row: dict[str, Any],
    train_years: Sequence[int],
    selection_profile: str,
) -> tuple[float, ...]:
    if selection_profile == "net":
        return single_edge_sort_key(row)
    by_year = {int(item["year"]): item for item in row["yearly_results"]}
    train_pnls = [float(by_year.get(year, {}).get("net_pnl") or 0.0) for year in train_years]
    train_counts = [int(by_year.get(year, {}).get("trade_count") or 0) for year in train_years]
    metrics = row["metrics"]
    positive_train_years = sum(1 for value in train_pnls if value > 0)
    worst_train_pnl = min(train_pnls) if train_pnls else 0.0
    min_train_trades = min(train_counts) if train_counts else 0
    train_avg_pnls = [
        pnl / trade_count
        for pnl, trade_count in zip(train_pnls, train_counts)
        if trade_count > 0
    ]
    if selection_profile == "floor":
        return (
            float(positive_train_years),
            float(min_train_trades),
            float(worst_train_pnl),
            float(metrics.get("net_pnl") or 0.0),
            float(metrics.get("avg_trade_net_pnl") or 0.0),
            float(metrics.get("profit_factor") or 0.0),
            float(metrics.get("net_pnl_to_max_drawdown") or 0.0),
            -float(row.get("avg_hold_bars") or 0.0),
        )
    if selection_profile == "stress":
        return (
            float(positive_train_years),
            float(worst_train_pnl),
            float(min_train_trades),
            float(metrics.get("net_pnl") or 0.0),
            float(min(train_avg_pnls) if train_avg_pnls else 0.0),
            float(metrics.get("avg_trade_net_pnl") or 0.0),
            float(metrics.get("profit_factor") or 0.0),
            float(metrics.get("net_pnl_to_max_drawdown") or 0.0),
            -float(row.get("avg_hold_bars") or 0.0),
        )
    if selection_profile == "defensive":
        return (
            float(worst_train_pnl),
            float(positive_train_years),
            float(metrics.get("profit_factor") or 0.0),
            float(metrics.get("net_pnl_to_max_drawdown") or 0.0),
            float(min_train_trades),
            float(metrics.get("avg_trade_net_pnl") or 0.0),
            float(metrics.get("net_pnl") or 0.0),
            -float(row.get("avg_hold_bars") or 0.0),
        )
    return (
        float(positive_train_years),
        float(worst_train_pnl),
        float(min_train_trades),
        float(metrics.get("profit_factor") or 0.0),
        float(metrics.get("avg_trade_net_pnl") or 0.0),
        float(metrics.get("net_pnl_to_max_drawdown") or 0.0),
        float(metrics.get("net_pnl") or 0.0),
        -float(row.get("avg_hold_bars") or 0.0),
    )


def train_combo_sort_key(
    row: dict[str, Any],
    train_years: Sequence[int],
    selection_profile: str,
    min_full_year_trades: int,
) -> tuple[float, ...]:
    by_year = {int(item["year"]): item for item in row["yearly_results"]}
    train_pnls = [float(by_year.get(year, {}).get("net_pnl") or 0.0) for year in train_years]
    train_counts = [int(by_year.get(year, {}).get("trade_count") or 0) for year in train_years]
    metrics = row["metrics"]
    all_positive = all(value > 0 for value in train_pnls)
    min_train_trades = min(train_counts) if train_counts else 0
    train_avg_pnls = [
        pnl / trade_count
        for pnl, trade_count in zip(train_pnls, train_counts)
        if trade_count > 0
    ]
    if selection_profile == "floor":
        return (
            1.0 if all_positive else 0.0,
            1.0 if min_train_trades > min_full_year_trades else 0.0,
            float(min_train_trades),
            float(min(train_pnls) if train_pnls else 0.0),
            float(metrics.get("net_pnl") or 0.0),
            float(metrics.get("avg_trade_net_pnl") or 0.0),
            float(metrics.get("profit_factor") or 0.0),
            float(metrics.get("net_pnl_to_max_drawdown") or 0.0),
            -float(metrics.get("trade_count") or 0.0),
        )
    if selection_profile == "stress":
        return (
            1.0 if all_positive else 0.0,
            1.0 if min_train_trades > min_full_year_trades else 0.0,
            float(min(train_pnls) if train_pnls else 0.0),
            float(min_train_trades),
            float(metrics.get("net_pnl") or 0.0),
            float(min(train_avg_pnls) if train_avg_pnls else 0.0),
            float(metrics.get("avg_trade_net_pnl") or 0.0),
            float(metrics.get("profit_factor") or 0.0),
            float(metrics.get("net_pnl_to_max_drawdown") or 0.0),
            -float(metrics.get("trade_count") or 0.0),
        )
    return (
        1.0 if all_positive else 0.0,
        float(min(train_pnls) if train_pnls else 0.0),
        float(min_train_trades),
        float(metrics.get("net_pnl") or 0.0),
        float(metrics.get("profit_factor") or 0.0),
        float(metrics.get("net_pnl_to_max_drawdown") or 0.0),
        -float(row.get("avg_hold_bars") or 0.0),
    )


def fixed_horizon_cost_adjustment_usd(config: LowRRegimeBasketConfig) -> float:
    replay_round_trip_cost = (
        config.round_trip_fees_usd
        + config.slippage_ticks_per_side * config.tick_size * 2.0 * config.point_value
    )
    fixed_horizon_base_cost = 15.0
    return max(replay_round_trip_cost - fixed_horizon_base_cost, 0.0)


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


def compact_year(result: dict[str, Any], year: int, coverage_days: dict[int, int]) -> dict[str, Any]:
    by_year = {int(item["year"]): item for item in result["yearly_results"]}
    row = by_year.get(year)
    if not row:
        return {
            "year": year,
            "trade_count": 0,
            "annualized_trade_count": 0.0,
            "coverage_days": coverage_days.get(year, 0),
            "net_pnl": 0.0,
            "profit_factor": None,
        }
    trade_count = int(row.get("trade_count") or 0)
    annualized_count = annualized_trade_count(trade_count, coverage_days.get(year, 365)) if year == 2026 else float(trade_count)
    return {
        "year": year,
        "trade_count": trade_count,
        "annualized_trade_count": annualized_count,
        "coverage_days": coverage_days.get(year, 365),
        "net_pnl": row.get("net_pnl"),
        "profit_factor": row.get("profit_factor"),
        "avg_trade_net_pnl": row.get("avg_trade_net_pnl"),
        "max_drawdown": row.get("max_drawdown"),
    }


def summarize_walk_forward(
    folds: Sequence[dict[str, Any]],
    all_candidate_stats: Sequence[CandidateStats],
    min_full_year_trades: int,
) -> dict[str, Any]:
    ok_folds = [fold for fold in folds if fold.get("status") == "ok"]
    test_rows = [fold["test_yearly_result"] for fold in ok_folds]
    test_pnls = [float(row.get("net_pnl") or 0.0) for row in test_rows]
    test_counts = [int(row.get("trade_count") or 0) for row in test_rows]
    trade_floor_counts = [float(row.get("annualized_trade_count") or row.get("trade_count") or 0) for row in test_rows]
    turnover_values = [
        float(fold["selected_edge_turnover"]["jaccard_similarity"])
        for fold in ok_folds
        if fold["selected_edge_turnover"]["jaccard_similarity"] is not None
    ]
    evaluated_combos = sum(int(fold.get("evaluated_combo_count") or 0) for fold in ok_folds)
    positive_test_years = sum(1 for value in test_pnls if value > 0)
    trade_floor_years = sum(1 for value in trade_floor_counts if value > min_full_year_trades)
    failed_positive_years = [
        int(row["year"])
        for row in test_rows
        if float(row.get("net_pnl") or 0.0) <= 0
    ]
    failed_trade_floor_years = [
        int(row["year"])
        for row in test_rows
        if float(row.get("annualized_trade_count") or row.get("trade_count") or 0) <= min_full_year_trades
    ]
    decision = {
        "passed": positive_test_years == len(test_pnls) and trade_floor_years == len(test_counts),
        "positive_test_years": positive_test_years,
        "test_year_count": len(test_pnls),
        "trade_floor_years": trade_floor_years,
        "failed_positive_years": failed_positive_years,
        "failed_trade_floor_years": failed_trade_floor_years,
    }
    effective_trials = len(all_candidate_stats) + evaluated_combos
    return {
        "decision": decision,
        "oos_total_net_pnl": sum(test_pnls),
        "oos_min_year_pnl": min(test_pnls) if test_pnls else None,
        "oos_min_year_trades": min(test_counts) if test_counts else None,
        "oos_min_year_trade_floor_count": min(trade_floor_counts) if trade_floor_counts else None,
        "oos_total_trades": sum(test_counts),
        "average_turnover_jaccard": sum(turnover_values) / len(turnover_values) if turnover_values else None,
        "min_turnover_jaccard": min(turnover_values) if turnover_values else None,
        "multiple_testing": {
            "candidate_group_count": len(all_candidate_stats),
            "evaluated_train_combo_count": evaluated_combos,
            "effective_trial_count_floor": effective_trials,
            "penalty_decision": "fail" if failed_positive_years or failed_trade_floor_years else "pass",
            "selection_bias_note": "Treat in-sample net PnL as rejected unless rolling OOS gates pass; statistical deflated Sharpe is not estimated because per-trial return paths are not persisted.",
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
