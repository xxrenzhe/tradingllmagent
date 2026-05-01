from __future__ import annotations

from datetime import datetime, timedelta

from tlm.role_retest import RoleRetestConfig, run_role_retest_backtest


def _bar(index: int, open_: float, high: float, low: float, close: float) -> dict:
    return {
        "symbol": "NQ_CME",
        "timestamp": datetime(2024, 1, 2, 14, 35) + timedelta(minutes=5 * index),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
    }


def _config() -> RoleRetestConfig:
    return RoleRetestConfig(
        swing_left_bars=2,
        swing_right_bars=2,
        lookback_bars=20,
        atr_percentile_window=5,
        atr_low_percentile=1,
        atr_high_percentile=99,
        trend_ema_fast=2,
        trend_ema_slow=4,
        trend_htf_multiple=2,
        session_timezone="UTC",
        trade_sessions=("00:00-23:59",),
        minimum_rr=1.2,
        pending_order_ttl_bars=3,
        round_trip_fees_usd=0,
        slippage_ticks_per_side=0,
    )


def test_role_retest_enters_first_retest_and_exits_at_structural_target() -> None:
    bars = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 100, 104, 99, 103),
        _bar(2, 103, 108, 102, 107),
        _bar(3, 107, 112, 106, 111),
        _bar(4, 111, 110, 107, 108),
        _bar(5, 108, 108, 104, 105),
        _bar(6, 105, 106, 102, 103),
        _bar(7, 103, 105, 103, 104),
        _bar(8, 104, 104.5, 104, 104.2),
        _bar(9, 105, 105, 103, 104),
        _bar(10, 104, 105.25, 103.5, 104.5),
        _bar(11, 104.5, 104.5, 103, 103.8),
        _bar(12, 103.8, 104, 103, 103.5),
        _bar(13, 104, 107, 103.8, 106.5),
        _bar(14, 106, 106.5, 104, 105.5),
        _bar(15, 105.5, 112.5, 105.5, 112),
    ]

    result = run_role_retest_backtest(bars, _config())

    assert result["setup_count"] == 1
    assert result["metrics"]["trade_count"] == 1
    trade = result["trades"][0]
    assert trade["side"] == "long"
    assert trade["entry_price"] == 104.125
    assert trade["take_profit"] == 112
    assert trade["exit_reason"] == "take_profit"


def test_role_retest_rejects_setup_when_nearest_target_rr_is_too_small() -> None:
    bars = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 100, 104, 99, 103),
        _bar(2, 103, 108, 102, 107),
        _bar(3, 107, 107, 106, 106.8),
        _bar(4, 106.8, 106.5, 104, 104.5),
        _bar(5, 104.5, 105.25, 103, 104.8),
        _bar(6, 104.8, 104.8, 102, 102.5),
        _bar(7, 102.5, 104, 103, 103.5),
        _bar(8, 103.5, 105.25, 103, 104.5),
        _bar(9, 104.5, 104.5, 103, 103.7),
        _bar(10, 103.7, 104, 103, 103.5),
        _bar(11, 104, 106, 103.8, 105.8),
        _bar(12, 105.8, 106, 105, 105.4),
    ]

    result = run_role_retest_backtest(bars, _config())

    assert result["setup_count"] == 0
    assert result["metrics"]["trade_count"] == 0
