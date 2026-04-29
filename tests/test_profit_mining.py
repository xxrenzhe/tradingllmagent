from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tlm.config import CostModelConfig, get_symbol
from tlm.profit_mining import (
    _adaptive_recent_regime_candidates,
    _horizon_spec_dicts,
    _optimized_regime_basket_subsets,
    _replay_metrics,
    mine_databento_nq_profitable_strategies,
)
from tlm.storage import bar_path, write_bars_parquet


class ProfitMiningTests(unittest.TestCase):
    def test_mine_databento_nq_profitable_strategies_finds_synthetic_edge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            start = datetime(2025, 1, 2, 0, 0)
            rows_by_day = {}
            for index in range(24 * 60 * 2):
                timestamp = start + timedelta(minutes=index)
                close = 100.0 + index * 0.25
                rows_by_day.setdefault(timestamp.date(), []).append(
                    (
                        "NQ_CME",
                        timestamp,
                        close - 0.25,
                        close + 0.25,
                        close - 0.25,
                        close,
                        close,
                        close,
                        100,
                        1.0,
                        1.0,
                        0.0,
                    )
                )
            for day, rows in rows_by_day.items():
                write_bars_parquet(bar_path(data_root, "NQ_CME", "1m", day), rows)
            output = root / "report.json"

            report = mine_databento_nq_profitable_strategies(
                data_root=data_root,
                symbol_config=get_symbol("NQ_CME"),
                cost_model=CostModelConfig(
                    name="zero_cost_test",
                    tick_size=0.25,
                    point_value=20.0,
                    tick_value=5.0,
                    slippage_ticks_per_side=0.0,
                    round_trip_fees_usd=0.0,
                ),
                date_from=start.date(),
                date_to=(start + timedelta(days=1)).date(),
                output_path=output,
            )

            self.assertEqual(report["status"], "target_found")
            self.assertGreater(report["summary"]["qualified_candidate_count"], 0)
            self.assertGreater(report["qualified_candidates"][0]["annual_trades"], 1000)
            self.assertGreater(report["qualified_candidates"][0]["win_probability"], 0.53)
            self.assertTrue(output.exists())

    def test_horizon_specs_preserve_minutes_on_higher_timeframe(self) -> None:
        context = {"timeframe_minutes": 5}

        self.assertEqual(
            _horizon_spec_dicts(context, "close"),
            [
                {"horizon_bars": 1, "horizon_minutes": 5},
                {"horizon_bars": 3, "horizon_minutes": 15},
                {"horizon_bars": 6, "horizon_minutes": 30},
                {"horizon_bars": 12, "horizon_minutes": 60},
                {"horizon_bars": 24, "horizon_minutes": 120},
            ],
        )

    def test_mine_databento_nq_profitable_strategies_supports_5m_horizons(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            start = datetime(2025, 1, 2, 0, 0)
            rows_by_day = {}
            for index in range(24 * 12 * 2):
                timestamp = start + timedelta(minutes=index * 5)
                close = 100.0 + index * 0.25
                rows_by_day.setdefault(timestamp.date(), []).append(
                    (
                        "NQ_CME",
                        timestamp,
                        close - 0.25,
                        close + 0.25,
                        close - 0.25,
                        close,
                        close,
                        close,
                        500,
                        1.0,
                        1.0,
                        0.0,
                    )
                )
            for day, rows in rows_by_day.items():
                write_bars_parquet(bar_path(data_root, "NQ_CME", "5m", day), rows)

            report = mine_databento_nq_profitable_strategies(
                data_root=data_root,
                symbol_config=get_symbol("NQ_CME"),
                cost_model=CostModelConfig(
                    name="zero_cost_test",
                    tick_size=0.25,
                    point_value=20.0,
                    tick_value=5.0,
                    slippage_ticks_per_side=0.0,
                    round_trip_fees_usd=0.0,
                ),
                date_from=start.date(),
                date_to=(start + timedelta(days=1)).date(),
                timeframe="5m",
            )

            self.assertEqual(report["timeframe"], "5m")
            self.assertEqual(report["timeframe_minutes"], 5)
            self.assertEqual(
                report["search_space"]["close_to_close_horizon_specs"][1],
                {"horizon_bars": 3, "horizon_minutes": 15},
            )
            self.assertGreater(report["summary"]["qualified_candidate_count"], 0)

    def test_optimized_regime_basket_subsets_keeps_qualified_subset(self) -> None:
        edges = [
            {
                "scan_type": "low_volume_drift",
                "horizon_minutes": 120,
                "session_bucket": "utc_1200_1659",
                "dow": 1,
                "direction_label": "long",
                "trend_bin": 1,
                "volume_bin": -1,
                "range_bin": -1,
                "break_even_cost_usd": 100,
                "cost_adjusted_win_probability": 0.75,
                "cost_adjusted_net_pnl": 290,
            },
            {
                "scan_type": "breakout_continuation",
                "horizon_minutes": 120,
                "session_bucket": "utc_1200_1659",
                "dow": 1,
                "direction_label": "long",
                "trend_bin": 1,
                "volume_bin": 2,
                "range_bin": 2,
                "break_even_cost_usd": -50,
                "cost_adjusted_win_probability": 0.25,
                "cost_adjusted_net_pnl": -200,
            },
        ]
        start = datetime(2025, 1, 2, 0, 0)
        signals = [
            {"timestamp": start + timedelta(minutes=offset), "rule_index": 0, "pnl": pnl}
            for offset, pnl in enumerate([100.0, 100.0, 100.0, -10.0])
        ]
        signals.extend(
            {"timestamp": start + timedelta(minutes=10 + offset), "rule_index": 1, "pnl": pnl}
            for offset, pnl in enumerate([-100.0, -100.0, 50.0, -50.0])
        )

        subsets = _optimized_regime_basket_subsets(
            edges,
            signals,
            day_count=1,
            context={"min_annual_trades": 1000, "min_win_probability": 0.53},
        )

        self.assertGreater(len(subsets), 0)
        self.assertEqual(subsets[0]["edge_indexes"], [0])
        self.assertGreater(subsets[0]["one_trade_per_timestamp"]["profit_factor"], 1)
        self.assertTrue(subsets[0]["one_trade_per_timestamp"]["target_qualified"])

    def test_replay_metrics_include_yearly_results(self) -> None:
        signals = [
            {"timestamp": datetime(2024, 12, 31, 23, 59), "rule_index": 0, "pnl": 100.0},
            {"timestamp": datetime(2025, 1, 2, 0, 0), "rule_index": 0, "pnl": -40.0},
            {"timestamp": datetime(2025, 1, 3, 0, 0), "rule_index": 0, "pnl": 80.0},
        ]

        metrics = _replay_metrics(
            signals,
            day_count=3,
            context={"min_annual_trades": 100, "min_win_probability": 0.53},
        )

        self.assertEqual([row["year"] for row in metrics["yearly_results"]], [2024, 2025])
        self.assertEqual(metrics["yearly_results"][0]["trades"], 1)
        self.assertEqual(metrics["yearly_results"][0]["period_from"], "2024-12-31")
        self.assertEqual(metrics["yearly_results"][0]["period_to"], "2024-12-31")
        self.assertEqual(metrics["yearly_results"][0]["covered_days"], 1)
        self.assertEqual(metrics["yearly_results"][1]["net_pnl"], 40.0)

    def test_adaptive_recent_regime_candidates_split_train_test(self) -> None:
        edges = [
            {
                "scan_type": "low_volume_drift",
                "horizon_minutes": 120,
                "session_bucket": "utc_1200_1659",
                "dow": 1,
                "direction_label": "long",
                "trend_bin": 1,
                "volume_bin": -1,
                "range_bin": -1,
                "break_even_cost_usd": 100,
                "cost_adjusted_win_probability": 0.75,
                "cost_adjusted_net_pnl": 1000,
            }
        ]
        signals = []
        for year in range(2020, 2027):
            for day in range(3):
                signals.append(
                    {
                        "timestamp": datetime(year, 1, day + 1, 12, 0),
                        "rule_index": 0,
                        "pnl": 100.0 if year >= 2021 else -20.0,
                    }
                )

        candidates = _adaptive_recent_regime_candidates(
            edges,
            signals,
            context={"min_annual_trades": 1, "min_win_probability": 0.53},
        )

        self.assertGreater(len(candidates), 0)
        self.assertLess(candidates[0]["activation_start_year"], 2024)
        self.assertEqual(candidates[0]["test_period"]["from"], "2024-01-01")
        self.assertGreater(candidates[0]["test"]["net_pnl"], 0)

    def test_report_includes_evaluation_periods(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            start = datetime(2025, 1, 2, 0, 0)
            rows_by_day = {}
            for index in range(24 * 60 * 2):
                timestamp = start + timedelta(minutes=index)
                close = 100.0 + index * 0.25
                rows_by_day.setdefault(timestamp.date(), []).append(
                    (
                        "NQ_CME",
                        timestamp,
                        close - 0.25,
                        close + 0.25,
                        close - 0.25,
                        close,
                        close,
                        close,
                        100,
                        1.0,
                        1.0,
                        0.0,
                    )
                )
            for day, rows in rows_by_day.items():
                write_bars_parquet(bar_path(data_root, "NQ_CME", "1m", day), rows)

            report = mine_databento_nq_profitable_strategies(
                data_root=data_root,
                symbol_config=get_symbol("NQ_CME"),
                cost_model=CostModelConfig(
                    name="zero_cost_test",
                    tick_size=0.25,
                    point_value=20.0,
                    tick_value=5.0,
                    slippage_ticks_per_side=0.0,
                    round_trip_fees_usd=0.0,
                ),
                date_from=start.date(),
                date_to=(start + timedelta(days=1)).date(),
            )

            self.assertEqual(report["evaluation_periods"]["full_backtest"]["from"], "2025-01-02")
            self.assertIn("walk_forward", report["evaluation_periods"])


if __name__ == "__main__":
    unittest.main()
