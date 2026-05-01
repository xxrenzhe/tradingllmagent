from __future__ import annotations

from collections import Counter
import os
import unittest

from tlm.api import _ibkr_default_strategy
from tlm.expanded_high_edge import (
    EXPANDED_HIGH_EDGE_NETMAX_EDGES,
    EXPANDED_HIGH_EDGE_NETMAX_PRESET,
    EXPANDED_HIGH_EDGE_PRESETS,
    EXPANDED_HIGH_EDGE_WF_ORB_2026_EDGES,
    EXPANDED_HIGH_EDGE_WF_ORB_2026_PRESET,
    check_expanded_high_edge_replay,
    expanded_high_edge_preset_spec,
    expanded_high_edge_yearly_gate_report,
)


class ExpandedHighEdgePresetTests(unittest.TestCase):
    def test_netmax_preset_is_registered_with_replay_parameters(self) -> None:
        spec = expanded_high_edge_preset_spec(EXPANDED_HIGH_EDGE_NETMAX_PRESET)

        self.assertEqual(spec.source_label, "positive_expanded_edge_top_32")
        self.assertEqual(spec.max_concurrent_positions, 99)
        self.assertEqual(spec.max_hold_minutes, 120)
        self.assertEqual(spec.stop_range_multiple, 6.0)
        self.assertEqual(len(spec.edges), 32)
        self.assertIs(EXPANDED_HIGH_EDGE_PRESETS[EXPANDED_HIGH_EDGE_NETMAX_PRESET], EXPANDED_HIGH_EDGE_NETMAX_EDGES)

    def test_netmax_preset_matches_expected_edge_family_mix(self) -> None:
        scan_types = Counter(edge.scan_type for edge in EXPANDED_HIGH_EDGE_NETMAX_EDGES)
        sessions = Counter(edge.session_bucket for edge in EXPANDED_HIGH_EDGE_NETMAX_EDGES)
        take_profit = Counter(edge.take_profit_r for edge in EXPANDED_HIGH_EDGE_NETMAX_EDGES)

        self.assertEqual(
            dict(sorted(scan_types.items())),
            {
                "opening_range_breakout": 13,
                "prior_day_breakout": 12,
                "range_expansion_continuation": 1,
                "selling_absorption_reversal": 1,
                "session_extreme_reversion": 1,
                "trend_pullback_reclaim": 1,
                "vwap_pullback_bounce": 2,
                "vwap_reclaim_continuation": 1,
            },
        )
        self.assertEqual(dict(sorted(sessions.items())), {"ny_0930_1159": 17, "ny_1200_1559": 15})
        self.assertEqual(dict(sorted(take_profit.items())), {1.0: 4, 1.25: 8, 1.5: 20})

    def test_walk_forward_orb_2026_preset_is_registered_with_replay_parameters(self) -> None:
        spec = expanded_high_edge_preset_spec(EXPANDED_HIGH_EDGE_WF_ORB_2026_PRESET)

        self.assertEqual(spec.source_label, "walk_forward_no_prior_highvol_donchian_min13_2026")
        self.assertEqual(spec.max_concurrent_positions, 6)
        self.assertEqual(spec.max_hold_minutes, 120)
        self.assertEqual(spec.stop_range_multiple, 6.0)
        self.assertEqual(len(spec.edges), 13)
        self.assertIs(EXPANDED_HIGH_EDGE_PRESETS[EXPANDED_HIGH_EDGE_WF_ORB_2026_PRESET], EXPANDED_HIGH_EDGE_WF_ORB_2026_EDGES)

    def test_walk_forward_orb_2026_preset_excludes_failed_families(self) -> None:
        scan_types = Counter(edge.scan_type for edge in EXPANDED_HIGH_EDGE_WF_ORB_2026_EDGES)
        sessions = Counter(edge.session_bucket for edge in EXPANDED_HIGH_EDGE_WF_ORB_2026_EDGES)
        take_profit = Counter(edge.take_profit_r for edge in EXPANDED_HIGH_EDGE_WF_ORB_2026_EDGES)

        self.assertEqual(
            dict(sorted(scan_types.items())),
            {
                "opening_range_breakout": 10,
                "opening_range_retest_reclaim": 1,
                "range_expansion_continuation": 1,
                "vwap_pullback_bounce": 1,
            },
        )
        self.assertNotIn("prior_day_breakout", scan_types)
        self.assertNotIn("high_volume_impulse_continuation", scan_types)
        self.assertNotIn("donchian20_breakout", scan_types)
        self.assertEqual(dict(sorted(sessions.items())), {"ny_0930_1159": 8, "ny_1200_1559": 5})
        self.assertEqual(dict(sorted(take_profit.items())), {1.0: 1, 1.25: 2, 1.5: 10})

    def test_netmax_yearly_gate_snapshot_passes_hard_constraints(self) -> None:
        report = expanded_high_edge_yearly_gate_report(EXPANDED_HIGH_EDGE_NETMAX_PRESET)

        self.assertTrue(report["trade_floor_pass"])
        self.assertTrue(report["positive_years_pass"])
        self.assertEqual(report["full_year_min_trades"], 5498)
        self.assertEqual(report["positive_years"], 8)
        self.assertEqual(report["worst_year_pnl"], 17623.75)
        self.assertEqual(report["net_pnl"], 5804050.0)

    def test_netmax_replay_check_detects_yearly_drift(self) -> None:
        spec = expanded_high_edge_preset_spec(EXPANDED_HIGH_EDGE_NETMAX_PRESET)
        replay_result = {"yearly_results": [row.__dict__ for row in spec.yearly_results]}

        self.assertTrue(check_expanded_high_edge_replay(EXPANDED_HIGH_EDGE_NETMAX_PRESET, replay_result)["passed"])

        drifted = {"yearly_results": [dict(row) for row in replay_result["yearly_results"]]}
        drifted["yearly_results"][0]["net_pnl"] -= 0.02

        check = check_expanded_high_edge_replay(EXPANDED_HIGH_EDGE_NETMAX_PRESET, drifted)
        self.assertFalse(check["passed"])
        self.assertEqual(check["mismatches"][0]["field"], "net_pnl")

    def test_ibkr_default_strategy_uses_netmax_params_when_requested(self) -> None:
        original = os.environ.get("TLM_IBKR_EXPANDED_HIGH_EDGE_PRESET")
        os.environ["TLM_IBKR_EXPANDED_HIGH_EDGE_PRESET"] = EXPANDED_HIGH_EDGE_NETMAX_PRESET
        try:
            strategy = _ibkr_default_strategy("MNQ")
        finally:
            if original is None:
                os.environ.pop("TLM_IBKR_EXPANDED_HIGH_EDGE_PRESET", None)
            else:
                os.environ["TLM_IBKR_EXPANDED_HIGH_EDGE_PRESET"] = original

        self.assertEqual(strategy["preset"], EXPANDED_HIGH_EDGE_NETMAX_PRESET)
        self.assertEqual(strategy["max_concurrent_positions"], 99)
        self.assertEqual(strategy["max_holding_minutes"], 120)
        self.assertEqual(strategy["stop_range_multiple"], 6.0)

    def test_ibkr_default_strategy_uses_walk_forward_orb_params_when_requested(self) -> None:
        original = os.environ.get("TLM_IBKR_EXPANDED_HIGH_EDGE_PRESET")
        os.environ["TLM_IBKR_EXPANDED_HIGH_EDGE_PRESET"] = EXPANDED_HIGH_EDGE_WF_ORB_2026_PRESET
        try:
            strategy = _ibkr_default_strategy("MNQ")
        finally:
            if original is None:
                os.environ.pop("TLM_IBKR_EXPANDED_HIGH_EDGE_PRESET", None)
            else:
                os.environ["TLM_IBKR_EXPANDED_HIGH_EDGE_PRESET"] = original

        self.assertEqual(strategy["preset"], EXPANDED_HIGH_EDGE_WF_ORB_2026_PRESET)
        self.assertEqual(strategy["max_concurrent_positions"], 6)
        self.assertEqual(strategy["max_holding_minutes"], 120)
        self.assertEqual(strategy["stop_range_multiple"], 6.0)


if __name__ == "__main__":
    unittest.main()
