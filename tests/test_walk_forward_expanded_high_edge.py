from __future__ import annotations

from datetime import datetime, timedelta
import unittest

from scripts.walk_forward_expanded_high_edge import (
    build_walk_forward_specs,
    cached_single_replay,
    hard_gate_report_for_result,
    parse_float_grid,
    parse_scan_type_counts,
    summarize_walk_forward,
)
from scripts.cached_walk_forward_expanded_high_edge import (
    leaderboard_sort_key,
    parse_int_grid,
    parse_text_grid,
)
from scripts.search_expanded_high_edge_strategy import CandidateStats
from scripts.search_expanded_high_edge_cooldown import (
    parse_int_grid as parse_cooldown_int_grid,
    replay_with_regime_cooldown,
    signal_regime_key,
)
from tlm.low_r_regime_basket import LowRRegimeBasketConfig, RegimeEdge


def _single(candidate_id: str, scan_type: str, avg_pnl: float) -> dict:
    return {
        "source_candidate_id": candidate_id,
        "avg_hold_bars": 10,
        "constraints": {
            "positive_years": 6,
            "full_year_min_trades": 100,
        },
        "edges": [{"scan_type": scan_type}],
        "metrics": {
            "avg_trade_net_pnl": avg_pnl,
            "net_pnl": avg_pnl * 100,
            "profit_factor": 1.2,
        },
    }


class WalkForwardExpandedHighEdgeTests(unittest.TestCase):
    def test_parse_scan_type_counts(self) -> None:
        self.assertEqual(
            parse_scan_type_counts(("prior_day_breakout=2", "donchian20_breakout=0"), option_name="--x"),
            {"prior_day_breakout": 2, "donchian20_breakout": 0},
        )

    def test_parse_float_grid(self) -> None:
        self.assertEqual(parse_float_grid("1.25, 1.5", option_name="--tp"), (1.25, 1.5))

    def test_build_specs_respects_scan_type_caps_and_minimums(self) -> None:
        singles = [
            _single("prior-1", "prior_day_breakout", 10),
            _single("prior-2", "prior_day_breakout", 9),
            _single("orb-1", "opening_range_breakout", 8),
            _single("orb-2", "opening_range_breakout", 7),
            _single("vwap-1", "vwap_pullback_bounce", 6),
        ]

        specs = build_walk_forward_specs(
            singles,
            train_years=(2024, 2025),
            selection_profile="net",
            min_edge_count=4,
            max_scan_type_counts={"prior_day_breakout": 1},
            min_scan_type_counts={"prior_day_breakout": 1},
        )

        self.assertEqual(specs[0]["candidate_ids"], ("prior-1", "orb-1", "orb-2", "vwap-1"))

    def test_cached_single_replay_reuses_existing_result(self) -> None:
        stats = CandidateStats(
            candidate_id="cached-edge",
            group_level="scan_session_dow_trend",
            scan_type="opening_range_breakout",
            direction_label="long",
            session_bucket="ny_0930_1159",
            dow=1,
            trend_bin=1,
            volume_bin=None,
            range_bin=None,
            total_trades=100,
            fixed_net_pnl=1000.0,
            fixed_avg_pnl=10.0,
            fixed_profit_factor=1.2,
            fixed_positive_years=2,
            fixed_worst_year_pnl=100.0,
            fixed_min_full_year_trades=10,
        )
        edge = RegimeEdge(
            scan_type="opening_range_breakout",
            direction_label="long",
            horizon_minutes=120,
            session_bucket="ny_0930_1159",
            dow=1,
            trend_bin=1,
            volume_bin=0,
            range_bin=0,
            take_profit_r=1.0,
        )
        params = {
            "max_hold_minutes": 120,
            "stop_range_multiple": 6.0,
            "min_stop_points": 8.0,
            "max_stop_points": 90.0,
            "flatten_on_date_change": True,
        }
        cache = {
            ((2019, 2020), "cached-edge", 1.0, tuple(sorted(params.items()))): {
                "source_candidate_id": "cached-edge",
                "source_stats": {},
                "metrics": {"net_pnl": 123.0},
            }
        }

        result = cached_single_replay(
            cache=cache,
            train_years=(2019, 2020),
            candidate_id="cached-edge",
            take_profit_r=1.0,
            params=params,
            stats=stats,
            bars=[],
            signals=[],
            edge=edge,
            config=LowRRegimeBasketConfig(),
            coverage_days={},
            min_full_year_trades=1000,
        )

        self.assertEqual(result["metrics"]["net_pnl"], 123.0)

    def test_cached_grid_parsers_and_leaderboard_sort(self) -> None:
        self.assertEqual(parse_int_grid("4, 8", option_name="--x"), (4, 8))
        self.assertEqual(parse_text_grid("stress,floor", option_name="--profile"), ("stress", "floor"))
        passed = {
            "summary": {
                "decision": {"passed": True, "positive_test_years": 6, "trade_floor_years": 6},
                "oos_min_year_pnl": 10.0,
                "oos_total_net_pnl": 100.0,
                "oos_min_year_trade_floor_count": 1000.0,
                "multiple_testing": {"effective_trial_count_floor": 10},
            }
        }
        failed = {
            "summary": {
                "decision": {"passed": False, "positive_test_years": 5, "trade_floor_years": 6},
                "oos_min_year_pnl": 1000.0,
                "oos_total_net_pnl": 10000.0,
                "oos_min_year_trade_floor_count": 2000.0,
                "multiple_testing": {"effective_trial_count_floor": 1},
            }
        }

        self.assertGreater(leaderboard_sort_key(passed), leaderboard_sort_key(failed))

    def test_hard_gate_report_rejects_low_win_rate_year(self) -> None:
        result = {
            "yearly_results": [
                {
                    "year": 2025,
                    "trade_count": 1200,
                    "net_pnl": 1000.0,
                    "win_rate": 0.69,
                }
            ]
        }

        gate = hard_gate_report_for_result(
            result,
            (2025,),
            {2025: 365},
            min_full_year_trades=1000,
            min_win_rate=0.70,
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["failed_win_rate_years"], [2025])
        self.assertEqual(gate["min_observed_win_rate"], 0.69)

    def test_summarize_walk_forward_applies_test_win_rate_gate(self) -> None:
        summary = summarize_walk_forward(
            [
                {
                    "status": "ok",
                    "evaluated_combo_count": 1,
                    "selected_edge_turnover": {"jaccard_similarity": None},
                    "test_yearly_result": {
                        "year": 2025,
                        "trade_count": 1200,
                        "annualized_trade_count": 1200.0,
                        "net_pnl": 1000.0,
                        "win_rate": 0.69,
                    },
                }
            ],
            [],
            min_full_year_trades=1000,
            min_test_win_rate=0.70,
        )

        self.assertFalse(summary["decision"]["passed"])
        self.assertEqual(summary["decision"]["failed_win_rate_years"], [2025])
        self.assertEqual(summary["oos_min_year_win_rate"], 0.69)

    def test_summarize_walk_forward_allows_no_combo_fold(self) -> None:
        summary = summarize_walk_forward(
            [{"status": "no_train_combo", "evaluated_combo_count": 3}],
            [],
            min_full_year_trades=1000,
            min_test_win_rate=0.70,
        )

        self.assertFalse(summary["decision"]["passed"])
        self.assertEqual(summary["decision"]["test_year_count"], 0)
        self.assertEqual(summary["multiple_testing"]["evaluated_train_combo_count"], 3)
        self.assertEqual(summary["multiple_testing"]["penalty_decision"], "fail")

    def test_cooldown_replay_suppresses_same_regime_entries(self) -> None:
        edge = RegimeEdge(
            scan_type="opening_range_breakout",
            direction_label="long",
            horizon_minutes=120,
            session_bucket="ny_0930_1159",
            dow=1,
            trend_bin=1,
            volume_bin=0,
            range_bin=0,
            take_profit_r=1.0,
        )
        start = datetime(2026, 1, 5, 14, 30)
        bars = [
            {
                "timestamp": start + timedelta(minutes=index),
                "open": 100.0 + index,
                "high": 101.0 + index,
                "low": 99.0 + index,
                "close": 100.0 + index,
                "range20": 1.0,
            }
            for index in range(10)
        ]
        signals = [
            {
                "signal_time": bars[0]["timestamp"],
                "entry_time": bars[1]["timestamp"],
                "entry_open": 101.0,
                "range20": 1.0,
                "edge_index": 0,
                "entry_index": 1,
                "scan_type": edge.scan_type,
                "direction_label": "long",
            },
            {
                "signal_time": bars[2]["timestamp"],
                "entry_time": bars[3]["timestamp"],
                "entry_open": 103.0,
                "range20": 1.0,
                "edge_index": 0,
                "entry_index": 3,
                "scan_type": edge.scan_type,
                "direction_label": "long",
            },
        ]

        no_cooldown = replay_with_regime_cooldown(
            bars=bars,
            signals=signals,
            edges=(edge,),
            config=LowRRegimeBasketConfig(max_concurrent_positions=99, max_hold_minutes=1),
            cooldown_minutes=0,
            same_scan_only=False,
        )
        with_cooldown = replay_with_regime_cooldown(
            bars=bars,
            signals=signals,
            edges=(edge,),
            config=LowRRegimeBasketConfig(max_concurrent_positions=99, max_hold_minutes=1),
            cooldown_minutes=5,
            same_scan_only=False,
        )

        self.assertEqual(len(no_cooldown), 2)
        self.assertEqual(len(with_cooldown), 1)
        self.assertIn("opening_range_breakout", signal_regime_key(signals[0], edge, same_scan_only=False))
        self.assertEqual(parse_cooldown_int_grid("0,5", option_name="--cooldown"), (0, 5))


if __name__ == "__main__":
    unittest.main()
