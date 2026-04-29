from __future__ import annotations

import unittest

from tlm.ibkr_optimizer import apply_fast_path_control_diff


class IbkrOptimizerTests(unittest.TestCase):
    def test_applies_only_risk_reducing_fast_path_changes(self) -> None:
        state = {
            "min_confidence": 0.55,
            "max_spread_ticks": 3.0,
            "daily_trade_cap": 6,
            "mode": "paper",
            "safe_mode": False,
            "strategies": {"s1": {"enabled": True, "weight": 1.0}},
            "trade_session": {"start": "09:30", "end": "15:55"},
        }
        review = {
            "action": "paper_block",
            "confidence": "medium",
            "fast_path_control_diff": {
                "auto_apply": True,
                "changes": [
                    {"change": "pause_strategy", "strategy_id": "s1"},
                    {"change": "demote_strategy", "strategy_id": "s1", "weight": 0.4},
                    {"change": "raise_min_confidence", "value": 0.65},
                    {"change": "tighten_max_spread_ticks", "value": 2.0},
                    {"change": "reduce_daily_trade_cap", "value": 3},
                    {"change": "narrow_session", "start": "10:00", "end": "15:00"},
                    {"change": "observe_only"},
                ],
            },
            "mutation_proposal": {},
        }

        report = apply_fast_path_control_diff(state, review)
        next_state = report["control_state"]

        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["rejected"], [])
        self.assertFalse(next_state["strategies"]["s1"]["enabled"])
        self.assertEqual(next_state["strategies"]["s1"]["weight"], 0.4)
        self.assertEqual(next_state["min_confidence"], 0.65)
        self.assertEqual(next_state["max_spread_ticks"], 2.0)
        self.assertEqual(next_state["daily_trade_cap"], 3)
        self.assertEqual(next_state["trade_session"], {"start": "10:00", "end": "15:00"})
        self.assertEqual(next_state["mode"], "observe_only")
        self.assertTrue(state["strategies"]["s1"]["enabled"])

    def test_rejects_risk_increasing_values(self) -> None:
        state = {
            "min_confidence": 0.7,
            "max_spread_ticks": 2.0,
            "daily_trade_cap": 3,
            "strategies": {"s1": {"weight": 0.5}},
            "trade_session": {"start": "10:00", "end": "15:00"},
        }
        review = {
            "action": "observe",
            "confidence": "medium",
            "fast_path_control_diff": {
                "auto_apply": True,
                "changes": [
                    {"change": "demote_strategy", "strategy_id": "s1", "weight": 0.8},
                    {"change": "raise_min_confidence", "value": 0.6},
                    {"change": "tighten_max_spread_ticks", "value": 4.0},
                    {"change": "reduce_daily_trade_cap", "value": 5},
                    {"change": "narrow_session", "start": "09:30", "end": "15:30"},
                ],
            },
            "mutation_proposal": {},
        }

        report = apply_fast_path_control_diff(state, review)

        self.assertEqual(report["status"], "rejected")
        self.assertEqual(len(report["applied"]), 0)
        self.assertEqual(len(report["rejected"]), 5)
        self.assertEqual(report["control_state"], state)

    def test_no_auto_apply_keeps_state_unchanged(self) -> None:
        state = {"mode": "paper", "min_confidence": 0.5}
        review = {
            "action": "observe",
            "confidence": "medium",
            "fast_path_control_diff": {
                "auto_apply": False,
                "changes": [{"change": "observe_only"}],
            },
            "mutation_proposal": {},
        }

        report = apply_fast_path_control_diff(state, review)

        self.assertEqual(report["status"], "no_change")
        self.assertEqual(report["control_state"], state)


if __name__ == "__main__":
    unittest.main()
