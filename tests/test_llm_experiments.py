from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from datetime import datetime
from pathlib import Path

from tlm.backtest import Trade
from tlm.experiments import (
    load_experiment_audit_logs,
    load_experiment_summary,
    record_audit_event,
    record_experiment,
    record_trial,
)
from tlm.llm import (
    DeterministicLocalLLM,
    OpenAICompatibleLLM,
    append_audit_log,
    create_llm_adapter,
    load_train_validation_feedback,
    train_validation_feedback,
)
from tlm.metrics import BacktestMetrics
from tlm.research import (
    ResearchRunResult,
    ResearchSplitArtifact,
    StrategyTargetCriteria,
    discover_strategy_seed_specs,
    evaluate_strategy_target,
    run_llm_target_discovery,
    run_research_bar_validation,
    write_research_result,
)
from tlm.strategy import parse_strategy_spec
from tlm.validation import generate_rolling_folds
from tlm.variants import parameter_grid_metadata, stable_hash

from test_strategy_backtest import base_spec
from test_validation_leaderboard import symbol_config, write_breakout_day


class LLMAndExperimentTests(unittest.TestCase):
    def test_local_llm_proposal_is_valid_and_audited(self) -> None:
        seed = parse_strategy_spec(base_spec())
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            proposal = DeterministicLocalLLM().propose(seed)
            append_audit_log(audit_path, proposal.audit_record())
            records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(proposal.strategy.strategy_family, "opening_range_breakout")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["prompt_hash"], proposal.prompt_hash)
        self.assertIn("strategy_spec_hash", records[0])
        self.assertEqual(records[0]["metadata"]["provider"], "deterministic")

    def test_openai_compatible_adapter_uses_mock_transport_and_audits_usage(self) -> None:
        seed = parse_strategy_spec(base_spec())
        calls = []

        def transport(url, payload, headers, timeout_seconds):
            calls.append((url, payload, headers, timeout_seconds))
            raw = dict(seed.raw)
            raw["name"] = "mock_live_llm_candidate"
            return {
                "choices": [{"message": {"content": json.dumps({"strategy_spec": raw})}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }

        adapter = OpenAICompatibleLLM(
            model="gpt-test",
            parameters={
                "provider": "openai_compatible",
                "api_key": "test-key",
                "base_url": "https://llm.example/v1",
                "temperature": 0,
            },
            transport=transport,
        )
        proposal = adapter.propose(seed, feedback=[{"folds": []}])
        audit = proposal.audit_record()

        self.assertEqual(proposal.strategy.name, "mock_live_llm_candidate")
        self.assertEqual(calls[0][0], "https://llm.example/v1/chat/completions")
        self.assertEqual(calls[0][1]["temperature"], 0)
        self.assertNotIn("api_key", calls[0][1])
        self.assertEqual(audit["metadata"]["provider"], "openai_compatible")
        self.assertEqual(audit["metadata"]["usage"]["total_tokens"], 15)

    def test_create_llm_adapter_requires_key_for_live_provider(self) -> None:
        with self.assertRaises(ValueError):
            create_llm_adapter(
                "gpt-test",
                {"provider": "openai_compatible", "api_key_env": "TLM_MISSING_KEY"},
            )

    def test_feedback_excludes_test_and_holdout_metrics(self) -> None:
        spec = parse_strategy_spec(base_spec())
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir) / "data"
            for offset in range(40):
                write_breakout_day(data_root, date(2025, 1, 1) + timedelta(days=offset))
            result = run_research_bar_validation(
                spec=spec,
                symbol_config=symbol_config(),
                data_root=data_root,
                experiment_id="exp_feedback",
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
            feedback = train_validation_feedback([result])

        payload = json.dumps(feedback, sort_keys=True)
        self.assertIn("validation_metrics", payload)
        self.assertNotIn("test_metrics", payload)
        self.assertNotIn("final_holdout", payload)

    def test_load_train_validation_feedback_from_persisted_results_is_safe(self) -> None:
        spec = parse_strategy_spec(base_spec())
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            experiments_root = root / "experiments"
            for offset in range(40):
                write_breakout_day(data_root, date(2025, 1, 1) + timedelta(days=offset))
            result = run_research_bar_validation(
                spec=spec,
                symbol_config=symbol_config(),
                data_root=data_root,
                experiment_id="safe_feedback_trial_0000",
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
            write_research_result(
                experiments_root / result.experiment_id / "leaderboard.json",
                result,
            )
            feedback = load_train_validation_feedback(
                experiments_root,
                experiment_id="safe_feedback",
            )

        payload = json.dumps(feedback, sort_keys=True)
        self.assertEqual(len(feedback), 1)
        self.assertIn("train_metrics", payload)
        self.assertIn("validation_metrics", payload)
        self.assertNotIn("test_metrics", payload)
        self.assertNotIn("final_holdout", payload)

    def test_discover_strategy_seed_specs_filters_local_templates(self) -> None:
        seeds, report = discover_strategy_seed_specs(
            Path("strategies"),
            symbol="NQmain",
            timeframe="1m",
            strategy_families=["opening_range_breakout", "intraday_momentum"],
        )

        self.assertGreaterEqual(len(seeds), 2)
        self.assertEqual(report["selected_count"], len(seeds))
        self.assertTrue(all(seed.symbol == "NQmain" for seed in seeds))
        self.assertEqual(
            {seed.strategy_family for seed in seeds},
            {"opening_range_breakout", "intraday_momentum"},
        )

    def test_discover_strategy_seed_specs_reports_no_matching_seed(self) -> None:
        seeds, report = discover_strategy_seed_specs(Path("strategies"), symbol="UNKNOWN")

        self.assertEqual(seeds, [])
        self.assertEqual(report["selected_count"], 0)
        self.assertTrue(report["skipped"])

    def test_experiment_database_records_trials_and_audit_events(self) -> None:
        spec = parse_strategy_spec(base_spec())
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_root = temp_path / "data"
            db_path = temp_path / "experiments.sqlite3"
            for offset in range(40):
                write_breakout_day(data_root, date(2025, 1, 1) + timedelta(days=offset))
            result = run_research_bar_validation(
                spec=spec,
                symbol_config=symbol_config(),
                data_root=data_root,
                experiment_id="exp_db_trial_0000",
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
            record_experiment(db_path, "exp_db", "NQmain", "running")
            record_trial(db_path, "exp_db", result)
            record_audit_event(
                db_path,
                "exp_db",
                "research_trial_completed",
                {"prompt_hash": result.prompt_hash, "strategy_spec_hash": result.strategy_spec_hash},
                trial_id=result.experiment_id,
            )
            record_experiment(db_path, "exp_db", "NQmain", "completed")
            summary = load_experiment_summary(db_path, "exp_db")
            audit_logs = load_experiment_audit_logs(db_path, "exp_db")

        self.assertEqual(summary["experiment"]["status"], "completed")
        self.assertEqual(len(summary["trials"]), 1)
        self.assertEqual(summary["trials"][0]["trial_id"], "exp_db_trial_0000")
        self.assertEqual(summary["trials"][0]["positive_year_ratio"], result.positive_year_ratio)
        self.assertEqual(summary["trials"][0]["round_trip_cost"], result.round_trip_cost)
        self.assertEqual(summary["trials"][0]["yearly_results"], result.yearly_results)
        self.assertEqual(
            summary["trials"][0]["non_overlap_test_fold_indexes"],
            result.non_overlap_test_fold_indexes,
        )
        self.assertIn("validation_to_test_sharpe_decay", summary["trials"][0])
        self.assertIn("test_to_holdout_sharpe_decay", summary["trials"][0])
        self.assertIn("sharpe_non_overlap_test", summary["trials"][0])
        self.assertIn("promotion_report", summary["trials"][0])
        self.assertIn("strategy_card", summary["trials"][0])
        self.assertTrue(summary["trials"][0]["next_round_suggestions"])
        self.assertFalse(summary["trials"][0]["final_holdout_policy"]["llm_feedback_includes_final_holdout"])
        self.assertEqual(len(audit_logs), 1)
        self.assertEqual(audit_logs[0]["event_type"], "research_trial_completed")
        self.assertEqual(audit_logs[0]["payload"]["strategy_spec_hash"], result.strategy_spec_hash)

    def test_target_criteria_requires_frequency_sharpe_and_win_probability(self) -> None:
        passing = fake_research_result(
            "target_pass",
            annual_trades=1200,
            sharpe=2.1,
            winning_trades=54,
            losing_trades=46,
        )
        failing = fake_research_result(
            "target_fail",
            annual_trades=999,
            sharpe=1.9,
            winning_trades=53,
            losing_trades=47,
        )

        self.assertTrue(evaluate_strategy_target(passing).passed)
        failed = evaluate_strategy_target(failing)
        self.assertFalse(failed.passed)
        self.assertEqual(
            failed.reasons,
            [
                "annual_trades_below_target",
                "sharpe_below_target",
                "win_probability_below_target",
            ],
        )
        self.assertEqual(passing.to_dict()["win_probability_test"], 0.54)

    def test_llm_target_discovery_stops_when_candidate_meets_target(self) -> None:
        seed = parse_strategy_spec(base_spec())
        adapter = SequencedLLM()

        def research_runner(**kwargs):
            strategy_name = kwargs["seed_spec"].name
            if strategy_name.endswith("0001"):
                return [
                    fake_research_result(
                        f"{kwargs['experiment_id']}_trial_0000",
                        annual_trades=1200,
                        sharpe=2.2,
                        winning_trades=56,
                        losing_trades=44,
                    )
                ]
            return [
                fake_research_result(
                    f"{kwargs['experiment_id']}_trial_0000",
                    annual_trades=800,
                    sharpe=1.5,
                    winning_trades=40,
                    losing_trades=60,
                )
            ]

        discovery = run_llm_target_discovery(
            seed_spec=seed,
            symbol_config=symbol_config(),
            data_root=Path("data"),
            discovery_id="target_discovery",
            date_from=date(2025, 1, 1),
            date_to=date(2025, 2, 1),
            max_rounds=3,
            target_count=1,
            llm_adapter=adapter,
            research_runner=research_runner,
        )

        payload = discovery.to_dict()
        self.assertEqual(payload["stop_reason"], "target_found")
        self.assertEqual(payload["completed_rounds"], 2)
        self.assertEqual(payload["qualified_count"], 1)
        self.assertEqual(payload["qualified_strategies"][0]["metrics"]["win_probability"], 0.56)
        self.assertEqual([audit["round_index"] for audit in payload["proposal_audits"]], [0, 1])

    def test_llm_target_discovery_reports_budget_exhausted(self) -> None:
        seed = parse_strategy_spec(base_spec())

        def research_runner(**kwargs):
            return [
                fake_research_result(
                    f"{kwargs['experiment_id']}_trial_0000",
                    annual_trades=700,
                    sharpe=1.1,
                    winning_trades=45,
                    losing_trades=55,
                )
            ]

        discovery = run_llm_target_discovery(
            seed_spec=seed,
            symbol_config=symbol_config(),
            data_root=Path("data"),
            discovery_id="target_not_found",
            date_from=date(2025, 1, 1),
            date_to=date(2025, 2, 1),
            max_rounds=2,
            llm_adapter=SequencedLLM(),
            research_runner=research_runner,
        )

        payload = discovery.to_dict()
        self.assertEqual(payload["stop_reason"], "budget_exhausted")
        self.assertEqual(payload["conclusion"], "target_not_found")
        self.assertEqual(payload["qualified_count"], 0)

class SequencedLLM:
    def __init__(self) -> None:
        self.count = 0

    def propose(self, seed_spec, feedback=None):
        raw = dict(seed_spec.raw)
        raw["name"] = f"candidate_{self.count:04d}"
        self.count += 1
        strategy = parse_strategy_spec(raw)
        response = json.dumps({"strategy_spec": raw}, sort_keys=True)
        prompt = json.dumps({"feedback_count": len(feedback or [])}, sort_keys=True)
        return ProposalStub(strategy, prompt, response)


class ProposalStub:
    def __init__(self, strategy, prompt: str, response: str) -> None:
        self.model = "sequenced-test"
        self.strategy = strategy
        self.prompt = prompt
        self.response = response
        self.prompt_hash = stable_hash(prompt)
        self.response_hash = stable_hash(response)

    def audit_record(self) -> dict:
        return {
            "model": self.model,
            "prompt": self.prompt,
            "response": self.response,
            "prompt_hash": self.prompt_hash,
            "response_hash": self.response_hash,
            "strategy_name": self.strategy.name,
            "strategy_spec_hash": stable_hash(self.strategy.raw),
        }


def fake_research_result(
    experiment_id: str,
    annual_trades: float,
    sharpe: float,
    winning_trades: int,
    losing_trades: int,
) -> ResearchRunResult:
    spec = parse_strategy_spec(base_spec())
    plan = generate_rolling_folds(
        start=date(2025, 1, 1),
        end=date(2025, 2, 20),
        train_days=5,
        validation_days=5,
        test_days=5,
        step_days=5,
        embargo_days=1,
        final_holdout_days=5,
        min_folds=1,
    )
    metrics = BacktestMetrics(
        trade_count=winning_trades + losing_trades,
        net_pnl=1000,
        gross_profit=winning_trades * 25,
        gross_loss=-(losing_trades * 10),
        profit_factor=2.0,
        sharpe=sharpe,
        max_drawdown=100,
        annual_trades=annual_trades,
        avg_trade_net_pnl=10,
    )
    trades = [
        Trade(
            symbol="NQmain",
            side="long",
            entry_time=datetime(2025, 1, 10, 13, 30) + timedelta(minutes=index),
            exit_time=datetime(2025, 1, 10, 13, 31) + timedelta(minutes=index),
            entry_price=100,
            exit_price=101 if index < winning_trades else 99,
            contracts=1,
            gross_pnl=25 if index < winning_trades else -10,
            fees=0,
            slippage_cost=0,
            net_pnl=25 if index < winning_trades else -10,
            entry_reason="test",
            exit_reason="test",
        )
        for index in range(winning_trades + losing_trades)
    ]
    artifact = ResearchSplitArtifact(
        split="test",
        fold_index=0,
        start=date(2025, 1, 10),
        end=date(2025, 1, 15),
        data_version_hash="data",
        metrics=metrics,
        trades=trades,
        starting_equity=100_000,
    )
    grid = parameter_grid_metadata(spec, max_trials=1)
    return ResearchRunResult(
        experiment_id=experiment_id,
        execution_mode="bar",
        data_version_hash="data",
        snapshot={},
        cost_model={},
        strategy_name=experiment_id,
        strategy_spec_hash=stable_hash(spec.raw),
        strategy_spec=spec.raw,
        prompt_hash="prompt",
        variant_parameters={},
        parameter_grid=grid.to_dict(),
        trial_count=1,
        parameter_combination_count=1,
        parameter_budget_exceeded=False,
        parameter_grid_hash=grid.parameter_grid_hash,
        validation_plan=plan,
        fold_results=[],
        yearly_results=[],
        trade_count_distribution_report={},
        positive_year_ratio=1.0,
        round_trip_cost=0,
        aggregate_validation_metrics=metrics,
        aggregate_test_metrics=metrics,
        overlapping_test_folds=False,
        non_overlap_test_fold_indexes=[0],
        non_overlap_test_metrics=metrics,
        validation_to_test_sharpe_decay=0,
        test_to_holdout_sharpe_decay=0,
        overfitting_report={},
        cost_sensitivity_report={},
        parameter_stability_report={},
        tick_replay_report={},
        final_holdout_data_version_hash="holdout",
        final_holdout_metrics=metrics,
        gates={"passed": True, "reasons": []},
        hard_gate_report=[],
        robustness_score=0.8,
        signal_similarity_report={},
        split_artifacts=[artifact],
        promotion_report={},
        strategy_card={"module_id": "opening_range_breakout"},
        next_round_suggestions=[],
        final_holdout_policy={},
    )


if __name__ == "__main__":
    unittest.main()
