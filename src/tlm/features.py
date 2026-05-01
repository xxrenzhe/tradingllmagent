from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right, insort
from datetime import time
from typing import Sequence

CORE_EXECUTABLE_FEATURES = {
    "atr_14",
    "bar_body_ratio",
    "body_to_range",
    "breakout_failure_flag",
    "bar_volume",
    "close_zscore_20",
    "ema_9_minus_ema_21",
    "ema_slope_9",
    "inside_bar",
    "inside_bar_flag",
    "lower_shadow_pct",
    "lower_wick_ratio",
    "minutes_since_open",
    "minutes_to_close",
    "opening_range_high_dist",
    "opening_range_low_dist",
    "outside_bar",
    "outside_bar_flag",
    "prior_day_high_dist",
    "prior_day_low_dist",
    "pullback_depth",
    "range_percentile_20",
    "realized_volatility_20",
    "return_1m",
    "return_5m",
    "return_15m",
    "session_high_dist",
    "session_low_dist",
    "spread_ticks",
    "tick_count_1m",
    "trend_age",
    "upper_shadow_pct",
    "upper_wick_ratio",
    "volatility_compression",
    "volume_absorption_flag",
    "volume_ma_5",
    "volume_ma_20",
    "volume_percentile_session",
    "volume_price_confirm",
    "volume_spike_flag",
    "relative_volume_5",
    "relative_volume_20",
    "multi_timeframe_volume_confirm",
    "low_volume_filter",
    "vwap_dist",
    "vwap_reclaim_flag",
}


def compute_executable_features(
    bars: Sequence[dict],
    *,
    session_trade: str = "13:30-20:45",
    flatten: str = "20:55",
    tick_size: float = 0.25,
    opening_range_minutes: int = 30,
) -> list[dict]:
    if not bars:
        return []

    trade_start, _trade_end = _parse_session_range(session_trade)
    flatten_time = _parse_clock(flatten)
    closes = [float(bar["close"]) for bar in bars]
    highs = [float(bar["high"]) for bar in bars]
    lows = [float(bar["low"]) for bar in bars]
    opens = [float(bar["open"]) for bar in bars]
    volumes = [_bar_volume(bar) for bar in bars]
    ema_9 = _ema_series(closes, 9)
    ema_21 = _ema_series(closes, 21)
    atr_14 = _atr_series(highs, lows, closes, 14)
    zscore_20 = _zscore_series(closes, 20)
    realized_vol_20 = _realized_volatility_series(closes, 20)
    range_percentile_20 = _range_percentile_series(highs, lows, 20)
    volume_ma_5 = _rolling_mean(volumes, 5)
    volume_ma_20 = _rolling_mean(volumes, 20)
    volume_sum_5 = _rolling_sum(volumes, 5)
    volume_sum_15 = _rolling_sum(volumes, 15)

    enriched: list[dict] = []
    prior_day_high: float | None = None
    prior_day_low: float | None = None
    current_day = None
    day_high = None
    day_low = None
    day_open = None
    day_close = None
    vwap_numerator = 0.0
    vwap_denominator = 0.0
    opening_high = None
    opening_low = None
    trend_direction = 0
    trend_age = 0
    previous_vwap_dist = None
    day_volumes_sorted: list[float] = []

    for index, bar in enumerate(bars):
        timestamp = bar["timestamp"]
        bar_day = timestamp.date()
        close = closes[index]
        high = highs[index]
        low = lows[index]
        open_price = opens[index]
        bar_volume = volumes[index]
        if current_day != bar_day:
            if current_day is not None:
                prior_day_high = day_high
                prior_day_low = day_low
            current_day = bar_day
            day_high = high
            day_low = low
            day_open = open_price
            day_close = close
            vwap_numerator = 0.0
            vwap_denominator = 0.0
            opening_high = None
            opening_low = None
            trend_direction = 0
            trend_age = 0
            previous_vwap_dist = None
            day_volumes_sorted = []
        else:
            day_high = max(day_high if day_high is not None else high, high)
            day_low = min(day_low if day_low is not None else low, low)
            day_close = close

        session_minute = _session_minutes(timestamp.time(), trade_start)
        minutes_to_close = _minutes_between(timestamp.time(), flatten_time)
        typical_price = (high + low + close) / 3
        weight = max(bar_volume, 1.0)
        vwap_numerator += typical_price * weight
        vwap_denominator += weight
        vwap = vwap_numerator / vwap_denominator if vwap_denominator else close
        vwap_dist = close - vwap

        if 0 <= session_minute < opening_range_minutes:
            opening_high = high if opening_high is None else max(opening_high, high)
            opening_low = low if opening_low is None else min(opening_low, low)

        fast = ema_9[index]
        slow = ema_21[index]
        ema_spread = _none_if_missing(fast, slow, lambda left, right: left - right)
        ema_slope = None
        if fast is not None and index > 0 and ema_9[index - 1] is not None:
            ema_slope = fast - ema_9[index - 1]

        direction = 1 if ema_spread is not None and ema_spread > 0 else -1 if ema_spread is not None and ema_spread < 0 else 0
        trend_age = trend_age + 1 if direction and direction == trend_direction else 1 if direction else 0
        trend_direction = direction or trend_direction

        bar_range = high - low
        body = close - open_price
        upper_wick = high - max(open_price, close)
        lower_wick = min(open_price, close) - low
        previous = bars[index - 1] if index > 0 else None
        prior_high = float(previous["high"]) if previous else None
        prior_low = float(previous["low"]) if previous else None
        inside = bool(previous and high <= prior_high and low >= prior_low)
        outside = bool(previous and high >= prior_high and low <= prior_low)
        session_high_dist = close - (day_high if day_high is not None else high)
        session_low_dist = close - (day_low if day_low is not None else low)
        pullback_depth = 0.0
        if ema_spread is not None:
            pullback_depth = max(0.0, (fast or close) - close) if ema_spread > 0 else max(0.0, close - (fast or close))
        volatility_compression = None
        if atr_14[index] is not None and index >= 20:
            recent_atr = [value for value in atr_14[index - 20 : index] if value is not None]
            if recent_atr:
                volatility_compression = 1.0 if atr_14[index] < sum(recent_atr) / len(recent_atr) else 0.0
        breakout_failure = 0.0
        if previous and high > prior_high and close < prior_high:
            breakout_failure = -1.0
        elif previous and low < prior_low and close > prior_low:
            breakout_failure = 1.0
        vwap_reclaim = 0.0
        if previous_vwap_dist is not None:
            if previous_vwap_dist < 0 <= vwap_dist:
                vwap_reclaim = 1.0
            elif previous_vwap_dist > 0 >= vwap_dist:
                vwap_reclaim = -1.0
        previous_vwap_dist = vwap_dist
        relative_volume_5 = _ratio(bar_volume, volume_ma_5[index])
        relative_volume_20 = _ratio(bar_volume, volume_ma_20[index])
        insort(day_volumes_sorted, bar_volume)
        volume_percentile_session = (
            bisect_right(day_volumes_sorted, bar_volume) / len(day_volumes_sorted)
            if len(day_volumes_sorted) >= 5
            else None
        )
        five_minute_confirm = _ratio(volume_sum_5[index], _rolling_mean_before(volume_sum_5, index, 20))
        fifteen_minute_confirm = _ratio(volume_sum_15[index], _rolling_mean_before(volume_sum_15, index, 20))
        multi_timeframe_volume_confirm = (
            1.0
            if relative_volume_5 is not None
            and five_minute_confirm is not None
            and fifteen_minute_confirm is not None
            and relative_volume_5 >= 1.5
            and five_minute_confirm >= 1.2
            and fifteen_minute_confirm >= 1.1
            else 0.0
        )
        return_1m = _return(closes, index, 1)
        return_5m = _return(closes, index, 5)
        volume_spike_flag = 1.0 if relative_volume_20 is not None and relative_volume_20 >= 2.0 else 0.0
        price_confirm = 0.0
        if volume_spike_flag and return_5m is not None:
            price_confirm = 1.0 if return_5m > 0 else -1.0 if return_5m < 0 else 0.0
        absorption_flag = 0.0
        if volume_spike_flag and atr_14[index] is not None:
            weak_body = abs(close - open_price) <= max(atr_14[index] * 0.15, tick_size)
            if weak_body:
                absorption_flag = -1.0 if close >= open_price else 1.0
        low_volume_filter = 1.0 if relative_volume_20 is not None and relative_volume_20 < 0.5 else 0.0

        features = {
            "atr_14": atr_14[index],
            "bar_volume": bar_volume,
            "bar_body_ratio": body / bar_range if bar_range else 0.0,
            "body_to_range": abs(body) / bar_range if bar_range else 0.0,
            "breakout_failure_flag": breakout_failure,
            "close_zscore_20": zscore_20[index],
            "ema_9_minus_ema_21": ema_spread,
            "ema_slope_9": ema_slope,
            "inside_bar": 1.0 if inside else 0.0,
            "inside_bar_flag": 1.0 if inside else 0.0,
            "lower_shadow_pct": lower_wick / bar_range if bar_range else 0.0,
            "lower_wick_ratio": lower_wick / bar_range if bar_range else 0.0,
            "minutes_since_open": session_minute,
            "minutes_to_close": minutes_to_close,
            "opening_range_high_dist": close - opening_high if opening_high is not None else None,
            "opening_range_low_dist": close - opening_low if opening_low is not None else None,
            "outside_bar": 1.0 if outside else 0.0,
            "outside_bar_flag": 1.0 if outside else 0.0,
            "prior_day_high_dist": close - prior_day_high if prior_day_high is not None else None,
            "prior_day_low_dist": close - prior_day_low if prior_day_low is not None else None,
            "pullback_depth": pullback_depth,
            "range_percentile_20": range_percentile_20[index],
            "realized_volatility_20": realized_vol_20[index],
            "return_1m": return_1m,
            "return_5m": _return(closes, index, 5),
            "return_15m": _return(closes, index, 15),
            "session_high_dist": session_high_dist,
            "session_low_dist": session_low_dist,
            "spread_ticks": float(bar.get("avg_spread", 0.0) or 0.0) / tick_size if tick_size else 0.0,
            "tick_count_1m": bar_volume,
            "trend_age": float(trend_age),
            "upper_shadow_pct": upper_wick / bar_range if bar_range else 0.0,
            "upper_wick_ratio": upper_wick / bar_range if bar_range else 0.0,
            "volatility_compression": volatility_compression,
            "volume_absorption_flag": absorption_flag,
            "volume_ma_5": volume_ma_5[index],
            "volume_ma_20": volume_ma_20[index],
            "volume_percentile_session": volume_percentile_session,
            "volume_price_confirm": price_confirm,
            "volume_spike_flag": volume_spike_flag,
            "relative_volume_5": relative_volume_5,
            "relative_volume_20": relative_volume_20,
            "multi_timeframe_volume_confirm": multi_timeframe_volume_confirm,
            "low_volume_filter": low_volume_filter,
            "vwap_dist": vwap_dist,
            "vwap_reclaim_flag": vwap_reclaim,
        }
        enriched_bar = dict(bar)
        enriched_bar["features"] = features
        enriched.append(enriched_bar)

    return enriched


def executable_feature_names() -> tuple[str, ...]:
    return tuple(sorted(CORE_EXECUTABLE_FEATURES))


def feature_snapshot_hash(bars: Sequence[dict]) -> str:
    payload = [
        {
            "timestamp": bar["timestamp"].isoformat() if hasattr(bar["timestamp"], "isoformat") else str(bar["timestamp"]),
            "features": {
                key: _round_feature(value)
                for key, value in sorted((bar.get("features") or {}).items())
                if key in CORE_EXECUTABLE_FEATURES
            },
        }
        for bar in bars
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def feature_value(bar: dict, name: str):
    if name in bar:
        return bar[name]
    features = bar.get("features") or {}
    return features.get(_FEATURE_ALIASES.get(name, name))


_FEATURE_ALIASES = {
    "volume": "bar_volume",
    "volume_1m": "bar_volume",
    "relative_volume_5m": "relative_volume_5",
    "volume_spike_ratio": "relative_volume_20",
    "candle_body_pct": "bar_body_ratio",
    "realized_vol_20": "realized_volatility_20",
}


def _round_feature(value):
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 10)
    return value


def _parse_session_range(value: str) -> tuple[time, time]:
    ranges = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        start, end = part.split("-", 1)
        ranges.append((_parse_clock(start), _parse_clock(end)))
    if not ranges:
        raise ValueError("session range must contain at least one HH:MM-HH:MM window")
    return ranges[0][0], ranges[-1][1]


def _parse_clock(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def _session_minutes(current: time, start: time) -> float:
    return _minutes_between(start, current)


def _minutes_between(start: time, end: time) -> float:
    return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)


def _ema_series(values: Sequence[float], window: int) -> list[float | None]:
    alpha = 2 / (window + 1)
    series: list[float | None] = []
    ema = None
    for index, value in enumerate(values):
        ema = value if ema is None else alpha * value + (1 - alpha) * ema
        series.append(ema if index + 1 >= window else None)
    return series


def _atr_series(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], window: int) -> list[float | None]:
    true_ranges: list[float] = []
    for index, high in enumerate(highs):
        if index == 0:
            true_ranges.append(high - lows[index])
            continue
        previous_close = closes[index - 1]
        true_ranges.append(max(high - lows[index], abs(high - previous_close), abs(lows[index] - previous_close)))
    return _rolling_mean(true_ranges, window)


def _zscore_series(values: Sequence[float], window: int) -> list[float | None]:
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


def _realized_volatility_series(closes: Sequence[float], window: int) -> list[float | None]:
    returns = [0.0] + [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    series: list[float | None] = []
    for index, _value in enumerate(returns):
        if index + 1 < window:
            series.append(None)
            continue
        sample = returns[index + 1 - window : index + 1]
        mean = sum(sample) / window
        variance = sum((item - mean) ** 2 for item in sample) / window
        series.append(variance**0.5)
    return series


def _range_percentile_series(highs: Sequence[float], lows: Sequence[float], window: int) -> list[float | None]:
    ranges = [high - low for high, low in zip(highs, lows)]
    series: list[float | None] = []
    for index, current in enumerate(ranges):
        if index + 1 < window:
            series.append(None)
            continue
        sample = ranges[index + 1 - window : index + 1]
        series.append(sum(1 for value in sample if value <= current) / len(sample))
    return series


def _rolling_mean(values: Sequence[float], window: int) -> list[float | None]:
    series: list[float | None] = []
    for index, _value in enumerate(values):
        if index + 1 < window:
            series.append(None)
            continue
        sample = values[index + 1 - window : index + 1]
        series.append(sum(sample) / window)
    return series


def _rolling_sum(values: Sequence[float], window: int) -> list[float | None]:
    series: list[float | None] = []
    for index, _value in enumerate(values):
        if index + 1 < window:
            series.append(None)
            continue
        series.append(sum(values[index + 1 - window : index + 1]))
    return series


def _return(values: Sequence[float], index: int, lookback: int) -> float | None:
    if index < lookback:
        return None
    return values[index] - values[index - lookback]


def _bar_volume(bar: dict) -> float:
    value = bar.get("bar_volume", bar.get("volume", bar.get("tick_count", 0)))
    return max(float(value or 0), 0.0)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _rolling_mean_before(values: Sequence[float | None], index: int, lookback: int) -> float | None:
    start = max(0, index - lookback)
    sample = [float(value) for value in values[start:index] if value is not None]
    if not sample:
        return None
    return sum(sample) / len(sample)


def _volume_percentile_same_day(volumes: Sequence[float], bars: Sequence[dict], index: int) -> float | None:
    current_day = bars[index]["timestamp"].date()
    sample = [
        volumes[item]
        for item in range(0, index + 1)
        if bars[item]["timestamp"].date() == current_day
    ]
    if len(sample) < 5:
        return None
    current = volumes[index]
    return sum(1 for value in sample if value <= current) / len(sample)


def _none_if_missing(left, right, fn):
    if left is None or right is None:
        return None
    return fn(left, right)
