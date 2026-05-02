#!/usr/bin/env python3
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tlm.config import get_symbol
from tlm.quotes import load_quote_rows
from tlm.storage import write_json


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit whether entry-time MBP-1 microstructure filters rescue 1.5R candidates."
    )
    parser.add_argument("--quote-replay-report", type=Path, default=Path("reports/nq_expanded_high_edge_quote_replay_mbp1_full_2026-05-02.json"))
    parser.add_argument("--quote-replay-trades", type=Path, default=Path("reports/nq_expanded_high_edge_quote_replay_mbp1_full_2026-05-02_trades.json"))
    parser.add_argument("--config-dir", type=Path, default=Path("configs"))
    parser.add_argument("--symbol", default="NQ_CME")
    parser.add_argument("--min-take-profit-r", type=float, default=1.5)
    parser.add_argument("--min-win-rate", type=float, default=0.55)
    parser.add_argument("--min-train-trades", type=int, default=30)
    parser.add_argument("--min-test-trades", type=int, default=30)
    parser.add_argument("--output", type=Path, default=Path("reports/nq_tick_microstructure_filter_audit_55wr_15r_2026-05-03.json"))
    args = parser.parse_args(argv)

    replay = json.loads(args.quote_replay_report.read_text(encoding="utf-8"))
    trades = json.loads(args.quote_replay_trades.read_text(encoding="utf-8"))["trades"]
    symbol_config = get_symbol(args.symbol, args.config_dir)
    eligible_trades = [
        trade
        for trade in trades
        if float(trade.get("take_profit_r") or 0.0) >= args.min_take_profit_r
    ]
    validations = validation_map(replay.get("validations", []))
    windows = quote_windows(eligible_trades, lookback_seconds=300, forward_seconds=900)
    quote_files = [Path(path) for path in replay.get("quote_files", [])]
    quotes = load_quote_rows(quote_files, windows=windows)
    rows = build_microstructure_rows(
        eligible_trades,
        validations,
        quotes,
        tick_size=symbol_config.tick_size,
    )
    train_rows, test_rows = chronological_split(rows)
    rules = build_candidate_rules(train_rows)
    evaluated = [
        evaluate_rule(
            rule,
            train_rows=train_rows,
            test_rows=test_rows,
            min_train_trades=args.min_train_trades,
            min_test_trades=args.min_test_trades,
            min_win_rate=args.min_win_rate,
        )
        for rule in rules
    ]
    evaluated.sort(key=rule_sort_key, reverse=True)
    selected = next((row for row in evaluated if row["train"]["gate_passed"]), evaluated[0] if evaluated else None)
    baseline = evaluate_rule(
        {"name": "no_filter", "conditions": []},
        train_rows=train_rows,
        test_rows=test_rows,
        min_train_trades=args.min_train_trades,
        min_test_trades=args.min_test_trades,
        min_win_rate=args.min_win_rate,
    )
    decision = {
        "passed": bool(selected and selected["test"]["gate_passed"]),
        "reason": None
        if selected and selected["test"]["gate_passed"]
        else "No train-selected entry-time MBP-1 filter passed the holdout 55% win-rate, trade-count, and positive-PnL gates.",
        "live_ready": False,
        "live_ready_reason": "This is a two-month tick-window diagnostic, not a multi-year walk-forward strategy with paper-readiness evidence.",
    }
    payload = {
        "artifact": "nq_tick_microstructure_filter_audit_55wr_15r",
        "schema_version": 1,
        "quote_replay_report": str(args.quote_replay_report),
        "quote_replay_trades": str(args.quote_replay_trades),
        "target": {
            "min_take_profit_r": args.min_take_profit_r,
            "min_win_rate": args.min_win_rate,
            "min_train_trades": args.min_train_trades,
            "min_test_trades": args.min_test_trades,
        },
        "method": {
            "scope": "Only trades from the existing MBP-1 quote replay window are analyzed.",
            "selection": "Candidate filters use only entry-time observable quote features; first half of trades selects the filter, second half is holdout.",
            "non_overfit_guardrail": "Future adverse-selection and quote-arrival latency fields are reported as diagnostics only and are not used as filter inputs.",
            "candidate_features": [
                "spread_ticks",
                "relevant_depth",
                "aligned_imbalance",
                "pre_mid_move_10s_aligned",
                "pre_mid_move_60s_aligned",
                "pre_mid_move_300s_aligned",
            ],
            "eligible_trade_rule": f"take_profit_r >= {args.min_take_profit_r}",
            "train_test_split": {
                "train_end_entry_time": train_rows[-1]["entry_time"] if train_rows else None,
                "test_start_entry_time": test_rows[0]["entry_time"] if test_rows else None,
            },
            "candidate_rule_count": len(evaluated),
        },
        "coverage": {
            "source_trade_count": len(trades),
            "eligible_trade_count": len(eligible_trades),
            "microstructure_row_count": len(rows),
            "missing_quote_count": len(eligible_trades) - len(rows),
            "quote_file_count": len([path for path in quote_files if path.exists()]),
            "first_entry_time": rows[0]["entry_time"] if rows else None,
            "last_entry_time": rows[-1]["entry_time"] if rows else None,
        },
        "baseline": baseline,
        "selected_rule": selected,
        "top_rules": evaluated[:25],
        "feature_diagnostics": feature_diagnostics(rows),
        "decision": decision,
    }
    write_json(args.output, payload)
    print(json.dumps({"output": str(args.output), **decision}, indent=2))
    return 0


def validation_map(validations: Sequence[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(row.get("entry_time")), str(row.get("exit_time"))): row
        for row in validations
        if row.get("status") == "validated"
    }


def quote_windows(
    trades: Sequence[dict[str, Any]],
    *,
    lookback_seconds: int,
    forward_seconds: int,
) -> list[tuple[datetime, datetime]]:
    windows = []
    for trade in trades:
        entry_time = parse_time(trade["entry_time"])
        windows.append((entry_time - timedelta(seconds=lookback_seconds), entry_time + timedelta(seconds=forward_seconds)))
    return windows


def build_microstructure_rows(
    trades: Sequence[dict[str, Any]],
    validations: dict[tuple[str, str], dict[str, Any]],
    quotes: Sequence[dict[str, Any]],
    *,
    tick_size: float,
) -> list[dict[str, Any]]:
    timestamps = [quote["timestamp"] for quote in quotes]
    rows = []
    for trade in sorted(trades, key=lambda row: row["entry_time"]):
        entry_time = parse_time(trade["entry_time"])
        validation = validations.get((trade["entry_time"], trade["exit_time"]))
        entry_quote = first_quote_at_or_after(quotes, timestamps, entry_time)
        if validation is None or entry_quote is None:
            continue
        side = str(trade["side"])
        contracts = int(trade.get("contracts") or 1)
        relevant_depth = float(entry_quote["ask_size"] if side == "long" else entry_quote["bid_size"])
        opposite_depth = float(entry_quote["bid_size"] if side == "long" else entry_quote["ask_size"])
        raw_imbalance = safe_divide(float(entry_quote["bid_size"]) - float(entry_quote["ask_size"]), float(entry_quote["bid_size"]) + float(entry_quote["ask_size"]))
        aligned_imbalance = raw_imbalance if side == "long" else -raw_imbalance
        quote_net_pnl = float(validation["quote_gross_pnl"]) - float(trade.get("costs") or 0.0)
        rows.append(
            {
                "entry_time": trade["entry_time"],
                "exit_time": trade["exit_time"],
                "side": side,
                "scan_type": trade.get("scan_type"),
                "take_profit_r": float(trade.get("take_profit_r") or 0.0),
                "contracts": contracts,
                "bar_net_pnl": float(trade.get("net_pnl") or 0.0),
                "quote_net_pnl": quote_net_pnl,
                "quote_win": quote_net_pnl > 0.0,
                "spread_ticks": float(entry_quote["spread"]) / tick_size if tick_size > 0 else None,
                "relevant_depth": relevant_depth,
                "opposite_depth": opposite_depth,
                "top_level_fill_ratio": min(max(relevant_depth, 0.0) / contracts, 1.0) if contracts > 0 else 0.0,
                "aligned_imbalance": aligned_imbalance,
                "entry_latency_ms": (entry_quote["timestamp"] - entry_time).total_seconds() * 1000.0,
                "pre_mid_move_10s_aligned": aligned_mid_move(quotes, timestamps, entry_time, entry_quote, side, tick_size, 10),
                "pre_mid_move_60s_aligned": aligned_mid_move(quotes, timestamps, entry_time, entry_quote, side, tick_size, 60),
                "pre_mid_move_300s_aligned": aligned_mid_move(quotes, timestamps, entry_time, entry_quote, side, tick_size, 300),
                "diagnostic_adverse_1m_ticks": (validation.get("adverse_excursion_ticks") or {}).get("1m"),
                "diagnostic_adverse_5m_ticks": (validation.get("adverse_excursion_ticks") or {}).get("5m"),
            }
        )
    return rows


def chronological_split(rows: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(rows, key=lambda row: row["entry_time"])
    split_index = len(ordered) // 2
    return ordered[:split_index], ordered[split_index:]


def build_candidate_rules(train_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    base_conditions = [
        threshold_conditions(train_rows, "spread_ticks", "<=", (0.25, 0.50, 0.75)),
        threshold_conditions(train_rows, "relevant_depth", ">=", (0.25, 0.50, 0.75)),
        threshold_conditions(train_rows, "aligned_imbalance", ">=", (0.25, 0.50, 0.75)),
        threshold_conditions(train_rows, "pre_mid_move_10s_aligned", ">=", (0.25, 0.50, 0.75)),
        threshold_conditions(train_rows, "pre_mid_move_60s_aligned", ">=", (0.25, 0.50, 0.75)),
        threshold_conditions(train_rows, "pre_mid_move_300s_aligned", ">=", (0.25, 0.50, 0.75)),
    ]
    singles = dedupe_conditions([condition for group in base_conditions for condition in group])
    rules = [{"name": "no_filter", "conditions": []}]
    rules.extend(rule_from_conditions([condition]) for condition in singles)
    for left_index, left in enumerate(singles):
        for right in singles[left_index + 1 :]:
            if left["feature"] == right["feature"]:
                continue
            rules.append(rule_from_conditions([left, right]))
    return rules


def threshold_conditions(
    rows: Sequence[dict[str, Any]],
    feature: str,
    op: str,
    quantiles: Sequence[float],
) -> list[dict[str, Any]]:
    values = [float(row[feature]) for row in rows if row.get(feature) is not None]
    return [
        {"feature": feature, "op": op, "value": percentile(values, quantile)}
        for quantile in quantiles
        if values
    ]


def dedupe_conditions(conditions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    output = []
    for condition in conditions:
        key = (condition["feature"], condition["op"], round(float(condition["value"]), 8))
        if key in seen:
            continue
        seen.add(key)
        output.append(condition)
    return output


def rule_from_conditions(conditions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    parts = [f"{condition['feature']} {condition['op']} {condition['value']:.6g}" for condition in conditions]
    return {"name": " AND ".join(parts), "conditions": list(conditions)}


def evaluate_rule(
    rule: dict[str, Any],
    *,
    train_rows: Sequence[dict[str, Any]],
    test_rows: Sequence[dict[str, Any]],
    min_train_trades: int,
    min_test_trades: int,
    min_win_rate: float,
) -> dict[str, Any]:
    train_selected = [row for row in train_rows if row_passes(row, rule["conditions"])]
    test_selected = [row for row in test_rows if row_passes(row, rule["conditions"])]
    train = summarize_rows(train_selected, min_trades=min_train_trades, min_win_rate=min_win_rate)
    test = summarize_rows(test_selected, min_trades=min_test_trades, min_win_rate=min_win_rate)
    return {
        "name": rule["name"],
        "conditions": rule["conditions"],
        "train": train,
        "test": test,
    }


def row_passes(row: dict[str, Any], conditions: Sequence[dict[str, Any]]) -> bool:
    for condition in conditions:
        value = row.get(condition["feature"])
        threshold = float(condition["value"])
        if value is None:
            return False
        if condition["op"] == "<=" and not float(value) <= threshold:
            return False
        if condition["op"] == ">=" and not float(value) >= threshold:
            return False
    return True


def summarize_rows(rows: Sequence[dict[str, Any]], *, min_trades: int, min_win_rate: float) -> dict[str, Any]:
    trade_count = len(rows)
    wins = sum(1 for row in rows if row["quote_win"])
    net_pnl = sum(float(row["quote_net_pnl"]) for row in rows)
    win_rate = wins / trade_count if trade_count else 0.0
    return {
        "trade_count": trade_count,
        "winning_trade_count": wins,
        "win_rate": win_rate,
        "net_pnl": net_pnl,
        "avg_trade_net_pnl": net_pnl / trade_count if trade_count else None,
        "first_entry_time": rows[0]["entry_time"] if rows else None,
        "last_entry_time": rows[-1]["entry_time"] if rows else None,
        "gate_passed": trade_count >= min_trades and win_rate >= min_win_rate and net_pnl > 0.0,
    }


def rule_sort_key(row: dict[str, Any]) -> tuple[float, ...]:
    train = row["train"]
    test = row["test"]
    return (
        1.0 if train["gate_passed"] else 0.0,
        float(train["win_rate"] or 0.0),
        float(train["net_pnl"] or 0.0),
        float(train["trade_count"] or 0.0),
        float(test["win_rate"] or 0.0),
        float(test["net_pnl"] or 0.0),
    )


def feature_diagnostics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    features = (
        "spread_ticks",
        "relevant_depth",
        "aligned_imbalance",
        "entry_latency_ms",
        "pre_mid_move_10s_aligned",
        "pre_mid_move_60s_aligned",
        "pre_mid_move_300s_aligned",
        "diagnostic_adverse_1m_ticks",
        "diagnostic_adverse_5m_ticks",
    )
    return {
        feature: {
            "p25": percentile([float(row[feature]) for row in rows if row.get(feature) is not None], 0.25),
            "p50": percentile([float(row[feature]) for row in rows if row.get(feature) is not None], 0.50),
            "p75": percentile([float(row[feature]) for row in rows if row.get(feature) is not None], 0.75),
        }
        for feature in features
    }


def aligned_mid_move(
    quotes: Sequence[dict[str, Any]],
    timestamps: Sequence[datetime],
    entry_time: datetime,
    entry_quote: dict[str, Any],
    side: str,
    tick_size: float,
    seconds: int,
) -> float | None:
    prior = last_quote_at_or_before(quotes, timestamps, entry_time - timedelta(seconds=seconds))
    if prior is None or tick_size <= 0:
        return None
    move = (float(entry_quote["mid"]) - float(prior["mid"])) / tick_size
    return move if side == "long" else -move


def first_quote_at_or_after(
    quotes: Sequence[dict[str, Any]],
    timestamps: Sequence[datetime],
    target: datetime,
) -> dict[str, Any] | None:
    index = bisect_left(timestamps, target)
    return quotes[index] if index < len(quotes) else None


def last_quote_at_or_before(
    quotes: Sequence[dict[str, Any]],
    timestamps: Sequence[datetime],
    target: datetime,
) -> dict[str, Any] | None:
    index = bisect_right(timestamps, target) - 1
    return quotes[index] if index >= 0 else None


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def safe_divide(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(max(int(round((len(ordered) - 1) * quantile)), 0), len(ordered) - 1)
    return ordered[index]


if __name__ == "__main__":
    raise SystemExit(main())
