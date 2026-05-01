from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, time
from math import ceil, floor
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

import duckdb

from .config import CostModelConfig, SymbolConfig
from .features import compute_executable_features, feature_snapshot_hash, feature_value
from .metrics import BacktestMetrics, calculate_metrics
from .smc_state import SmcLqemParameters, SmcLqemStateMachine
from .storage import compute_data_version_hash
from .strategy import StrategySpec


EXECUTABLE_STRATEGY_FAMILIES = {
    "opening_range_breakout",
    "trend_pullback",
    "volatility_expansion",
    "intraday_momentum",
    "regime_filtered_mean_reversion",
    "time_of_day_edge",
    "gap_fade_or_continuation",
    "vol_breakout_trend",
    "ma_pullback_volume_confirm",
    "volume_absorption_reversion",
    "macd_ma_volume_confirm",
    "rsi_reversion_low_volume",
    "smc_lqem_ce",
}


@dataclass(frozen=True)
class Trade:
    symbol: str
    side: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    contracts: int
    gross_pnl: float
    fees: float
    slippage_cost: float
    net_pnl: float
    entry_reason: str
    exit_reason: str
    event_state_at_entry: str = "normal"
    event_state_at_exit: str = "normal"
    active_event_ids_at_entry: list[str] = field(default_factory=list)
    active_event_ids_at_exit: list[str] = field(default_factory=list)
    event_policy_action: str = "allow"
    blocked_or_delayed_reason: str | None = None
    feature_values_at_entry: dict = field(default_factory=dict)
    predicate_evaluation_at_entry: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["entry_time"] = self.entry_time.isoformat()
        payload["exit_time"] = self.exit_time.isoformat()
        return payload


@dataclass(frozen=True)
class BacktestResult:
    strategy_name: str
    symbol: str
    data_version_hash: str
    cost_model: dict
    trades: list[Trade]
    metrics: BacktestMetrics
    event_attribution: dict = field(default_factory=dict)
    feature_snapshot_hash: str | None = None
    executable_features: list[str] = field(default_factory=list)
    signal_health_report: dict | None = None

    def to_dict(self) -> dict:
        return {
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "data_version_hash": self.data_version_hash,
            "cost_model": self.cost_model,
            "trades": [trade.to_dict() for trade in self.trades],
            "metrics": self.metrics.to_dict(),
            "event_attribution": self.event_attribution,
            "feature_snapshot_hash": self.feature_snapshot_hash,
            "executable_features": self.executable_features,
            "signal_health_report": self.signal_health_report,
        }


def load_bar_rows(bar_files: Sequence[Path]) -> list[dict]:
    files = [str(path) for path in bar_files if path.exists()]
    if not files:
        return []
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            """
            SELECT
                symbol,
                timestamp,
                open,
                high,
                low,
                close,
                bid_close,
                ask_close,
                tick_count,
                avg_spread
            FROM read_parquet(?)
            ORDER BY timestamp
            """,
            [files],
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "symbol": row[0],
            "timestamp": row[1],
            "open": float(row[2]),
            "high": float(row[3]),
            "low": float(row[4]),
            "close": float(row[5]),
            "bid_close": float(row[6]),
            "ask_close": float(row[7]),
            "tick_count": int(row[8]),
            "avg_spread": float(row[9]) if row[9] is not None else 0.0,
        }
        for row in rows
    ]


def load_tick_rows(tick_files: Sequence[Path]) -> list[dict]:
    files = [str(path) for path in tick_files if path.exists()]
    if not files:
        return []
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            """
            SELECT
                symbol,
                timestamp,
                bid,
                ask,
                mid,
                spread
            FROM read_parquet(?)
            ORDER BY timestamp
            """,
            [files],
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "symbol": row[0],
            "timestamp": row[1],
            "bid": float(row[2]),
            "ask": float(row[3]),
            "mid": float(row[4]),
            "spread": float(row[5]),
        }
        for row in rows
    ]


def run_bar_backtest(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    bar_files: Sequence[Path],
    starting_equity: float = 100_000,
    cost_model: CostModelConfig | None = None,
    event_contexts: Sequence[dict] | None = None,
    signal_health_start=None,
    signal_health_end=None,
) -> BacktestResult:
    if spec.strategy_family not in EXECUTABLE_STRATEGY_FAMILIES:
        raise ValueError(f"Bar backtester does not support strategy_family: {spec.strategy_family}")
    cost_model = cost_model or default_cost_model(symbol_config, spec.cost_model)
    bars = load_bar_rows(bar_files)
    if spec.strategy_family == "smc_lqem_ce":
        feature_bars = bars
    else:
        feature_bars = compute_executable_features(
            bars,
            session_trade=spec.session.trade,
            flatten=spec.session.flatten,
            tick_size=cost_model.tick_size,
        )
    signal_health_bars = feature_bars
    if signal_health_start is not None and signal_health_end is not None:
        signal_health_bars = [
            bar
            for bar in feature_bars
            if signal_health_start <= bar["timestamp"].date() <= signal_health_end
        ]
    raw_trades = run_bar_strategy(spec, symbol_config, feature_bars, cost_model)
    trades, event_attribution = apply_event_policy_to_trades(spec, raw_trades, event_contexts)
    trade_pnls = [trade.net_pnl for trade in trades]
    equity = [starting_equity]
    for pnl in trade_pnls:
        equity.append(equity[-1] + pnl)
    days = len({bar["timestamp"].date() for bar in bars}) or 1
    metrics = calculate_metrics(trade_pnls, equity, starting_equity, days)
    event_attribution = enrich_event_attribution(event_attribution, trades, starting_equity, days)
    data_version_hash = backtest_data_version_hash(
        bar_files,
        spec,
        symbol_config,
        cost_model,
        execution_mode="bar",
    )
    return BacktestResult(
        spec.name,
        spec.symbol,
        data_version_hash,
        cost_model.to_dict(),
        trades,
        metrics,
        event_attribution,
        feature_snapshot_hash=feature_snapshot_hash(feature_bars),
        executable_features=sorted((feature_bars[0].get("features") or {}).keys()) if feature_bars else [],
        signal_health_report=build_signal_grammar_health_report(spec, signal_health_bars),
    )


def run_tick_backtest(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    tick_files: Sequence[Path],
    starting_equity: float = 100_000,
    cost_model: CostModelConfig | None = None,
    event_contexts: Sequence[dict] | None = None,
) -> BacktestResult:
    if spec.strategy_family not in EXECUTABLE_STRATEGY_FAMILIES:
        raise ValueError(f"Tick replay does not support strategy_family: {spec.strategy_family}")
    cost_model = cost_model or default_cost_model(symbol_config, spec.cost_model)
    ticks = load_tick_rows(tick_files)
    raw_trades = run_tick_strategy(spec, symbol_config, ticks, cost_model)
    trades, event_attribution = apply_event_policy_to_trades(spec, raw_trades, event_contexts)
    trade_pnls = [trade.net_pnl for trade in trades]
    equity = [starting_equity]
    for pnl in trade_pnls:
        equity.append(equity[-1] + pnl)
    days = len({tick["timestamp"].date() for tick in ticks}) or 1
    metrics = calculate_metrics(trade_pnls, equity, starting_equity, days)
    event_attribution = enrich_event_attribution(event_attribution, trades, starting_equity, days)
    data_version_hash = backtest_data_version_hash(
        tick_files,
        spec,
        symbol_config,
        cost_model,
        execution_mode="tick",
    )
    return BacktestResult(
        spec.name,
        spec.symbol,
        data_version_hash,
        cost_model.to_dict(),
        trades,
        metrics,
        event_attribution,
    )


def apply_event_policy_to_trades(
    spec: StrategySpec,
    trades: Sequence[Trade],
    event_contexts: Sequence[dict] | None,
) -> tuple[list[Trade], dict]:
    context_by_timestamp = normalize_event_contexts(event_contexts or [])
    if not context_by_timestamp:
        return list(trades), empty_event_attribution()
    kept: list[Trade] = []
    blocked = []
    event_window_trade_count = 0
    non_event_trade_count = 0
    for trade in trades:
        entry_context = context_by_timestamp.get(trade.entry_time) or normal_event_context()
        exit_context = context_by_timestamp.get(trade.exit_time) or normal_event_context()
        policy_action, reason = event_policy_action(spec, entry_context)
        annotated = replace(
            trade,
            event_state_at_entry=entry_context["event_state"],
            event_state_at_exit=exit_context["event_state"],
            active_event_ids_at_entry=list(entry_context["active_event_ids"]),
            active_event_ids_at_exit=list(exit_context["active_event_ids"]),
            event_policy_action=policy_action,
            blocked_or_delayed_reason=reason,
        )
        if entry_context["event_state"] == "normal":
            non_event_trade_count += 1
        else:
            event_window_trade_count += 1
        if policy_action == "block":
            blocked.append(annotated.to_dict())
            continue
        kept.append(annotated)
    return kept, {
        "schema_version": 1,
        "event_context_applied": True,
        "event_window_trade_count": event_window_trade_count,
        "non_event_trade_count": non_event_trade_count,
        "blocked_trade_count": len(blocked),
        "blocked_trades": blocked,
    }


def normalize_event_contexts(contexts: Sequence[dict]) -> dict[datetime, dict]:
    normalized = {}
    for context in contexts:
        timestamp = context.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).replace(tzinfo=None)
        if timestamp is None:
            continue
        normalized[timestamp] = {
            "event_state": context.get("event_state", "normal"),
            "active_event_ids": context.get("active_event_ids", []),
            "max_importance": context.get("max_importance"),
            "policy_ref": context.get("policy_ref"),
        }
    return normalized


def event_policy_action(spec: StrategySpec, context: dict) -> tuple[str, str | None]:
    event_state = context.get("event_state", "normal")
    if event_state == "normal":
        return "allow", None
    policy = spec.event_policy or {}
    if context.get("max_importance") == "high" and event_state == "release_window":
        return "block", "high_impact_release_window"
    if policy.get("new_entries") == "block":
        return "block", "event_policy_new_entries_block"
    if event_state == "pre_event" and int(policy.get("pre_event_blackout_minutes", 0)) > 0:
        return "block", "pre_event_blackout"
    if event_state == "post_event" and int(policy.get("post_event_blackout_minutes", 0)) > 0:
        return "block", "post_event_blackout"
    if policy.get("new_entries") == "require_extra_confirmation":
        return "delay", "event_policy_requires_extra_confirmation"
    return "allow", None


def normal_event_context() -> dict:
    return {"event_state": "normal", "active_event_ids": [], "max_importance": None, "policy_ref": None}


def empty_event_attribution() -> dict:
    return {
        "schema_version": 1,
        "event_context_applied": False,
        "event_window_trade_count": 0,
        "non_event_trade_count": 0,
        "blocked_trade_count": 0,
        "blocked_trades": [],
        "event_dependency_ratio": 0.0,
        "non_event_sharpe": None,
        "event_window_drawdown": 0.0,
        "event_window_metrics": empty_metric_payload(),
        "non_event_metrics": empty_metric_payload(),
    }


def enrich_event_attribution(
    attribution: dict,
    trades: Sequence[Trade],
    starting_equity: float,
    calendar_days: int,
) -> dict:
    event_trades = [trade for trade in trades if trade.event_state_at_entry != "normal"]
    non_event_trades = [trade for trade in trades if trade.event_state_at_entry == "normal"]
    blocked_count = int(attribution.get("blocked_trade_count", 0))
    total_candidates = len(trades) + blocked_count
    event_candidates = len(event_trades) + blocked_count
    event_metrics = metrics_payload(event_trades, starting_equity, calendar_days)
    non_event_metrics = metrics_payload(non_event_trades, starting_equity, calendar_days)
    return {
        **attribution,
        "event_dependency_ratio": event_candidates / total_candidates if total_candidates else 0.0,
        "non_event_sharpe": non_event_metrics["sharpe"],
        "event_window_drawdown": event_metrics["max_drawdown"],
        "event_window_metrics": event_metrics,
        "non_event_metrics": non_event_metrics,
    }


def metrics_payload(
    trades: Sequence[Trade],
    starting_equity: float,
    calendar_days: int,
) -> dict:
    if not trades:
        return empty_metric_payload()
    trade_pnls = [trade.net_pnl for trade in trades]
    equity = [starting_equity]
    for pnl in trade_pnls:
        equity.append(equity[-1] + pnl)
    return calculate_metrics(trade_pnls, equity, starting_equity, calendar_days).to_dict()


def empty_metric_payload() -> dict:
    return {
        "trade_count": 0,
        "net_pnl": 0,
        "gross_profit": 0,
        "gross_loss": 0,
        "profit_factor": None,
        "sharpe": None,
        "max_drawdown": 0.0,
        "annual_trades": 0,
        "avg_trade_net_pnl": None,
    }


def run_bar_strategy(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    bars: Sequence[dict],
    cost_model: CostModelConfig,
) -> list[Trade]:
    if spec.raw.get("signal_grammar"):
        return run_signal_grammar_strategy(spec, symbol_config, bars, cost_model)
    if spec.strategy_family == "opening_range_breakout":
        return run_opening_range_breakout(spec, symbol_config, bars, cost_model)
    if spec.strategy_family == "trend_pullback":
        return run_trend_pullback(spec, symbol_config, bars, cost_model)
    if spec.strategy_family == "volatility_expansion":
        return run_volatility_expansion(spec, symbol_config, bars, cost_model)
    if spec.strategy_family == "intraday_momentum":
        return run_intraday_momentum(spec, symbol_config, bars, cost_model)
    if spec.strategy_family == "regime_filtered_mean_reversion":
        return run_regime_filtered_mean_reversion(spec, symbol_config, bars, cost_model)
    if spec.strategy_family == "time_of_day_edge":
        return run_time_of_day_edge(spec, symbol_config, bars, cost_model)
    if spec.strategy_family == "gap_fade_or_continuation":
        return run_gap_fade_or_continuation(spec, symbol_config, bars, cost_model)
    if spec.strategy_family == "smc_lqem_ce":
        return run_smc_lqem_ce(spec, symbol_config, bars, cost_model)
    raise ValueError(f"Unsupported strategy_family: {spec.strategy_family}")


def run_tick_strategy(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    ticks: Sequence[dict],
    cost_model: CostModelConfig,
) -> list[Trade]:
    if spec.strategy_family == "opening_range_breakout":
        return run_opening_range_breakout_tick_replay(spec, symbol_config, ticks, cost_model)
    bars = minute_bars_from_ticks(ticks)
    return run_bar_strategy(spec, symbol_config, bars, cost_model)


def run_opening_range_breakout(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    bars: Sequence[dict],
    cost_model: CostModelConfig | None = None,
) -> list[Trade]:
    if not bars:
        return []
    cost_model = cost_model or default_cost_model(symbol_config, spec.cost_model)
    opening_range_minutes = int(
        spec.indicators.get("opening_range", {}).get(
            "minutes",
            spec.parameters.get("opening_range_minutes", {}).get("values", [15])[0],
        )
    )
    stop_points = float(_exit_value(spec.exit["stop_loss"]))
    take_profit_points = float(_exit_value(spec.exit["take_profit"]))
    max_holding_minutes = int(spec.exit["max_holding_minutes"])
    contracts = int(spec.risk.get("position_sizing", {}).get("contracts", 1))
    max_trades_per_day = int(spec.risk.get("max_trades_per_day", 999_999))
    trade_start, trade_end = parse_session_range(spec.session.trade)
    flatten_time = parse_clock(spec.session.flatten)

    trades: list[Trade] = []
    by_day: dict[object, list[dict]] = {}
    for bar in bars:
        by_day.setdefault(bar["timestamp"].date(), []).append(bar)

    for _, day_bars in sorted(by_day.items(), key=lambda item: item[0]):
        session_bars = [
            bar for bar in day_bars if trade_start <= bar["timestamp"].time() <= flatten_time
        ]
        if len(session_bars) <= opening_range_minutes:
            continue
        opening = session_bars[:opening_range_minutes]
        opening_high = max(bar["high"] for bar in opening)
        opening_low = min(bar["low"] for bar in opening)
        position = None
        trades_today = 0

        for index, bar in enumerate(session_bars[opening_range_minutes:], start=opening_range_minutes):
            if bar["timestamp"].time() > trade_end and position is None:
                continue
            if position is None and trades_today < max_trades_per_day:
                if spec.direction in {"long", "long_short"} and bar["close"] > opening_high:
                    position = _open_position(
                        "long",
                        bar,
                        contracts,
                        index,
                        "close_above_opening_range_high",
                    )
                    trades_today += 1
                    continue
                if spec.direction in {"short", "long_short"} and bar["close"] < opening_low:
                    position = _open_position(
                        "short",
                        bar,
                        contracts,
                        index,
                        "close_below_opening_range_low",
                    )
                    trades_today += 1
                    continue

            if position is not None:
                holding_minutes = index - position["entry_index"]
                exit_reason = None
                exit_price = None
                if position["side"] == "long":
                    stop_price = position["entry_price"] - stop_points
                    take_price = position["entry_price"] + take_profit_points
                    if bar["low"] <= stop_price:
                        exit_reason, exit_price = "stop_loss", stop_price
                    elif bar["high"] >= take_price:
                        exit_reason, exit_price = "take_profit", take_price
                    elif holding_minutes >= max_holding_minutes:
                        exit_reason, exit_price = "max_holding", bar["bid_close"]
                    elif bar["timestamp"].time() >= flatten_time:
                        exit_reason, exit_price = "session_flatten", bar["bid_close"]
                else:
                    stop_price = position["entry_price"] + stop_points
                    take_price = position["entry_price"] - take_profit_points
                    if bar["high"] >= stop_price:
                        exit_reason, exit_price = "stop_loss", stop_price
                    elif bar["low"] <= take_price:
                        exit_reason, exit_price = "take_profit", take_price
                    elif holding_minutes >= max_holding_minutes:
                        exit_reason, exit_price = "max_holding", bar["ask_close"]
                    elif bar["timestamp"].time() >= flatten_time:
                        exit_reason, exit_price = "session_flatten", bar["ask_close"]

                if exit_reason and exit_price is not None:
                    trades.append(
                        _close_position(position, bar, exit_price, exit_reason, cost_model)
                    )
                    position = None

        if position is not None:
            last_bar = session_bars[-1]
            exit_price = last_bar["bid_close"] if position["side"] == "long" else last_bar["ask_close"]
            trades.append(_close_position(position, last_bar, exit_price, "end_of_data", cost_model))

    return trades


def run_trend_pullback(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    bars: Sequence[dict],
    cost_model: CostModelConfig | None = None,
) -> list[Trade]:
    if not bars:
        return []
    fast_name, fast_config, slow_name, slow_config = _ema_pair(spec)
    fast = _ema_series([bar["close"] for bar in bars], int(fast_config.get("window", 9)))
    slow = _ema_series([bar["close"] for bar in bars], int(slow_config.get("window", 21)))

    def signal(index: int, _session_index: int, bar: dict) -> tuple[str | None, str | None]:
        if index <= 0 or fast[index] is None or slow[index] is None or fast[index - 1] is None:
            return None, None
        close = bar["close"]
        previous_close = bars[index - 1]["close"]
        if (
            spec.direction in {"long", "long_short"}
            and fast[index] > slow[index]
            and previous_close < fast[index - 1]
            and close >= fast[index]
        ):
            return "long", f"close_pullback_reclaim_{fast_name}_above_{slow_name}"
        if (
            spec.direction in {"short", "long_short"}
            and fast[index] < slow[index]
            and previous_close > fast[index - 1]
            and close <= fast[index]
        ):
            return "short", f"close_pullback_reject_{fast_name}_below_{slow_name}"
        return None, None

    return run_signal_bar_strategy(spec, bars, cost_model or default_cost_model(symbol_config, spec.cost_model), signal)


def run_regime_filtered_mean_reversion(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    bars: Sequence[dict],
    cost_model: CostModelConfig | None = None,
) -> list[Trade]:
    if not bars:
        return []
    indicator_name, indicator = _indicator_by_type(spec, "z_score")
    window = int(indicator.get("window", 20))
    threshold = float(
        indicator.get(
            "entry_z",
            spec.parameters.get("mean_reversion_entry_z", {}).get("values", [1.5])[0],
        )
    )
    z_scores = _z_score_series([bar["close"] for bar in bars], window)

    def signal(index: int, _session_index: int, _bar: dict) -> tuple[str | None, str | None]:
        z_score = z_scores[index]
        if z_score is None:
            return None, None
        if spec.direction in {"long", "long_short"} and z_score <= -threshold:
            return "long", f"{indicator_name}_below_negative_{threshold:g}"
        if spec.direction in {"short", "long_short"} and z_score >= threshold:
            return "short", f"{indicator_name}_above_positive_{threshold:g}"
        return None, None

    return run_signal_bar_strategy(spec, bars, cost_model or default_cost_model(symbol_config, spec.cost_model), signal)


def run_volatility_expansion(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    bars: Sequence[dict],
    cost_model: CostModelConfig | None = None,
) -> list[Trade]:
    if not bars:
        return []
    indicator = _optional_indicator_by_type(spec, "realized_volatility")
    window = int((indicator or {}).get("window", _parameter_first(spec, "volatility_window", 5)))
    multiplier = float(
        (indicator or {}).get(
            "expansion_multiple",
            _parameter_first(spec, "volatility_expansion_multiple", 1.5),
        )
    )
    ranges = [bar["high"] - bar["low"] for bar in bars]
    average_ranges = _rolling_mean_series(ranges, window)

    def signal(index: int, _session_index: int, bar: dict) -> tuple[str | None, str | None]:
        if index <= 0 or average_ranges[index - 1] is None:
            return None, None
        if ranges[index] < average_ranges[index - 1] * multiplier:
            return None, None
        previous = bars[index - 1]
        if spec.direction in {"long", "long_short"} and bar["close"] > previous["high"]:
            return "long", f"range_expansion_{multiplier:g}_close_above_prior_high"
        if spec.direction in {"short", "long_short"} and bar["close"] < previous["low"]:
            return "short", f"range_expansion_{multiplier:g}_close_below_prior_low"
        return None, None

    return run_signal_bar_strategy(spec, bars, cost_model or default_cost_model(symbol_config, spec.cost_model), signal)


def run_intraday_momentum(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    bars: Sequence[dict],
    cost_model: CostModelConfig | None = None,
) -> list[Trade]:
    if not bars:
        return []
    indicator = _optional_indicator_by_type(spec, "momentum")
    lookback = int((indicator or {}).get("lookback_minutes", _parameter_first(spec, "momentum_lookback_minutes", 3)))
    threshold = float((indicator or {}).get("threshold_points", _parameter_first(spec, "momentum_threshold_points", 1.0)))

    def signal(index: int, session_index: int, bar: dict) -> tuple[str | None, str | None]:
        if session_index < lookback:
            return None, None
        momentum = bar["close"] - bars[index - lookback]["close"]
        if spec.direction in {"long", "long_short"} and momentum >= threshold:
            return "long", f"momentum_{lookback}m_above_{threshold:g}"
        if spec.direction in {"short", "long_short"} and momentum <= -threshold:
            return "short", f"momentum_{lookback}m_below_negative_{threshold:g}"
        return None, None

    return run_signal_bar_strategy(spec, bars, cost_model or default_cost_model(symbol_config, spec.cost_model), signal)


def run_time_of_day_edge(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    bars: Sequence[dict],
    cost_model: CostModelConfig | None = None,
) -> list[Trade]:
    if not bars:
        return []
    indicator = _optional_indicator_by_type(spec, "time_of_day")
    entry_time = parse_clock(
        str((indicator or {}).get("entry_time", _parameter_first(spec, "entry_time", spec.session.trade.split("-", 1)[0])))
    )
    side = str((indicator or {}).get("entry_side", _parameter_first(spec, "entry_side", "long")))
    if side not in {"long", "short"}:
        raise ValueError("time_of_day_edge entry_side must be long or short")

    def signal(_index: int, _session_index: int, bar: dict) -> tuple[str | None, str | None]:
        if bar["timestamp"].time() < entry_time:
            return None, None
        if side == "long" and spec.direction in {"long", "long_short"}:
            return "long", f"time_of_day_{entry_time.isoformat()}_long"
        if side == "short" and spec.direction in {"short", "long_short"}:
            return "short", f"time_of_day_{entry_time.isoformat()}_short"
        return None, None

    return run_signal_bar_strategy(spec, bars, cost_model or default_cost_model(symbol_config, spec.cost_model), signal)


def run_gap_fade_or_continuation(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    bars: Sequence[dict],
    cost_model: CostModelConfig | None = None,
) -> list[Trade]:
    if not bars:
        return []
    indicator = _optional_indicator_by_type(spec, "gap")
    threshold = float((indicator or {}).get("threshold_points", _parameter_first(spec, "gap_threshold_points", 1.0)))
    mode = str((indicator or {}).get("mode", _parameter_first(spec, "gap_mode", "fade")))
    if mode not in {"fade", "continuation"}:
        raise ValueError("gap_mode must be fade or continuation")
    trade_start, _ = parse_session_range(spec.session.trade)

    first_session_index_by_day: dict[object, int] = {}
    previous_close_by_day: dict[object, float] = {}
    prior_close = None
    for index, bar in enumerate(bars):
        day = bar["timestamp"].date()
        if bar["timestamp"].time() >= trade_start and day not in first_session_index_by_day:
            first_session_index_by_day[day] = index
            if prior_close is not None:
                previous_close_by_day[day] = prior_close
        prior_close = bar["close"]

    def signal(index: int, _session_index: int, bar: dict) -> tuple[str | None, str | None]:
        day = bar["timestamp"].date()
        if first_session_index_by_day.get(day) != index or day not in previous_close_by_day:
            return None, None
        gap = bar["open"] - previous_close_by_day[day]
        if abs(gap) < threshold:
            return None, None
        if gap > 0:
            side = "short" if mode == "fade" else "long"
        else:
            side = "long" if mode == "fade" else "short"
        if side == "long" and spec.direction in {"long", "long_short"}:
            return "long", f"gap_{mode}_up" if gap > 0 else f"gap_{mode}_down"
        if side == "short" and spec.direction in {"short", "long_short"}:
            return "short", f"gap_{mode}_up" if gap > 0 else f"gap_{mode}_down"
        return None, None

    return run_signal_bar_strategy(spec, bars, cost_model or default_cost_model(symbol_config, spec.cost_model), signal)


def run_smc_lqem_ce(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    bars: Sequence[dict],
    cost_model: CostModelConfig | None = None,
) -> list[Trade]:
    if not bars:
        return []
    cost_model = cost_model or default_cost_model(symbol_config, spec.cost_model)
    parameters = _smc_parameters_from_spec(spec, cost_model)
    contracts = int(spec.risk.get("position_sizing", {}).get("contracts", 1))
    max_trades_per_day = int(spec.risk.get("max_trades_per_day", 999_999))
    trade_ranges = parse_session_ranges(spec.session.trade)
    flatten_time = parse_clock(spec.session.flatten)
    source_timezone = ZoneInfo(symbol_config.timezone)
    session_timezone = ZoneInfo(spec.session.timezone)
    warmup_start = _session_warmup_start(
        trade_ranges[0][0],
        parameters.htf_minutes * (parameters.htf_swing_left + parameters.htf_swing_right + 2),
    )

    trades: list[Trade] = []
    by_day: dict[object, list[tuple[int, dict]]] = {}
    for index, bar in enumerate(bars):
        session_datetime = _session_datetime(bar["timestamp"], source_timezone, session_timezone)
        by_day.setdefault(session_datetime.date(), []).append((index, {**bar, "_session_time": session_datetime.time()}))

    for _, indexed_day_bars in sorted(by_day.items(), key=lambda item: item[0]):
        machine = SmcLqemStateMachine(parameters)
        day_bars = [
            (global_index, bar)
            for global_index, bar in indexed_day_bars
            if _time_in_warmup_to_flatten(bar["_session_time"], warmup_start, flatten_time)
        ]
        position = None
        trades_today = 0

        for session_index, (_global_index, bar) in enumerate(day_bars):
            bar_time = bar["_session_time"]
            session_allowed = (
                _time_in_ranges(bar_time, trade_ranges)
                and trades_today < max_trades_per_day
                and position is None
            )
            decision = machine.on_bar(
                bar,
                session_allowed=session_allowed,
                force_flatten=bar_time >= flatten_time,
            )
            transition = decision.transition
            if (
                transition is not None
                and transition.to_state == "IN_POSITION"
                and decision.signal is not None
                and _smc_side_allowed(spec, decision.signal.direction)
            ):
                position = _open_limit_position(
                    decision.signal.direction,
                    bar,
                    contracts,
                    session_index,
                    decision.signal.entry_price,
                    decision.signal.reason,
                    decision.signal.audit,
                )
                trades_today += 1
                continue

            if decision.exit_reason is not None and position is not None:
                exit_reason = (
                    "session_flatten" if decision.exit_reason == "force_flatten" else decision.exit_reason
                )
                exit_price = _smc_exit_price(position, bar, decision.exit_reason)
                trades.append(_close_position(position, bar, exit_price, exit_reason, cost_model))
                position = None

        if position is not None and day_bars:
            last_bar = day_bars[-1][1]
            exit_price = _smc_market_exit_price(position, last_bar)
            trades.append(_close_position(position, last_bar, exit_price, "end_of_data", cost_model))

    return trades


def run_signal_grammar_strategy(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    bars: Sequence[dict],
    cost_model: CostModelConfig | None = None,
) -> list[Trade]:
    if not bars:
        return []
    cost_model = cost_model or default_cost_model(symbol_config, spec.cost_model)
    grammar = spec.raw.get("signal_grammar")
    if not isinstance(grammar, dict):
        raise ValueError("signal_grammar must be an object")
    entry = grammar.get("entry")
    if not isinstance(entry, dict):
        raise ValueError("signal_grammar.entry must be an object")
    filters = grammar.get("filters", {})
    exit_spec = grammar.get("exit", {})
    contracts = int(spec.risk.get("position_sizing", {}).get("contracts", 1))
    max_trades_per_day = int(spec.risk.get("max_trades_per_day", 999_999))
    trade_start, trade_end = parse_session_range(spec.session.trade)
    flatten_time = parse_clock(spec.session.flatten)

    trades: list[Trade] = []
    by_day: dict[object, list[tuple[int, dict]]] = {}
    for index, bar in enumerate(bars):
        by_day.setdefault(bar["timestamp"].date(), []).append((index, bar))

    for _, indexed_day_bars in sorted(by_day.items(), key=lambda item: item[0]):
        session_bars = [
            (global_index, bar)
            for global_index, bar in indexed_day_bars
            if trade_start <= bar["timestamp"].time() <= flatten_time
        ]
        position = None
        trades_today = 0
        for session_index, (_global_index, bar) in enumerate(session_bars):
            if bar["timestamp"].time() > trade_end and position is None:
                continue
            if position is None and trades_today < max_trades_per_day:
                filter_passed, filter_audit = _evaluate_grammar_node(filters, bar)
                if filter_passed:
                    for side in ("long", "short"):
                        if side == "long" and spec.direction not in {"long", "long_short"}:
                            continue
                        if side == "short" and spec.direction not in {"short", "long_short"}:
                            continue
                        side_rule = entry.get(side)
                        passed, predicate_audit = _evaluate_grammar_node(side_rule, bar)
                        if passed:
                            position = _open_position(
                                side,
                                bar,
                                contracts,
                                session_index,
                                f"signal_grammar_{side}",
                                feature_values=_selected_feature_values(bar, predicate_audit + filter_audit),
                                predicate_evaluation=predicate_audit + filter_audit,
                            )
                            position.update(_grammar_exit_points(exit_spec, bar, spec))
                            trades_today += 1
                            break
                if position is not None:
                    continue

            if position is not None:
                holding_minutes = session_index - position["entry_index"]
                stop_points = float(position["stop_points"])
                take_profit_points = float(position["take_profit_points"])
                max_holding_minutes = int(position["max_holding_minutes"])
                exit_reason = None
                exit_price = None
                if position["side"] == "long":
                    stop_price = position["entry_price"] - stop_points
                    take_price = position["entry_price"] + take_profit_points
                    if bar["low"] <= stop_price:
                        exit_reason, exit_price = "stop_loss", stop_price
                    elif bar["high"] >= take_price:
                        exit_reason, exit_price = "take_profit", take_price
                    elif holding_minutes >= max_holding_minutes:
                        exit_reason, exit_price = "max_holding", bar["bid_close"]
                    elif bar["timestamp"].time() >= flatten_time:
                        exit_reason, exit_price = "session_flatten", bar["bid_close"]
                else:
                    stop_price = position["entry_price"] + stop_points
                    take_price = position["entry_price"] - take_profit_points
                    if bar["high"] >= stop_price:
                        exit_reason, exit_price = "stop_loss", stop_price
                    elif bar["low"] <= take_price:
                        exit_reason, exit_price = "take_profit", take_price
                    elif holding_minutes >= max_holding_minutes:
                        exit_reason, exit_price = "max_holding", bar["ask_close"]
                    elif bar["timestamp"].time() >= flatten_time:
                        exit_reason, exit_price = "session_flatten", bar["ask_close"]
                if exit_reason and exit_price is not None:
                    trades.append(_close_position(position, bar, exit_price, exit_reason, cost_model))
                    position = None

        if position is not None and session_bars:
            last_bar = session_bars[-1][1]
            exit_price = last_bar["bid_close"] if position["side"] == "long" else last_bar["ask_close"]
            trades.append(_close_position(position, last_bar, exit_price, "end_of_data", cost_model))

    return trades


def build_signal_grammar_health_report(spec: StrategySpec, bars: Sequence[dict]) -> dict:
    grammar = spec.raw.get("signal_grammar")
    if not isinstance(grammar, dict):
        return {"status": "not_signal_grammar", "reasons": ["missing_signal_grammar"]}
    entry = grammar.get("entry")
    if not isinstance(entry, dict):
        return {"status": "invalid", "reasons": ["missing_signal_grammar_entry"]}
    filters = grammar.get("filters", {})
    trade_start, trade_end = parse_session_range(spec.session.trade)
    session_bars = [
        bar
        for bar in bars
        if trade_start <= bar["timestamp"].time() <= trade_end
    ]
    filter_pass_count = 0
    long_entry_count = 0
    short_entry_count = 0
    post_filter_long_count = 0
    post_filter_short_count = 0
    filter_predicates: Counter[str] = Counter()
    filter_predicate_failures: Counter[str] = Counter()
    entry_predicates: Counter[str] = Counter()
    entry_predicate_failures: Counter[str] = Counter()

    for bar in session_bars:
        filter_passed, filter_audit = _evaluate_grammar_node(filters, bar)
        if filter_passed:
            filter_pass_count += 1
        _count_predicates(filter_audit, filter_predicates, filter_predicate_failures)
        for side in ("long", "short"):
            if side == "long" and spec.direction not in {"long", "long_short"}:
                continue
            if side == "short" and spec.direction not in {"short", "long_short"}:
                continue
            passed, predicate_audit = _evaluate_grammar_node(entry.get(side), bar)
            _count_predicates(predicate_audit, entry_predicates, entry_predicate_failures)
            if side == "long" and passed:
                long_entry_count += 1
                if filter_passed:
                    post_filter_long_count += 1
            if side == "short" and passed:
                short_entry_count += 1
                if filter_passed:
                    post_filter_short_count += 1

    post_filter_entry_count = post_filter_long_count + post_filter_short_count
    raw_entry_count = long_entry_count + short_entry_count
    reasons = []
    if not bars:
        reasons.append("no_input_bars")
    if not session_bars:
        reasons.append("no_session_bars")
    if session_bars and filter_pass_count == 0:
        reasons.append("filters_block_all_session_bars")
    if session_bars and raw_entry_count == 0:
        reasons.append("entry_rules_have_no_raw_hits")
    if session_bars and raw_entry_count > 0 and post_filter_entry_count == 0:
        reasons.append("filters_block_all_raw_entries")
    status = "ok" if not reasons else "blocked"
    return {
        "status": status,
        "reasons": reasons,
        "total_bars": len(bars),
        "session_bars": len(session_bars),
        "filter_pass_count": filter_pass_count,
        "filter_pass_ratio": _ratio(filter_pass_count, len(session_bars)),
        "raw_entry_count": raw_entry_count,
        "long_entry_count": long_entry_count,
        "short_entry_count": short_entry_count,
        "post_filter_entry_count": post_filter_entry_count,
        "post_filter_long_count": post_filter_long_count,
        "post_filter_short_count": post_filter_short_count,
        "post_filter_entry_ratio": _ratio(post_filter_entry_count, len(session_bars)),
        "filter_predicate_counts": dict(sorted(filter_predicates.items())),
        "filter_predicate_failure_counts": dict(sorted(filter_predicate_failures.items())),
        "entry_predicate_counts": dict(sorted(entry_predicates.items())),
        "entry_predicate_failure_counts": dict(sorted(entry_predicate_failures.items())),
    }


def run_signal_bar_strategy(
    spec: StrategySpec,
    bars: Sequence[dict],
    cost_model: CostModelConfig,
    signal_fn,
) -> list[Trade]:
    stop_points = float(_exit_value(spec.exit["stop_loss"]))
    take_profit_points = float(_exit_value(spec.exit["take_profit"]))
    max_holding_minutes = int(spec.exit["max_holding_minutes"])
    contracts = int(spec.risk.get("position_sizing", {}).get("contracts", 1))
    max_trades_per_day = int(spec.risk.get("max_trades_per_day", 999_999))
    trade_start, trade_end = parse_session_range(spec.session.trade)
    flatten_time = parse_clock(spec.session.flatten)

    trades: list[Trade] = []
    by_day: dict[object, list[tuple[int, dict]]] = {}
    for index, bar in enumerate(bars):
        by_day.setdefault(bar["timestamp"].date(), []).append((index, bar))

    for _, indexed_day_bars in sorted(by_day.items(), key=lambda item: item[0]):
        session_bars = [
            (global_index, bar)
            for global_index, bar in indexed_day_bars
            if trade_start <= bar["timestamp"].time() <= flatten_time
        ]
        position = None
        trades_today = 0
        for session_index, (global_index, bar) in enumerate(session_bars):
            if bar["timestamp"].time() > trade_end and position is None:
                continue
            if position is None and trades_today < max_trades_per_day:
                side, reason = signal_fn(global_index, session_index, bar)
                if side is not None and reason is not None:
                    position = _open_position(side, bar, contracts, session_index, reason)
                    trades_today += 1
                    continue

            if position is not None:
                holding_minutes = session_index - position["entry_index"]
                exit_reason = None
                exit_price = None
                if position["side"] == "long":
                    stop_price = position["entry_price"] - stop_points
                    take_price = position["entry_price"] + take_profit_points
                    if bar["low"] <= stop_price:
                        exit_reason, exit_price = "stop_loss", stop_price
                    elif bar["high"] >= take_price:
                        exit_reason, exit_price = "take_profit", take_price
                    elif holding_minutes >= max_holding_minutes:
                        exit_reason, exit_price = "max_holding", bar["bid_close"]
                    elif bar["timestamp"].time() >= flatten_time:
                        exit_reason, exit_price = "session_flatten", bar["bid_close"]
                else:
                    stop_price = position["entry_price"] + stop_points
                    take_price = position["entry_price"] - take_profit_points
                    if bar["high"] >= stop_price:
                        exit_reason, exit_price = "stop_loss", stop_price
                    elif bar["low"] <= take_price:
                        exit_reason, exit_price = "take_profit", take_price
                    elif holding_minutes >= max_holding_minutes:
                        exit_reason, exit_price = "max_holding", bar["ask_close"]
                    elif bar["timestamp"].time() >= flatten_time:
                        exit_reason, exit_price = "session_flatten", bar["ask_close"]
                if exit_reason and exit_price is not None:
                    trades.append(_close_position(position, bar, exit_price, exit_reason, cost_model))
                    position = None

        if position is not None and session_bars:
            last_bar = session_bars[-1][1]
            exit_price = last_bar["bid_close"] if position["side"] == "long" else last_bar["ask_close"]
            trades.append(_close_position(position, last_bar, exit_price, "end_of_data", cost_model))

    return trades


def run_opening_range_breakout_tick_replay(
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    ticks: Sequence[dict],
    cost_model: CostModelConfig | None = None,
) -> list[Trade]:
    if not ticks:
        return []
    cost_model = cost_model or default_cost_model(symbol_config, spec.cost_model)
    opening_range_minutes = int(
        spec.indicators.get("opening_range", {}).get(
            "minutes",
            spec.parameters.get("opening_range_minutes", {}).get("values", [15])[0],
        )
    )
    stop_points = float(_exit_value(spec.exit["stop_loss"]))
    take_profit_points = float(_exit_value(spec.exit["take_profit"]))
    max_holding_minutes = int(spec.exit["max_holding_minutes"])
    contracts = int(spec.risk.get("position_sizing", {}).get("contracts", 1))
    max_trades_per_day = int(spec.risk.get("max_trades_per_day", 999_999))
    trade_start, trade_end = parse_session_range(spec.session.trade)
    flatten_time = parse_clock(spec.session.flatten)

    trades: list[Trade] = []
    by_day: dict[object, list[dict]] = {}
    for tick in ticks:
        by_day.setdefault(tick["timestamp"].date(), []).append(tick)

    for _, day_ticks in sorted(by_day.items(), key=lambda item: item[0]):
        session_ticks = [
            tick for tick in day_ticks if trade_start <= tick["timestamp"].time() <= flatten_time
        ]
        if not session_ticks:
            continue

        opening_end = datetime.combine(
            session_ticks[0]["timestamp"].date(),
            trade_start,
        ) + timedelta(minutes=opening_range_minutes)
        opening = [tick for tick in session_ticks if tick["timestamp"] < opening_end]
        if not opening:
            continue
        opening_high = max(tick["mid"] for tick in opening)
        opening_low = min(tick["mid"] for tick in opening)

        position = None
        pending_side = None
        pending_entry_reason = None
        trades_today = 0
        for tick in session_ticks:
            tick_time = tick["timestamp"].time()
            if tick["timestamp"] < opening_end:
                continue

            if pending_side is not None:
                position = _open_tick_position(
                    pending_side,
                    tick,
                    contracts,
                    cost_model,
                    pending_entry_reason or "opening_range_breakout",
                )
                pending_side = None
                pending_entry_reason = None

            if position is not None:
                exit_reason, exit_price = _tick_exit_signal(
                    position,
                    tick,
                    stop_points,
                    take_profit_points,
                    max_holding_minutes,
                    flatten_time,
                    cost_model,
                )
                if exit_reason and exit_price is not None:
                    trades.append(
                        _close_position(position, tick, exit_price, exit_reason, cost_model)
                    )
                    position = None
                continue

            if tick_time > trade_end or trades_today >= max_trades_per_day:
                continue
            if spec.direction in {"long", "long_short"} and tick["mid"] > opening_high:
                pending_side = "long"
                pending_entry_reason = "mid_above_opening_range_high"
                trades_today += 1
                continue
            if spec.direction in {"short", "long_short"} and tick["mid"] < opening_low:
                pending_side = "short"
                pending_entry_reason = "mid_below_opening_range_low"
                trades_today += 1

        if position is not None:
            last_tick = session_ticks[-1]
            side = "sell" if position["side"] == "long" else "buy"
            exit_price = _align_price(
                last_tick["bid"] if position["side"] == "long" else last_tick["ask"],
                cost_model.tick_size,
                side,
            )
            trades.append(_close_position(position, last_tick, exit_price, "end_of_data", cost_model))

    return trades


def minute_bars_from_ticks(ticks: Sequence[dict]) -> list[dict]:
    buckets: dict[datetime, list[dict]] = {}
    for tick in ticks:
        timestamp = tick["timestamp"].replace(second=0, microsecond=0)
        buckets.setdefault(timestamp, []).append(tick)

    bars: list[dict] = []
    for timestamp in sorted(buckets):
        bucket = sorted(buckets[timestamp], key=lambda item: item["timestamp"])
        mids = [tick["mid"] for tick in bucket]
        bars.append(
            {
                "symbol": bucket[-1]["symbol"],
                "timestamp": timestamp,
                "open": mids[0],
                "high": max(mids),
                "low": min(mids),
                "close": mids[-1],
                "bid_close": bucket[-1]["bid"],
                "ask_close": bucket[-1]["ask"],
                "tick_count": len(bucket),
                "avg_spread": sum(tick["spread"] for tick in bucket) / len(bucket),
            }
        )
    return bars


def _open_tick_position(
    side: str,
    tick: dict,
    contracts: int,
    cost_model: CostModelConfig,
    entry_reason: str,
) -> dict:
    entry_side = "buy" if side == "long" else "sell"
    entry_price = _align_price(
        tick["ask"] if side == "long" else tick["bid"],
        cost_model.tick_size,
        entry_side,
    )
    return {
        "side": side,
        "entry_time": tick["timestamp"],
        "entry_price": entry_price,
        "entry_index": None,
        "contracts": contracts,
        "entry_reason": entry_reason,
    }


def _tick_exit_signal(
    position: dict,
    tick: dict,
    stop_points: float,
    take_profit_points: float,
    max_holding_minutes: int,
    flatten_time: time,
    cost_model: CostModelConfig,
) -> tuple[str | None, float | None]:
    holding_minutes = (tick["timestamp"] - position["entry_time"]).total_seconds() / 60
    if position["side"] == "long":
        stop_price = position["entry_price"] - stop_points
        take_price = position["entry_price"] + take_profit_points
        if tick["bid"] <= stop_price:
            return "stop_loss", _align_price(tick["bid"], cost_model.tick_size, "sell")
        if tick["bid"] >= take_price:
            return "take_profit", _align_price(tick["bid"], cost_model.tick_size, "sell")
        if holding_minutes >= max_holding_minutes:
            return "max_holding", _align_price(tick["bid"], cost_model.tick_size, "sell")
        if tick["timestamp"].time() >= flatten_time:
            return "session_flatten", _align_price(tick["bid"], cost_model.tick_size, "sell")
        return None, None

    stop_price = position["entry_price"] + stop_points
    take_price = position["entry_price"] - take_profit_points
    if tick["ask"] >= stop_price:
        return "stop_loss", _align_price(tick["ask"], cost_model.tick_size, "buy")
    if tick["ask"] <= take_price:
        return "take_profit", _align_price(tick["ask"], cost_model.tick_size, "buy")
    if holding_minutes >= max_holding_minutes:
        return "max_holding", _align_price(tick["ask"], cost_model.tick_size, "buy")
    if tick["timestamp"].time() >= flatten_time:
        return "session_flatten", _align_price(tick["ask"], cost_model.tick_size, "buy")
    return None, None


def _exit_value(exit_config: dict) -> float:
    if exit_config.get("type") == "points":
        return float(exit_config["value"])
    raise ValueError("Phase 2 backtester supports point-based exits only")


def _grammar_exit_points(exit_spec: dict, bar: dict, spec: StrategySpec) -> dict:
    if not isinstance(exit_spec, dict):
        exit_spec = {}
    stop_config = exit_spec.get("stop") or spec.exit.get("stop_loss", {})
    take_config = exit_spec.get("take_profit") or spec.exit.get("take_profit", {})
    time_stop = exit_spec.get("time_stop") or {"minutes": spec.exit.get("max_holding_minutes", 10)}
    return {
        "stop_points": _exit_points_from_config(stop_config, bar, spec.exit["stop_loss"]),
        "take_profit_points": _exit_points_from_config(take_config, bar, spec.exit["take_profit"]),
        "max_holding_minutes": int(time_stop.get("minutes", spec.exit.get("max_holding_minutes", 10))),
    }


def _exit_points_from_config(config: dict, bar: dict, fallback: dict) -> float:
    exit_type = config.get("type", fallback.get("type", "points"))
    if exit_type == "points":
        return float(config.get("value", fallback.get("value", 1)))
    if exit_type == "atr_multiple":
        feature = feature_value(bar, str(config.get("feature", "atr_14")))
        if feature is None:
            return float(fallback.get("value", 1))
        return max(float(feature) * float(config.get("multiple", 1)), 0.25)
    raise ValueError(f"Unsupported signal_grammar exit type: {exit_type}")


def _evaluate_grammar_node(node, bar: dict) -> tuple[bool, list[dict]]:
    if node in (None, {}):
        return True, []
    if not isinstance(node, dict):
        raise ValueError("signal_grammar nodes must be objects")
    if "all" in node:
        audits: list[dict] = []
        passed = True
        for child in node["all"]:
            child_passed, child_audit = _evaluate_grammar_node(child, bar)
            audits.extend(child_audit)
            passed = passed and child_passed
        return passed, audits
    if "any" in node:
        audits = []
        passed = False
        for child in node["any"]:
            child_passed, child_audit = _evaluate_grammar_node(child, bar)
            audits.extend(child_audit)
            passed = passed or child_passed
        return passed, audits
    if "not" in node:
        child_passed, child_audit = _evaluate_grammar_node(node["not"], bar)
        return not child_passed, child_audit
    audit = _evaluate_predicate(node, bar)
    return bool(audit["passed"]), [audit]


def _evaluate_predicate(predicate: dict, bar: dict) -> dict:
    left_name = str(predicate.get("feature", predicate.get("left", "")))
    operator = str(predicate.get("op", "=="))
    right_value = predicate.get("value", predicate.get("right"))
    left_value = feature_value(bar, left_name)
    resolved_right = feature_value(bar, str(right_value)) if isinstance(right_value, str) and _looks_like_feature(right_value) else right_value
    passed = _compare_values(left_value, operator, resolved_right)
    return {
        "feature": left_name,
        "op": operator,
        "left": left_value,
        "right": resolved_right,
        "passed": passed,
    }


def _looks_like_feature(value: str) -> bool:
    if value.lower() in {"true", "false"}:
        return False
    try:
        float(value)
        return False
    except ValueError:
        return True


def _compare_values(left, operator: str, right) -> bool:
    if left is None or right is None:
        return False
    left = _coerce_scalar(left)
    right = _coerce_scalar(right)
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    raise ValueError(f"Unsupported signal_grammar operator: {operator}")


def _coerce_scalar(value):
    if isinstance(value, str):
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        try:
            return float(value)
        except ValueError:
            return value
    return value


def _selected_feature_values(bar: dict, audits: Sequence[dict]) -> dict:
    names = sorted({audit["feature"] for audit in audits if audit.get("feature")})
    return {name: feature_value(bar, name) for name in names}


def _count_predicates(audits: Sequence[dict], counts: Counter[str], failures: Counter[str]) -> None:
    for audit in audits:
        key = _predicate_key(audit)
        counts[key] += 1
        if not audit.get("passed"):
            failures[key] += 1


def _predicate_key(audit: dict) -> str:
    return f"{audit.get('feature')} {audit.get('op')} {audit.get('right')}"


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _indicator_by_type(spec: StrategySpec, indicator_type: str) -> tuple[str, dict]:
    for name, config in spec.indicators.items():
        if config.get("type") == indicator_type:
            return name, config
    raise ValueError(f"{spec.strategy_family} requires indicator type {indicator_type}")


def _optional_indicator_by_type(spec: StrategySpec, indicator_type: str) -> dict | None:
    for config in spec.indicators.values():
        if config.get("type") == indicator_type:
            return config
    return None


def _parameter_first(spec: StrategySpec, name: str, default):
    config = spec.parameters.get(name)
    if isinstance(config, dict) and config.get("values"):
        return config["values"][0]
    return default


def _smc_parameters_from_spec(spec: StrategySpec, cost_model: CostModelConfig) -> SmcLqemParameters:
    return SmcLqemParameters(
        tick_size=float(_parameter_first(spec, "tick_size", cost_model.tick_size)),
        htf_minutes=int(_parameter_first(spec, "htf_minutes", 15)),
        htf_swing_left=int(_parameter_first(spec, "htf_swing_left", 3)),
        htf_swing_right=int(_parameter_first(spec, "htf_swing_right", 3)),
        ltf_swing_left=int(_parameter_first(spec, "ltf_swing_left", 2)),
        ltf_swing_right=int(_parameter_first(spec, "ltf_swing_right", 2)),
        break_buffer_ticks=int(_parameter_first(spec, "break_buffer_ticks", 1)),
        min_htf_range_ticks=int(_parameter_first(spec, "min_htf_range_ticks", 80)),
        min_ob_ticks=int(_parameter_first(spec, "min_ob_ticks", 8)),
        max_ob_ticks=int(_parameter_first(spec, "max_ob_ticks", 120)),
        min_micro_ob_ticks=int(_parameter_first(spec, "min_micro_ob_ticks", 4)),
        max_micro_ob_ticks=int(_parameter_first(spec, "max_micro_ob_ticks", 60)),
        pbl_clearance_ticks=int(_parameter_first(spec, "pbl_clearance_ticks", 4)),
        sweep_buffer_ticks=int(_parameter_first(spec, "sweep_buffer_ticks", 1)),
        max_reclaim_bars=int(_parameter_first(spec, "max_reclaim_bars", 3)),
        stop_buffer_ticks=int(_parameter_first(spec, "stop_buffer_ticks", 4)),
        min_stop_ticks=int(_parameter_first(spec, "min_stop_ticks", 8)),
        max_stop_ticks=int(_parameter_first(spec, "max_stop_ticks", 80)),
        min_reward_r=float(_parameter_first(spec, "min_reward_r", 2.0)),
        default_take_profit_r=float(_parameter_first(spec, "default_take_profit_r", 3.0)),
        pending_ttl_bars=int(_parameter_first(spec, "pending_ttl_bars", 10)),
        max_spread_ticks=float(_parameter_first(spec, "max_spread_ticks", 4.0)),
        cooldown_bars_after_cancel=int(_parameter_first(spec, "cooldown_bars_after_cancel", 5)),
        cooldown_bars_after_exit=int(_parameter_first(spec, "cooldown_bars_after_exit", 15)),
        max_context_bars=int(_parameter_first(spec, "max_context_bars", 360)),
    )


def _smc_side_allowed(spec: StrategySpec, side: str) -> bool:
    return spec.direction == "long_short" or spec.direction == side


def _open_limit_position(
    side: str,
    bar: dict,
    contracts: int,
    entry_index: int,
    entry_price: float,
    entry_reason: str,
    audit: dict,
) -> dict:
    return {
        "side": side,
        "entry_time": bar["timestamp"],
        "entry_price": entry_price,
        "entry_index": entry_index,
        "contracts": contracts,
        "entry_reason": entry_reason,
        "feature_values": {
            "smc_direction": audit.get("direction"),
            "smc_entry_price": audit.get("entry_price"),
            "smc_stop_price": audit.get("stop_price"),
            "smc_take_profit_price": audit.get("take_profit_price"),
            "smc_stop_ticks": audit.get("stop_ticks"),
            "smc_signal_audit": audit,
        },
        "predicate_evaluation": [],
    }


def _smc_exit_price(position: dict, bar: dict, exit_reason: str) -> float:
    audit = position.get("feature_values", {}).get("smc_signal_audit", {})
    if exit_reason == "stop_loss":
        return float(audit["stop_price"])
    if exit_reason == "take_profit":
        return float(audit["take_profit_price"])
    return _smc_market_exit_price(position, bar)


def _smc_market_exit_price(position: dict, bar: dict) -> float:
    if position["side"] == "long":
        return float(bar.get("bid_close", bar["close"]))
    return float(bar.get("ask_close", bar["close"]))


def _ema_pair(spec: StrategySpec) -> tuple[str, dict, str, dict]:
    emas = [
        (name, config)
        for name, config in spec.indicators.items()
        if config.get("type") == "ema"
    ]
    if len(emas) < 2:
        raise ValueError("trend_pullback requires at least two EMA indicators")
    fast, slow = sorted(emas, key=lambda item: int(item[1].get("window", 0)))[:2]
    return fast[0], fast[1], slow[0], slow[1]


def _ema_series(values: Sequence[float], window: int) -> list[float | None]:
    if window <= 0:
        raise ValueError("EMA window must be positive")
    alpha = 2 / (window + 1)
    series: list[float | None] = []
    ema = None
    for index, value in enumerate(values):
        ema = value if ema is None else alpha * value + (1 - alpha) * ema
        series.append(ema if index + 1 >= window else None)
    return series


def _z_score_series(values: Sequence[float], window: int) -> list[float | None]:
    if window <= 1:
        raise ValueError("z_score window must be greater than 1")
    series: list[float | None] = []
    for index, value in enumerate(values):
        if index + 1 < window:
            series.append(None)
            continue
        sample = values[index + 1 - window : index + 1]
        mean = sum(sample) / window
        variance = sum((item - mean) ** 2 for item in sample) / window
        stddev = variance**0.5
        series.append((value - mean) / stddev if stddev else 0.0)
    return series


def _rolling_mean_series(values: Sequence[float], window: int) -> list[float | None]:
    if window <= 0:
        raise ValueError("rolling mean window must be positive")
    series: list[float | None] = []
    for index, _value in enumerate(values):
        if index + 1 < window:
            series.append(None)
            continue
        sample = values[index + 1 - window : index + 1]
        series.append(sum(sample) / window)
    return series


def _open_position(
    side: str,
    bar: dict,
    contracts: int,
    entry_index: int,
    entry_reason: str,
    feature_values: dict | None = None,
    predicate_evaluation: list[dict] | None = None,
) -> dict:
    entry_price = bar["ask_close"] if side == "long" else bar["bid_close"]
    return {
        "side": side,
        "entry_time": bar["timestamp"],
        "entry_price": entry_price,
        "entry_index": entry_index,
        "contracts": contracts,
        "entry_reason": entry_reason,
        "feature_values": feature_values or {},
        "predicate_evaluation": predicate_evaluation or [],
    }


def _close_position(
    position: dict,
    bar: dict,
    exit_price: float,
    exit_reason: str,
    cost_model: CostModelConfig,
) -> Trade:
    direction = 1 if position["side"] == "long" else -1
    gross_pnl = (
        (exit_price - position["entry_price"])
        * direction
        * cost_model.point_value
        * position["contracts"]
    )
    fees = cost_model.round_trip_fees_usd * position["contracts"]
    slippage_cost = (
        2
        * cost_model.slippage_ticks_per_side
        * cost_model.tick_size
        * cost_model.point_value
        * position["contracts"]
    )
    net_pnl = gross_pnl - fees - slippage_cost
    return Trade(
        symbol=bar["symbol"],
        side=position["side"],
        entry_time=position["entry_time"],
        exit_time=bar["timestamp"],
        entry_price=position["entry_price"],
        exit_price=exit_price,
        contracts=position["contracts"],
        gross_pnl=gross_pnl,
        fees=fees,
        slippage_cost=slippage_cost,
        net_pnl=net_pnl,
        entry_reason=position["entry_reason"],
        exit_reason=exit_reason,
        feature_values_at_entry=position.get("feature_values", {}),
        predicate_evaluation_at_entry=position.get("predicate_evaluation", []),
    )


def _align_price(price: float, tick_size: float, side: str) -> float:
    ticks = price / tick_size
    if side == "buy":
        return ceil(ticks) * tick_size
    if side == "sell":
        return floor(ticks) * tick_size
    raise ValueError(f"Unsupported side for price alignment: {side}")


def default_cost_model(symbol_config: SymbolConfig, name: str) -> CostModelConfig:
    return CostModelConfig(
        name=name,
        tick_size=symbol_config.tick_size,
        point_value=symbol_config.point_value,
        tick_value=symbol_config.tick_size * symbol_config.point_value,
        slippage_ticks_per_side=1,
        round_trip_fees_usd=5,
    )


def backtest_data_version_hash(
    data_files: Sequence[Path],
    spec: StrategySpec,
    symbol_config: SymbolConfig,
    cost_model: CostModelConfig,
    execution_mode: str,
) -> str:
    return compute_data_version_hash(
        data_files,
        {
            "requested_files": [str(path) for path in data_files],
            "execution_mode": execution_mode,
            "strategy_name": spec.name,
            "strategy_family": spec.strategy_family,
            "symbol": spec.symbol,
            "timeframe": spec.timeframe,
            "cost_model": cost_model.to_dict(),
            "symbol_config": {
                "alias": symbol_config.alias,
                "provider": symbol_config.provider,
                "instrument": symbol_config.instrument,
                "price_scale": symbol_config.price_scale,
                "tick_size": symbol_config.tick_size,
                "point_value": symbol_config.point_value,
            },
        },
    )


def parse_session_range(value: str) -> tuple[time, time]:
    ranges = parse_session_ranges(value)
    return ranges[0][0], ranges[-1][1]


def parse_session_ranges(value: str) -> list[tuple[time, time]]:
    ranges = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        start, end = part.split("-", 1)
        ranges.append((parse_clock(start), parse_clock(end)))
    if not ranges:
        raise ValueError("session range must contain at least one HH:MM-HH:MM window")
    return ranges


def _time_in_ranges(value: time, ranges: Sequence[tuple[time, time]]) -> bool:
    return any(start <= value <= end for start, end in ranges)


def _session_warmup_start(session_start: time, warmup_minutes: int) -> time:
    anchor = datetime.combine(date.min, session_start)
    return (anchor - timedelta(minutes=max(warmup_minutes, 0))).time()


def _time_in_warmup_to_flatten(value: time, warmup_start: time, flatten_time: time) -> bool:
    if warmup_start <= flatten_time:
        return warmup_start <= value <= flatten_time
    return value >= warmup_start or value <= flatten_time


def _session_datetime(timestamp: datetime, source_timezone: ZoneInfo, session_timezone: ZoneInfo) -> datetime:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=source_timezone)
    else:
        timestamp = timestamp.astimezone(source_timezone)
    return timestamp.astimezone(session_timezone).replace(tzinfo=None)


def parse_clock(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def result_to_json(result: BacktestResult) -> str:
    return json.dumps(result.to_dict(), indent=2, sort_keys=True)
