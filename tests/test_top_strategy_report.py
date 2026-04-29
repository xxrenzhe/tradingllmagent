from __future__ import annotations

import unittest
from datetime import datetime

from tlm.top_strategy_report import (
    _aligned_equity_curves,
    _benchmark_metrics,
    _benchmark_period_results,
    _candlestick_svg,
    _merge_period_results,
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

    def test_select_top_yearly_strategies_can_rank_by_annualized_net_pnl(self) -> None:
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
                    "basket_id": "slow_big",
                    "basket_hash": "a",
                    "yearly_profitable_candidates": [
                        {
                            "selection_rule": "all_edges",
                            "activation_start_year": 2019,
                            "constituent_edges": [edge],
                            "train_period": {"covered_days": 1000},
                            "test_period": {"covered_days": 1000},
                            "full_after_activation": {"net_pnl": 1000, "annual_trades": 1200, "profit_factor": 1.2},
                            "test": {"net_pnl": 500, "profit_factor": 1.1},
                        }
                    ],
                },
                {
                    "basket_id": "fast_smaller",
                    "basket_hash": "b",
                    "yearly_profitable_candidates": [
                        {
                            "selection_rule": "fast_edges",
                            "activation_start_year": 2023,
                            "constituent_edges": [{**edge, "dow": 2}],
                            "train_period": {"covered_days": 100},
                            "test_period": {"covered_days": 100},
                            "full_after_activation": {"net_pnl": 800, "annual_trades": 1200, "profit_factor": 1.1},
                            "test": {"net_pnl": 400, "profit_factor": 1.0},
                        }
                    ],
                },
            ]
        }

        selected = _select_top_yearly_strategies(report, top_n=2, objective="annualized_net_pnl")

        self.assertEqual(selected[0]["basket_id"], "fast_smaller")
        self.assertEqual(selected[1]["basket_id"], "slow_big")

    def test_select_top_yearly_strategies_can_rank_by_annualized_quality(self) -> None:
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
                    "basket_id": "fails_gates",
                    "basket_hash": "a",
                    "yearly_profitable_candidates": [
                        {
                            "selection_rule": "all_edges",
                            "activation_start_year": 2019,
                            "constituent_edges": [edge],
                            "train_period": {"covered_days": 500},
                            "test_period": {"covered_days": 500},
                            "full_after_activation": {
                                "net_pnl": 1500,
                                "annual_trades": 1500,
                                "profit_factor": 1.10,
                                "win_probability": 0.52,
                                "return_to_drawdown": 0.9,
                                "cost_stress": [{"label": "configured_cost_plus_2_ticks", "net_pnl": -50}],
                                "yearly_results": [{"year": 2024, "net_pnl": 100}, {"year": 2025, "net_pnl": -50}],
                            },
                            "test": {"net_pnl": 400, "profit_factor": 1.0},
                        }
                    ],
                },
                {
                    "basket_id": "passes_gates",
                    "basket_hash": "b",
                    "yearly_profitable_candidates": [
                        {
                            "selection_rule": "quality_edges",
                            "activation_start_year": 2020,
                            "constituent_edges": [{**edge, "dow": 2}],
                            "train_period": {"covered_days": 500},
                            "test_period": {"covered_days": 500},
                            "full_after_activation": {
                                "net_pnl": 1200,
                                "annual_trades": 1600,
                                "profit_factor": 1.20,
                                "win_probability": 0.55,
                                "return_to_drawdown": 1.5,
                                "cost_stress": [{"label": "configured_cost_plus_2_ticks", "net_pnl": 100}],
                                "yearly_results": [{"year": 2024, "net_pnl": 100}, {"year": 2025, "net_pnl": 150}],
                            },
                            "test": {"net_pnl": 500, "profit_factor": 1.1},
                        }
                    ],
                },
            ]
        }

        selected = _select_top_yearly_strategies(report, top_n=2, objective="annualized_quality")

        self.assertEqual(selected[0]["basket_id"], "passes_gates")
        self.assertTrue(selected[0]["evaluation_summary"]["fully_qualified"])
        self.assertFalse(selected[1]["evaluation_summary"]["fully_qualified"])

    def test_select_top_yearly_strategies_can_merge_multiple_reports(self) -> None:
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
        report_1m = {
            "_source_report_path": "experiments/profit_mining/nq_cme_1m_ohlcv_strategy_mining_report.json",
            "symbol": "NQ_CME",
            "timeframe": "1m",
            "date_from": "2010-01-01",
            "date_to": "2026-01-01",
            "regime_basket_replays": [
                {
                    "basket_id": "one_minute",
                    "basket_hash": "a",
                    "yearly_profitable_candidates": [
                        {
                            "selection_rule": "all_edges",
                            "activation_start_year": 2019,
                            "constituent_edges": [edge],
                            "train_period": {"covered_days": 100},
                            "test_period": {"covered_days": 100},
                            "full_after_activation": {"net_pnl": 900, "annual_trades": 1200, "profit_factor": 1.2},
                            "test": {"net_pnl": 400, "profit_factor": 1.1},
                        }
                    ],
                }
            ],
        }
        report_5m = {
            "_source_report_path": "experiments/profit_mining/nq_cme_5m_ohlcv_strategy_mining_report.json",
            "symbol": "NQ_CME",
            "timeframe": "5m",
            "date_from": "2010-01-01",
            "date_to": "2026-01-01",
            "regime_basket_replays": [
                {
                    "basket_id": "five_minute",
                    "basket_hash": "b",
                    "yearly_profitable_candidates": [
                        {
                            "selection_rule": "all_edges",
                            "activation_start_year": 2020,
                            "constituent_edges": [{**edge, "dow": 2}],
                            "train_period": {"covered_days": 100},
                            "test_period": {"covered_days": 100},
                            "full_after_activation": {"net_pnl": 1200, "annual_trades": 1200, "profit_factor": 1.2},
                            "test": {"net_pnl": 600, "profit_factor": 1.1},
                        }
                    ],
                }
            ],
        }

        selected = _select_top_yearly_strategies([report_1m, report_5m], top_n=2, objective="annualized_net_pnl")

        self.assertEqual(selected[0]["source_timeframe"], "5m")
        self.assertEqual(selected[1]["source_timeframe"], "1m")

    def test_select_top_yearly_strategies_can_rank_by_stability_first(self) -> None:
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
                    "basket_id": "higher_annualized_less_stable",
                    "basket_hash": "a",
                    "yearly_profitable_candidates": [
                        {
                            "selection_rule": "fast_edges",
                            "activation_start_year": 2022,
                            "constituent_edges": [edge],
                            "train_period": {"covered_days": 200},
                            "test_period": {"covered_days": 200},
                            "full_after_activation": {
                                "net_pnl": 1500,
                                "annual_trades": 1500,
                                "profit_factor": 1.18,
                                "win_probability": 0.55,
                                "return_to_drawdown": 1.2,
                                "max_drawdown": 200,
                                "cost_stress": [{"label": "configured_cost_plus_2_ticks", "net_pnl": 100}],
                                "yearly_results": [{"year": 2024, "net_pnl": 100}, {"year": 2025, "net_pnl": -20}],
                            },
                            "test": {"net_pnl": 400, "profit_factor": 1.08},
                        }
                    ],
                },
                {
                    "basket_id": "slightly_lower_annualized_more_stable",
                    "basket_hash": "b",
                    "yearly_profitable_candidates": [
                        {
                            "selection_rule": "stable_edges",
                            "activation_start_year": 2021,
                            "constituent_edges": [{**edge, "dow": 2}],
                            "train_period": {"covered_days": 300},
                            "test_period": {"covered_days": 300},
                            "full_after_activation": {
                                "net_pnl": 1400,
                                "annual_trades": 1600,
                                "profit_factor": 1.22,
                                "win_probability": 0.56,
                                "return_to_drawdown": 2.0,
                                "max_drawdown": 120,
                                "cost_stress": [{"label": "configured_cost_plus_2_ticks", "net_pnl": 120}],
                                "yearly_results": [{"year": 2024, "net_pnl": 100}, {"year": 2025, "net_pnl": 80}],
                            },
                            "test": {"net_pnl": 380, "profit_factor": 1.12},
                        }
                    ],
                },
            ]
        }

        selected = _select_top_yearly_strategies(report, top_n=2, objective="stability_first")

        self.assertEqual(selected[0]["basket_id"], "slightly_lower_annualized_more_stable")

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

    def test_benchmark_metrics_and_excess_merge(self) -> None:
        curve = [
            {"timestamp": "2025-01-02 10:00:00", "close": 100.0, "equity": 0.0, "pnl": 0.0},
            {"timestamp": "2025-01-03 10:00:00", "close": 110.0, "equity": 200.0, "pnl": 200.0},
            {"timestamp": "2025-01-04 10:00:00", "close": 105.0, "equity": 100.0, "pnl": 100.0},
        ]
        metrics = _benchmark_metrics(curve)
        merged = _merge_period_results(
            [{"period": "2025-01", "net_pnl": 150.0, "profit_factor": 1.2, "win_probability": 0.6}],
            [{"period": "2025-01", "net_pnl": 100.0}],
            "period",
        )

        self.assertEqual(metrics["net_pnl"], 100.0)
        self.assertGreater(metrics["annualized_net_pnl"], 0.0)
        self.assertEqual(metrics["max_drawdown"], 100.0)
        self.assertEqual(merged[0]["excess_net_pnl"], 50.0)

    def test_benchmark_period_results_use_equity_boundaries(self) -> None:
        curve = [
            {"timestamp": "2025-01-31 23:59:00", "close": 100.0, "equity": 0.0, "pnl": 0.0},
            {"timestamp": "2025-02-01 00:00:00", "close": 110.0, "equity": 200.0, "pnl": 200.0},
            {"timestamp": "2025-02-28 23:59:00", "close": 120.0, "equity": 400.0, "pnl": 400.0},
            {"timestamp": "2025-03-01 00:00:00", "close": 115.0, "equity": 300.0, "pnl": 300.0},
            {"timestamp": "2025-03-31 23:59:00", "close": 130.0, "equity": 600.0, "pnl": 600.0},
        ]

        monthly = _benchmark_period_results(curve, "month", 20.0)

        self.assertEqual([row["net_pnl"] for row in monthly], [0.0, 400.0, 200.0])
        self.assertEqual(sum(row["net_pnl"] for row in monthly), 600.0)

    def test_aligned_equity_curves_uses_latest_benchmark_value(self) -> None:
        aligned = _aligned_equity_curves(
            [
                {"timestamp": "2025-01-01 09:31:00", "equity": 10.0},
                {"timestamp": "2025-01-01 09:33:00", "equity": 20.0},
                {"timestamp": "2025-01-01 09:35:00", "equity": 40.0},
            ],
            [
                {"timestamp": "2025-01-01 09:30:00", "equity": 1.0},
                {"timestamp": "2025-01-01 09:32:00", "equity": 2.0},
                {"timestamp": "2025-01-01 09:34:00", "equity": 3.0},
            ],
            max_points=10,
        )

        self.assertEqual([row["benchmark"] for row in aligned], [1.0, 2.0, 3.0])
        self.assertEqual([row["strategy"] for row in aligned], [10.0, 20.0, 40.0])

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
