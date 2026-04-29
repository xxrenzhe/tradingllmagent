from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

import duckdb

from .bars import timeframe_minutes
from .profit_mining import (
    _cost_stress_metrics,
    _duckdb_glob,
    _fetch_dicts,
    _one_trade_per_timestamp,
    _period_replay_metrics,
    _replay_regime_edges_for_horizon,
    _signal_metrics,
    _signal_timestamp,
    _signals_covered_days,
    _yearly_signal_results,
)
from .storage import write_json
from .variants import stable_hash


def generate_top_strategy_html_report(
    *,
    mining_report_path: Path,
    data_root: Path,
    output_html: Path,
    top_n: int = 3,
    sample_trade_count: int = 3,
    objective: str = "annualized_net_pnl",
) -> dict[str, Any]:
    mining_report = json.loads(mining_report_path.read_text(encoding="utf-8"))
    strategies = _select_top_yearly_strategies(mining_report, top_n=top_n, objective=objective)
    context = _replay_context(mining_report, data_root)
    round_trip_cost_usd = float(mining_report.get("round_trip_cost_usd") or 0.0)
    bar_minutes = int(mining_report.get("timeframe_minutes") or timeframe_minutes(str(mining_report["timeframe"])))

    con = duckdb.connect(":memory:")
    try:
        enriched = []
        for index, strategy in enumerate(strategies, start=1):
            signals = _replay_strategy_signals(con, context, strategy, round_trip_cost_usd)
            samples = _sample_trades(signals, limit=sample_trade_count)
            enriched.append(
                _enrich_strategy(
                    con=con,
                    context=context,
                    strategy={**strategy, "display_id": f"S{index}"},
                    signals=signals,
                    sample_trades=samples,
                    bar_minutes=bar_minutes,
                )
            )
    finally:
        con.close()

    payload = {
        "artifact": "top_strategy_html_report",
        "source_report": str(mining_report_path),
        "symbol": mining_report.get("symbol"),
        "timeframe": mining_report.get("timeframe"),
        "date_from": mining_report.get("date_from"),
        "date_to": mining_report.get("date_to"),
        "round_trip_cost_usd": round_trip_cost_usd,
        "selection_objective": objective,
        "selection_policy": _selection_policy(objective),
        "strategy_count": len(enriched),
        "strategies": enriched,
    }
    payload["report_hash"] = stable_hash(_json_ready(payload))

    output_html.parent.mkdir(parents=True, exist_ok=True)
    data_path = output_html.with_suffix(".data.json")
    write_json(data_path, _json_ready(_export_payload(payload)))
    output_html.write_text(_render_html(payload, data_path.name), encoding="utf-8")
    return {
        "html": str(output_html),
        "data": str(data_path),
        "selection_objective": objective,
        "strategy_count": len(enriched),
        "report_hash": payload["report_hash"],
    }


def _select_top_yearly_strategies(
    report: dict[str, Any],
    top_n: int,
    objective: str = "annualized_net_pnl",
) -> list[dict[str, Any]]:
    candidates = []
    for replay in report.get("regime_basket_replays", []):
        for candidate in replay.get("yearly_profitable_candidates", []):
            candidates.append(
                {
                    **candidate,
                    "basket_id": replay.get("basket_id"),
                    "basket_hash": replay.get("basket_hash"),
                }
            )
    candidates.sort(key=lambda row: _selection_key(row, objective), reverse=True)
    selected = []
    seen = set()
    for candidate in candidates:
        signature = stable_hash(
            {
                "selection_rule": candidate.get("selection_rule"),
                "constituent_edges": candidate.get("constituent_edges"),
            }
        )
        if signature in seen:
            continue
        seen.add(signature)
        selected.append({**candidate, "strategy_signature": signature})
        if len(selected) >= top_n:
            break
    return selected


def _selection_key(candidate: dict[str, Any], objective: str) -> tuple[float, float, float, float]:
    full = candidate.get("full_after_activation") or {}
    test = candidate.get("test") or {}
    train_period = candidate.get("train_period") or {}
    test_period = candidate.get("test_period") or {}
    full_pf = float(full.get("profit_factor") or 0.0)
    test_pf = float(test.get("profit_factor") or 0.0)
    full_net = float(full.get("net_pnl") or 0.0)
    test_net = float(test.get("net_pnl") or 0.0)
    annual_trades = float(full.get("annual_trades") or 0.0)
    covered_days = max(1.0, float(train_period.get("covered_days") or 0.0) + float(test_period.get("covered_days") or 0.0))
    annualized_net = full_net / covered_days * 365.0
    if objective == "profit_factor":
        return (full_pf, test_pf, full_net, annual_trades)
    if objective == "test_profit_factor":
        return (test_pf, full_pf, test_net, annual_trades)
    if objective == "balanced":
        return (annualized_net, min(full_pf, test_pf), full_net, annual_trades)
    if objective == "net_pnl":
        return (full_net, test_net, annual_trades, full_pf)
    if objective == "annualized_net_pnl":
        return (annualized_net, full_net, test_net, full_pf)
    return (full_net, test_net, annual_trades, full_pf)


def _selection_policy(objective: str) -> str:
    policies = {
        "net_pnl": "top yearly-profitable strategies by net PnL, de-duplicated by edge composition",
        "annualized_net_pnl": "top yearly-profitable strategies by annualized net PnL per 1 contract, de-duplicated by edge composition",
        "profit_factor": "top yearly-profitable strategies by full-period profit factor, de-duplicated by edge composition",
        "test_profit_factor": "top yearly-profitable strategies by recent test-period profit factor, de-duplicated by edge composition",
        "balanced": "top yearly-profitable strategies by annualized net PnL, then the weaker of full-period and test-period profit factor",
    }
    return policies.get(objective, policies["annualized_net_pnl"])


def _replay_context(report: dict[str, Any], data_root: Path) -> dict[str, Any]:
    cost_model = report.get("cost_model") or {}
    return {
        "parquet_glob": _duckdb_glob(data_root, str(report["symbol"]), str(report["timeframe"])),
        "date_from": str(report["date_from"]),
        "date_to_exclusive": (date.fromisoformat(str(report["date_to"])) + timedelta(days=1)).isoformat(),
        "point_value": float(cost_model.get("point_value") or 20.0),
        "timeframe_minutes": int(report.get("timeframe_minutes") or timeframe_minutes(str(report["timeframe"]))),
        "continuity_tolerance_minutes": max(2, int(report.get("timeframe_minutes") or 1) * 2),
        "min_annual_trades": float((report.get("target") or {}).get("min_annual_trades") or 1000),
        "min_win_probability": float((report.get("target") or {}).get("min_win_probability") or 0.53),
        "cost_stress_usd_per_trade": [
            {"label": "configured_cost", "additional_round_trip_ticks": 0, "extra_usd_per_trade": 0.0},
            {
                "label": "configured_cost_plus_1_tick",
                "additional_round_trip_ticks": 1,
                "extra_usd_per_trade": float(cost_model.get("tick_value") or 5.0),
            },
            {
                "label": "configured_cost_plus_2_ticks",
                "additional_round_trip_ticks": 2,
                "extra_usd_per_trade": float(cost_model.get("tick_value") or 5.0) * 2.0,
            },
        ],
    }


def _replay_strategy_signals(
    con: duckdb.DuckDBPyConnection,
    context: dict[str, Any],
    strategy: dict[str, Any],
    round_trip_cost_usd: float,
) -> list[dict[str, Any]]:
    edges = strategy.get("constituent_edges") or []
    signals = []
    for horizon_minutes in sorted({int(edge["horizon_minutes"]) for edge in edges}):
        indexed_edges = [
            (index, edge)
            for index, edge in enumerate(edges)
            if int(edge["horizon_minutes"]) == horizon_minutes
        ]
        signals.extend(
            _replay_regime_edges_for_horizon(
                con,
                context,
                horizon_minutes=horizon_minutes,
                indexed_edges=indexed_edges,
                round_trip_cost_usd=round_trip_cost_usd,
            )
        )
    activation_year = int(strategy["activation_start_year"])
    active = [
        signal
        for signal in signals
        if _signal_timestamp(signal).year >= activation_year
    ]
    return sorted(_one_trade_per_timestamp(active), key=lambda signal: signal["timestamp"])


def _enrich_strategy(
    *,
    con: duckdb.DuckDBPyConnection,
    context: dict[str, Any],
    strategy: dict[str, Any],
    signals: Sequence[dict[str, Any]],
    sample_trades: Sequence[dict[str, Any]],
    bar_minutes: int,
) -> dict[str, Any]:
    day_count = _signals_covered_days(signals)
    replay_metrics = _period_replay_metrics(signals, context)
    replay_metrics["annualized_net_pnl"] = float(replay_metrics.get("net_pnl") or 0.0) / max(1, day_count) * 365.0
    monthly = _monthly_signal_results(signals)
    equity_curve = _equity_curve(signals)
    benchmark = _benchmark_bundle(con, context, signals)
    excess_summary = _excess_summary(replay_metrics, benchmark["metrics"])
    annual_excess = _merge_period_results(_yearly_signal_results(signals), benchmark["annual_results"], "year")
    monthly_excess = _merge_period_results(monthly, benchmark["monthly_results"], "period")
    samples = []
    for sample in sample_trades:
        bars = _load_trade_window_bars(con, context, sample, bar_minutes=bar_minutes)
        samples.append(
            {
                "label": sample["sample_label"],
                "trade": _trade_marker(sample),
                "bars": bars,
                "chart_svg": _candlestick_svg(bars, sample),
            }
        )
    return {
        **strategy,
        "replayed_metrics": replay_metrics,
        "annual_results": _yearly_signal_results(signals),
        "monthly_results": monthly,
        "equity_curve": equity_curve,
        "equity_curve_sampled": _sample_series(equity_curve, max_points=1200),
        "benchmark": benchmark,
        "excess_summary": excess_summary,
        "annual_excess_results": annual_excess,
        "monthly_excess_results": monthly_excess,
        "cost_stress": _cost_stress_metrics(signals, day_count, context),
        "trade_markers": [_trade_marker(signal) for signal in signals],
        "sample_trade_charts": samples,
        "signal_count": len(signals),
    }


def _monthly_signal_results(signals: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_month: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        timestamp = _signal_timestamp(signal)
        by_month[(timestamp.year, timestamp.month)].append(signal)
    rows = []
    for (year, month), month_signals in sorted(by_month.items()):
        ordered = sorted(month_signals, key=lambda signal: signal["timestamp"])
        first_day = _signal_timestamp(ordered[0]).date()
        last_day = _signal_timestamp(ordered[-1]).date()
        metrics = _signal_metrics(ordered, max(1, (last_day - first_day).days + 1))
        rows.append(
            {
                "year": year,
                "month": month,
                "period": f"{year}-{month:02d}",
                "period_from": first_day.isoformat(),
                "period_to": last_day.isoformat(),
                **metrics,
            }
        )
    return rows


def _equity_curve(signals: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    equity = 0.0
    rows = []
    for index, signal in enumerate(sorted(signals, key=lambda item: item["timestamp"]), start=1):
        equity += float(signal["pnl"])
        rows.append(
            {
                "trade": index,
                "timestamp": _signal_timestamp(signal).isoformat(sep=" "),
                "equity": equity,
                "pnl": float(signal["pnl"]),
            }
        )
    return rows


def _benchmark_bundle(
    con: duckdb.DuckDBPyConnection,
    context: dict[str, Any],
    signals: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    if not signals:
        return {
            "metrics": {"net_pnl": 0.0, "annual_trades": 0.0},
            "equity_curve": [],
            "equity_curve_sampled": [],
            "annual_results": [],
            "monthly_results": [],
        }
    start_ts = _signal_timestamp(signals[0])
    end_ts = _signal_timestamp(signals[-1])
    bars = _fetch_dicts(
        con,
        f"""
        SELECT timestamp, close
        FROM read_parquet('{context["parquet_glob"]}')
        WHERE timestamp >= '{start_ts.isoformat(sep=" ")}'
          AND timestamp <= '{end_ts.isoformat(sep=" ")}'
        ORDER BY timestamp
        """,
    )
    if not bars:
        return {
            "metrics": {"net_pnl": 0.0, "annual_trades": 0.0},
            "equity_curve": [],
            "equity_curve_sampled": [],
            "annual_results": [],
            "monthly_results": [],
        }
    start_close = float(bars[0]["close"])
    point_value = float(context["point_value"])
    curve = []
    for index, bar in enumerate(bars, start=1):
        pnl = (float(bar["close"]) - start_close) * point_value
        curve.append(
            {
                "trade": index,
                "timestamp": _to_iso(bar["timestamp"]),
                "close": float(bar["close"]),
                "equity": pnl,
                "pnl": pnl,
            }
        )
    metrics = _benchmark_metrics(curve)
    return {
        "metrics": metrics,
        "equity_curve": curve,
        "equity_curve_sampled": _sample_series(curve, max_points=1200),
        "annual_results": _benchmark_period_results(curve, "year", point_value),
        "monthly_results": _benchmark_period_results(curve, "month", point_value),
    }


def _benchmark_metrics(curve: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not curve:
        return {"trades": 0, "annual_trades": 0.0, "net_pnl": 0.0, "win_probability": None, "avg_pnl": None, "profit_factor": None, "max_drawdown": 0.0, "return_to_drawdown": None}
    net_pnl = float(curve[-1]["equity"])
    peak = float("-inf")
    max_drawdown = 0.0
    for row in curve:
        equity = float(row["equity"])
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    first_ts = datetime.fromisoformat(str(curve[0]["timestamp"]))
    last_ts = datetime.fromisoformat(str(curve[-1]["timestamp"]))
    covered_days = max(1, (last_ts.date() - first_ts.date()).days + 1)
    return {
        "trades": len(curve),
        "annual_trades": len(curve) / covered_days * 365,
        "net_pnl": net_pnl,
        "annualized_net_pnl": net_pnl / covered_days * 365,
        "win_probability": None,
        "avg_pnl": net_pnl / len(curve),
        "profit_factor": None,
        "max_drawdown": max_drawdown,
        "return_to_drawdown": net_pnl / max_drawdown if max_drawdown else None,
    }


def _benchmark_period_results(curve: Sequence[dict[str, Any]], mode: str, point_value: float) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, int] | int, list[dict[str, Any]]] = defaultdict(list)
    for row in curve:
        ts = datetime.fromisoformat(str(row["timestamp"]))
        key = ts.year if mode == "year" else (ts.year, ts.month)
        grouped[key].append(row)
    results = []
    previous_period_end_equity = 0.0
    for key, rows in sorted(grouped.items()):
        first = rows[0]
        last = rows[-1]
        first_ts = datetime.fromisoformat(str(first["timestamp"]))
        last_ts = datetime.fromisoformat(str(last["timestamp"]))
        period_end_equity = float(last["equity"])
        period_pnl = period_end_equity - previous_period_end_equity
        previous_period_end_equity = period_end_equity
        payload = {
            "period_from": first_ts.date().isoformat(),
            "period_to": last_ts.date().isoformat(),
            "net_pnl": period_pnl,
            "profit_factor": None,
            "win_probability": None,
            "max_drawdown": None,
            "trades": len(rows),
            "annual_trades": 0.0,
        }
        if mode == "year":
            payload["year"] = int(key)
        else:
            year, month = key
            payload["year"] = year
            payload["month"] = month
            payload["period"] = f"{year}-{month:02d}"
        results.append(payload)
    return results


def _merge_period_results(
    strategy_rows: Sequence[dict[str, Any]],
    benchmark_rows: Sequence[dict[str, Any]],
    label_key: str,
) -> list[dict[str, Any]]:
    bench_by_key = {row[label_key]: row for row in benchmark_rows if label_key in row}
    merged = []
    for row in strategy_rows:
        bench = bench_by_key.get(row[label_key], {})
        merged.append(
            {
                **row,
                "benchmark_net_pnl": float(bench.get("net_pnl") or 0.0),
                "excess_net_pnl": float(row.get("net_pnl") or 0.0) - float(bench.get("net_pnl") or 0.0),
            }
        )
    return merged


def _excess_summary(strategy_metrics: dict[str, Any], benchmark_metrics: dict[str, Any]) -> dict[str, Any]:
    strategy_net = float(strategy_metrics.get("net_pnl") or 0.0)
    strategy_annualized = float(strategy_metrics.get("annualized_net_pnl") or 0.0)
    benchmark_net = float(benchmark_metrics.get("net_pnl") or 0.0)
    benchmark_annualized = float(benchmark_metrics.get("annualized_net_pnl") or 0.0)
    return {
        "strategy_net_pnl": strategy_net,
        "strategy_annualized_net_pnl": strategy_annualized,
        "benchmark_net_pnl": benchmark_net,
        "benchmark_annualized_net_pnl": benchmark_annualized,
        "excess_net_pnl": strategy_net - benchmark_net,
        "excess_annualized_net_pnl": strategy_annualized - benchmark_annualized,
    }


def _sample_trades(signals: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if not signals:
        return []
    candidates = [
        ("best_trade", max(signals, key=lambda row: float(row["pnl"]))),
        ("worst_trade", min(signals, key=lambda row: float(row["pnl"]))),
        ("latest_trade", max(signals, key=lambda row: _signal_timestamp(row))),
    ]
    selected = []
    seen = set()
    for label, signal in candidates:
        key = (_signal_timestamp(signal), signal.get("rule_index"))
        if key in seen:
            continue
        seen.add(key)
        selected.append({**signal, "sample_label": label})
        if len(selected) >= limit:
            break
    return selected


def _load_trade_window_bars(
    con: duckdb.DuckDBPyConnection,
    context: dict[str, Any],
    trade: dict[str, Any],
    *,
    bar_minutes: int,
) -> list[dict[str, Any]]:
    entry = _signal_timestamp(trade)
    exit_value = trade.get("exit_timestamp")
    exit_ts = datetime.fromisoformat(str(exit_value)) if isinstance(exit_value, str) else exit_value
    if not isinstance(exit_ts, datetime):
        exit_ts = entry + timedelta(minutes=int(trade.get("horizon_minutes") or bar_minutes))
    pad = timedelta(minutes=bar_minutes * 40)
    rows = _fetch_dicts(
        con,
        f"""
        SELECT timestamp, open, high, low, close, tick_count
        FROM read_parquet('{context["parquet_glob"]}')
        WHERE timestamp >= '{(entry - pad).isoformat(sep=" ")}'
          AND timestamp <= '{(exit_ts + pad).isoformat(sep=" ")}'
        ORDER BY timestamp
        """,
    )
    return [
        {
            "timestamp": _to_iso(row["timestamp"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "bar_volume": int(row.get("tick_count") or 0),
        }
        for row in rows
    ]


def _trade_marker(signal: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_timestamp": _to_iso(signal["timestamp"]),
        "exit_timestamp": _to_iso(signal.get("exit_timestamp")),
        "entry_price": float(signal.get("entry_price") or 0.0),
        "exit_price": float(signal.get("exit_price") or 0.0),
        "direction": signal.get("direction_label"),
        "pnl": float(signal.get("pnl") or 0.0),
        "horizon_minutes": int(signal.get("horizon_minutes") or 0),
        "scan_type": signal.get("scan_type"),
        "session_bucket": signal.get("session_bucket"),
        "volume_bin": signal.get("volume_bin"),
        "range_bin": signal.get("range_bin"),
    }


def _render_html(payload: dict[str, Any], data_filename: str) -> str:
    strategy_sections = "\n".join(_strategy_section(strategy) for strategy in payload["strategies"])
    overview_rows = "\n".join(_overview_row(strategy) for strategy in payload["strategies"])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NQ_CME Top 3 策略表现报告</title>
  <style>
    :root {{
      --bg: #f6f8fb;
      --surface: #ffffff;
      --ink: #172033;
      --muted: #667085;
      --line: #d9e0ea;
      --accent: #1769aa;
      --good: #117a4c;
      --bad: #b42318;
      --warn: #a15c00;
      --soft: #eaf2fb;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: var(--bg); line-height: 1.5; }}
    header {{ background: #102033; color: white; padding: 28px 32px; }}
    main {{ max-width: 1320px; margin: 0 auto; padding: 24px; }}
    h1, h2, h3 {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 30px; }}
    h2 {{ font-size: 22px; margin-bottom: 14px; }}
    h3 {{ font-size: 17px; margin: 18px 0 10px; }}
    p {{ margin: 8px 0; }}
    .meta {{ color: #c9d6e6; margin-top: 10px; }}
    .section {{ background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 20px; margin-bottom: 18px; }}
    .grid {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    .metric {{ border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #fbfcfe; }}
    .metric span {{ display: block; color: var(--muted); font-size: 12px; }}
    .metric strong {{ display: block; margin-top: 4px; font-size: 20px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 8px; text-align: right; vertical-align: top; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ color: var(--muted); font-weight: 600; background: #f8fafc; }}
    .pill {{ display: inline-block; border: 1px solid var(--line); border-radius: 999px; padding: 3px 8px; margin: 2px; background: var(--soft); font-size: 12px; }}
    .good {{ color: var(--good); }}
    .bad {{ color: var(--bad); }}
    .warn {{ color: var(--warn); }}
    .chart {{ width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; background: white; padding: 10px; }}
    .kline-grid {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); }}
    .note {{ color: var(--muted); font-size: 13px; }}
    .heat td {{ min-width: 68px; }}
    details {{ border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; margin-top: 10px; background: #fbfcfe; }}
    summary {{ cursor: pointer; font-weight: 600; }}
    @media (max-width: 760px) {{
      header {{ padding: 22px 18px; }}
      main {{ padding: 14px; }}
      h1 {{ font-size: 24px; }}
      table {{ font-size: 12px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>NQ_CME Top 3 策略表现报告</h1>
    <div class="meta">数据: {html.escape(str(payload["symbol"]))} {html.escape(str(payload["timeframe"]))} OHLCV, {html.escape(str(payload["date_from"]))} 至 {html.escape(str(payload["date_to"]))}; 成本: {fmt_usd(payload["round_trip_cost_usd"])} / trade</div>
  </header>
  <main>
    <section class="section">
      <h2>总览</h2>
      <p>本报告从已有挖掘结果中选择 Top 3，并按策略构成去重。选择目标: {html.escape(str(payload.get("selection_objective")))}；选择规则: {html.escape(str(payload.get("selection_policy")))}。所有交易均基于 OHLCV bar 级重放，不能证明真实 bid/ask、限价成交率或排队成本。</p>
      <table>
        <thead><tr><th>策略</th><th>激活年份</th><th>边数量</th><th>净收益</th><th>PF</th><th>胜率</th><th>年化交易</th><th>最大回撤</th></tr></thead>
        <tbody>{overview_rows}</tbody>
      </table>
      <p class="note">配套结构化数据: {html.escape(data_filename)}</p>
    </section>
    {strategy_sections}
  </main>
</body>
</html>
"""


def _strategy_section(strategy: dict[str, Any]) -> str:
    metrics = strategy["replayed_metrics"]
    benchmark = strategy["benchmark"]
    benchmark_metrics = benchmark["metrics"]
    excess = strategy["excess_summary"]
    analysis = strategy.get("strategy_analysis") or {}
    profile = analysis.get("strategy_profile") or {}
    return f"""
    <section class="section">
      <h2>{html.escape(strategy["display_id"])} · {html.escape(str(strategy["selection_rule"]))}</h2>
      <div class="grid">
        {_metric("净收益", fmt_usd(metrics["net_pnl"]))}
        {_metric("Profit Factor", fmt_num(metrics["profit_factor"], 3))}
        {_metric("胜率", fmt_pct(metrics["win_probability"]))}
        {_metric("年化交易", fmt_num(metrics["annual_trades"], 1))}
        {_metric("年化净收益", fmt_usd(metrics.get("annualized_net_pnl")))}
        {_metric("最大回撤", fmt_usd(metrics["max_drawdown"]))}
        {_metric("交易数", fmt_int(metrics["trades"]))}
      </div>
      <h3>基准与超额收益</h3>
      <div class="grid">
        {_metric("策略净收益", fmt_usd(excess["strategy_net_pnl"]))}
        {_metric("策略年化净收益", fmt_usd(excess["strategy_annualized_net_pnl"]))}
        {_metric("NQ 持有净收益", fmt_usd(excess["benchmark_net_pnl"]))}
        {_metric("NQ 持有年化净收益", fmt_usd(excess["benchmark_annualized_net_pnl"]))}
        {_metric("超额收益", fmt_usd(excess["excess_net_pnl"]))}
        {_metric("年化超额收益", fmt_usd(excess["excess_annualized_net_pnl"]))}
        {_metric("基准最大回撤", fmt_usd(benchmark_metrics["max_drawdown"]))}
      </div>
      <h3>策略构成</h3>
      <p>{_profile_pills(profile)}</p>
      <table>{_edge_rows(strategy.get("constituent_edges") or [])}</table>
      <h3>资金曲线</h3>
      <div class="chart">{_line_svg(strategy["equity_curve_sampled"], title="累计净收益")}</div>
      <div class="chart">{_comparison_line_svg(strategy["equity_curve_sampled"], benchmark["equity_curve_sampled"])}</div>
      <h3>年度表现</h3>
      <div class="chart">{_bar_svg(strategy["annual_results"], "year", "net_pnl")}</div>
      <table>{_period_rows(strategy["annual_results"], ["year"])}</table>
      <details><summary>展开年度基准与超额收益</summary><table>{_excess_period_rows(strategy["annual_excess_results"], ["year"])}</table></details>
      <h3>月度表现</h3>
      {_monthly_heatmap(strategy["monthly_results"])}
      <details><summary>展开月度明细</summary><table>{_period_rows(strategy["monthly_results"], ["period"])}</table></details>
      <details><summary>展开月度基准与超额收益</summary><table>{_excess_period_rows(strategy["monthly_excess_results"], ["period"])}</table></details>
      <h3>进场/出场 K 线位置</h3>
      <div class="kline-grid">{''.join(_sample_chart(sample) for sample in strategy["sample_trade_charts"])}</div>
      <h3>优点与缺点</h3>
      <div class="grid">
        <div>{_bullet_list("优点", analysis.get("strengths") or [])}</div>
        <div>{_bullet_list("缺点", analysis.get("weaknesses") or [])}</div>
      </div>
      <h3>成本压力</h3>
      <table>{_cost_stress_rows(strategy["cost_stress"])}</table>
    </section>
    """


def _overview_row(strategy: dict[str, Any]) -> str:
    metrics = strategy["replayed_metrics"]
    return (
        f"<tr><td>{html.escape(strategy['display_id'])}</td>"
        f"<td>{strategy['activation_start_year']}</td>"
        f"<td>{strategy['edge_count']}</td>"
        f"<td>{fmt_usd(metrics['net_pnl'])}</td>"
        f"<td>{fmt_num(metrics['profit_factor'], 3)}</td>"
        f"<td>{fmt_pct(metrics['win_probability'])}</td>"
        f"<td>{fmt_num(metrics['annual_trades'], 1)}</td>"
        f"<td>{fmt_usd(metrics['max_drawdown'])}</td></tr>"
    )


def _metric(label: str, value: str) -> str:
    return f"<div class=\"metric\"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>"


def _profile_pills(profile: dict[str, Any]) -> str:
    parts = [
        ("主导 VOL", profile.get("dominant_volume_profile")),
        ("主导时段", profile.get("dominant_session_bucket")),
        ("家族", profile.get("families")),
        ("方向", profile.get("directions")),
    ]
    return " ".join(f"<span class=\"pill\">{html.escape(label)}: {html.escape(str(value))}</span>" for label, value in parts)


def _edge_rows(edges: Sequence[dict[str, Any]]) -> str:
    rows = [
        "<thead><tr><th>scan_type</th><th>方向</th><th>周期</th><th>时段</th><th>DOW</th><th>trend</th><th>volume</th><th>range</th></tr></thead><tbody>"
    ]
    for edge in edges:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(edge.get('scan_type')))}</td>"
            f"<td>{html.escape(str(edge.get('direction_label')))}</td>"
            f"<td>{edge.get('horizon_minutes')}</td>"
            f"<td>{html.escape(str(edge.get('session_bucket')))}</td>"
            f"<td>{edge.get('dow')}</td>"
            f"<td>{edge.get('trend_bin')}</td>"
            f"<td>{edge.get('volume_bin')}</td>"
            f"<td>{edge.get('range_bin')}</td>"
            "</tr>"
        )
    rows.append("</tbody>")
    return "".join(rows)


def _period_rows(rows: Sequence[dict[str, Any]], label_keys: Sequence[str]) -> str:
    out = ["<thead><tr><th>周期</th><th>交易数</th><th>净收益</th><th>PF</th><th>胜率</th><th>最大回撤</th></tr></thead><tbody>"]
    for row in rows:
        label = " ".join(str(row.get(key)) for key in label_keys)
        css = "good" if float(row.get("net_pnl") or 0) >= 0 else "bad"
        out.append(
            f"<tr><td>{html.escape(label)}</td><td>{fmt_int(row.get('trades'))}</td>"
            f"<td class=\"{css}\">{fmt_usd(row.get('net_pnl'))}</td>"
            f"<td>{fmt_num(row.get('profit_factor'), 3)}</td>"
            f"<td>{fmt_pct(row.get('win_probability'))}</td>"
            f"<td>{fmt_usd(row.get('max_drawdown'))}</td></tr>"
        )
    out.append("</tbody>")
    return "".join(out)


def _cost_stress_rows(rows: Sequence[dict[str, Any]]) -> str:
    out = ["<thead><tr><th>场景</th><th>额外成本</th><th>净收益</th><th>PF</th><th>胜率</th></tr></thead><tbody>"]
    for row in rows:
        out.append(
            f"<tr><td>{html.escape(str(row.get('label')))}</td><td>{fmt_usd(row.get('extra_usd_per_trade'))}</td>"
            f"<td>{fmt_usd(row.get('net_pnl'))}</td><td>{fmt_num(row.get('profit_factor'), 3)}</td>"
            f"<td>{fmt_pct(row.get('win_probability'))}</td></tr>"
        )
    out.append("</tbody>")
    return "".join(out)


def _excess_period_rows(rows: Sequence[dict[str, Any]], label_keys: Sequence[str]) -> str:
    out = ["<thead><tr><th>周期</th><th>策略净收益</th><th>基准净收益</th><th>超额收益</th><th>策略PF</th><th>策略胜率</th></tr></thead><tbody>"]
    for row in rows:
        label = " ".join(str(row.get(key)) for key in label_keys)
        excess_css = "good" if float(row.get("excess_net_pnl") or 0) >= 0 else "bad"
        out.append(
            f"<tr><td>{html.escape(label)}</td>"
            f"<td>{fmt_usd(row.get('net_pnl'))}</td>"
            f"<td>{fmt_usd(row.get('benchmark_net_pnl'))}</td>"
            f"<td class=\"{excess_css}\">{fmt_usd(row.get('excess_net_pnl'))}</td>"
            f"<td>{fmt_num(row.get('profit_factor'), 3)}</td>"
            f"<td>{fmt_pct(row.get('win_probability'))}</td></tr>"
        )
    out.append("</tbody>")
    return "".join(out)


def _monthly_heatmap(rows: Sequence[dict[str, Any]]) -> str:
    by_year_month = {(row["year"], row["month"]): row for row in rows}
    years = sorted({row["year"] for row in rows})
    max_abs = max((abs(float(row.get("net_pnl") or 0.0)) for row in rows), default=1.0)
    out = ["<table class=\"heat\"><thead><tr><th>年</th>"]
    out.extend(f"<th>{month:02d}</th>" for month in range(1, 13))
    out.append("</tr></thead><tbody>")
    for year in years:
        out.append(f"<tr><td>{year}</td>")
        for month in range(1, 13):
            row = by_year_month.get((year, month))
            if not row:
                out.append("<td></td>")
                continue
            pnl = float(row.get("net_pnl") or 0.0)
            alpha = 0.12 + 0.72 * min(1.0, abs(pnl) / max_abs)
            color = f"rgba(17,122,76,{alpha:.2f})" if pnl >= 0 else f"rgba(180,35,24,{alpha:.2f})"
            out.append(f"<td style=\"background:{color}\">{fmt_usd(pnl)}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _sample_chart(sample: dict[str, Any]) -> str:
    trade = sample["trade"]
    return (
        "<div>"
        f"<h3>{html.escape(sample['label'])}: {html.escape(str(trade['entry_timestamp']))} "
        f"{html.escape(str(trade['direction']))} PnL {fmt_usd(trade['pnl'])}</h3>"
        f"<div class=\"chart\">{sample['chart_svg']}</div>"
        "</div>"
    )


def _bullet_list(title: str, items: Sequence[str]) -> str:
    body = "".join(f"<li>{html.escape(str(item))}</li>" for item in items)
    return f"<h3>{html.escape(title)}</h3><ul>{body}</ul>"


def _line_svg(points: Sequence[dict[str, Any]], *, title: str) -> str:
    width, height = 900, 260
    pad = 38
    if not points:
        return "<svg viewBox=\"0 0 900 260\"></svg>"
    values = [float(point["equity"]) for point in points]
    low, high = min(values), max(values)
    if high == low:
        high += 1.0
        low -= 1.0
    def xy(index: int, value: float) -> tuple[float, float]:
        x = pad + index / max(1, len(points) - 1) * (width - pad * 2)
        y = height - pad - (value - low) / (high - low) * (height - pad * 2)
        return x, y
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, float(point["equity"])) for i, point in enumerate(points)))
    zero_y = xy(0, 0.0)[1] if low <= 0 <= high else height - pad
    return (
        f"<svg viewBox=\"0 0 {width} {height}\" role=\"img\" aria-label=\"{html.escape(title)}\">"
        f"<text x=\"{pad}\" y=\"22\" fill=\"#667085\" font-size=\"13\">{html.escape(title)}</text>"
        f"<line x1=\"{pad}\" y1=\"{zero_y:.1f}\" x2=\"{width-pad}\" y2=\"{zero_y:.1f}\" stroke=\"#d9e0ea\"/>"
        f"<polyline fill=\"none\" stroke=\"#1769aa\" stroke-width=\"2.2\" points=\"{poly}\"/>"
        f"<text x=\"{pad}\" y=\"{height-8}\" fill=\"#667085\" font-size=\"12\">{html.escape(str(points[0]['timestamp'])[:10])}</text>"
        f"<text x=\"{width-pad-86}\" y=\"{height-8}\" fill=\"#667085\" font-size=\"12\">{html.escape(str(points[-1]['timestamp'])[:10])}</text>"
        f"<text x=\"{width-pad-112}\" y=\"22\" fill=\"#172033\" font-size=\"12\">max {fmt_usd(high)}</text>"
        f"</svg>"
    )


def _comparison_line_svg(strategy_points: Sequence[dict[str, Any]], benchmark_points: Sequence[dict[str, Any]]) -> str:
    width, height = 900, 260
    pad = 38
    if not strategy_points or not benchmark_points:
        return "<svg viewBox=\"0 0 900 260\"></svg>"
    bench_by_ts = {str(point["timestamp"]): float(point["equity"]) for point in benchmark_points}
    aligned = [
        {
            "timestamp": point["timestamp"],
            "strategy": float(point["equity"]),
            "benchmark": bench_by_ts.get(str(point["timestamp"]), 0.0),
        }
        for point in strategy_points
    ]
    values = [row["strategy"] for row in aligned] + [row["benchmark"] for row in aligned]
    low, high = min(values), max(values)
    if high == low:
        high += 1.0
        low -= 1.0
    def xy(index: int, value: float) -> tuple[float, float]:
        x = pad + index / max(1, len(aligned) - 1) * (width - pad * 2)
        y = height - pad - (value - low) / (high - low) * (height - pad * 2)
        return x, y
    strategy_poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, row["strategy"]) for i, row in enumerate(aligned)))
    benchmark_poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, row["benchmark"]) for i, row in enumerate(aligned)))
    return (
        f"<svg viewBox=\"0 0 {width} {height}\" role=\"img\" aria-label=\"策略与基准对比曲线\">"
        f"<text x=\"{pad}\" y=\"22\" fill=\"#667085\" font-size=\"13\">策略 vs NQ 持有累计收益</text>"
        f"<polyline fill=\"none\" stroke=\"#1769aa\" stroke-width=\"2.2\" points=\"{strategy_poly}\"/>"
        f"<polyline fill=\"none\" stroke=\"#a15c00\" stroke-width=\"2.2\" points=\"{benchmark_poly}\"/>"
        f"<text x=\"{pad}\" y=\"{height-8}\" fill=\"#1769aa\" font-size=\"12\">策略</text>"
        f"<text x=\"{pad+48}\" y=\"{height-8}\" fill=\"#a15c00\" font-size=\"12\">基准</text>"
        f"</svg>"
    )


def _bar_svg(rows: Sequence[dict[str, Any]], label_key: str, value_key: str) -> str:
    width, height = 900, 260
    pad = 38
    values = [float(row.get(value_key) or 0.0) for row in rows]
    if not values:
        return "<svg viewBox=\"0 0 900 260\"></svg>"
    low, high = min(0.0, min(values)), max(0.0, max(values))
    if high == low:
        high += 1.0
    zero_y = height - pad - (0.0 - low) / (high - low) * (height - pad * 2)
    bar_w = (width - pad * 2) / max(1, len(rows))
    bars = []
    for index, row in enumerate(rows):
        value = float(row.get(value_key) or 0.0)
        x = pad + index * bar_w + 3
        y = height - pad - (value - low) / (high - low) * (height - pad * 2)
        top = min(y, zero_y)
        h = max(1, abs(zero_y - y))
        color = "#117a4c" if value >= 0 else "#b42318"
        bars.append(f"<rect x=\"{x:.1f}\" y=\"{top:.1f}\" width=\"{max(2, bar_w-6):.1f}\" height=\"{h:.1f}\" fill=\"{color}\"/>")
        bars.append(f"<text x=\"{x:.1f}\" y=\"{height-8}\" fill=\"#667085\" font-size=\"10\">{html.escape(str(row.get(label_key)))}</text>")
    return (
        f"<svg viewBox=\"0 0 {width} {height}\" role=\"img\" aria-label=\"年度净收益柱状图\">"
        f"<line x1=\"{pad}\" y1=\"{zero_y:.1f}\" x2=\"{width-pad}\" y2=\"{zero_y:.1f}\" stroke=\"#667085\"/>"
        f"{''.join(bars)}</svg>"
    )


def _candlestick_svg(bars: Sequence[dict[str, Any]], trade: dict[str, Any]) -> str:
    width, height = 720, 320
    pad = 42
    if not bars:
        return f"<svg viewBox=\"0 0 {width} {height}\"></svg>"
    lows = [float(bar["low"]) for bar in bars]
    highs = [float(bar["high"]) for bar in bars]
    low, high = min(lows), max(highs)
    if high == low:
        high += 1.0
    x_step = (width - pad * 2) / max(1, len(bars))
    def y(price: float) -> float:
        return height - pad - (price - low) / (high - low) * (height - pad * 2)
    by_ts = {str(bar["timestamp"]): index for index, bar in enumerate(bars)}
    candles = []
    for index, bar in enumerate(bars):
        x = pad + index * x_step + x_step / 2
        open_y = y(float(bar["open"]))
        close_y = y(float(bar["close"]))
        high_y = y(float(bar["high"]))
        low_y = y(float(bar["low"]))
        color = "#117a4c" if float(bar["close"]) >= float(bar["open"]) else "#b42318"
        candles.append(f"<line x1=\"{x:.1f}\" y1=\"{high_y:.1f}\" x2=\"{x:.1f}\" y2=\"{low_y:.1f}\" stroke=\"{color}\" stroke-width=\"1\"/>")
        candles.append(f"<rect x=\"{x - max(2, x_step*0.32):.1f}\" y=\"{min(open_y, close_y):.1f}\" width=\"{max(2, x_step*0.64):.1f}\" height=\"{max(1, abs(close_y-open_y)):.1f}\" fill=\"{color}\" opacity=\"0.85\"/>")
    markers = []
    for label, ts_key, price_key, color in (
        ("ENTRY", "timestamp", "entry_price", "#1769aa"),
        ("EXIT", "exit_timestamp", "exit_price", "#a15c00"),
    ):
        timestamp = _to_iso(trade.get(ts_key))
        if timestamp not in by_ts:
            continue
        x = pad + by_ts[timestamp] * x_step + x_step / 2
        marker_y = y(float(trade.get(price_key) or 0.0))
        markers.append(f"<circle cx=\"{x:.1f}\" cy=\"{marker_y:.1f}\" r=\"5\" fill=\"{color}\" stroke=\"white\" stroke-width=\"2\"/>")
        markers.append(f"<text x=\"{x + 7:.1f}\" y=\"{marker_y - 7:.1f}\" fill=\"{color}\" font-size=\"11\" font-weight=\"700\">{label}</text>")
    return (
        f"<svg viewBox=\"0 0 {width} {height}\" role=\"img\" aria-label=\"K线入场出场位置\">"
        f"<text x=\"{pad}\" y=\"22\" fill=\"#667085\" font-size=\"12\">{html.escape(str(bars[0]['timestamp']))} 到 {html.escape(str(bars[-1]['timestamp']))}</text>"
        f"<text x=\"{width-pad-90}\" y=\"22\" fill=\"#667085\" font-size=\"12\">{high:.2f}</text>"
        f"<text x=\"{width-pad-90}\" y=\"{height-12}\" fill=\"#667085\" font-size=\"12\">{low:.2f}</text>"
        f"<line x1=\"{pad}\" y1=\"{pad}\" x2=\"{pad}\" y2=\"{height-pad}\" stroke=\"#d9e0ea\"/>"
        f"<line x1=\"{pad}\" y1=\"{height-pad}\" x2=\"{width-pad}\" y2=\"{height-pad}\" stroke=\"#d9e0ea\"/>"
        f"{''.join(candles)}{''.join(markers)}</svg>"
    )


def _sample_series(rows: Sequence[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    if len(rows) <= max_points:
        return list(rows)
    step = max(1, len(rows) // max_points)
    sampled = [row for index, row in enumerate(rows) if index % step == 0]
    if sampled[-1] is not rows[-1]:
        sampled.append(rows[-1])
    return sampled


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _export_payload(payload: dict[str, Any]) -> dict[str, Any]:
    exported = {**payload}
    exported["strategies"] = [_export_strategy(strategy) for strategy in payload.get("strategies", [])]
    return exported


def _export_strategy(strategy: dict[str, Any]) -> dict[str, Any]:
    exported = dict(strategy)
    exported.pop("equity_curve", None)
    benchmark = dict(exported.get("benchmark") or {})
    benchmark.pop("equity_curve", None)
    exported["benchmark"] = benchmark
    return exported


def _to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return datetime.fromisoformat(str(value)).isoformat(sep=" ")


def fmt_usd(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"${float(value):,.0f}"


def fmt_pct(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.2f}%"


def fmt_num(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):,.{digits}f}"


def fmt_int(value: Any) -> str:
    if value is None:
        return "0"
    return f"{int(value):,}"
