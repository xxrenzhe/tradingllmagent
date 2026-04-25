from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from tlm.config import SymbolConfig
from tlm.dukascopy import Tick
from tlm.leaderboard import evaluate_hard_gates, robustness_score
from tlm.metrics import calculate_metrics
from tlm.research import (
    load_leaderboard,
    run_budgeted_research,
    run_research_bar_validation,
    write_research_result,
)
from tlm.storage import bar_path, normalized_tick_path, write_bars_parquet, write_ticks_parquet
from tlm.strategy import parse_strategy_spec
from tlm.variants import (
    DEFAULT_PARAMETER_BUDGET,
    ParameterBudgetError,
    expand_strategy_variants,
    parameter_grid_metadata,
    strategy_spec_hash,
)
from tlm.validation import generate_rolling_folds

from test_strategy_backtest import base_spec


def symbol_config() -> SymbolConfig:
    return SymbolConfig(
        alias="NQmain",
        provider="dukascopy",
        instrument="USATECHIDXUSD",
        description="proxy",
        timezone="UTC",
        price_scale=1000,
        tick_size=0.25,
        point_value=20,
        default_session_timezone="UTC",
        default_trade_session="13:30-20:45",
        default_flatten_time="20:55",
    )


def write_breakout_day(data_root: Path, day: date) -> None:
    start = datetime(day.year, day.month, day.day, 13, 30)
    rows = []
    prices = [
        (100.0, 100.5, 99.5, 100.0),
        (100.0, 100.4, 99.7, 100.1),
        (100.1, 100.6, 99.8, 100.2),
        (100.2, 104.4, 100.2, 104.0),
        (104.0, 107.2, 103.8, 106.8),
    ]
    for index, (open_, high, low, close) in enumerate(prices):
        timestamp = start + timedelta(minutes=index)
        rows.append(
            (
                "NQmain",
                timestamp,
                open_,
                high,
                low,
                close,
                close - 0.1,
                close + 0.1,
                10,
                1.0,
                1.0,
                0.2,
            )
        )
    write_bars_parquet(bar_path(data_root, "NQmain", "1m", day), rows)


def write_breakout_ticks(data_root: Path, day: date) -> None:
    start = datetime(day.year, day.month, day.day, 13, 30)
    ticks = [
        Tick(start + timedelta(seconds=0), bid=99.9, ask=100.1, bid_size=1, ask_size=1),
        Tick(start + timedelta(minutes=1), bid=100.0, ask=100.2, bid_size=1, ask_size=1),
        Tick(start + timedelta(minutes=2), bid=100.1, ask=100.3, bid_size=1, ask_size=1),
        Tick(start + timedelta(minutes=3), bid=104.0, ask=104.2, bid_size=1, ask_size=1),
        Tick(start + timedelta(minutes=3, seconds=1), bid=104.1, ask=104.3, bid_size=1, ask_size=1),
        Tick(start + timedelta(minutes=4), bid=107.6, ask=107.8, bid_size=1, ask_size=1),
    ]
    write_ticks_parquet(normalized_tick_path(data_root, "NQmain", day), "NQmain", ticks)


class RollingValidationTests(unittest.TestCase):
    def test_generate_rolling_folds_with_embargo_and_holdout(self) -> None:
        plan = generate_rolling_folds(
            start=date(2020, 1, 1),
            end=date(2021, 12, 31),
            train_days=60,
            validation_days=20,
            test_days=20,
            step_days=30,
            embargo_days=5,
            final_holdout_days=60,
            min_folds=3,
        )

        self.assertGreaterEqual(len(plan.folds), 3)
        first = plan.folds[0]
        self.assertEqual((first.validation.start - first.train.end).days, 6)
        self.assertEqual((first.test.start - first.validation.end).days, 6)
        self.assertEqual(plan.final_holdout.end, date(2021, 12, 31))

    def test_hard_gates_and_score(self) -> None:
        strong = calculate_metrics(
            [300, -50, 250, 400, -40, 280],
            [100_000, 100_300, 100_250, 100_500, 100_900, 100_860, 101_140],
            100_000,
            1,
        )
        fold_metrics = [strong, strong, strong]
        gates = evaluate_hard_gates(strong, fold_metrics, strong, max_drawdown_limit=10_000)
        normal_score = robustness_score(strong, strong, fold_metrics)
        penalized_score = robustness_score(
            strong,
            strong,
            fold_metrics,
            parameter_budget_exceeded=True,
            parameter_combination_count=100,
        )

        self.assertTrue(gates.passed)
        self.assertIsNotNone(normal_score)
        self.assertIsNotNone(penalized_score)
        self.assertLess(penalized_score, normal_score)

    def test_research_run_writes_leaderboard_row(self) -> None:
        spec = parse_strategy_spec(base_spec())
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir) / "data"
            experiments_root = Path(temp_dir) / "experiments"
            for offset in range(40):
                write_breakout_day(data_root, date(2025, 1, 1) + timedelta(days=offset))

            result = run_research_bar_validation(
                spec=spec,
                symbol_config=symbol_config(),
                data_root=data_root,
                experiment_id="exp_test",
                date_from=date(2025, 1, 1),
                date_to=date(2025, 2, 9),
                train_days=5,
                validation_days=5,
                test_days=5,
                step_days=5,
                embargo_days=1,
                final_holdout_days=5,
                min_folds=1,
            )
            output = experiments_root / "exp_test" / "leaderboard.json"
            write_research_result(output, result)
            rows = load_leaderboard(experiments_root)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["experiment_id"], "exp_test")
        self.assertEqual(rows[0]["execution_mode"], "bar")
        self.assertEqual(rows[0]["cost_model"]["name"], "nq_conservative_v1")
        self.assertIn("passed", rows[0])

    def test_research_run_can_use_tick_replay_execution(self) -> None:
        spec = parse_strategy_spec(base_spec())
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir) / "data"
            experiments_root = Path(temp_dir) / "experiments"
            for offset in range(40):
                write_breakout_ticks(data_root, date(2025, 1, 1) + timedelta(days=offset))

            result = run_research_bar_validation(
                spec=spec,
                symbol_config=symbol_config(),
                data_root=data_root,
                experiment_id="exp_tick",
                date_from=date(2025, 1, 1),
                date_to=date(2025, 2, 9),
                train_days=5,
                validation_days=5,
                test_days=5,
                step_days=5,
                embargo_days=1,
                final_holdout_days=5,
                min_folds=1,
                execution_mode="tick",
            )
            output = experiments_root / "exp_tick" / "leaderboard.json"
            write_research_result(output, result)
            rows = load_leaderboard(experiments_root)

        self.assertEqual(result.execution_mode, "tick")
        self.assertGreater(result.aggregate_test_metrics.trade_count, 0)
        self.assertEqual(rows[0]["execution_mode"], "tick")

    def test_expand_strategy_variants_applies_supported_parameters(self) -> None:
        payload = base_spec()
        payload["parameters"] = {
            "opening_range_minutes": {"values": [3, 4]},
            "stop_points": {"values": [2, 3]},
            "take_profit_points": {"values": [3]},
        }
        seed = parse_strategy_spec(payload)
        variants = expand_strategy_variants(seed, max_trials=3)

        self.assertEqual(len(variants), 3)
        self.assertEqual(len({strategy_spec_hash(variant) for variant in variants}), 3)
        self.assertEqual(variants[0].raw["indicators"]["opening_range"]["minutes"], 3)
        self.assertIn("variant_parameters", variants[0].raw)

    def test_parameter_grid_metadata_marks_budget_exceeded(self) -> None:
        payload = base_spec()
        payload["parameters"] = {
            "opening_range_minutes": {"values": [3, 4, 5, 6, 7]},
            "stop_points": {"values": [1, 2, 3, 4]},
            "take_profit_points": {"values": [2, 3, 4]},
        }
        seed = parse_strategy_spec(payload)
        metadata = parameter_grid_metadata(seed, max_trials=100)

        self.assertEqual(DEFAULT_PARAMETER_BUDGET, 50)
        self.assertEqual(metadata.total_combinations, 60)
        self.assertEqual(metadata.selected_combinations, 50)
        self.assertTrue(metadata.budget_exceeded)
        self.assertFalse(metadata.high_risk_budget)
        self.assertTrue(metadata.parameter_grid_hash)

    def test_high_risk_parameter_grid_requires_explicit_allow(self) -> None:
        payload = base_spec()
        payload["parameters"] = {
            "opening_range_minutes": {"values": list(range(1, 11))},
            "stop_points": {"values": list(range(1, 11))},
            "take_profit_points": {"values": list(range(1, 11))},
        }
        seed = parse_strategy_spec(payload)

        with self.assertRaises(ParameterBudgetError):
            expand_strategy_variants(seed, max_trials=5)

        variants = expand_strategy_variants(
            seed,
            max_trials=5,
            max_parameter_combinations=5,
            allow_high_parameter_budget=True,
        )
        self.assertEqual(len(variants), 5)

    def test_budgeted_research_writes_multiple_leaderboard_rows(self) -> None:
        payload = base_spec()
        payload["parameters"] = {
            "opening_range_minutes": {"values": [3, 4]},
            "stop_points": {"values": [2]},
            "take_profit_points": {"values": [3]},
        }
        spec = parse_strategy_spec(payload)
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir) / "data"
            experiments_root = Path(temp_dir) / "experiments"
            for offset in range(40):
                write_breakout_day(data_root, date(2025, 1, 1) + timedelta(days=offset))

            results = run_budgeted_research(
                seed_spec=spec,
                symbol_config=symbol_config(),
                data_root=data_root,
                experiment_id="grid",
                date_from=date(2025, 1, 1),
                date_to=date(2025, 2, 9),
                max_trials=2,
                train_days=5,
                validation_days=5,
                test_days=5,
                step_days=5,
                embargo_days=1,
                final_holdout_days=5,
                min_folds=1,
            )
            for result in results:
                write_research_result(
                    experiments_root / result.experiment_id / "leaderboard.json",
                    result,
                )
            rows = load_leaderboard(experiments_root)

        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["strategy_spec_hash"] for row in rows))
        self.assertTrue(all(row["variant_parameters"] for row in rows))
        self.assertTrue(all(row["trial_count"] == 2 for row in rows))
        self.assertTrue(all(row["parameter_grid_hash"] for row in rows))
        self.assertTrue(all(row["parameter_combination_count"] == 2 for row in rows))
        self.assertFalse(any(row["parameter_budget_exceeded"] for row in rows))


if __name__ == "__main__":
    unittest.main()
