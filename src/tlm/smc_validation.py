from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date
from pathlib import Path
from statistics import median
from typing import Sequence

from .backtest import BacktestResult, Trade, run_bar_backtest
from .cli_dates import iter_dates
from .config import CostModelConfig, SymbolConfig
from .metrics import calculate_metrics
from .storage import bar_path, write_json
from .strategy import StrategySpec
from .validation import DateRange, generate_rolling_folds


DEFAULT_COST_STRESS_MULTIPLIERS = (1.0, 2.0, 3.0)


def build_smc_validation_report(
    *,
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    data_root: Path,
    date_from: date,
    date_to: date,
    cost_model: CostModelConfig,
    starting_equity: float = 100_000,
    train_days: int = 730,
    validation_days: int = 182,
    test_days: int = 182,
    step_days: int = 91,
    embargo_days: int = 5,
    final_holdout_days: int = 365,
    min_folds: int = 1,
    indicator_warmup_days: int = 0,
    cost_stress_multipliers: Sequence[float] = DEFAULT_COST_STRESS_MULTIPLIERS,
    sample_trade_count: int = 5,
    min_trade_count: int = 200,
) -> dict:
    if spec.strategy_family != "smc_lqem_ce":
        raise ValueError("SMC validation requires strategy_family smc_lqem_ce")
    files = _bar_files_for_range(data_root, spec.symbol, spec.timeframe, date_from, date_to)
    existing_files = [path for path in files if path.exists()]
    full_result = run_bar_backtest(
        spec,
        symbol_config,
        files,
        starting_equity=starting_equity,
        cost_model=cost_model,
    )
    full_summary = summarize_smc_result(full_result, cost_model=cost_model, sample_trade_count=sample_trade_count)
    cost_stress = []
    for multiplier in cost_stress_multipliers:
        stressed_cost_model = _stress_cost_model(cost_model, multiplier)
        stressed_trades = _reprice_trades_for_cost_model(full_result.trades, stressed_cost_model)
        cost_stress.append(
            {
                "multiplier": multiplier,
                "cost_model": stressed_cost_model.to_dict(),
                "summary": summarize_smc_trades(
                    strategy_name=spec.name,
                    trades=stressed_trades,
                    cost_model=stressed_cost_model,
                    starting_equity=starting_equity,
                    calendar_days=_calendar_days(date_from, date_to),
                    sample_trade_count=sample_trade_count,
                    backtest_signal_health=full_result.signal_health_report,
                ),
            }
        )

    validation_plan = None
    walk_forward = {"status": "unavailable", "reason": None, "folds": []}
    final_holdout = {"status": "unavailable", "reason": None, "summary": None}
    try:
        validation_plan = generate_rolling_folds(
            date_from,
            date_to,
            train_days=train_days,
            validation_days=validation_days,
            test_days=test_days,
            step_days=step_days,
            embargo_days=embargo_days,
            final_holdout_days=final_holdout_days,
            min_folds=min_folds,
            indicator_warmup_days=indicator_warmup_days,
        )
    except ValueError as exc:
        walk_forward["reason"] = str(exc)
        final_holdout["reason"] = str(exc)
    else:
        fold_summaries = []
        for fold in validation_plan.folds:
            fold_summaries.append(
                {
                    "index": fold.index,
                    "train": _range_summary_from_trades(
                        full_result.trades, spec, data_root, fold.train, cost_model, starting_equity, sample_trade_count
                    ),
                    "validation": _range_summary_from_trades(
                        full_result.trades, spec, data_root, fold.validation, cost_model, starting_equity, sample_trade_count
                    ),
                    "test": _range_summary_from_trades(
                        full_result.trades, spec, data_root, fold.test, cost_model, starting_equity, sample_trade_count
                    ),
                }
            )
        walk_forward = {"status": "ok", "plan": validation_plan.to_dict(), "folds": fold_summaries}
        final_holdout = {
            "status": "ok",
            "range": validation_plan.final_holdout.to_dict(),
            "summary": _range_summary_from_trades(
                full_result.trades,
                spec,
                data_root,
                validation_plan.final_holdout,
                cost_model,
                starting_equity,
                sample_trade_count,
            ),
        }

    report = {
        "artifact": "smc_lqem_ce_validation_report",
        "strategy_name": spec.name,
        "strategy_family": spec.strategy_family,
        "symbol": spec.symbol,
        "timeframe": spec.timeframe,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "data_files_requested": len(files),
        "data_files_existing": len(existing_files),
        "data_file_coverage_ratio": len(existing_files) / len(files) if files else 0.0,
        "cost_model": cost_model.to_dict(),
        "starting_equity": starting_equity,
        "full_history": full_summary,
        "cost_stress": cost_stress,
        "walk_forward": walk_forward,
        "final_holdout": final_holdout,
        "promotion_gates": _promotion_gates(
            full_summary=full_summary,
            cost_stress=cost_stress,
            final_holdout=final_holdout,
            min_trade_count=min_trade_count,
        ),
    }
    report["status"] = "ready_for_review" if existing_files else "blocked_no_data"
    return report


def summarize_smc_result(
    result: BacktestResult,
    *,
    cost_model: CostModelConfig,
    sample_trade_count: int = 5,
) -> dict:
    summary = summarize_smc_trades(
        strategy_name=result.strategy_name,
        trades=result.trades,
        cost_model=cost_model,
        starting_equity=1,
        calendar_days=1,
        sample_trade_count=sample_trade_count,
        backtest_signal_health=result.signal_health_report,
    )
    summary["metrics"] = result.metrics.to_dict()
    return summary


def summarize_smc_trades(
    *,
    strategy_name: str,
    trades: Sequence[Trade],
    cost_model: CostModelConfig,
    starting_equity: float,
    calendar_days: int,
    sample_trade_count: int = 5,
    backtest_signal_health: dict | None = None,
) -> dict:
    trade_pnls = [trade.net_pnl for trade in trades]
    equity = [starting_equity]
    for pnl in trade_pnls:
        equity.append(equity[-1] + pnl)
    metrics = calculate_metrics(trade_pnls, equity, starting_equity, max(calendar_days, 1))
    return {
        "metrics": metrics.to_dict(),
        "yearly_attribution": _yearly_attribution(trades),
        "r_distribution": _r_distribution(trades, cost_model),
        "setup_attribution": _setup_attribution(trades, strategy_name),
        "signal_health": _signal_health(trades, backtest_signal_health),
        "audited_samples": _sample_audited_trades(trades, cost_model, sample_trade_count),
    }


def write_smc_validation_outputs(report: dict, output_dir: Path, basename: str | None = None) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    name = basename or f"{report['strategy_name']}_{report['date_from']}_{report['date_to']}"
    json_path = output_dir / f"{name}.json"
    markdown_path = output_dir / f"{name}.md"
    write_json(json_path, report)
    markdown_path.write_text(render_smc_validation_markdown(report), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def render_smc_validation_markdown(report: dict) -> str:
    full_metrics = report["full_history"]["metrics"]
    gates = report["promotion_gates"]
    lines = [
        f"# {report['strategy_name']} SMC Validation Report",
        "",
        f"- Symbol: `{report['symbol']}`",
        f"- Window: `{report['date_from']}` to `{report['date_to']}`",
        f"- Data coverage: {report['data_files_existing']}/{report['data_files_requested']} files",
        f"- Full-history trades: {full_metrics['trade_count']}",
        f"- Full-history net PnL: {full_metrics['net_pnl']:.2f}",
        f"- Full-history profit factor: {_format_optional(full_metrics['profit_factor'])}",
        f"- Full-history max drawdown: {full_metrics['max_drawdown']:.2f}",
        "",
        "## Promotion Gates",
        "",
    ]
    for gate in gates:
        status = "PASS" if gate["passed"] else "FAIL"
        lines.append(f"- {gate['name']}: {status} (actual={gate['actual']}, threshold={gate['threshold']})")
    lines.extend(["", "## Cost Stress", ""])
    for row in report["cost_stress"]:
        metrics = row["summary"]["metrics"]
        lines.append(
            f"- {row['multiplier']}x slippage: trades={metrics['trade_count']}, "
            f"net_pnl={metrics['net_pnl']:.2f}, avg_trade={_format_optional(metrics['avg_trade_net_pnl'])}"
        )
    lines.extend(["", "## Walk Forward", ""])
    lines.append(f"- Status: {report['walk_forward']['status']}")
    if report["walk_forward"].get("reason"):
        lines.append(f"- Reason: {report['walk_forward']['reason']}")
    else:
        lines.append(f"- Fold count: {len(report['walk_forward'].get('folds', []))}")
    lines.extend(["", "## Audited Samples", ""])
    for sample in report["full_history"]["audited_samples"]["top_winners"]:
        lines.append(
            f"- Winner {sample['entry_time']} {sample['side']} net={sample['net_pnl']:.2f} "
            f"r={_format_optional(sample['net_r'])}"
        )
    for sample in report["full_history"]["audited_samples"]["top_losers"]:
        lines.append(
            f"- Loser {sample['entry_time']} {sample['side']} net={sample['net_pnl']:.2f} "
            f"r={_format_optional(sample['net_r'])}"
        )
    lines.append("")
    return "\n".join(lines)


def _range_summary_from_trades(
    trades: Sequence[Trade],
    spec: StrategySpec,
    data_root: Path,
    date_range: DateRange,
    cost_model: CostModelConfig,
    starting_equity: float,
    sample_trade_count: int,
) -> dict:
    files = _bar_files_for_range(data_root, spec.symbol, spec.timeframe, date_range.start, date_range.end)
    range_trades = [
        trade
        for trade in trades
        if date_range.start <= trade.entry_time.date() <= date_range.end
    ]
    return {
        "range": date_range.to_dict(),
        "data_files_existing": len([path for path in files if path.exists()]),
        "summary": summarize_smc_trades(
            strategy_name=spec.name,
            trades=range_trades,
            cost_model=cost_model,
            starting_equity=starting_equity,
            calendar_days=_calendar_days(date_range.start, date_range.end),
            sample_trade_count=sample_trade_count,
        ),
    }


def _reprice_trades_for_cost_model(trades: Sequence[Trade], cost_model: CostModelConfig) -> list[Trade]:
    repriced = []
    for trade in trades:
        fees = cost_model.round_trip_fees_usd * trade.contracts
        slippage_cost = (
            2
            * cost_model.slippage_ticks_per_side
            * cost_model.tick_size
            * cost_model.point_value
            * trade.contracts
        )
        repriced.append(
            replace(
                trade,
                fees=fees,
                slippage_cost=slippage_cost,
                net_pnl=trade.gross_pnl - fees - slippage_cost,
            )
        )
    return repriced


def _calendar_days(start: date, end: date) -> int:
    return max((end - start).days + 1, 1)


def _bar_files_for_range(data_root: Path, symbol: str, timeframe: str, start: date, end: date) -> list[Path]:
    return [bar_path(data_root, symbol, timeframe, day) for day in iter_dates(start, end)]


def _stress_cost_model(cost_model: CostModelConfig, multiplier: float) -> CostModelConfig:
    return replace(
        cost_model,
        name=f"{cost_model.name}_slippage_{multiplier:g}x",
        slippage_ticks_per_side=cost_model.slippage_ticks_per_side * multiplier,
    )


def _yearly_attribution(trades: Sequence[Trade]) -> dict:
    pnl_by_year: dict[str, float] = defaultdict(float)
    count_by_year: Counter[str] = Counter()
    for trade in trades:
        year = str(trade.exit_time.year)
        pnl_by_year[year] += trade.net_pnl
        count_by_year[year] += 1
    total_positive_pnl = sum(value for value in pnl_by_year.values() if value > 0)
    rows = []
    for year in sorted(pnl_by_year):
        contribution = pnl_by_year[year] / total_positive_pnl if total_positive_pnl > 0 and pnl_by_year[year] > 0 else 0.0
        rows.append(
            {
                "year": year,
                "trade_count": count_by_year[year],
                "net_pnl": pnl_by_year[year],
                "positive_profit_contribution": contribution,
            }
        )
    return {
        "years": rows,
        "max_positive_profit_contribution": max((row["positive_profit_contribution"] for row in rows), default=0.0),
    }


def _r_distribution(trades: Sequence[Trade], cost_model: CostModelConfig) -> dict:
    gross_values = []
    net_values = []
    for trade in trades:
        risk = _trade_risk_usd(trade, cost_model)
        if risk is None or risk <= 0:
            continue
        gross_values.append(trade.gross_pnl / risk)
        net_values.append(trade.net_pnl / risk)
    return {
        "count": len(net_values),
        "gross": _distribution(gross_values),
        "net": _distribution(net_values),
    }


def _distribution(values: Sequence[float]) -> dict:
    if not values:
        return {"min": None, "p25": None, "median": None, "p75": None, "max": None, "average": None}
    ordered = sorted(values)
    return {
        "min": ordered[0],
        "p25": _percentile(ordered, 0.25),
        "median": median(ordered),
        "p75": _percentile(ordered, 0.75),
        "max": ordered[-1],
        "average": sum(ordered) / len(ordered),
    }


def _percentile(ordered: Sequence[float], quantile: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    index = quantile * (len(ordered) - 1)
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _setup_attribution(trades: Sequence[Trade], strategy_name: str) -> dict:
    by_side: Counter[str] = Counter()
    by_exit_reason: Counter[str] = Counter()
    by_session_window: Counter[str] = Counter()
    by_stop_bucket: Counter[str] = Counter()
    for trade in trades:
        by_side[trade.side] += 1
        by_exit_reason[trade.exit_reason] += 1
        by_session_window[_session_window_label(trade)] += 1
        by_stop_bucket[_stop_bucket(trade)] += 1
    return {
        "strategy_name": strategy_name,
        "by_side": dict(sorted(by_side.items())),
        "by_exit_reason": dict(sorted(by_exit_reason.items())),
        "by_session_window": dict(sorted(by_session_window.items())),
        "by_stop_size_bucket": dict(sorted(by_stop_bucket.items())),
    }


def _signal_health(trades: Sequence[Trade], backtest_signal_health: dict | None) -> dict:
    audit_count = sum(1 for trade in trades if _audit(trade))
    stop_ticks = [
        float(_audit(trade).get("stop_ticks"))
        for trade in trades
        if _audit(trade) and _audit(trade).get("stop_ticks") is not None
    ]
    return {
        "backtest_signal_health": backtest_signal_health,
        "trade_count": len(trades),
        "audit_available_count": audit_count,
        "audit_coverage_ratio": audit_count / len(trades) if trades else None,
        "stop_ticks": _distribution(stop_ticks),
    }


def _sample_audited_trades(trades: Sequence[Trade], cost_model: CostModelConfig, limit: int) -> dict:
    ordered_winners = sorted(trades, key=lambda trade: trade.net_pnl, reverse=True)[:limit]
    ordered_losers = sorted(trades, key=lambda trade: trade.net_pnl)[:limit]
    return {
        "top_winners": [_trade_sample(trade, cost_model) for trade in ordered_winners],
        "top_losers": [_trade_sample(trade, cost_model) for trade in ordered_losers],
    }


def _trade_sample(trade: Trade, cost_model: CostModelConfig) -> dict:
    risk = _trade_risk_usd(trade, cost_model)
    return {
        "side": trade.side,
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat(),
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "contracts": trade.contracts,
        "net_pnl": trade.net_pnl,
        "exit_reason": trade.exit_reason,
        "gross_r": trade.gross_pnl / risk if risk else None,
        "net_r": trade.net_pnl / risk if risk else None,
        "audit": _audit(trade),
    }


def _trade_risk_usd(trade: Trade, cost_model: CostModelConfig) -> float | None:
    audit = _audit(trade)
    if not audit or audit.get("stop_price") is None:
        return None
    stop_price = float(audit["stop_price"])
    return abs(trade.entry_price - stop_price) * cost_model.point_value * trade.contracts


def _audit(trade: Trade) -> dict:
    audit = trade.feature_values_at_entry.get("smc_signal_audit")
    return audit if isinstance(audit, dict) else {}


def _session_window_label(trade: Trade) -> str:
    hour = trade.entry_time.hour
    if hour < 12:
        return "morning"
    if hour < 16:
        return "afternoon"
    return "other"


def _stop_bucket(trade: Trade) -> str:
    audit = _audit(trade)
    stop_ticks = audit.get("stop_ticks")
    if stop_ticks is None:
        return "unknown"
    stop_ticks = float(stop_ticks)
    if stop_ticks < 12:
        return "lt_12_ticks"
    if stop_ticks <= 40:
        return "12_to_40_ticks"
    if stop_ticks <= 80:
        return "41_to_80_ticks"
    return "gt_80_ticks"


def _promotion_gates(
    *,
    full_summary: dict,
    cost_stress: Sequence[dict],
    final_holdout: dict,
    min_trade_count: int,
) -> list[dict]:
    full_metrics = full_summary["metrics"]
    holdout_metrics = (final_holdout.get("summary") or {}).get("summary", {}).get("metrics", {})
    stress_2x = next((row for row in cost_stress if float(row["multiplier"]) == 2.0), None)
    stress_2x_avg = None
    if stress_2x is not None:
        stress_2x_avg = stress_2x["summary"]["metrics"].get("avg_trade_net_pnl")
    return [
        {
            "name": "minimum_trade_count",
            "passed": int(full_metrics.get("trade_count") or 0) >= min_trade_count,
            "actual": full_metrics.get("trade_count"),
            "threshold": min_trade_count,
        },
        {
            "name": "positive_expectancy_after_2x_cost",
            "passed": stress_2x_avg is not None and stress_2x_avg > 0,
            "actual": stress_2x_avg,
            "threshold": "> 0",
        },
        {
            "name": "final_holdout_profit_factor",
            "passed": holdout_metrics.get("profit_factor") is not None and holdout_metrics["profit_factor"] > 1.05,
            "actual": holdout_metrics.get("profit_factor"),
            "threshold": "> 1.05",
        },
        {
            "name": "yearly_profit_concentration",
            "passed": full_summary["yearly_attribution"]["max_positive_profit_contribution"] <= 0.40,
            "actual": full_summary["yearly_attribution"]["max_positive_profit_contribution"],
            "threshold": "<= 0.40",
        },
    ]


def _format_optional(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)
