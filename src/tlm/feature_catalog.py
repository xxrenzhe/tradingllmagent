from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    category: str
    data_level: str
    description: str
    required_inputs: tuple[str, ...] = ()
    lookback_bars: int = 0
    warmup_days: int = 0
    implementation_status: str = "candidate"
    leakage_risk: str = "low"
    supported_timeframes: tuple[str, ...] = ("1m",)
    source_notes: str = "Seeded from intraday trading feature research."

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "data_level": self.data_level,
            "description": self.description,
            "required_inputs": list(self.required_inputs),
            "lookback_bars": self.lookback_bars,
            "warmup_days": self.warmup_days,
            "implementation_status": self.implementation_status,
            "leakage_risk": self.leakage_risk,
            "supported_timeframes": list(self.supported_timeframes),
            "source_notes": self.source_notes,
        }


_FEATURE_ROWS = [
    ("log_return_1m", "return", "bar", "One-minute log close-to-close return."),
    ("open_close_return_1m", "return", "bar", "One-minute open-to-close return."),
    ("return_3m", "return", "bar", "Three-minute close-to-close return."),
    ("return_5m", "return", "bar", "Five-minute close-to-close return."),
    ("return_10m", "return", "bar", "Ten-minute close-to-close return."),
    ("return_15m", "return", "bar", "Fifteen-minute close-to-close return."),
    ("return_30m", "return", "bar", "Thirty-minute close-to-close return."),
    ("return_60m", "return", "bar", "Sixty-minute close-to-close return."),
    ("overnight_gap_pct", "gap", "bar", "Session open versus prior session close."),
    ("open_to_now_return", "return", "bar", "Current close versus session open."),
    ("sma_dist_5", "trend", "bar", "Close distance from 5-bar simple moving average."),
    ("sma_dist_20", "trend", "bar", "Close distance from 20-bar simple moving average."),
    ("ema_dist_9", "trend", "bar", "Close distance from 9-bar exponential moving average."),
    ("ema_9_minus_ema_21", "trend", "bar", "Fast minus slow EMA spread."),
    ("ema_slope_9", "trend", "bar", "Slope of the 9-bar EMA."),
    ("linear_reg_slope_20", "trend", "bar", "Twenty-bar linear regression slope."),
    ("linear_reg_angle_20", "trend", "bar", "Twenty-bar linear regression angle."),
    ("tsf_error_20", "trend", "bar", "Close distance from time-series forecast."),
    ("aroon_osc_14", "trend", "bar", "Fourteen-bar Aroon oscillator."),
    ("adx_14", "trend", "bar", "Fourteen-bar directional trend strength."),
    ("plus_di_minus_di_spread", "trend", "bar", "Positive DI minus negative DI."),
    ("macd_hist", "trend", "bar", "MACD histogram."),
    ("ppo", "trend", "bar", "Percentage price oscillator."),
    ("trix", "trend", "bar", "Triple-smoothed rate of change."),
    ("true_range", "volatility", "bar", "Current true range."),
    ("atr_14", "volatility", "bar", "Fourteen-bar average true range."),
    ("natr_14", "volatility", "bar", "Normalized average true range."),
    ("realized_vol_5m", "volatility", "bar", "Five-minute realized volatility."),
    ("realized_vol_15m", "volatility", "bar", "Fifteen-minute realized volatility."),
    ("realized_vol_30m", "volatility", "bar", "Thirty-minute realized volatility."),
    ("parkinson_vol_20", "volatility", "bar", "High-low volatility estimator."),
    ("garman_klass_vol_20", "volatility", "bar", "OHLC volatility estimator."),
    ("high_low_range_pct", "volatility", "bar", "Current high-low range over close."),
    ("range_zscore_tod", "volatility", "bar", "Range z-score normalized by time of day."),
    ("volatility_ratio_5_30", "volatility", "bar", "Short volatility divided by longer volatility."),
    ("atr_percentile_20d", "volatility", "bar", "ATR percentile over recent sessions."),
    ("rolling_high_dist_20", "breakout", "bar", "Close distance from rolling 20-bar high."),
    ("rolling_low_dist_20", "breakout", "bar", "Close distance from rolling 20-bar low."),
    ("donchian_position_20", "breakout", "bar", "Close position within 20-bar Donchian channel."),
    ("breakout_strength_20", "breakout", "bar", "Breakout distance beyond 20-bar range."),
    ("opening_range_high_dist", "opening_range", "bar", "Close distance from opening range high."),
    ("opening_range_low_dist", "opening_range", "bar", "Close distance from opening range low."),
    ("premarket_high_dist", "opening_range", "bar", "Close distance from premarket high."),
    ("premarket_low_dist", "opening_range", "bar", "Close distance from premarket low."),
    ("prior_day_high_dist", "level", "bar", "Close distance from prior session high."),
    ("prior_day_low_dist", "level", "bar", "Close distance from prior session low."),
    ("prior_close_dist", "level", "bar", "Close distance from prior session close."),
    ("session_high_low_position", "level", "bar", "Close percentile inside current session range."),
    ("close_zscore_20", "mean_reversion", "bar", "Twenty-bar close z-score."),
    ("return_zscore_20", "mean_reversion", "bar", "Twenty-bar return z-score."),
    ("bollinger_percent_b", "mean_reversion", "bar", "Close position inside Bollinger bands."),
    ("bollinger_bandwidth", "volatility", "bar", "Bollinger band width."),
    ("rsi_14", "oscillator", "bar", "Fourteen-bar relative strength index."),
    ("stoch_k_14", "oscillator", "bar", "Fourteen-bar stochastic percent K."),
    ("stoch_d_14", "oscillator", "bar", "Fourteen-bar stochastic percent D."),
    ("stoch_rsi", "oscillator", "bar", "Stochastic RSI."),
    ("williams_r_14", "oscillator", "bar", "Fourteen-bar Williams R."),
    ("cci_20", "oscillator", "bar", "Twenty-bar commodity channel index."),
    ("cmo_14", "oscillator", "bar", "Fourteen-bar Chande momentum oscillator."),
    ("rolling_return_autocorr_20", "return", "bar", "Twenty-bar return autocorrelation."),
    ("volume_1m", "volume", "bar", "One-minute traded volume."),
    ("log_volume", "volume", "bar", "Log transformed volume."),
    ("volume_zscore_tod", "volume", "bar", "Volume z-score normalized by time of day."),
    ("relative_volume_5m", "volume", "bar", "Five-minute volume versus recent baseline."),
    ("cumulative_volume_vs_expected", "volume", "bar", "Session volume versus expected volume curve."),
    ("volume_spike_ratio", "volume", "bar", "Current volume divided by recent average volume."),
    ("obv_slope_20", "volume", "bar", "Twenty-bar on-balance volume slope."),
    ("money_flow_index_14", "volume", "bar", "Fourteen-bar money flow index."),
    ("chaikin_ad_slope", "volume", "bar", "Chaikin accumulation-distribution slope."),
    ("chaikin_oscillator", "volume", "bar", "Chaikin oscillator."),
    ("vwap_dist", "vwap", "bar", "Close distance from session VWAP."),
    ("anchored_vwap_open_dist", "vwap", "bar", "Close distance from open-anchored VWAP."),
    ("vwap_slope_15m", "vwap", "bar", "Fifteen-minute VWAP slope."),
    ("price_cross_vwap_age", "vwap", "bar", "Minutes since last VWAP cross."),
    ("vwap_reclaim_flag", "vwap", "bar", "Close reclaimed VWAP after trading below it."),
    ("typical_price_vwap_dist", "vwap", "bar", "Typical price distance from VWAP."),
    ("hlc3_return_5m", "return", "bar", "Five-minute return of typical price."),
    ("weighted_close_return_5m", "return", "bar", "Five-minute weighted close return."),
    ("candle_body_pct", "candle", "bar", "Candle body divided by close."),
    ("upper_shadow_pct", "candle", "bar", "Upper wick divided by close."),
    ("lower_shadow_pct", "candle", "bar", "Lower wick divided by close."),
    ("body_to_range", "candle", "bar", "Candle body divided by high-low range."),
    ("close_position_in_bar", "candle", "bar", "Close percentile inside current bar range."),
    ("inside_bar_flag", "candle", "bar", "Current bar range inside prior bar range."),
    ("outside_bar_flag", "candle", "bar", "Current bar range outside prior bar range."),
    ("doji_score", "candle", "bar", "Small body relative to full bar range."),
    ("engulfing_flag", "candle", "bar", "Current body engulfs prior body."),
    ("hammer_score", "candle", "bar", "Long lower wick hammer score."),
    ("shooting_star_score", "candle", "bar", "Long upper wick reversal score."),
    ("heikin_ashi_trend", "candle", "bar", "Heikin-Ashi directional state."),
    ("minute_of_session_sin", "time", "calendar", "Sine transform of session minute."),
    ("minute_of_session_cos", "time", "calendar", "Cosine transform of session minute."),
    ("minutes_since_open", "time", "calendar", "Elapsed minutes since regular session open."),
    ("minutes_to_close", "time", "calendar", "Remaining minutes before flatten time."),
    ("session_bucket", "time", "calendar", "Discrete open, midday, afternoon, close bucket."),
    ("day_of_week", "time", "calendar", "Day-of-week seasonality."),
    ("month_turn_flag", "time", "calendar", "Start or end of month flag."),
    ("fomc_day_flag", "event", "calendar", "Federal Reserve announcement day flag."),
    ("cpi_nfp_day_flag", "event", "calendar", "Inflation or payroll announcement day flag."),
    ("macro_event_window_flag", "event", "calendar", "Pre/post scheduled macro event window flag."),
    ("es_nq_spread_return", "cross_asset", "external_bar", "NQ return minus ES return."),
    ("nq_rty_relative_strength", "cross_asset", "external_bar", "NQ return versus RTY return."),
    ("vix_return_5m", "cross_asset", "external_bar", "Five-minute VIX move."),
    ("dxy_return_5m", "cross_asset", "external_bar", "Five-minute dollar index move."),
    ("ten_year_yield_change", "cross_asset", "external_bar", "Short-horizon Treasury yield change."),
    ("tick_rule_buy_volume_ratio", "order_flow", "tick", "Tick-rule buy volume divided by total volume."),
    ("signed_volume_imbalance", "order_flow", "tick", "Signed buy minus sell volume imbalance."),
    ("up_down_volume_delta", "order_flow", "tick", "Up-tick volume minus down-tick volume."),
    ("cumulative_delta", "order_flow", "tick", "Intraday cumulative signed volume delta."),
    ("vpin_proxy", "order_flow", "tick", "Volume-synchronized imbalance proxy."),
    ("bid_ask_spread", "microstructure", "quote", "Best bid-ask spread."),
    ("order_book_imbalance_l1", "microstructure", "quote", "Level-one bid size versus ask size imbalance."),
]

def feature_catalog_by_name() -> dict[str, FeatureDefinition]:
    return {feature.name: feature for feature in FEATURE_CATALOG}


def features_for_data_levels(data_levels: set[str]) -> tuple[FeatureDefinition, ...]:
    return tuple(feature for feature in FEATURE_CATALOG if feature.data_level in data_levels)


def feature_readiness_report() -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_data_level: dict[str, int] = {}
    by_leakage_risk: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for feature in FEATURE_CATALOG:
        by_status[feature.implementation_status] = by_status.get(feature.implementation_status, 0) + 1
        by_data_level[feature.data_level] = by_data_level.get(feature.data_level, 0) + 1
        by_leakage_risk[feature.leakage_risk] = by_leakage_risk.get(feature.leakage_risk, 0) + 1
        by_category[feature.category] = by_category.get(feature.category, 0) + 1
    usable_for_bar_research = [
        feature.name
        for feature in FEATURE_CATALOG
        if feature.implementation_status == "implemented" and feature.data_level in {"bar", "calendar"}
    ]
    external_required = [
        feature.name
        for feature in FEATURE_CATALOG
        if feature.implementation_status == "external_required"
    ]
    return {
        "feature_count": len(FEATURE_CATALOG),
        "by_status": dict(sorted(by_status.items())),
        "by_data_level": dict(sorted(by_data_level.items())),
        "by_leakage_risk": dict(sorted(by_leakage_risk.items())),
        "by_category": dict(sorted(by_category.items())),
        "usable_for_bar_research_count": len(usable_for_bar_research),
        "external_required_count": len(external_required),
        "usable_for_bar_research": usable_for_bar_research,
        "external_required": external_required,
    }


def _feature_from_row(row: tuple[str, str, str, str]) -> FeatureDefinition:
    name, category, data_level, description = row
    return FeatureDefinition(
        name=name,
        category=category,
        data_level=data_level,
        description=description,
        required_inputs=_required_inputs(data_level, category),
        lookback_bars=_lookback_bars(name),
        warmup_days=_warmup_days(name, data_level),
        implementation_status=_implementation_status(data_level),
        leakage_risk=_leakage_risk(name, category, data_level),
        supported_timeframes=_supported_timeframes(data_level),
    )


def _required_inputs(data_level: str, category: str) -> tuple[str, ...]:
    if data_level == "bar":
        if category in {"volume", "vwap"}:
            return ("timestamp", "open", "high", "low", "close", "volume")
        return ("timestamp", "open", "high", "low", "close")
    if data_level == "calendar":
        return ("timestamp", "session_calendar")
    if data_level == "external_bar":
        return ("timestamp", "external_symbol_ohlcv")
    if data_level == "tick":
        return ("timestamp", "price", "volume")
    if data_level == "quote":
        return ("timestamp", "bid", "ask", "bid_size", "ask_size")
    return ("timestamp",)


def _lookback_bars(name: str) -> int:
    for token in reversed(name.split("_")):
        digits = "".join(character for character in token if character.isdigit())
        if digits:
            return int(digits)
    if name.startswith(("overnight_", "prior_", "premarket_")):
        return 390
    return 1


def _warmup_days(name: str, data_level: str) -> int:
    if data_level in {"external_bar", "tick", "quote"}:
        return 5
    if any(token in name for token in ("20d", "prior_day", "overnight", "premarket")):
        return 20
    lookback = _lookback_bars(name)
    return max(1, min(5, (lookback // 390) + 1))


def _implementation_status(data_level: str) -> str:
    if data_level in {"bar", "calendar"}:
        return "implemented"
    return "external_required"


def _leakage_risk(name: str, category: str, data_level: str) -> str:
    if data_level in {"external_bar", "quote"}:
        return "medium"
    if category == "event" or "expected" in name:
        return "medium"
    return "low"


def _supported_timeframes(data_level: str) -> tuple[str, ...]:
    if data_level in {"tick", "quote"}:
        return ("tick",)
    return ("1m", "5m", "15m")


FEATURE_CATALOG = tuple(_feature_from_row(row) for row in _FEATURE_ROWS)
