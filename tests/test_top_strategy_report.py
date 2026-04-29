from __future__ import annotations

import unittest
from datetime import datetime

from tlm.top_strategy_report import (
    _candlestick_svg,
    _monthly_signal_results,
    _select_top_yearly_strategies,
)


class TopStrategyReportTests(unittest.TestCase):
    def test_select_top_yearly_strategies_dedupes_by_edge_composition(self) -> None:
        edge = {
            "scan_type": "low_volume_drift",
            "horizon_minutes": 120,
            "session_bucket": "utc_1200_1659",
            "dow": 1,
            "direction_label": "long",
            "trend_bin": 1,
            "volume_bin": -1,
            "range_bin": -1,
        }
        report = {
            "regime_basket_replays": [
                {
                    "basket_id": "basket_a",
                    "basket_hash": "a",
                    "yearly_profitable_candidates": [
                        {
                            "selection_rule": "all_edges",
                            "activation_start_year": 2019,
                            "constituent_edges": [edge],
                            "full_after_activation": {"net_pnl": 1000, "annual_trades": 1200},
                            "test": {"net_pnl": 500},
                        }
                    ],
                },
                {
                    "basket_id": "basket_b",
                    "basket_hash": "b",
                    "yearly_profitable_candidates": [
                        {
                            "selection_rule": "all_edges",
                            "activation_start_year": 2020,
                            "constituent_edges": [edge],
                            "full_after_activation": {"net_pnl": 900, "annual_trades": 1200},
                            "test": {"net_pnl": 600},
                        },
                        {
                            "selection_rule": "top_net_pnl",
                            "activation_start_year": 2019,
                            "constituent_edges": [{**edge, "dow": 2}],
                            "full_after_activation": {"net_pnl": 800, "annual_trades": 1200},
                            "test": {"net_pnl": 400},
                        },
                    ],
                },
            ]
        }

        selected = _select_top_yearly_strategies(report, top_n=3)

        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[0]["basket_id"], "basket_a")
        self.assertEqual(selected[1]["selection_rule"], "top_net_pnl")

    def test_select_top_yearly_strategies_can_rank_by_profit_factor(self) -> None:
        edge = {
            "scan_type": "low_volume_drift",
            "horizon_minutes": 120,
            "session_bucket": "utc_1200_1659",
            "dow": 1,
            "direction_label": "long",
            "trend_bin": 1,
            "volume_bin": -1,
            "range_bin": -1,
        }
        report = {
            "regime_basket_replays": [
                {
                    "basket_id": "high_net",
                    "basket_hash": "a",
                    "yearly_profitable_candidates": [
                        {
                            "selection_rule": "all_edges",
                            "activation_start_year": 2019,
                            "constituent_edges": [edge],
                            "full_after_activation": {"net_pnl": 1000, "annual_trades": 1200, "profit_factor": 1.2},
                            "test": {"net_pnl": 500, "profit_factor": 1.1},
                        }
                    ],
                },
                {
                    "basket_id": "high_pf",
                    "basket_hash": "b",
                    "yearly_profitable_candidates": [
                        {
                            "selection_rule": "pf_edges",
                            "activation_start_year": 2020,
                            "constituent_edges": [{**edge, "dow": 2}],
                            "full_after_activation": {"net_pnl": 800, "annual_trades": 1200, "profit_factor": 1.5},
                            "test": {"net_pnl": 400, "profit_factor": 1.4},
                        }
                    ],
                },
            ]
        }

        selected = _select_top_yearly_strategies(report, top_n=2, objective="profit_factor")

        self.assertEqual(selected[0]["basket_id"], "high_pf")
        self.assertEqual(selected[1]["basket_id"], "high_net")

    def test_monthly_signal_results_groups_by_calendar_month(self) -> None:
        rows = _monthly_signal_results(
            [
                {"timestamp": datetime(2025, 1, 2, 10), "pnl": 20},
                {"timestamp": datetime(2025, 1, 3, 10), "pnl": -5},
                {"timestamp": datetime(2025, 2, 3, 10), "pnl": 15},
            ]
        )

        self.assertEqual([row["period"] for row in rows], ["2025-01", "2025-02"])
        self.assertEqual(rows[0]["net_pnl"], 15)
        self.assertEqual(rows[1]["trades"], 1)

    def test_candlestick_svg_contains_entry_and_exit_markers(self) -> None:
        bars = [
            {"timestamp": "2025-01-02 10:00:00", "open": 100, "high": 102, "low": 99, "close": 101},
            {"timestamp": "2025-01-02 10:01:00", "open": 101, "high": 103, "low": 100, "close": 102},
        ]
        svg = _candlestick_svg(
            bars,
            {
                "timestamp": datetime(2025, 1, 2, 10, 0),
                "exit_timestamp": datetime(2025, 1, 2, 10, 1),
                "entry_price": 101,
                "exit_price": 102,
            },
        )

        self.assertIn("ENTRY", svg)
        self.assertIn("EXIT", svg)


if __name__ == "__main__":
    unittest.main()
