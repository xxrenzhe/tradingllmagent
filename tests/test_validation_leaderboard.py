from __future__ import annotations

import tempfile
import unittest
import io
import json
from dataclasses import replace
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

from tlm.backtest import BacktestResult, Trade
from tlm.cli import main
from tlm.config import SymbolConfig
from tlm.dukascopy import Tick
from tlm.leaderboard import evaluate_hard_gates, robustness_score
from tlm.metrics import BacktestMetrics, calculate_metrics
from tlm.research import (
    build_trade_count_distribution_report,
    build_signal_similarity_report,
    estimate_indicator_warmup_days,
    load_leaderboard,
    load_leaderboard_report,
    load_research_artifacts,
    run_budgeted_research,
    run_research_bar_validation,
    trim_backtest_result,
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

from test_strategy_backtest import base_spec, trend_pullback_spec


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
            indicator_warmup_days=3,
        )

        self.assertGreaterEqual(len(plan.folds), 3)
        first = plan.folds[0]
        self.assertEqual((first.validation.start - first.train.end).days, 6)
        self.assertEqual((first.test.start - first.validation.end).days, 6)
        self.assertEqual(first.train.warmup_start, date(2020, 1, 1))
        self.assertEqual(first.validation.warmup_start, first.validation.start - timedelta(days=3))
        self.assertEqual(plan.indicator_warmup_days, 3)
        self.assertEqual(plan.to_dict()["indicator_warmup_days"], 3)
        self.assertIn("warmup_start", plan.to_dict()["folds"][0]["validation"])
        self.assertEqual(plan.final_holdout.end, date(2021, 12, 31))
        self.assertFalse(plan.to_dict()["overlapping_test_folds"])
        self.assertEqual(
            plan.to_dict()["non_overlap_test_fold_indexes"],
            [fold.index for fold in plan.folds],
        )

    def test_generate_rolling_folds_marks_overlapping_tests(self) -> None:
        plan = generate_rolling_folds(
            start=date(2020, 1, 1),
            end=date(2020, 5, 31),
            train_days=20,
            validation_days=10,
            test_days=20,
            step_days=10,
            embargo_days=0,
            final_holdout_days=20,
            min_folds=3,
        )
        payload = plan.to_dict()

        self.assertTrue(payload["overlapping_test_folds"])
        self.assertLess(len(payload["non_overlap_test_fold_indexes"]), len(payload["folds"]))
        self.assertEqual(payload["non_overlap_test_fold_indexes"][:2], [0, 2])

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

    def test_hard_gates_require_yearly_stability_and_cost_edge(self) -> None:
        strong = calculate_metrics(
            [300, -50, 250, 400, -40, 280],
            [100_000, 100_300, 100_250, 100_500, 100_900, 100_860, 101_140],
            100_000,
            1,
        )
        fold_metrics = [strong, strong, strong]

        weak_cost_edge = evaluate_hard_gates(
            strong,
            fold_metrics,
            strong,
            max_drawdown_limit=10_000,
            round_trip_cost=200,
        )
        unstable_years = evaluate_hard_gates(
            strong,
            fold_metrics,
            strong,
            max_drawdown_limit=10_000,
            positive_year_ratio=0.5,
        )

        self.assertIn("avg_trade_net_pnl", weak_cost_edge.reasons)
        self.assertIn("positive_year_ratio", unstable_years.reasons)
        self.assertIsNone(robustness_score(strong, strong, fold_metrics, round_trip_cost=200))

    def test_hard_gates_reject_sharpe_decay(self) -> None:
        strong = calculate_metrics(
            [300, -50, 250, 400, -40, 280],
            [100_000, 100_300, 100_250, 100_500, 100_900, 100_860, 101_140],
            100_000,
            1,
        )
        validation = calculate_metrics(
            [500, 480, 520, 510, 490, 500],
            [100_000, 100_500, 100_980, 101_500, 102_010, 102_500, 103_000],
            100_000,
            1,
        )
        degraded_holdout = replace(strong, sharpe=(strong.sharpe or 0) * 0.4)
        fold_metrics = [strong, strong, strong]

        validation_decay = evaluate_hard_gates(
            strong,
            fold_metrics,
            strong,
            validation_metrics=validation,
            max_drawdown_limit=10_000,
        )
        holdout_decay = evaluate_hard_gates(
            strong,
            fold_metrics,
            degraded_holdout,
            max_drawdown_limit=10_000,
        )

        self.assertIn("validation_to_test_sharpe_decay", validation_decay.reasons)
        self.assertIn("test_to_holdout_sharpe_decay", holdout_decay.reasons)
        self.assertIsNone(robustness_score(strong, strong, fold_metrics, validation_metrics=validation))

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
                random_seed=123,
                llm_model="local-test-model",
                llm_parameters={"temperature": 0, "candidate_count": 1},
            )
            output = experiments_root / "exp_test" / "leaderboard.json"
            write_research_result(output, result)
            rows = load_leaderboard(experiments_root)
            artifact_payload = load_research_artifacts(experiments_root, "exp_test")
            manifest = json.loads((output.parent / "manifest.json").read_text(encoding="utf-8"))
            run_manifest = json.loads((output.parent / "run_manifest.json").read_text(encoding="utf-8"))
            persisted_spec = json.loads((output.parent / "strategy_spec.json").read_text(encoding="utf-8"))
            con = duckdb.connect(":memory:")
            try:
                trade_rows = con.execute(
                    "SELECT COUNT(*) FROM read_parquet(?)",
                    [str(output.parent / "trades.parquet")],
                ).fetchone()[0]
                equity_rows = con.execute(
                    "SELECT COUNT(*) FROM read_parquet(?)",
                    [str(output.parent / "equity.parquet")],
                ).fetchone()[0]
                metric_rows = con.execute(
                    "SELECT COUNT(*) FROM read_parquet(?)",
                    [str(output.parent / "fold_metrics.parquet")],
                ).fetchone()[0]
                metric_splits = {
                    row[0]
                    for row in con.execute(
                        "SELECT DISTINCT split FROM read_parquet(?)",
                        [str(output.parent / "fold_metrics.parquet")],
                    ).fetchall()
                }
                trade_splits = {
                    row[0]
                    for row in con.execute(
                        "SELECT DISTINCT split FROM read_parquet(?)",
                        [str(output.parent / "trades.parquet")],
                    ).fetchall()
                }
            finally:
                con.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(artifact_payload["experiment_id"], "exp_test")
        self.assertEqual(artifact_payload["manifest"]["schema_version"], 2)
        self.assertEqual(len(artifact_payload["fold_metrics"]), len(result.split_artifacts))
        self.assertTrue(artifact_payload["trades"])
        self.assertTrue(artifact_payload["equity"])
        self.assertTrue(artifact_payload["distributions"]["by_year"])
        self.assertTrue(artifact_payload["distributions"]["by_hour"])
        self.assertTrue(artifact_payload["distributions"]["by_direction"])
        self.assertTrue(artifact_payload["distributions"]["by_holding_minutes"])
        self.assertEqual(manifest["experiment_id"], "exp_test")
        self.assertEqual(run_manifest, manifest)
        self.assertEqual(persisted_spec["name"], result.strategy_name)
        self.assertEqual(manifest["artifacts"]["strategy_spec"]["path"], "strategy_spec.json")
        self.assertEqual(manifest["artifacts"]["run_manifest"]["path"], "run_manifest.json")
        self.assertTrue(manifest["data_disclaimer"]["not_cme_futures"])
        self.assertIn("CFD proxy", manifest["data_disclaimer"]["data_source"])
        self.assertEqual(manifest["reproducibility"]["random_seed"], 123)
        self.assertEqual(manifest["leaderboard_path"], "leaderboard.json")
        self.assertIn("strict_validation", manifest)
        self.assertEqual(
            manifest["strict_validation"]["tick_replay_report"]["status"],
            result.tick_replay_report["status"],
        )
        self.assertEqual(
            manifest["strict_validation"]["non_overlap_test_fold_indexes"],
            result.non_overlap_test_fold_indexes,
        )
        self.assertEqual(manifest["artifacts"]["fold_metrics"]["row_count"], len(result.split_artifacts))
        self.assertEqual(manifest["artifacts"]["trades"]["row_count"], trade_rows)
        self.assertEqual(manifest["artifacts"]["equity"]["row_count"], equity_rows)
        self.assertEqual(metric_rows, len(result.split_artifacts))
        self.assertEqual(equity_rows, trade_rows + len(result.split_artifacts))
        self.assertEqual(metric_splits, {"train", "validation", "test", "final_holdout"})
        self.assertIn("final_holdout", trade_splits)
        self.assertEqual(rows[0]["experiment_id"], "exp_test")
        self.assertEqual(rows[0]["execution_mode"], "bar")
        self.assertTrue(rows[0]["data_version_hash"])
        self.assertEqual(rows[0]["snapshot"]["random_seed"], 123)
        self.assertEqual(rows[0]["snapshot"]["experiment_id"], "exp_test")
        self.assertEqual(rows[0]["snapshot"]["strategy_spec_hash"], rows[0]["strategy_spec_hash"])
        self.assertEqual(rows[0]["snapshot"]["prompt_hash"], result.prompt_hash)
        self.assertEqual(rows[0]["snapshot"]["data_version_hash"], rows[0]["data_version_hash"])
        self.assertEqual(rows[0]["snapshot"]["llm_model"], "local-test-model")
        self.assertEqual(
            rows[0]["snapshot"]["llm_parameters"],
            {"temperature": 0, "candidate_count": 1},
        )
        self.assertTrue(rows[0]["snapshot"]["code_version"])
        self.assertIn("config_snapshot", rows[0]["snapshot"])
        self.assertTrue(rows[0]["snapshot"]["config_snapshot_hash"])
        self.assertTrue(rows[0]["snapshot"]["fold_definition_hash"])
        self.assertTrue(rows[0]["snapshot"]["cost_model_hash"])
        self.assertEqual(rows[0]["cost_model"]["name"], "nq_conservative_v1")
        self.assertIn("passed", rows[0])
        self.assertEqual(rows[0]["round_trip_cost"], 15.0)
        self.assertEqual(rows[0]["positive_year_ratio"], 1.0)
        self.assertEqual(rows[0]["yearly_results"][0]["year"], 2025)
        self.assertGreater(rows[0]["yearly_results"][0]["trade_count"], 0)
        self.assertIn("trade_count_distribution_report", rows[0])
        self.assertEqual(rows[0]["trade_count_distribution_report"]["status"], "balanced")
        self.assertEqual(
            rows[0]["trade_count_distribution_report"]["total_test_trades"],
            result.aggregate_test_metrics.trade_count,
        )
        self.assertIn("sharpe_validation", rows[0])
        self.assertIn("validation_to_test_sharpe_decay", rows[0])
        self.assertIn("test_to_holdout_sharpe_decay", rows[0])
        self.assertIn("overfitting_report", rows[0])
        self.assertEqual(rows[0]["overfitting_report"]["trial_count"], 1)
        self.assertEqual(rows[0]["overfitting_report"]["fold_count"], len(result.validation_plan.folds))
        self.assertEqual(rows[0]["overfitting_report"]["pbo_status"], "computed_proxy_v1")
        self.assertIn("trial_adjusted_sharpe", rows[0]["overfitting_report"])
        self.assertIn("trial_count_penalty", rows[0]["overfitting_report"])
        self.assertIn("cost_sensitivity_report", rows[0])
        self.assertEqual(rows[0]["cost_sensitivity_report"]["baseline_round_trip_cost"], 15.0)
        self.assertEqual(rows[0]["tick_replay_report"]["status"], "not_applicable")
        self.assertFalse(rows[0]["tick_replay_report"]["strict_tick_replay_gap"])
        self.assertEqual(rows[0]["promotion_report"]["stage"], "direct_bar")
        self.assertEqual(rows[0]["strategy_card"]["name"], result.strategy_name)
        self.assertTrue(rows[0]["hard_gate_report"])
        self.assertEqual(rows[0]["hard_gate_report"][0]["name"], "annual_trades_test")
        self.assertIn("threshold", rows[0]["hard_gate_report"][0])
        self.assertEqual(
            rows[0]["strategy_card"]["hard_gate_report"],
            rows[0]["hard_gate_report"],
        )
        self.assertFalse(rows[0]["final_holdout_policy"]["llm_feedback_includes_final_holdout"])
        self.assertEqual(rows[0]["final_holdout_policy"]["isolation_status"], "isolated_from_llm_feedback")
        self.assertEqual(rows[0]["final_holdout_policy"]["llm_visible_splits"], ["train", "validation"])
        self.assertIn("final_holdout", rows[0]["final_holdout_policy"]["llm_hidden_splits"])
        self.assertTrue(rows[0]["next_round_suggestions"])
        self.assertEqual(
            rows[0]["cost_sensitivity_report"]["stress_scenarios"][1]["name"],
            "plus_1_tick_slippage_per_side",
        )
        self.assertFalse(rows[0]["overlapping_test_folds"])
        self.assertEqual(
            rows[0]["non_overlap_test_fold_indexes"],
            [fold.index for fold in result.validation_plan.folds],
        )
        self.assertIn("sharpe_non_overlap_test", rows[0])
        self.assertEqual(result.round_trip_cost, 15.0)
        self.assertEqual(result.positive_year_ratio, 1.0)
        self.assertEqual(result.yearly_results[0]["year"], 2025)
        self.assertGreater(result.aggregate_validation_metrics.trade_count, 0)
        self.assertFalse(result.overlapping_test_folds)
        self.assertEqual(
            result.non_overlap_test_fold_indexes,
            [fold.index for fold in result.validation_plan.folds],
        )
        self.assertEqual(
            result.non_overlap_test_metrics.trade_count,
            result.aggregate_test_metrics.trade_count,
        )
        self.assertEqual(result.snapshot["random_seed"], 123)
        self.assertEqual(result.overfitting_report["parameter_combination_count"], 1)
        self.assertIn(result.overfitting_report["risk_level"], {"low", "medium", "high"})
        self.assertEqual(result.cost_sensitivity_report["method"], "deterministic_trade_pnl_adjustment")
        self.assertEqual(len(result.cost_sensitivity_report["stress_scenarios"]), 4)
        self.assertEqual(result.promotion_report["stage"], "direct_bar")
        self.assertEqual(result.strategy_card["promotion_stage"], "direct_bar")
        self.assertTrue(result.hard_gate_report)
        self.assertTrue(any(not gate["passed"] for gate in result.hard_gate_report))
        self.assertEqual(result.final_holdout_policy["status"], "frozen_once_after_candidate_selection")
        self.assertEqual(
            result.final_holdout_policy["run_timing"],
            "same_research_run_after_rolling_candidate_evaluation",
        )
        self.assertTrue(result.final_holdout_policy["separate_freeze_task_required_for_strict_plan"])
        self.assertFalse(result.final_holdout_policy["strict_freeze_task_implemented"])
        self.assertTrue(result.next_round_suggestions)
        self.assertTrue(result.final_holdout_data_version_hash)
        self.assertTrue(result.fold_results[0]["train_data_version_hash"])
        self.assertTrue(result.fold_results[0]["validation_data_version_hash"])
        self.assertTrue(result.fold_results[0]["test_data_version_hash"])

    def test_trade_count_distribution_report_flags_concentration(self) -> None:
        plan = generate_rolling_folds(
            date(2020, 1, 1),
            date(2024, 12, 31),
            train_days=365,
            validation_days=60,
            test_days=60,
            step_days=120,
            final_holdout_days=365,
            min_folds=2,
        )
        metrics = [
            BacktestMetrics(95, 1_000, 1_200, -200, 6.0, 2.1, 100, 577.9, 10.5),
            BacktestMetrics(5, -100, 50, -150, 0.33, -1.0, 200, 30.4, -20.0),
        ]
        report = build_trade_count_distribution_report(
            yearly_results=[
                {"year": 2022, "trade_count": 95, "net_pnl": 1_000},
                {"year": 2023, "trade_count": 5, "net_pnl": -100},
            ],
            fold_test_metrics=metrics,
            validation_plan=plan,
        )

        self.assertEqual(report["status"], "concentrated")
        self.assertIn("fold_trade_concentration", report["reasons"])
        self.assertIn("year_trade_concentration", report["reasons"])
        self.assertEqual(report["total_test_trades"], 100)
        self.assertEqual(report["by_fold"][0]["trade_count"], 95)

    def test_indicator_warmup_days_are_recorded_and_excluded_from_metrics(self) -> None:
        spec = parse_strategy_spec(trend_pullback_spec())
        self.assertEqual(estimate_indicator_warmup_days(spec), 1)
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir) / "data"
            for offset in range(45):
                write_breakout_day(data_root, date(2025, 1, 1) + timedelta(days=offset))

            result = run_research_bar_validation(
                spec=spec,
                symbol_config=symbol_config(),
                data_root=data_root,
                experiment_id="exp_warmup",
                date_from=date(2025, 1, 1),
                date_to=date(2025, 2, 14),
                train_days=5,
                validation_days=5,
                test_days=5,
                step_days=5,
                embargo_days=1,
                final_holdout_days=5,
                min_folds=1,
                indicator_warmup_days=2,
            )

        first_fold = result.validation_plan.folds[0]
        self.assertEqual(result.validation_plan.indicator_warmup_days, 2)
        self.assertEqual(first_fold.validation.warmup_start, first_fold.validation.start - timedelta(days=2))
        self.assertEqual(
            result.fold_results[0]["fold"]["validation"]["warmup_start"],
            first_fold.validation.warmup_start.isoformat(),
        )
        for fold_result in result.fold_results:
            for split in ("train", "validation", "test"):
                split_range = fold_result["fold"][split]
                metrics = fold_result[f"{split}_metrics"]
                self.assertLessEqual(
                    metrics["trade_count"],
                    ((date.fromisoformat(split_range["end"]) - date.fromisoformat(split_range["start"])).days + 1)
                    * spec.risk["max_trades_per_day"],
                )

    def test_trim_backtest_result_removes_warmup_trades(self) -> None:
        warmup_trade = Trade(
            symbol="NQmain",
            side="long",
            entry_time=datetime(2025, 1, 1, 13, 30),
            exit_time=datetime(2025, 1, 1, 13, 35),
            entry_price=100,
            exit_price=101,
            contracts=1,
            gross_pnl=20,
            fees=0,
            slippage_cost=0,
            net_pnl=20,
            entry_reason="warmup",
            exit_reason="warmup",
        )
        evaluation_trade = Trade(
            symbol="NQmain",
            side="long",
            entry_time=datetime(2025, 1, 2, 13, 30),
            exit_time=datetime(2025, 1, 2, 13, 35),
            entry_price=100,
            exit_price=102,
            contracts=1,
            gross_pnl=40,
            fees=0,
            slippage_cost=0,
            net_pnl=40,
            entry_reason="evaluation",
            exit_reason="evaluation",
        )
        result = BacktestResult(
            strategy_name="warmup_test",
            symbol="NQmain",
            data_version_hash="hash",
            cost_model={},
            trades=[warmup_trade, evaluation_trade],
            metrics=calculate_metrics([20, 40], [100_000, 100_020, 100_060], 100_000, 2),
        )

        trimmed = trim_backtest_result(
            result,
            start=date(2025, 1, 2),
            end=date(2025, 1, 2),
            starting_equity=100_000,
        )

        self.assertEqual([trade.entry_reason for trade in trimmed.trades], ["evaluation"])
        self.assertEqual(trimmed.metrics.trade_count, 1)
        self.assertEqual(trimmed.metrics.net_pnl, 40)

    def test_leaderboard_report_splits_passed_and_rejected_rows(self) -> None:
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
                experiment_id="split_trial_0000",
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
            passed_payload = result.to_dict()
            passed_payload["gates"] = {"passed": True, "reasons": []}
            passed_payload["robustness_score"] = 0.5
            rejected_payload = result.to_dict()
            rejected_payload["experiment_id"] = "split_trial_0001"
            rejected_payload["gates"] = {"passed": False, "reasons": ["test_rejection"]}
            rejected_payload["robustness_score"] = None
            (experiments_root / "split_trial_0000").mkdir(parents=True)
            (experiments_root / "split_trial_0001").mkdir(parents=True)
            (experiments_root / "split_trial_0000" / "leaderboard.json").write_text(
                json.dumps(passed_payload),
                encoding="utf-8",
            )
            (experiments_root / "split_trial_0001" / "leaderboard.json").write_text(
                json.dumps(rejected_payload),
                encoding="utf-8",
            )

            report = load_leaderboard_report(experiments_root)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "report",
                        "leaderboard",
                        "--experiments-root",
                        str(experiments_root),
                        "--experiment-id",
                        "split_trial_0001",
                    ]
                )
            filtered = json.loads(stdout.getvalue())

        self.assertEqual(report["summary"], {"passed": 1, "rejected": 1, "total": 2})
        self.assertEqual(report["conclusion"], "qualified_strategies_found")
        self.assertEqual(report["leaderboard"][0]["experiment_id"], "split_trial_0000")
        self.assertEqual(report["rejected"][0]["experiment_id"], "split_trial_0001")
        self.assertEqual(exit_code, 0)
        self.assertEqual(filtered["summary"], {"passed": 0, "rejected": 1, "total": 1})
        self.assertEqual(filtered["conclusion"], "no_qualified_strategies_found")
        self.assertIn("No qualified strategies found", filtered["message"])
        self.assertEqual(filtered["rejected"][0]["reasons"], ["test_rejection"])

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
        self.assertTrue(result.data_version_hash)
        self.assertTrue(result.snapshot["fold_definition_hash"])
        self.assertGreater(result.aggregate_test_metrics.trade_count, 0)
        self.assertEqual(result.tick_replay_report["status"], "native_tick_replay")
        self.assertTrue(result.tick_replay_report["native_tick_replay"])
        self.assertEqual(rows[0]["execution_mode"], "tick")
        self.assertEqual(rows[0]["tick_replay_report"]["status"], "native_tick_replay")
        self.assertTrue(rows[0]["data_version_hash"])

    def test_tick_replay_report_flags_bar_from_tick_fallback(self) -> None:
        spec = parse_strategy_spec(trend_pullback_spec())
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir) / "data"
            for offset in range(40):
                write_breakout_ticks(data_root, date(2025, 1, 1) + timedelta(days=offset))

            result = run_research_bar_validation(
                spec=spec,
                symbol_config=symbol_config(),
                data_root=data_root,
                experiment_id="exp_tick_fallback",
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

        self.assertEqual(result.tick_replay_report["status"], "bar_from_tick_fallback")
        self.assertTrue(result.tick_replay_report["strict_tick_replay_gap"])
        self.assertFalse(result.tick_replay_report["native_tick_replay"])

    def test_bar_then_tick_promotes_candidate_and_records_strategy_card(self) -> None:
        spec = parse_strategy_spec(base_spec())
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir) / "data"
            for offset in range(40):
                day = date(2025, 1, 1) + timedelta(days=offset)
                write_breakout_day(data_root, day)
                write_breakout_ticks(data_root, day)

            results = run_budgeted_research(
                seed_spec=spec,
                symbol_config=symbol_config(),
                data_root=data_root,
                experiment_id="staged",
                date_from=date(2025, 1, 1),
                date_to=date(2025, 2, 9),
                max_trials=1,
                train_days=5,
                validation_days=5,
                test_days=5,
                step_days=5,
                embargo_days=1,
                final_holdout_days=5,
                min_folds=1,
                execution_mode="bar_then_tick",
            )

        result = results[0]
        self.assertEqual(result.execution_mode, "tick")
        self.assertEqual(result.promotion_report["mode"], "bar_then_tick")
        self.assertEqual(result.promotion_report["stage"], "promoted_to_tick")
        self.assertTrue(result.promotion_report["promoted"])
        self.assertFalse(result.promotion_report["final_holdout_used_for_promotion"])
        self.assertEqual(result.strategy_card["promotion_stage"], "promoted_to_tick")
        self.assertEqual(result.strategy_card["key_metrics"]["test"]["trade_count"], result.aggregate_test_metrics.trade_count)
        self.assertFalse(result.final_holdout_policy["promotion_uses_final_holdout"])

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

    def test_expand_strategy_variants_applies_trend_parameters(self) -> None:
        payload = trend_pullback_spec()
        payload["parameters"] = {
            "ema_fast_window": {"values": [2, 3]},
            "ema_slow_window": {"values": [4]},
            "stop_points": {"values": [2]},
            "take_profit_points": {"values": [3]},
        }
        seed = parse_strategy_spec(payload)
        variants = expand_strategy_variants(seed, max_trials=2)

        self.assertEqual(len(variants), 2)
        self.assertEqual(variants[0].raw["indicators"]["ema_fast"]["window"], 2)
        self.assertEqual(variants[1].raw["indicators"]["ema_fast"]["window"], 3)

    def test_expand_strategy_variants_deduplicates_repeated_parameter_values(self) -> None:
        payload = base_spec()
        payload["parameters"] = {
            "opening_range_minutes": {"values": [3, 3]},
            "stop_points": {"values": [2, 2]},
            "take_profit_points": {"values": [3]},
        }
        seed = parse_strategy_spec(payload)
        metadata = parameter_grid_metadata(seed, max_trials=10)
        variants = expand_strategy_variants(seed, max_trials=10)

        self.assertEqual(metadata.total_combinations, 4)
        self.assertEqual(metadata.unique_combinations, 1)
        self.assertEqual(metadata.duplicate_combinations, 3)
        self.assertEqual(metadata.selected_combinations, 1)
        self.assertEqual(len(variants), 1)

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
        self.assertEqual(metadata.unique_combinations, 50)
        self.assertEqual(metadata.duplicate_combinations, 0)
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
        self.assertTrue(all(row["overfitting_report"]["trial_count"] == 2 for row in rows))
        self.assertTrue(all(row["parameter_stability_report"]["evaluated_trials"] == 2 for row in rows))
        self.assertTrue(all(row["parameter_stability_report"]["adjacent_pair_count"] >= 1 for row in rows))
        self.assertTrue(all("positive_neighbor_ratio" in row["parameter_stability_report"] for row in rows))
        self.assertTrue(all(row["signal_similarity_report"]["evaluated_trials"] == 2 for row in rows))
        self.assertTrue(all("pairs" in row["signal_similarity_report"] for row in rows))
        self.assertFalse(any(row["parameter_budget_exceeded"] for row in rows))

    def test_signal_similarity_report_flags_duplicate_trade_signals(self) -> None:
        spec = parse_strategy_spec(base_spec())
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir) / "data"
            for offset in range(40):
                write_breakout_day(data_root, date(2025, 1, 1) + timedelta(days=offset))
            result = run_research_bar_validation(
                spec=spec,
                symbol_config=symbol_config(),
                data_root=data_root,
                experiment_id="signals_a",
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

        report = build_signal_similarity_report(
            [
                result,
                replace(result, experiment_id="signals_b"),
            ]
        )

        self.assertEqual(report["status"], "near_duplicates_found")
        self.assertEqual(report["near_duplicate_pair_count"], 1)
        self.assertEqual(report["pairs"][0]["similarity"], 1.0)


if __name__ == "__main__":
    unittest.main()
