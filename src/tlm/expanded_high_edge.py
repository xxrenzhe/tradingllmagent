from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExpandedHighEdge:
    scan_type: str
    direction_label: str
    session_bucket: str
    take_profit_r: float
    dow: int | None = None
    trend_bin: int | None = None
    volume_bin: int | None = None
    range_bin: int | None = None

    @property
    def direction(self) -> int:
        return 1 if self.direction_label == "long" else -1


EXPANDED_HIGH_EDGE_CAP24_PRESET = "expanded_high_edge_cap24"
EXPANDED_HIGH_EDGE_CAP24_MAX_CONCURRENT_POSITIONS = 24
EXPANDED_HIGH_EDGE_CAP24_MAX_HOLD_MINUTES = 300
EXPANDED_HIGH_EDGE_CAP24_STOP_RANGE_MULTIPLE = 6.0
EXPANDED_HIGH_EDGE_CAP24_MIN_STOP_POINTS = 8.0
EXPANDED_HIGH_EDGE_CAP24_MAX_STOP_POINTS = 90.0


EXPANDED_HIGH_EDGE_CAP24_EDGES: tuple[ExpandedHighEdge, ...] = (
    ExpandedHighEdge("opening_range_breakout", "short", "ny_0930_1159", 1.0, dow=5, trend_bin=-1, volume_bin=0, range_bin=-1),
    ExpandedHighEdge("session_extreme_reversion", "long", "ny_1200_1559", 1.25, dow=2, trend_bin=-1),
    ExpandedHighEdge("trend_pullback_reclaim", "long", "ny_0930_1159", 1.5, dow=1, trend_bin=1, volume_bin=1),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_0930_1159", 1.5, dow=5, trend_bin=-1, volume_bin=0, range_bin=0),
    ExpandedHighEdge("prior_day_breakout", "short", "ny_0930_1159", 1.5, dow=2, trend_bin=-1, volume_bin=3),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1, volume_bin=0, range_bin=-1),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_0930_1159", 1.5, dow=5, trend_bin=-1, volume_bin=0),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_0930_1159", 1.25, dow=1, trend_bin=1, volume_bin=1, range_bin=-1),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_0930_1159", 1.5, dow=5, trend_bin=1, volume_bin=3),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_0930_1159", 1.5, dow=5, trend_bin=1),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_0930_1159", 1.5, dow=5, trend_bin=-1, volume_bin=1),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_0930_1159", 1.5, dow=1, trend_bin=1, volume_bin=1),
    ExpandedHighEdge("vwap_pullback_bounce", "long", "ny_0930_1159", 1.25, dow=2, trend_bin=1, volume_bin=0),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_1200_1559", 1.5, dow=1, trend_bin=1, volume_bin=1, range_bin=0),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1, volume_bin=1, range_bin=0),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_0930_1159", 1.5, dow=1, trend_bin=1),
    ExpandedHighEdge("selling_absorption_reversal", "long", "ny_0930_1159", 1.5, dow=1, trend_bin=-1),
    ExpandedHighEdge("vwap_pullback_bounce", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1, volume_bin=2),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_1200_1559", 1.5, dow=1, trend_bin=1, volume_bin=1, range_bin=1),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_1200_1559", 1.5, dow=1, trend_bin=1, volume_bin=2),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_1200_1559", 1.25, dow=1, trend_bin=1, volume_bin=3, range_bin=2),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_1200_1559", 1.5, dow=1, trend_bin=1, volume_bin=1),
    ExpandedHighEdge("trend_pullback_reclaim", "long", "ny_0930_1159", 1.5, trend_bin=1, volume_bin=1, range_bin=1),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_1200_1559", 1.5, dow=1, trend_bin=1),
    ExpandedHighEdge("prior_day_breakout", "short", "ny_0930_1159", 1.0, dow=2, trend_bin=-1),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1, volume_bin=1, range_bin=1),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1, volume_bin=1),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_0930_1159", 1.5, dow=1, trend_bin=1, volume_bin=3),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_0930_1159", 1.5, dow=5, trend_bin=-1),
    ExpandedHighEdge("opening_range_breakout", "long", "ny_1200_1559", 1.25, dow=3, trend_bin=1, volume_bin=0, range_bin=-1),
    ExpandedHighEdge("vwap_reclaim_continuation", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1),
)


EXPANDED_HIGH_EDGE_PRESETS: dict[str, tuple[ExpandedHighEdge, ...]] = {
    EXPANDED_HIGH_EDGE_CAP24_PRESET: EXPANDED_HIGH_EDGE_CAP24_EDGES,
}
