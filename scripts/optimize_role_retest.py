#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from tlm.metrics import calculate_metrics
from tlm.role_retest import RoleRetestConfig, load_role_retest_bars, run_role_retest_backtest


def main() -> int:
    parser = argparse.ArgumentParser(description="Search robust parameters for the inferred NQ OB retest strategy.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--timeframe", default="1m")
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    bars = load_role_retest_bars(Path(args.data_root), args.symbol, args.timeframe, args.date_from, args.date_to)
    rows = []
    for name, config in _candidate_configs(args.symbol, args.timeframe):
        result = run_role_retest_backtest(bars, config)
        yearly = _yearly_metrics(result["trades"])
        metrics = result["metrics"]
        positive_years = sum(1 for item in yearly.values() if item["net_pnl"] > 0)
        worst_year = min((item["net_pnl"] for item in yearly.values()), default=0.0)
        score = _score_candidate(metrics, positive_years, worst_year)
        rows.append(
            {
                "name": name,
                "score": score,
                "metrics": metrics,
                "diagnostics": result["diagnostics"],
                "yearly": yearly,
                "config": asdict(config),
            }
        )

    rows.sort(key=lambda row: row["score"], reverse=True)
    output = {
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "date_from": args.date_from,
        "date_to": args.date_to,
        "candidate_count": len(rows),
        "leaderboard": rows,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output_path)
    for row in rows[:10]:
        metrics = row["metrics"]
        print(
            row["name"],
            "score=", round(row["score"], 3),
            "trades=", metrics["trade_count"],
            "net=", round(metrics["net_pnl"], 2),
            "pf=", None if metrics["profit_factor"] is None else round(metrics["profit_factor"], 3),
            "win=", None if metrics["win_rate"] is None else round(metrics["win_rate"], 3),
            "dd=", round(metrics["max_drawdown"], 2),
        )
    return 0


def _candidate_configs(symbol: str, timeframe: str) -> Iterable[tuple[str, RoleRetestConfig]]:
    base = {
        "symbol": symbol,
        "timeframe": timeframe,
        "swing_left_bars": 3,
        "swing_right_bars": 3,
        "lookback_bars": 120,
        "atr_low_percentile": 35,
        "atr_high_percentile": 90,
        "body_impulse_multiple": 1.5,
        "atr_impulse_multiple": 1.1,
        "zone_mode": "opposite_candle",
        "max_zone_atr": 2.0,
        "flatten_outside_session": True,
        "trade_sessions": ("09:35-11:20", "13:35-15:35"),
    }
    for ratio in (0.25, 0.5, 0.75):
        for minimum_rr in (1.2, 1.6, 2.0):
            for ttl_bars in (8, 16, 30):
                yield (
                    f"ob_ratio{ratio}_rr{minimum_rr}_ttl{ttl_bars}",
                    RoleRetestConfig(
                        **base,
                        ob_entry_ratio=ratio,
                        minimum_rr=minimum_rr,
                        pending_order_ttl_bars=ttl_bars,
                        max_hold_bars=90,
                    ),
                )
    for ratio in (0.25, 0.5, 0.75):
        for sweep in (False, True):
            for fvg in (False, True):
                yield (
                    f"quality_ratio{ratio}_sweep{int(sweep)}_fvg{int(fvg)}",
                    RoleRetestConfig(
                        **base,
                        ob_entry_ratio=ratio,
                        minimum_rr=1.6,
                        pending_order_ttl_bars=20,
                        max_hold_bars=90,
                        require_liquidity_sweep=sweep,
                        require_fvg=fvg,
                    ),
                )
    for min_volume_percentile, max_volume_percentile in ((20, 90), (40, 100), (20, 100)):
        yield (
            f"vol_gate_pct{min_volume_percentile}_{max_volume_percentile}",
            RoleRetestConfig(
                **base,
                ob_entry_ratio=0.5,
                minimum_rr=1.6,
                pending_order_ttl_bars=16,
                max_hold_bars=90,
                min_volume_percentile=min_volume_percentile,
                max_volume_percentile=max_volume_percentile,
            ),
        )
    yield (
        "vol_gate_rel08_pct20_90",
        RoleRetestConfig(
            **base,
            ob_entry_ratio=0.5,
            minimum_rr=1.6,
            pending_order_ttl_bars=16,
            max_hold_bars=90,
            min_relative_volume=0.8,
            min_volume_percentile=20,
            max_volume_percentile=90,
        ),
    )


def _yearly_metrics(trades: list[dict]) -> dict[str, dict]:
    grouped: dict[str, list[float]] = {}
    for trade in trades:
        grouped.setdefault(trade["entry_time"][:4], []).append(float(trade["net_pnl"]))
    yearly = {}
    for year, pnls in sorted(grouped.items()):
        equity = [100_000.0]
        for pnl in pnls:
            equity.append(equity[-1] + pnl)
        yearly[year] = calculate_metrics(pnls, equity, 100_000.0, 365).to_dict()
    return yearly


def _score_candidate(metrics: dict, positive_years: int, worst_year: float) -> float:
    trade_count = metrics["trade_count"]
    if trade_count < 40:
        return -1_000_000.0 + trade_count
    profit_factor = metrics["profit_factor"] or 0.0
    net_pnl = metrics["net_pnl"]
    max_drawdown = max(metrics["max_drawdown"], 1.0)
    win_rate = metrics["win_rate"] or 0.0
    return (
        net_pnl / max_drawdown
        + (profit_factor - 1.0) * 8.0
        + positive_years * 1.5
        + win_rate * 2.0
        + worst_year / max_drawdown
    )


if __name__ == "__main__":
    raise SystemExit(main())
