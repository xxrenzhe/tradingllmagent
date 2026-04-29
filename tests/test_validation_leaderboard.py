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


def write_breakout_day(data_root: Path, day: date, symbol: str = "NQmain") -> None:
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
    rows = [(symbol, *row[1:]) for row in rows]
    write_bars_parquet(bar_path(data_root, symbol, "1m", day), rows)


def write_breakout_ticks(data_root: Path, day: date, symbol: str = "NQmain") -> None:
    start = datetime(day.year, day.month, day.day, 13, 30)
    ticks = [
        Tick(start + timedelta(seconds=0), bid=99.9, ask=100.1, bid_size=1, ask_size=1),
        Tick(start + timedelta(minutes=1), bid=100.0, ask=100.2, bid_size=1, ask_size=1),
        Tick(start + timedelta(minutes=2), bid=100.1, ask=100.3, bid_size=1, ask_size=1),
        Tick(start + timedelta(minutes=3), bid=104.0, ask=104.2, bid_size=1, ask_size=1),
        Tick(start + timedelta(minutes=3, seconds=1), bid=104.1, ask=104.3, bid_size=1, ask_size=1),
        Tick(start + timedelta(minutes=4), bid=107.6, ask=107.8, bid_size=1, ask_size=1),
    ]
    write_ticks_parquet(normalized_tick_path(data_root, symbol, day), symbol, ticks)


def write_ready_quote_report(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "artifact": "quote_execution_validation",
                "trade_count": 2,
                "validated_trade_count": 2,
                "avg_bid_ask_cost_usd": 10.0,
                "execution_models": {
                    "market_order_bid_ask_replay": {},
                    "fixed_conservative": {},
                    "limit_missed_fill": {"fill_rate": 0.5},
                    "adverse_selection": {"5m": {"avg_ticks": 1.25}},
                },
            }
        ),
        encoding="utf-8",
    )


def write_ready_paper_shadow(path: Path) -> None:
    events = []
    for index, day in enumerate(("2026-04-27", "2026-04-28", "2026-04-29")):
        events.append(
            {
                "event_type": "paper_shadow_intent",
                "intent": {
                    "intent_id": f"signal_{index}",
                    "source_strategy": {"strategy_spec_hash": "hash", "regime": "trend"},
                    "expected_edge": 12.5,
                },
                "market_snapshot": {
                    "snapshot_time": f"{day}T13:30:00",
                    "spread_ticks": 2,
                    "adverse_selection_ticks_5m": -1,
                },
                "paper_shadow_run": {
                    "paper_shadow_run_id": f"ps_{index}",
                    "intent_id": f"signal_{index}",
                    "blocked": False,
                    "created_at": f"{day}T13:30:00Z",
                    "hypothetical_fill": {"fill_price": 19000 + index},
                },
                "llm_diagnosis": {"summary": "ok"},
                "mutation_proposal": {"allowed_mutations": ["tighten_time_window"], "blocked_mutations": []},
            }
        )
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")


class RollingValidationTests(unittest.TestCase):
    def test_calculate_metrics_includes_loss_and_drawdown_detail(self) -> None:
        metrics = calculate_metrics(
            [100, -50, -25, 75, -10, -5],
            [100_000, 100_100, 100_050, 100_025, 100_100, 100_090, 100_085],
            100_000,
            6,
        )

        self.assertEqual(metrics.winning_trade_count, 2)
        self.assertEqual(metrics.losing_trade_count, 4)
        self.assertAlmostEqual(metrics.win_rate or 0.0, 2 / 6)
        self.assertEqual(metrics.avg_win_net_pnl, 87.5)
        self.assertEqual(metrics.avg_loss_net_pnl, -22.5)
        self.assertEqual(metrics.largest_win_net_pnl, 100)
        self.assertEqual(metrics.largest_loss_net_pnl, -50)
        self.assertEqual(metrics.max_consecutive_losses, 2)
        self.assertAlmostEqual(metrics.payoff_ratio or 0.0, 87.5 / 22.5)
        self.assertAlmostEqual(metrics.max_drawdown_pct or 0.0, metrics.max_drawdown / 100_000)
        self.assertAlmostEqual(metrics.net_pnl_to_max_drawdown or 0.0, metrics.net_pnl / metrics.max_drawdown)

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

    def test_research_validation_honors_symbol_config_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir)
            start = date(2025, 3, 1)
            for offset in range(25):
                write_breakout_day(data_root, start + timedelta(days=offset), symbol="NQ_1M")

            result = run_research_bar_validation(
                spec=parse_strategy_spec(base_spec()),
                symbol_config=replace(symbol_config(), alias="NQ_1M", provider="firstratedata", instrument="NQ"),
                data_root=data_root,
                experiment_id="override_symbol",
                date_from=start,
                date_to=start + timedelta(days=24),
                train_days=5,
                validation_days=5,
                test_days=5,
                step_days=5,
                embargo_days=0,
                final_holdout_days=5,
                min_folds=1,
                indicator_warmup_days=0,
            )

        self.assertEqual(result.strategy_spec["symbol"], "NQ_1M")
        self.assertTrue(result.split_artifacts)
        self.assertGreater(result.aggregate_test_metrics.trade_count, 0)

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
        candidate_only = evaluate_hard_gates(
            strong,
            fold_metrics,
            degraded_holdout,
            max_drawdown_limit=10_000,
            include_final_holdout=False,
        )

        self.assertIn("validation_to_test_sharpe_decay", validation_decay.reasons)
        self.assertIn("test_to_holdout_sharpe_decay", holdout_decay.reasons)
        self.assertTrue(candidate_only.passed)
        self.assertNotIn("test_to_holdout_sharpe_decay", candidate_only.reasons)
        self.assertIsNone(robustness_score(strong, strong, fold_metrics, validation_metrics=validation))
        self.assertIsNotNone(robustness_score(strong, degraded_holdout, fold_metrics, include_final_holdout=False))

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
            research_ledger = json.loads((output.parent / "research_ledger.json").read_text(encoding="utf-8"))
            run_manifest = json.loads((output.parent / "run_manifest.json").read_text(encoding="utf-8"))
            persisted_spec = json.loads((output.parent / "strategy_spec.json").read_text(encoding="utf-8"))
            module_memory = [
                json.loads(line)
                for line in (output.parent / "module_performance.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
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
        self.assertEqual(manifest["module_id"], "opening_range_breakout")
        self.assertEqual(manifest["module"]["family"], "opening_range_breakout")
        self.assertEqual(run_manifest, manifest)
        self.assertEqual(persisted_spec["name"], result.strategy_name)
        self.assertEqual(manifest["artifacts"]["strategy_spec"]["path"], "strategy_spec.json")
        self.assertEqual(manifest["artifacts"]["research_ledger"]["path"], "research_ledger.json")
        self.assertEqual(manifest["artifacts"]["run_manifest"]["path"], "run_manifest.json")
        self.assertEqual(manifest["research_ledger_hash"], research_ledger["ledger_hash"])
        self.assertEqual(artifact_payload["research_ledger"]["ledger_hash"], research_ledger["ledger_hash"])
        self.assertEqual(research_ledger["artifact"], "research_ledger")
        self.assertEqual(research_ledger["primary_research_policy"]["symbol"], "NQ_CME")
        self.assertEqual(research_ledger["primary_research_policy"]["top_strategy_objective"], "expectancy_first")
        self.assertEqual(research_ledger["run_context"]["symbol"], result.strategy_spec["symbol"])
        self.assertEqual(research_ledger["search_actions"]["parameter_grid_hash"], result.parameter_grid_hash)
        self.assertEqual(research_ledger["search_actions"]["report_objective"], "expectancy_first")
        self.assertTrue(research_ledger["search_actions"]["final_holdout_inputs_excluded"])
        self.assertIn("candidate_ranking", research_ledger["search_actions"]["final_holdout_excluded_from"])
        self.assertIn("pre_screen_thresholds", research_ledger["search_actions"])
        self.assertIn("split_years", research_ledger["search_actions"])
        self.assertIn("final_holdout", research_ledger["validation_policy"]["split_years"])
        self.assertTrue(research_ledger["validation_policy"]["final_holdout_evaluated"])
        self.assertEqual(
            research_ledger["validation_policy"]["final_holdout_evaluation_phase"],
            "freeze_confirmation_after_candidate_decision",
        )
        self.assertEqual(
            research_ledger["validation_policy"]["split_boundaries"]["freeze_confirmation"]["splits"],
            ["final_holdout"],
        )
        self.assertFalse(research_ledger["decision_audit"]["generation_uses_final_holdout"])
        self.assertFalse(research_ledger["decision_audit"]["parameter_selection_uses_final_holdout"])
        self.assertFalse(research_ledger["decision_audit"]["promotion_uses_final_holdout"])
        self.assertFalse(research_ledger["decision_audit"]["candidate_ranking_uses_final_holdout"])
        self.assertFalse(research_ledger["decision_audit"]["ranking_objective_switching_uses_final_holdout"])
        self.assertTrue(research_ledger["decision_audit"]["final_holdout_evaluated"])
        self.assertEqual(
            research_ledger["promotion_policy"]["execution_validation"]["status"],
            rows[0]["execution_validation"]["status"],
        )
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
        self.assertEqual(rows[0]["module_id"], "opening_range_breakout")
        self.assertEqual(rows[0]["module"]["minimum_sample_size"], 100)
        self.assertEqual(rows[0]["strategy_card"]["module_id"], "opening_range_breakout")
        self.assertEqual(rows[0]["strategy_card"]["timeframe"], "1m")
        self.assertEqual(module_memory[0]["module_id"], "opening_range_breakout")
        self.assertEqual(module_memory[0]["strategy_spec_hash"], result.strategy_spec_hash)
        self.assertEqual(module_memory[0]["trade_count"], result.aggregate_test_metrics.trade_count)
        self.assertIn("feature_names", module_memory[0])
        self.assertIn("feature_categories", module_memory[0])
        self.assertIn("feature_count", module_memory[0])
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
            "plus_1_round_trip_tick",
        )
        self.assertIn("execution_stress_summary", rows[0])
        self.assertIn("plus_2_round_trip_ticks_survives", rows[0]["execution_stress_summary"])
        self.assertTrue(any(gate["name"] == "cost_stress_survival" for gate in rows[0]["hard_gate_report"]))
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
        self.assertEqual(len(result.cost_sensitivity_report["stress_scenarios"]), 5)
        self.assertEqual(result.promotion_report["stage"], "direct_bar")
        self.assertEqual(result.strategy_card["promotion_stage"], "direct_bar")
        self.assertTrue(result.strategy_card["candidate_stage"].endswith("_candidate"))
        self.assertTrue(result.hard_gate_report)
        self.assertTrue(any(not gate["passed"] for gate in result.hard_gate_report))
        self.assertEqual(result.final_holdout_policy["status"], "freeze_confirmed_after_hidden_holdout_gate")
        self.assertEqual(
            result.final_holdout_policy["run_timing"],
            "deterministic_freeze_confirmation_after_candidate_evaluation",
        )
        self.assertFalse(result.final_holdout_policy["separate_freeze_task_required_for_strict_plan"])
        self.assertTrue(result.final_holdout_policy["strict_freeze_task_implemented"])
        self.assertFalse(result.final_holdout_policy["candidate_leaderboard_uses_final_holdout_details"])
        self.assertTrue(result.final_holdout_policy["freeze_confirmed_leaderboard_uses_final_holdout_details"])
        self.assertFalse(result.final_holdout_policy["generation_uses_final_holdout"])
        self.assertFalse(result.final_holdout_policy["parameter_selection_uses_final_holdout"])
        self.assertFalse(result.final_holdout_policy["promotion_uses_final_holdout"])
        self.assertFalse(result.final_holdout_policy["candidate_ranking_uses_final_holdout"])
        self.assertEqual(
            result.final_holdout_policy["final_holdout_evaluation_phase"],
            "freeze_confirmation_after_candidate_decision",
        )
        self.assertIn("passed", result.final_holdout_policy["freeze_gate"])
        self.assertIn("reasons", result.final_holdout_policy["freeze_gate"])
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
            passed_payload["strategy_spec"] = {**passed_payload["strategy_spec"], "symbol": "NQ_CME"}
            passed_payload["execution_validation"] = {
                "required": True,
                "status": "validated",
                "promotion_stage": "direct_tick",
                "paper_shadow_allowed": True,
            }
            passed_payload["execution_evidence"] = {
                "required": True,
                "status": "ready_for_promotion",
                "missing_requirements": [],
                "promotion_gate": {"ready_for_promotion": True},
            }
            passed_payload["final_holdout_policy"] = {
                **passed_payload["final_holdout_policy"],
                "freeze_gate": {"passed": True, "reasons": []},
            }
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

        self.assertEqual(
            report["summary"],
            {"passed": 1, "candidate": 1, "freeze_confirmed": 1, "rejected": 1, "total": 2},
        )
        self.assertEqual(report["conclusion"], "qualified_strategies_found")
        self.assertEqual(report["leaderboard"][0]["experiment_id"], "split_trial_0000")
        self.assertEqual(report["candidate_leaderboard"][0]["experiment_id"], "split_trial_0000")
        self.assertEqual(
            report["freeze_confirmed_leaderboard"][0]["experiment_id"],
            "split_trial_0000",
        )
        self.assertEqual(report["rejected"][0]["experiment_id"], "split_trial_0001")
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            filtered["summary"],
            {"passed": 0, "candidate": 0, "freeze_confirmed": 0, "rejected": 1, "total": 1},
        )
        self.assertEqual(
            filtered["execution_validation_summary"]["overall"],
            {
                "validated": 0,
                "pending_execution_validation": 0,
                "blocked_before_execution": 0,
                "not_required": 1,
                "required": 0,
                "total": 1,
            },
        )
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

    def test_write_research_result_accepts_external_execution_evidence_reports(self) -> None:
        payload = base_spec()
        payload["symbol"] = "NQ_CME"
        spec = parse_strategy_spec(payload)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            experiments_root = root / "experiments"
            evidence_root = root / "evidence"
            evidence_root.mkdir(parents=True)
            quote_report = evidence_root / "quote_replay.json"
            paper_report = evidence_root / "paper_shadow.jsonl"
            write_ready_quote_report(quote_report)
            write_ready_paper_shadow(paper_report)
            for offset in range(40):
                write_breakout_ticks(data_root, date(2025, 1, 1) + timedelta(days=offset), symbol="NQ_CME")

            result = run_research_bar_validation(
                spec=spec,
                symbol_config=replace(symbol_config(), alias="NQ_CME", provider="firstratedata", instrument="NQ"),
                data_root=data_root,
                experiment_id="exp_tick_external_evidence",
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
            output = experiments_root / "exp_tick_external_evidence" / "leaderboard.json"
            write_research_result(
                output,
                result,
                quote_reports=[quote_report],
                paper_reports=[paper_report],
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            research_ledger = json.loads((output.parent / "research_ledger.json").read_text(encoding="utf-8"))
            rows = load_leaderboard(experiments_root)

        self.assertEqual(payload["execution_evidence"]["status"], "ready_for_promotion")
        self.assertEqual(payload["execution_evidence"]["quote_reports"], [str(quote_report)])
        self.assertEqual(payload["execution_evidence"]["paper_reports"], [str(paper_report)])
        self.assertEqual(research_ledger["evidence_inputs"]["quote_reports"], [str(quote_report)])
        self.assertEqual(research_ledger["evidence_inputs"]["paper_reports"], [str(paper_report)])
        self.assertEqual(research_ledger["promotion_policy"]["execution_evidence_status"], "ready_for_promotion")
        self.assertEqual(payload["strategy_card"]["candidate_stage"], payload["candidate_stage"])
        self.assertEqual(rows[0]["execution_evidence"]["status"], "ready_for_promotion")

    def test_load_leaderboard_report_prefers_higher_expectancy_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            experiments_root = Path(temp_dir) / "experiments"
            experiments_root.mkdir(parents=True)

            base_payload = {
                "experiment_id": "expectancy_low",
                "strategy_name": "low_expectancy",
                "gates": {"passed": True, "reasons": []},
                "robustness_score": 0.5,
                "aggregate_validation_metrics": {"net_pnl": 100.0, "sharpe": 1.2},
                "aggregate_test_metrics": {
                    "net_pnl": 500.0,
                    "sharpe": 1.4,
                    "annual_trades": 1200.0,
                    "avg_trade_net_pnl": 0.5,
                },
                "non_overlap_test_metrics": {"net_pnl": 400.0, "sharpe": 1.2, "annual_trades": 1000.0},
                "final_holdout_metrics": {"net_pnl": 300.0},
                "final_holdout_policy": {"strict_freeze_task_implemented": True},
                "strategy_spec": {"symbol": "NQ_CME"},
                "execution_validation": {
                    "required": True,
                    "status": "validated",
                    "promotion_stage": "direct_tick",
                    "paper_shadow_allowed": True,
                },
                "execution_evidence": {
                    "required": True,
                    "status": "ready_for_promotion",
                    "missing_requirements": [],
                    "promotion_gate": {"ready_for_promotion": True},
                },
            }
            higher_expectancy = {
                **base_payload,
                "experiment_id": "expectancy_high",
                "strategy_name": "high_expectancy",
                "aggregate_test_metrics": {
                    **base_payload["aggregate_test_metrics"],
                    "net_pnl": 450.0,
                    "avg_trade_net_pnl": 1.25,
                },
            }

            for payload in (base_payload, higher_expectancy):
                experiment_dir = experiments_root / payload["experiment_id"]
                experiment_dir.mkdir(parents=True)
                (experiment_dir / "leaderboard.json").write_text(json.dumps(payload), encoding="utf-8")

            report = load_leaderboard_report(experiments_root)

        self.assertEqual(report["candidate_leaderboard"][0]["experiment_id"], "expectancy_high")
        self.assertEqual(report["leaderboard"][0]["experiment_id"], "expectancy_high")

    def test_candidate_ranking_ignores_final_holdout_until_freeze_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            experiments_root = Path(temp_dir) / "experiments"
            experiments_root.mkdir(parents=True)

            base_payload = {
                "strategy_name": "candidate",
                "execution_mode": "tick",
                "gates": {"passed": True, "reasons": []},
                "aggregate_validation_metrics": {
                    "net_pnl": 600.0,
                    "sharpe": 2.0,
                    "avg_trade_net_pnl": 10.0,
                },
                "aggregate_test_metrics": {
                    "net_pnl": 1000.0,
                    "sharpe": 2.5,
                    "annual_trades": 1200.0,
                    "avg_trade_net_pnl": 10.0,
                },
                "non_overlap_test_metrics": {"net_pnl": 900.0, "sharpe": 2.0, "annual_trades": 1000.0},
                "strategy_spec": {"symbol": "NQ_CME"},
                "execution_validation": {
                    "required": True,
                    "status": "validated",
                    "promotion_stage": "direct_tick",
                    "paper_shadow_allowed": True,
                },
                "execution_evidence": {
                    "required": True,
                    "status": "ready_for_promotion",
                    "missing_requirements": [],
                    "promotion_gate": {"ready_for_promotion": True},
                },
            }
            higher_candidate_quality = {
                **base_payload,
                "experiment_id": "higher_candidate_quality_low_holdout",
                "robustness_score": 0.9,
                "final_holdout_metrics": {"net_pnl": 100.0, "avg_trade_net_pnl": 1.0},
                "final_holdout_policy": {
                    "strict_freeze_task_implemented": True,
                    "freeze_gate": {"passed": True, "reasons": []},
                },
            }
            higher_holdout_only = {
                **base_payload,
                "experiment_id": "higher_holdout_only",
                "robustness_score": 0.8,
                "final_holdout_metrics": {"net_pnl": 5000.0, "avg_trade_net_pnl": 500.0},
                "final_holdout_policy": {
                    "strict_freeze_task_implemented": True,
                    "freeze_gate": {"passed": True, "reasons": []},
                },
            }
            failed_freeze_gate = {
                **base_payload,
                "experiment_id": "failed_freeze_gate",
                "robustness_score": 1.0,
                "aggregate_test_metrics": {
                    **base_payload["aggregate_test_metrics"],
                    "avg_trade_net_pnl": 99.0,
                },
                "final_holdout_metrics": {"net_pnl": -100.0, "avg_trade_net_pnl": -20.0},
                "final_holdout_policy": {
                    "strict_freeze_task_implemented": True,
                    "freeze_gate": {"passed": False, "reasons": ["net_pnl_final_holdout"]},
                },
            }

            for payload in (higher_holdout_only, higher_candidate_quality, failed_freeze_gate):
                experiment_dir = experiments_root / payload["experiment_id"]
                experiment_dir.mkdir(parents=True)
                (experiment_dir / "leaderboard.json").write_text(json.dumps(payload), encoding="utf-8")

            report = load_leaderboard_report(experiments_root)

        self.assertEqual(
            report["candidate_leaderboard"][0]["experiment_id"],
            "failed_freeze_gate",
        )
        self.assertEqual(
            report["candidate_leaderboard"][1]["experiment_id"],
            "higher_candidate_quality_low_holdout",
        )
        self.assertEqual(
            report["candidate_leaderboard"][2]["experiment_id"],
            "higher_holdout_only",
        )
        self.assertNotIn(
            "failed_freeze_gate",
            [row["experiment_id"] for row in report["leaderboard"]],
        )
        self.assertEqual(report["leaderboard"][0]["experiment_id"], "higher_candidate_quality_low_holdout")

    def test_load_leaderboard_report_requires_execution_validation_for_primary_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            experiments_root = Path(temp_dir) / "experiments"
            experiments_root.mkdir(parents=True)

            bar_only_payload = {
                "experiment_id": "nq_cme_bar_only",
                "strategy_name": "bar_only_candidate",
                "execution_mode": "bar",
                "gates": {"passed": True, "reasons": []},
                "robustness_score": 0.6,
                "aggregate_validation_metrics": {"net_pnl": 100.0, "sharpe": 1.1},
                "aggregate_test_metrics": {
                    "net_pnl": 600.0,
                    "sharpe": 1.5,
                    "annual_trades": 1200.0,
                    "avg_trade_net_pnl": 1.0,
                },
                "non_overlap_test_metrics": {"net_pnl": 500.0, "sharpe": 1.3, "annual_trades": 1000.0},
                "final_holdout_metrics": {"net_pnl": 320.0},
                "final_holdout_policy": {"strict_freeze_task_implemented": True},
                "promotion_report": {"mode": "bar", "stage": "direct_bar", "promoted": False, "reasons": []},
                "strategy_spec": {"symbol": "NQ_CME"},
            }
            tick_validated_payload = {
                **bar_only_payload,
                "experiment_id": "nq_cme_tick_validated",
                "strategy_name": "tick_validated_candidate",
                "execution_mode": "tick",
                "aggregate_test_metrics": {
                    **bar_only_payload["aggregate_test_metrics"],
                    "net_pnl": 550.0,
                    "avg_trade_net_pnl": 0.9,
                },
                "promotion_report": {"mode": "tick", "stage": "direct_tick", "promoted": True, "reasons": []},
            }
            tick_validated_without_evidence = {
                **tick_validated_payload,
                "experiment_id": "nq_cme_tick_missing_evidence",
                "strategy_name": "tick_missing_evidence_candidate",
                "aggregate_test_metrics": {
                    **bar_only_payload["aggregate_test_metrics"],
                    "net_pnl": 575.0,
                    "avg_trade_net_pnl": 0.95,
                },
            }
            blocked_payload = {
                **bar_only_payload,
                "experiment_id": "nq_cme_prescreen_blocked",
                "strategy_name": "blocked_candidate",
                "gates": {"passed": False, "reasons": ["pre_screen_failed"]},
                "robustness_score": None,
                "promotion_report": {
                    "mode": "bar",
                    "stage": "pre_screen_rejected",
                    "promoted": False,
                    "reasons": ["insufficient_edge"],
                },
                "pre_screen_report": {
                    "passed": False,
                    "prescreen_stage": "hard_gate_rejected",
                    "trades_per_day": 1.5,
                    "validation_avg_mid_trade": 4.0,
                    "validation_cost_coverage": 0.8,
                    "stress_survival_flag": False,
                },
            }

            for payload in (bar_only_payload, tick_validated_payload, tick_validated_without_evidence, blocked_payload):
                experiment_dir = experiments_root / payload["experiment_id"]
                experiment_dir.mkdir(parents=True)
                (experiment_dir / "leaderboard.json").write_text(json.dumps(payload), encoding="utf-8")
            (experiments_root / "nq_cme_tick_validated" / "quote_replay.json").write_text(
                json.dumps(
                    {
                        "artifact": "quote_execution_validation",
                        "trade_count": 2,
                        "validated_trade_count": 2,
                        "avg_bid_ask_cost_usd": 10.0,
                        "execution_models": {
                            "market_order_bid_ask_replay": {},
                            "fixed_conservative": {},
                            "limit_missed_fill": {"fill_rate": 0.5},
                            "adverse_selection": {"5m": {"avg_ticks": 1.25}},
                        },
                    }
                ),
                encoding="utf-8",
            )
            paper_events = []
            for index, day in enumerate(("2026-04-27", "2026-04-28", "2026-04-29")):
                paper_events.append(
                    {
                        "event_type": "paper_shadow_intent",
                        "intent": {
                            "intent_id": f"signal_{index}",
                            "source_strategy": {"strategy_spec_hash": "hash", "regime": "trend"},
                            "expected_edge": 12.5,
                        },
                        "market_snapshot": {
                            "snapshot_time": f"{day}T13:30:00",
                            "spread_ticks": 2,
                            "adverse_selection_ticks_5m": -1,
                        },
                        "paper_shadow_run": {
                            "paper_shadow_run_id": f"ps_{index}",
                            "intent_id": f"signal_{index}",
                            "blocked": False,
                            "created_at": f"{day}T13:30:00Z",
                            "hypothetical_fill": {"fill_price": 19000 + index},
                        },
                        "llm_diagnosis": {"summary": "ok"},
                        "mutation_proposal": {"allowed_mutations": ["tighten_time_window"], "blocked_mutations": []},
                    }
                )
            (experiments_root / "nq_cme_tick_validated" / "paper_shadow.jsonl").write_text(
                "\n".join(json.dumps(event) for event in paper_events),
                encoding="utf-8",
            )

            report = load_leaderboard_report(experiments_root)

        self.assertEqual(report["candidate_leaderboard"][0]["experiment_id"], "nq_cme_bar_only")
        self.assertEqual(report["candidate_leaderboard"][0]["execution_validation"]["status"], "pending_execution_validation")
        self.assertFalse(report["candidate_leaderboard"][0]["execution_validation"]["paper_shadow_allowed"])
        self.assertEqual(report["candidate_leaderboard"][0]["candidate_stage"], "bar_validated_candidate")
        self.assertEqual(report["candidate_leaderboard"][0]["execution_evidence"]["status"], "awaiting_execution_validation")
        self.assertEqual(
            report["candidate_leaderboard"][0]["execution_evidence"]["paper_shadow_gate"],
            "blocked_until_execution_validation",
        )
        self.assertEqual(report["leaderboard"][0]["experiment_id"], "nq_cme_tick_validated")
        self.assertEqual(report["leaderboard"][0]["execution_validation"]["status"], "validated")
        self.assertTrue(report["leaderboard"][0]["execution_validation"]["paper_shadow_allowed"])
        self.assertEqual(report["leaderboard"][0]["candidate_stage"], "freeze_confirmed_candidate")
        self.assertEqual(report["leaderboard"][0]["execution_evidence"]["status"], "ready_for_promotion")
        self.assertEqual(
            report["leaderboard"][0]["execution_evidence"]["paper_shadow_gate"],
            "allowed_after_execution_validation",
        )
        self.assertEqual(report["candidate_leaderboard"][1]["experiment_id"], "nq_cme_tick_missing_evidence")
        self.assertEqual(report["candidate_leaderboard"][1]["execution_evidence"]["status"], "incomplete_evidence")
        self.assertEqual(report["candidate_leaderboard"][1]["candidate_stage"], "execution_validated_candidate")
        self.assertEqual(report["rejected"][0]["candidate_stage"], "prescreen_rejected_candidate")
        self.assertFalse(report["rejected"][0]["execution_validation"]["paper_shadow_allowed"])
        self.assertEqual(report["rejected"][0]["prescreen_stage"], "hard_gate_rejected")
        self.assertEqual(report["rejected"][0]["trades_per_day"], 1.5)
        self.assertEqual(report["rejected"][0]["validation_avg_mid_trade"], 4.0)
        self.assertEqual(report["rejected"][0]["validation_cost_coverage"], 0.8)
        self.assertFalse(report["rejected"][0]["stress_survival_flag"])
        self.assertEqual(report["rejected"][0]["execution_evidence"]["status"], "blocked_before_execution")
        self.assertEqual(
            report["execution_validation_summary"]["overall"],
            {
                "validated": 2,
                "pending_execution_validation": 1,
                "blocked_before_execution": 1,
                "not_required": 0,
                "required": 4,
                "total": 4,
            },
        )
        self.assertEqual(
            report["execution_validation_summary"]["candidate"],
            {
                "validated": 2,
                "pending_execution_validation": 1,
                "blocked_before_execution": 0,
                "not_required": 0,
                "required": 3,
                "total": 3,
            },
        )
        self.assertEqual(
            report["execution_validation_summary"]["primary_candidate"],
            {
                "validated": 2,
                "pending_execution_validation": 1,
                "blocked_before_execution": 0,
                "not_required": 0,
                "required": 3,
                "total": 3,
            },
        )
        self.assertEqual(report["execution_validation_summary"]["freeze_confirmed"], 1)
        self.assertEqual(
            report["execution_evidence_summary"]["statuses"],
            {
                "ready_for_promotion": 1,
                "incomplete_evidence": 1,
                "awaiting_execution_validation": 1,
                "blocked_before_execution": 1,
                "not_required": 0,
                "total": 4,
            },
        )
        self.assertEqual(
            report["candidate_stage_summary"],
            {
                "bar_validated_candidate": 1,
                "freeze_confirmed_candidate": 1,
                "execution_validated_candidate": 1,
                "prescreen_rejected_candidate": 1,
            },
        )
        self.assertIn("1 passed candidates still await execution validation", report["message"])
        self.assertIn("1 primary-track runs were blocked before execution validation", report["message"])
        self.assertIn("1 validated runs still lack complete quote/paper execution evidence", report["message"])
        self.assertEqual(report["execution_evidence_summary"]["missing_requirements"], {"paper_shadow": 2, "quote_replay": 2})

    def test_cfd_proxy_run_cannot_enter_primary_final_leaderboard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            experiments_root = Path(temp_dir) / "experiments"
            experiments_root.mkdir(parents=True)
            cfd_proxy_payload = {
                "experiment_id": "nqmain_proxy_passed",
                "strategy_name": "proxy_candidate",
                "execution_mode": "bar",
                "gates": {"passed": True, "reasons": []},
                "robustness_score": 0.9,
                "aggregate_validation_metrics": {"net_pnl": 100.0, "sharpe": 2.0, "avg_trade_net_pnl": 10.0},
                "aggregate_test_metrics": {
                    "net_pnl": 1000.0,
                    "sharpe": 3.0,
                    "annual_trades": 1500.0,
                    "avg_trade_net_pnl": 25.0,
                },
                "non_overlap_test_metrics": {"net_pnl": 900.0, "sharpe": 2.8, "annual_trades": 1400.0},
                "final_holdout_metrics": {"net_pnl": 500.0, "avg_trade_net_pnl": 20.0},
                "final_holdout_policy": {"strict_freeze_task_implemented": True},
                "promotion_report": {"mode": "bar", "stage": "direct_bar", "promoted": False, "reasons": []},
                "strategy_spec": {"symbol": "NQmain"},
            }
            experiment_dir = experiments_root / cfd_proxy_payload["experiment_id"]
            experiment_dir.mkdir(parents=True)
            (experiment_dir / "leaderboard.json").write_text(json.dumps(cfd_proxy_payload), encoding="utf-8")

            report = load_leaderboard_report(experiments_root)

        self.assertEqual(report["candidate_leaderboard"][0]["experiment_id"], "nqmain_proxy_passed")
        self.assertFalse(report["candidate_leaderboard"][0]["primary_conclusion_eligible"])
        self.assertEqual(report["candidate_leaderboard"][0]["execution_validation"]["status"], "not_required")
        self.assertEqual(report["leaderboard"], [])
        self.assertEqual(report["freeze_confirmed_leaderboard"], [])

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
        expected_stage = (
            "freeze_confirmed_candidate"
            if result.final_holdout_policy["freeze_gate"]["passed"]
            else "execution_validated_candidate"
        )
        self.assertEqual(result.strategy_card["candidate_stage"], expected_stage)
        self.assertEqual(result.strategy_card["key_metrics"]["test"]["trade_count"], result.aggregate_test_metrics.trade_count)
        self.assertFalse(result.final_holdout_policy["promotion_uses_final_holdout"])

    def test_prescreen_failure_blocks_bar_then_tick_promotion(self) -> None:
        spec = parse_strategy_spec(base_spec())
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir) / "data"
            for offset in range(20):
                day = date(2025, 1, 1) + timedelta(days=offset)
                write_breakout_day(data_root, day, symbol="NQ_CME")
                write_breakout_ticks(data_root, day, symbol="NQ_CME")

            results = run_budgeted_research(
                seed_spec=spec,
                symbol_config=replace(symbol_config(), alias="NQ_CME", provider="firstratedata", instrument="NQ"),
                data_root=data_root,
                experiment_id="staged_prescreen_block",
                date_from=date(2025, 1, 1),
                date_to=date(2025, 1, 20),
                max_trials=1,
                train_days=5,
                validation_days=5,
                test_days=5,
                step_days=5,
                embargo_days=0,
                final_holdout_days=5,
                min_folds=1,
                execution_mode="bar_then_tick",
            )

        result = results[0]
        self.assertEqual(result.execution_mode, "bar")
        self.assertFalse(result.gates["passed"])
        self.assertIn("trade_count_below_prescreen_minimum", result.gates["reasons"])
        self.assertFalse(result.pre_screen_report["passed"])
        self.assertTrue(result.pre_screen_report["hard_gate_enforced"])
        self.assertEqual(result.pre_screen_report["stage"], "hard_gate")
        self.assertEqual(result.pre_screen_report["prescreen_stage"], "hard_gate_rejected")
        self.assertIsNotNone(result.pre_screen_report["trades_per_day"])
        self.assertIn("validation_cost_coverage", result.pre_screen_report)
        self.assertIn("stress_survival_flag", result.pre_screen_report)
        self.assertIsNone(result.robustness_score)
        self.assertEqual(result.promotion_report["mode"], "bar_then_tick")
        self.assertEqual(result.promotion_report["stage"], "bar_rejected_before_tick")
        self.assertFalse(result.promotion_report["promoted"])
        self.assertIn("trade_count_below_prescreen_minimum", result.promotion_report["reasons"])
        self.assertEqual(result.strategy_card["candidate_stage"], "prescreen_rejected_candidate")

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
