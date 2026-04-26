from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

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
from tlm.research import run_research_bar_validation, write_research_result
from tlm.strategy import parse_strategy_spec

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


if __name__ == "__main__":
    unittest.main()
