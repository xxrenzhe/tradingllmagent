from __future__ import annotations

import unittest

from tlm.ibkr_review import (
    build_five_minute_review_request,
    deterministic_fallback_review,
    validate_review_result,
)


class IbkrReviewTests(unittest.TestCase):
    def test_five_minute_review_allows_paper_plan_for_strong_signal(self) -> None:
        request = build_five_minute_review_request(
            bars_1m=[{"symbol": "MNQ", "close": 19000 + index} for index in range(5)],
            signals=[
                {
                    "signal_class": "strong_review",
                    "symbol": "MNQ",
                    "side": "BUY",
                    "bar_1m": {"close": 19005.0},
                    "trigger_reasons": ["range_breakout_up"],
                }
            ],
            execution_ledger={"positions": [], "fill_count": 0, "net_realized_pnl": 0, "total_commission": 0},
            strategy_state={"stop_loss_ticks": 20, "take_profit_ticks": 40, "max_holding_minutes": 15},
        )
        result = deterministic_fallback_review(request)

        self.assertEqual(request["review_window"], "5m")
        self.assertEqual(request["strong_signal_count"], 1)
        self.assertEqual(result["action"], "paper_allow")
        self.assertEqual(result["paper_plan"]["action"], "BUY")
        self.assertEqual(result["paper_plan"]["stop_price"], 19000.0)
        self.assertEqual(result["paper_plan"]["take_profit_price"], 19015.0)
        self.assertIn("review_result_hash", result)

    def test_five_minute_review_blocks_on_stale_data_and_auto_applies_only_risk_reduction(self) -> None:
        request = build_five_minute_review_request(
            bars_1m=[],
            signals=[{"signal_class": "strong_review", "symbol": "MNQ", "side": "BUY", "bar_1m": {"close": 1}}],
            execution_ledger={"positions": [], "fill_count": 0, "net_realized_pnl": 0, "total_commission": 0},
            risk_context={"data_stale": True},
        )
        result = deterministic_fallback_review(request)

        self.assertEqual(result["action"], "paper_block")
        self.assertEqual(result["fast_path_control_diff"]["changes"][0]["change"], "safe_mode")
        self.assertIn("data_stale", result["risk_review"]["blocked_reasons"])

    def test_five_minute_review_blocks_when_concurrency_cap_is_reached(self) -> None:
        request = build_five_minute_review_request(
            bars_1m=[{"symbol": "MNQ", "close": 19000}],
            signals=[
                {
                    "signal_class": "strong_review",
                    "symbol": "MNQ",
                    "side": "BUY",
                    "bar_1m": {"close": 19000.0},
                    "trigger_reasons": ["expanded_high_edge:prior_day_breakout"],
                }
            ],
            execution_ledger={"positions": [{"symbol": "MNQ", "quantity": 20}], "fill_count": 0},
            risk_context={"open_bracket_order_count": 4},
            strategy_state={"max_concurrent_positions": 24},
        )
        result = deterministic_fallback_review(request)

        self.assertEqual(result["action"], "paper_block")
        self.assertIn("max_concurrent_positions_reached", result["risk_review"]["blocked_reasons"])

    def test_validate_review_result_rejects_forbidden_changes_and_order_objects(self) -> None:
        with self.assertRaisesRegex(ValueError, "fast path change is not allowed"):
            validate_review_result(
                {
                    "action": "observe",
                    "confidence": "medium",
                    "fast_path_control_diff": {"changes": [{"change": "lower_min_confidence"}]},
                    "mutation_proposal": {},
                }
            )
        with self.assertRaisesRegex(ValueError, "IBKR order objects"):
            validate_review_result(
                {
                    "action": "observe",
                    "confidence": "medium",
                    "fast_path_control_diff": {"changes": []},
                    "mutation_proposal": {},
                    "ibkr_order_object": {"orderType": "MKT"},
                }
            )

    def test_previous_reviews_are_summarized_without_recursive_payloads(self) -> None:
        previous = {
            "review_request": {
                "created_at": "2026-04-30T14:00:00+00:00",
                "strong_signal_count": 1,
                "blocked_signal_count": 0,
                "previous_review_summaries": [{"nested": {"review_request": {"large": "payload"}}}],
                "review_request_hash": "request-hash",
            },
            "review_result": {
                "action": "paper_allow",
                "confidence": "low",
                "final_summary": {"decision_reason": "paper_allow", "next_check": "next_5m_close"},
                "created_at": "2026-04-30T14:00:01+00:00",
                "review_result_hash": "result-hash",
            },
        }

        request = build_five_minute_review_request(
            bars_1m=[],
            signals=[],
            execution_ledger={"positions": [], "fill_count": 0, "net_realized_pnl": 0, "total_commission": 0},
            previous_reviews=[previous],
        )

        summaries = request["previous_review_summaries"]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["action"], "paper_allow")
        self.assertNotIn("review_request", summaries[0])
        self.assertNotIn("previous_review_summaries", summaries[0])


if __name__ == "__main__":
    unittest.main()
