#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

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
    EXPANDED_HIGH_EDGE_NETMAX_PRESET,
    check_expanded_high_edge_replay,
    expanded_high_edge_preset_spec,
)
from tlm.low_r_regime_basket import LowRRegimeBasketConfig, RegimeEdge, _json_default


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay and validate the versioned positive_expanded_edge_top_32 NQ basket."
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--date-from", default="2019-01-01")
    parser.add_argument("--date-to", default="2026-04-27")
    parser.add_argument("--output", type=Path, default=Path("reports/nq_expanded_high_edge_netmax_replay_2026-05-01.json"))
    parser.add_argument("--fail-on-drift", action="store_true")
    args = parser.parse_args(argv)

    spec = expanded_high_edge_preset_spec(EXPANDED_HIGH_EDGE_NETMAX_PRESET)
    config = LowRRegimeBasketConfig(
        data_root=args.data_root,
        symbol=args.symbol,
        date_from=args.date_from,
        date_to=args.date_to,
        preset=spec.preset,
        stop_range_multiple=spec.stop_range_multiple,
        min_stop_points=spec.min_stop_points,
        max_stop_points=spec.max_stop_points,
        max_hold_minutes=spec.max_hold_minutes,
        max_concurrent_positions=spec.max_concurrent_positions,
        flatten_on_date_change=True,
    )
    pattern = str(Path(args.data_root) / "bars" / "1m" / args.symbol / "date=*" / "part-000.parquet")

    con = duckdb.connect(":memory:")
    try:
        build_feature_table(con, pattern, args.date_from, args.date_to)
        bars = load_bars(con)
        coverage_days = coverage_days_by_year(bars)
        build_signal_table(con)
        candidate_ids = [_candidate_id(edge) for edge in spec.edges]
        signals_by_id = load_signals_by_candidate(con, sorted(set(candidate_ids)))
        attach_entry_indexes(signals_by_id, bars)
    finally:
        con.close()

    edge_order = {candidate_id: index for index, candidate_id in enumerate(candidate_ids)}
    signals = combine_signals(
        [signals_by_id[candidate_id] for candidate_id in candidate_ids if candidate_id in signals_by_id],
        edge_order=edge_order,
    )
    edges = tuple(_to_regime_edge(edge) for edge in spec.edges)
    result = replay_result(
        label=spec.preset,
        spec={
            "name": spec.preset,
            "source_label": spec.source_label,
            "candidate_ids": candidate_ids,
        },
        bars=bars,
        signals=signals,
        edges=edges,
        config=config,
        coverage_days=coverage_days,
        min_full_year_trades=1000,
        params={
            "max_hold_minutes": spec.max_hold_minutes,
            "stop_range_multiple": spec.stop_range_multiple,
            "min_stop_points": spec.min_stop_points,
            "max_stop_points": spec.max_stop_points,
            "flatten_on_date_change": True,
            "max_concurrent_positions": spec.max_concurrent_positions,
        },
    )
    drift_check = check_expanded_high_edge_replay(spec.preset, result)
    payload = {
        "artifact": "expanded_high_edge_netmax_replay_validation",
        "preset": spec.preset,
        "source_label": spec.source_label,
        "source_report": spec.source_report,
        "date_from": args.date_from,
        "date_to": args.date_to,
        "signal_count": len(signals),
        "metrics": result["metrics"],
        "constraints": result["constraints"],
        "edge_summary": result["edge_summary"],
        "exit_reasons": result["exit_reasons"],
        "yearly_results": result["yearly_results"],
        "drift_check": drift_check,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "drift_passed": drift_check["passed"]}, indent=2))
    return 1 if args.fail_on_drift and not drift_check["passed"] else 0


def _candidate_id(edge) -> str:
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


def _to_regime_edge(edge) -> RegimeEdge:
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


if __name__ == "__main__":
    raise SystemExit(main())
