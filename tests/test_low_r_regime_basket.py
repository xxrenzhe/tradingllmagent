from __future__ import annotations

from datetime import datetime, timedelta

from tlm.low_r_regime_basket import (
    LowRRegimeBasketConfig,
    PRESETS,
    RegimeEdge,
    SIMPLE_ROBUST_LOW_R_EDGES,
    TOP_NET_2019_LOW_R_EDGES,
    _OpenTrade,
    _edges_with_take_profit_profile,
    _maybe_close_trade,
    _simulate_trade_exit,
    _stop_points,
)


def test_stop_points_are_bounded_and_tick_rounded() -> None:
    config = LowRRegimeBasketConfig(
        stop_range_multiple=1.0,
        min_stop_points=4.0,
        max_stop_points=12.0,
        tick_size=0.25,
    )

    assert _stop_points(1.2, config) == 4.0
    assert _stop_points(7.13, config) == 7.25
    assert _stop_points(20.0, config) == 12.0


def test_default_config_uses_simple_robust_preset() -> None:
    assert LowRRegimeBasketConfig().preset == "simple_robust_low_r"


def test_simple_robust_preset_matches_top_pf_plus_pullback_subset() -> None:
    assert PRESETS["simple_robust_low_r"] == SIMPLE_ROBUST_LOW_R_EDGES
    assert SIMPLE_ROBUST_LOW_R_EDGES == tuple(TOP_NET_2019_LOW_R_EDGES[index] for index in (0, 2, 3, 5, 8, 10))


def test_take_profit_profile_is_capped_to_explicit_low_r_choices() -> None:
    edges = (
        RegimeEdge("low_volume_drift", "long", 120, "utc_1700_2059", 3, 1, -1, 0, 0.75),
        RegimeEdge("opening_range_breakout", "long", 120, "utc_1700_2059", 3, 1, 1, 0, 1.0),
    )

    updated = _edges_with_take_profit_profile(edges, {"low_volume_drift": 1.25})

    assert updated[0].take_profit_r == 1.25
    assert updated[1].take_profit_r == 1.0


def test_same_bar_target_and_stop_uses_conservative_stop_first() -> None:
    config = LowRRegimeBasketConfig(round_trip_fees_usd=0.0, slippage_ticks_per_side=0.0)
    entry_time = datetime(2024, 1, 2, 14, 31)
    edge = RegimeEdge("low_volume_drift", "long", 120, "utc_1700_2059", 3, 1, -1, 0, 0.75)
    trade = _OpenTrade(
        edge_index=0,
        edge=edge,
        signal_time=entry_time - timedelta(minutes=1),
        entry_index=0,
        entry_time=entry_time,
        entry_price=100.0,
        stop_loss=96.0,
        take_profit=103.0,
        stop_points=4.0,
    )
    bar = {
        "timestamp": entry_time + timedelta(minutes=1),
        "open": 100.0,
        "high": 104.0,
        "low": 95.0,
        "close": 102.0,
    }

    closed = _maybe_close_trade(trade, 1, bar, config)

    assert closed is not None
    assert closed.exit_reason == "stop_loss_conservative"
    assert closed.exit_price == 96.0
    assert closed.net_pnl == -80.0


def test_date_change_flatten_exits_before_next_date_bar() -> None:
    config = LowRRegimeBasketConfig(
        round_trip_fees_usd=0.0,
        slippage_ticks_per_side=0.0,
        max_hold_minutes=10,
        flatten_on_date_change=True,
    )
    entry_time = datetime(2024, 1, 2, 23, 58)
    edge = RegimeEdge("low_volume_drift", "long", 120, "utc_2100_2359", 2, 1, -1, 0, 1.0)
    trade = _OpenTrade(
        edge_index=0,
        edge=edge,
        signal_time=entry_time - timedelta(minutes=1),
        entry_index=0,
        entry_time=entry_time,
        entry_price=100.0,
        stop_loss=90.0,
        take_profit=110.0,
        stop_points=10.0,
    )
    bars = [
        {"timestamp": entry_time, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5},
        {"timestamp": entry_time + timedelta(minutes=1), "open": 100.5, "high": 102.0, "low": 99.5, "close": 101.0},
        {"timestamp": datetime(2024, 1, 3, 0, 0), "open": 101.0, "high": 103.0, "low": 100.0, "close": 102.0},
    ]

    closed = _simulate_trade_exit(trade, bars, config)

    assert closed.exit_reason == "date_change_flatten"
    assert closed.exit_time == entry_time + timedelta(minutes=1)
    assert closed.exit_price == 101.0
