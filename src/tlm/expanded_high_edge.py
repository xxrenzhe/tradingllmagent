from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


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


@dataclass(frozen=True)
class ExpandedHighEdgeYearResult:
    year: int
    trade_count: int
    net_pnl: float
    profit_factor: float
    max_drawdown: float


@dataclass(frozen=True)
class ExpandedHighEdgePresetSpec:
    preset: str
    source_label: str
    max_concurrent_positions: int
    max_hold_minutes: int
    stop_range_multiple: float
    min_stop_points: float
    max_stop_points: float
    source_report: str
    edges: tuple[ExpandedHighEdge, ...]
    yearly_results: tuple[ExpandedHighEdgeYearResult, ...]


EXPANDED_HIGH_EDGE_CAP24_PRESET = "expanded_high_edge_cap24"
EXPANDED_HIGH_EDGE_CAP24_MAX_CONCURRENT_POSITIONS = 24
EXPANDED_HIGH_EDGE_CAP24_MAX_HOLD_MINUTES = 300
EXPANDED_HIGH_EDGE_CAP24_STOP_RANGE_MULTIPLE = 6.0
EXPANDED_HIGH_EDGE_CAP24_MIN_STOP_POINTS = 8.0
EXPANDED_HIGH_EDGE_CAP24_MAX_STOP_POINTS = 90.0
EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_PRESET = "expanded_high_edge_cap24_return_guard"
EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_MAX_CONCURRENT_POSITIONS = 24
EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_MAX_HOLD_MINUTES = 300
EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_STOP_RANGE_MULTIPLE = 8.0
EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_MIN_STOP_POINTS = 8.0
EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_MAX_STOP_POINTS = 90.0
EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_PRESET = "expanded_high_edge_cap24_balanced_risk"
EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_MAX_CONCURRENT_POSITIONS = 18
EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_MAX_HOLD_MINUTES = 300
EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_STOP_RANGE_MULTIPLE = 8.0
EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_MIN_STOP_POINTS = 8.0
EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_MAX_STOP_POINTS = 90.0
EXPANDED_HIGH_EDGE_NETMAX_PRESET = "positive_expanded_edge_top_32"
EXPANDED_HIGH_EDGE_NETMAX_MAX_CONCURRENT_POSITIONS = 99
EXPANDED_HIGH_EDGE_NETMAX_MAX_HOLD_MINUTES = 120
EXPANDED_HIGH_EDGE_NETMAX_STOP_RANGE_MULTIPLE = 6.0
EXPANDED_HIGH_EDGE_NETMAX_MIN_STOP_POINTS = 8.0
EXPANDED_HIGH_EDGE_NETMAX_MAX_STOP_POINTS = 90.0
EXPANDED_HIGH_EDGE_WF_ORB_2026_PRESET = "walk_forward_orb_2026_min13"
EXPANDED_HIGH_EDGE_WF_ORB_2026_MAX_CONCURRENT_POSITIONS = 6
EXPANDED_HIGH_EDGE_WF_ORB_2026_MAX_HOLD_MINUTES = 120
EXPANDED_HIGH_EDGE_WF_ORB_2026_STOP_RANGE_MULTIPLE = 6.0
EXPANDED_HIGH_EDGE_WF_ORB_2026_MIN_STOP_POINTS = 8.0
EXPANDED_HIGH_EDGE_WF_ORB_2026_MAX_STOP_POINTS = 90.0
EXPANDED_HIGH_EDGE_SOURCE_REPORT = "experiments/profit_mining/expanded_high_edge_strategy_search_2019_2026.json"
EXPANDED_HIGH_EDGE_WF_ORB_2026_SOURCE_REPORT = "reports/nq_expanded_high_edge_walk_forward_no_prior_highvol_donchian_min13_2026-05-01.json"


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


EXPANDED_HIGH_EDGE_NETMAX_EDGES: tuple[ExpandedHighEdge, ...] = (
    ExpandedHighEdge("opening_range_breakout", "short", "ny_0930_1159", 1.5, dow=5, trend_bin=-1, volume_bin=0, range_bin=-1),
    ExpandedHighEdge("session_extreme_reversion", "long", "ny_1200_1559", 1.5, dow=2, trend_bin=-1),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_0930_1159", 1.5, dow=5, trend_bin=-1, volume_bin=0, range_bin=0),
    ExpandedHighEdge("trend_pullback_reclaim", "long", "ny_0930_1159", 1.5, dow=1, trend_bin=1, volume_bin=1),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_0930_1159", 1.5, dow=1, trend_bin=1, volume_bin=1, range_bin=-1),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_0930_1159", 1.5, dow=5, trend_bin=-1, volume_bin=0),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_1200_1559", 1.25, dow=4, trend_bin=-1, volume_bin=0, range_bin=-1),
    ExpandedHighEdge("vwap_pullback_bounce", "long", "ny_0930_1159", 1.5, dow=2, trend_bin=1, volume_bin=0),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_0930_1159", 1.0, dow=5, trend_bin=1),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_0930_1159", 1.5, dow=5, trend_bin=-1, volume_bin=1),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1, volume_bin=1, range_bin=0),
    ExpandedHighEdge("prior_day_breakout", "short", "ny_0930_1159", 1.5, dow=5, trend_bin=-1, volume_bin=0, range_bin=-1),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_0930_1159", 1.0, dow=5, trend_bin=1, volume_bin=3),
    ExpandedHighEdge("prior_day_breakout", "short", "ny_0930_1159", 1.25, dow=2, trend_bin=-1, volume_bin=3),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1, volume_bin=2),
    ExpandedHighEdge("prior_day_breakout", "short", "ny_0930_1159", 1.25, dow=2, trend_bin=-1),
    ExpandedHighEdge("vwap_reclaim_continuation", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1, volume_bin=1),
    ExpandedHighEdge("vwap_pullback_bounce", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1),
    ExpandedHighEdge("opening_range_breakout", "long", "ny_0930_1159", 1.5, dow=2, trend_bin=1),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_1200_1559", 1.25, dow=1, trend_bin=1, volume_bin=3, range_bin=2),
    ExpandedHighEdge("range_expansion_continuation", "short", "ny_0930_1159", 1.25, dow=2, trend_bin=-1, volume_bin=3, range_bin=3),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_1200_1559", 1.0, dow=1, trend_bin=1, volume_bin=1, range_bin=2),
    ExpandedHighEdge("opening_range_breakout", "long", "ny_1200_1559", 1.5, dow=1, trend_bin=1, volume_bin=1, range_bin=2),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1, volume_bin=1, range_bin=1),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_1200_1559", 1.25, dow=1, trend_bin=1, volume_bin=1, range_bin=1),
    ExpandedHighEdge("prior_day_breakout", "short", "ny_0930_1159", 1.0, dow=2, trend_bin=-1, volume_bin=2),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_1200_1559", 1.5, dow=1, trend_bin=1, volume_bin=1, range_bin=0),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_1200_1559", 1.25, dow=4, trend_bin=-1, volume_bin=0, range_bin=0),
    ExpandedHighEdge("selling_absorption_reversal", "long", "ny_0930_1159", 1.5, dow=1, trend_bin=-1),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_1200_1559", 1.25, dow=4, trend_bin=-1, volume_bin=0),
    ExpandedHighEdge("prior_day_breakout", "long", "ny_0930_1159", 1.5, dow=1, trend_bin=1, volume_bin=3),
)


EXPANDED_HIGH_EDGE_NETMAX_YEARLY_RESULTS: tuple[ExpandedHighEdgeYearResult, ...] = (
    ExpandedHighEdgeYearResult(2019, 5498, 89935.0, 1.0875462992256286, 172061.25),
    ExpandedHighEdgeYearResult(2020, 6134, 718960.0, 1.2890518050661257, 315828.75),
    ExpandedHighEdgeYearResult(2021, 6150, 825376.25, 1.3524123907219026, 185562.5),
    ExpandedHighEdgeYearResult(2022, 7210, 1570143.75, 1.4137608338757408, 290920.0),
    ExpandedHighEdgeYearResult(2023, 6410, 730503.75, 1.2757481588573025, 310460.0),
    ExpandedHighEdgeYearResult(2024, 6563, 882780.0, 1.2713949734839751, 259670.0),
    ExpandedHighEdgeYearResult(2025, 6693, 968727.5, 1.221057862027025, 339425.0),
    ExpandedHighEdgeYearResult(2026, 2193, 17623.75, 1.0112538114014782, 475640.0),
)


EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_YEARLY_RESULTS: tuple[ExpandedHighEdgeYearResult, ...] = (
    ExpandedHighEdgeYearResult(2019, 3398, 146065.0, 1.1758315176174599, 99732.5),
    ExpandedHighEdgeYearResult(2020, 4079, 1004995.0, 1.479540688394819, 290465.0),
    ExpandedHighEdgeYearResult(2021, 3870, 1064108.75, 1.5757868237292996, 156230.0),
    ExpandedHighEdgeYearResult(2022, 4488, 1066186.25, 1.3326037450379418, 242837.5),
    ExpandedHighEdgeYearResult(2023, 3920, 790127.5, 1.3768361837900742, 137511.25),
    ExpandedHighEdgeYearResult(2024, 4338, 739100.0, 1.264524037472151, 217190.0),
    ExpandedHighEdgeYearResult(2025, 5117, 662960.0, 1.1709986355394262, 271625.0),
    ExpandedHighEdgeYearResult(2026, 1773, 2172.5, 1.0014129529904523, 317577.5),
)


EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_YEARLY_RESULTS: tuple[ExpandedHighEdgeYearResult, ...] = (
    ExpandedHighEdgeYearResult(2019, 2816, 112318.75, 1.1610281573024042, 84047.5),
    ExpandedHighEdgeYearResult(2020, 3376, 787437.5, 1.4478708095871868, 240635.0),
    ExpandedHighEdgeYearResult(2021, 3182, 849276.25, 1.5527450910203258, 126140.0),
    ExpandedHighEdgeYearResult(2022, 3705, 838161.25, 1.3142823797623087, 185472.5),
    ExpandedHighEdgeYearResult(2023, 3189, 672855.0, 1.3926924782892893, 108973.75),
    ExpandedHighEdgeYearResult(2024, 3583, 659385.0, 1.2865962981555974, 191295.0),
    ExpandedHighEdgeYearResult(2025, 4396, 542520.0, 1.159971928541626, 217270.0),
    ExpandedHighEdgeYearResult(2026, 1492, 49387.5, 1.0384284691638526, 257240.0),
)


EXPANDED_HIGH_EDGE_WF_ORB_2026_EDGES: tuple[ExpandedHighEdge, ...] = (
    ExpandedHighEdge("opening_range_breakout", "short", "ny_0930_1159", 1.5, dow=5, trend_bin=-1, volume_bin=0, range_bin=-1),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_0930_1159", 1.5, dow=5, trend_bin=-1, volume_bin=0, range_bin=0),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_0930_1159", 1.5, dow=5, trend_bin=-1, volume_bin=0, range_bin=0),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_0930_1159", 1.5, dow=5, trend_bin=-1, volume_bin=1, range_bin=0),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1, volume_bin=1, range_bin=0),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1, volume_bin=2, range_bin=0),
    ExpandedHighEdge("opening_range_breakout", "long", "ny_0930_1159", 1.5, dow=2, trend_bin=1, volume_bin=0, range_bin=0),
    ExpandedHighEdge("opening_range_retest_reclaim", "short", "ny_0930_1159", 1.0, dow=5, trend_bin=-1, volume_bin=0, range_bin=0),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1, volume_bin=1, range_bin=0),
    ExpandedHighEdge("vwap_pullback_bounce", "short", "ny_1200_1559", 1.5, dow=4, trend_bin=-1, volume_bin=0, range_bin=0),
    ExpandedHighEdge("opening_range_breakout", "long", "ny_0930_1159", 1.5, dow=2, trend_bin=1, volume_bin=1, range_bin=0),
    ExpandedHighEdge("range_expansion_continuation", "short", "ny_0930_1159", 1.25, dow=2, trend_bin=-1, volume_bin=3, range_bin=3),
    ExpandedHighEdge("opening_range_breakout", "short", "ny_1200_1559", 1.25, dow=4, trend_bin=-1, volume_bin=0, range_bin=0),
)


EXPANDED_HIGH_EDGE_CAP24_SPEC = ExpandedHighEdgePresetSpec(
    preset=EXPANDED_HIGH_EDGE_CAP24_PRESET,
    source_label="robust_expanded_positive_edge_top_32",
    max_concurrent_positions=EXPANDED_HIGH_EDGE_CAP24_MAX_CONCURRENT_POSITIONS,
    max_hold_minutes=EXPANDED_HIGH_EDGE_CAP24_MAX_HOLD_MINUTES,
    stop_range_multiple=EXPANDED_HIGH_EDGE_CAP24_STOP_RANGE_MULTIPLE,
    min_stop_points=EXPANDED_HIGH_EDGE_CAP24_MIN_STOP_POINTS,
    max_stop_points=EXPANDED_HIGH_EDGE_CAP24_MAX_STOP_POINTS,
    source_report=EXPANDED_HIGH_EDGE_SOURCE_REPORT,
    edges=EXPANDED_HIGH_EDGE_CAP24_EDGES,
    yearly_results=(),
)


EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_SPEC = ExpandedHighEdgePresetSpec(
    preset=EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_PRESET,
    source_label="cap24_return_guard_pos24_hold300_stop8",
    max_concurrent_positions=EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_MAX_CONCURRENT_POSITIONS,
    max_hold_minutes=EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_MAX_HOLD_MINUTES,
    stop_range_multiple=EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_STOP_RANGE_MULTIPLE,
    min_stop_points=EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_MIN_STOP_POINTS,
    max_stop_points=EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_MAX_STOP_POINTS,
    source_report="reports/nq_expanded_high_edge_cap24_risk_optimization_2026-05-02.json",
    edges=EXPANDED_HIGH_EDGE_CAP24_EDGES,
    yearly_results=EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_YEARLY_RESULTS,
)


EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_SPEC = ExpandedHighEdgePresetSpec(
    preset=EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_PRESET,
    source_label="cap24_balanced_risk_pos18_hold300_stop8",
    max_concurrent_positions=EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_MAX_CONCURRENT_POSITIONS,
    max_hold_minutes=EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_MAX_HOLD_MINUTES,
    stop_range_multiple=EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_STOP_RANGE_MULTIPLE,
    min_stop_points=EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_MIN_STOP_POINTS,
    max_stop_points=EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_MAX_STOP_POINTS,
    source_report="reports/nq_expanded_high_edge_cap24_risk_optimization_2026-05-02.json",
    edges=EXPANDED_HIGH_EDGE_CAP24_EDGES,
    yearly_results=EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_YEARLY_RESULTS,
)


EXPANDED_HIGH_EDGE_NETMAX_SPEC = ExpandedHighEdgePresetSpec(
    preset=EXPANDED_HIGH_EDGE_NETMAX_PRESET,
    source_label=EXPANDED_HIGH_EDGE_NETMAX_PRESET,
    max_concurrent_positions=EXPANDED_HIGH_EDGE_NETMAX_MAX_CONCURRENT_POSITIONS,
    max_hold_minutes=EXPANDED_HIGH_EDGE_NETMAX_MAX_HOLD_MINUTES,
    stop_range_multiple=EXPANDED_HIGH_EDGE_NETMAX_STOP_RANGE_MULTIPLE,
    min_stop_points=EXPANDED_HIGH_EDGE_NETMAX_MIN_STOP_POINTS,
    max_stop_points=EXPANDED_HIGH_EDGE_NETMAX_MAX_STOP_POINTS,
    source_report=EXPANDED_HIGH_EDGE_SOURCE_REPORT,
    edges=EXPANDED_HIGH_EDGE_NETMAX_EDGES,
    yearly_results=EXPANDED_HIGH_EDGE_NETMAX_YEARLY_RESULTS,
)


EXPANDED_HIGH_EDGE_WF_ORB_2026_SPEC = ExpandedHighEdgePresetSpec(
    preset=EXPANDED_HIGH_EDGE_WF_ORB_2026_PRESET,
    source_label="walk_forward_no_prior_highvol_donchian_min13_2026",
    max_concurrent_positions=EXPANDED_HIGH_EDGE_WF_ORB_2026_MAX_CONCURRENT_POSITIONS,
    max_hold_minutes=EXPANDED_HIGH_EDGE_WF_ORB_2026_MAX_HOLD_MINUTES,
    stop_range_multiple=EXPANDED_HIGH_EDGE_WF_ORB_2026_STOP_RANGE_MULTIPLE,
    min_stop_points=EXPANDED_HIGH_EDGE_WF_ORB_2026_MIN_STOP_POINTS,
    max_stop_points=EXPANDED_HIGH_EDGE_WF_ORB_2026_MAX_STOP_POINTS,
    source_report=EXPANDED_HIGH_EDGE_WF_ORB_2026_SOURCE_REPORT,
    edges=EXPANDED_HIGH_EDGE_WF_ORB_2026_EDGES,
    yearly_results=(),
)


EXPANDED_HIGH_EDGE_PRESETS: dict[str, tuple[ExpandedHighEdge, ...]] = {
    EXPANDED_HIGH_EDGE_CAP24_PRESET: EXPANDED_HIGH_EDGE_CAP24_EDGES,
    EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_PRESET: EXPANDED_HIGH_EDGE_CAP24_EDGES,
    EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_PRESET: EXPANDED_HIGH_EDGE_CAP24_EDGES,
    EXPANDED_HIGH_EDGE_NETMAX_PRESET: EXPANDED_HIGH_EDGE_NETMAX_EDGES,
    EXPANDED_HIGH_EDGE_WF_ORB_2026_PRESET: EXPANDED_HIGH_EDGE_WF_ORB_2026_EDGES,
}


EXPANDED_HIGH_EDGE_PRESET_SPECS: dict[str, ExpandedHighEdgePresetSpec] = {
    EXPANDED_HIGH_EDGE_CAP24_PRESET: EXPANDED_HIGH_EDGE_CAP24_SPEC,
    EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_PRESET: EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_SPEC,
    EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_PRESET: EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_SPEC,
    EXPANDED_HIGH_EDGE_NETMAX_PRESET: EXPANDED_HIGH_EDGE_NETMAX_SPEC,
    EXPANDED_HIGH_EDGE_WF_ORB_2026_PRESET: EXPANDED_HIGH_EDGE_WF_ORB_2026_SPEC,
}


def expanded_high_edge_preset_spec(preset: str) -> ExpandedHighEdgePresetSpec:
    try:
        return EXPANDED_HIGH_EDGE_PRESET_SPECS[preset]
    except KeyError as exc:
        known = ", ".join(sorted(EXPANDED_HIGH_EDGE_PRESET_SPECS))
        raise ValueError(f"Unknown expanded high-edge preset {preset!r}. Known presets: {known}") from exc


def expanded_high_edge_yearly_gate_report(
    preset: str,
    *,
    full_years: Sequence[int] = (2019, 2020, 2021, 2022, 2023, 2024, 2025),
    partial_years: Sequence[int] = (2026,),
    min_full_year_trades: int = 1000,
) -> dict:
    spec = expanded_high_edge_preset_spec(preset)
    yearly = {row.year: row for row in spec.yearly_results}
    checked_years = tuple(full_years) + tuple(partial_years)
    rows = [yearly[year] for year in checked_years if year in yearly]
    positive_years = sum(1 for row in rows if row.net_pnl > 0)
    full_year_trade_counts = [yearly[year].trade_count for year in full_years if year in yearly]
    return {
        "preset": preset,
        "source_label": spec.source_label,
        "source_report": spec.source_report,
        "checked_years": list(checked_years),
        "full_year_min_trades": min(full_year_trade_counts) if full_year_trade_counts else None,
        "trade_floor_pass": bool(full_year_trade_counts)
        and min(full_year_trade_counts) > min_full_year_trades,
        "positive_years": positive_years,
        "positive_years_pass": positive_years == len(checked_years),
        "worst_year_pnl": min((row.net_pnl for row in rows), default=None),
        "net_pnl": sum(row.net_pnl for row in rows),
        "yearly_results": [row.__dict__ for row in rows],
    }


def check_expanded_high_edge_replay(
    preset: str,
    replay_result: dict,
    *,
    pnl_tolerance: float = 0.01,
    metric_tolerance: float = 1e-9,
) -> dict:
    spec = expanded_high_edge_preset_spec(preset)
    expected_by_year = {row.year: row for row in spec.yearly_results}
    actual_by_year = {
        int(row["year"]): row
        for row in replay_result.get("yearly_results", [])
        if isinstance(row, dict) and row.get("year") is not None
    }
    mismatches = []
    for year, expected in expected_by_year.items():
        actual = actual_by_year.get(year)
        if actual is None:
            mismatches.append({"year": year, "field": "year", "expected": year, "actual": None})
            continue
        _append_mismatch(mismatches, year, "trade_count", expected.trade_count, actual.get("trade_count"))
        _append_mismatch(
            mismatches,
            year,
            "net_pnl",
            expected.net_pnl,
            actual.get("net_pnl"),
            tolerance=pnl_tolerance,
        )
        _append_mismatch(
            mismatches,
            year,
            "profit_factor",
            expected.profit_factor,
            actual.get("profit_factor"),
            tolerance=metric_tolerance,
        )
        _append_mismatch(
            mismatches,
            year,
            "max_drawdown",
            expected.max_drawdown,
            actual.get("max_drawdown"),
            tolerance=pnl_tolerance,
        )
    return {
        "preset": preset,
        "source_label": spec.source_label,
        "checked_years": sorted(expected_by_year),
        "passed": not mismatches,
        "mismatches": mismatches,
    }


def _append_mismatch(
    mismatches: list[dict],
    year: int,
    field: str,
    expected: int | float,
    actual: object,
    *,
    tolerance: float = 0.0,
) -> None:
    if actual is None:
        mismatches.append({"year": year, "field": field, "expected": expected, "actual": None})
        return
    if isinstance(expected, int):
        if int(actual) != expected:
            mismatches.append({"year": year, "field": field, "expected": expected, "actual": actual})
        return
    if abs(float(actual) - expected) > tolerance:
        mismatches.append({"year": year, "field": field, "expected": expected, "actual": actual})
