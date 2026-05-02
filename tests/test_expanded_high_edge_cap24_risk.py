from __future__ import annotations

import unittest

from tlm.expanded_high_edge import (
    EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_PRESET,
    EXPANDED_HIGH_EDGE_CAP24_PRESET,
    EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_PRESET,
    expanded_high_edge_preset_spec,
    expanded_high_edge_yearly_gate_report,
)


class ExpandedHighEdgeCap24RiskPresetTests(unittest.TestCase):
    def test_return_guard_registers_cap24_edges_with_wider_stop(self) -> None:
        baseline = expanded_high_edge_preset_spec(EXPANDED_HIGH_EDGE_CAP24_PRESET)
        spec = expanded_high_edge_preset_spec(EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_PRESET)

        self.assertIs(spec.edges, baseline.edges)
        self.assertEqual(spec.max_concurrent_positions, 24)
        self.assertEqual(spec.max_hold_minutes, 300)
        self.assertEqual(spec.stop_range_multiple, 8.0)

    def test_balanced_risk_registers_cap24_edges_with_lower_concurrency(self) -> None:
        baseline = expanded_high_edge_preset_spec(EXPANDED_HIGH_EDGE_CAP24_PRESET)
        spec = expanded_high_edge_preset_spec(EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_PRESET)

        self.assertIs(spec.edges, baseline.edges)
        self.assertEqual(spec.max_concurrent_positions, 18)
        self.assertEqual(spec.max_hold_minutes, 300)
        self.assertEqual(spec.stop_range_multiple, 8.0)

    def test_cap24_risk_presets_keep_yearly_gates_positive(self) -> None:
        return_guard = expanded_high_edge_yearly_gate_report(EXPANDED_HIGH_EDGE_CAP24_RETURN_GUARD_PRESET)
        balanced = expanded_high_edge_yearly_gate_report(EXPANDED_HIGH_EDGE_CAP24_BALANCED_RISK_PRESET)

        self.assertTrue(return_guard["trade_floor_pass"])
        self.assertTrue(return_guard["positive_years_pass"])
        self.assertEqual(return_guard["net_pnl"], 5475715.0)
        self.assertTrue(balanced["trade_floor_pass"])
        self.assertTrue(balanced["positive_years_pass"])
        self.assertEqual(balanced["net_pnl"], 4511341.25)


if __name__ == "__main__":
    unittest.main()
