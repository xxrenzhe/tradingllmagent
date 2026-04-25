from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tlm.experiments import load_experiment_summary, record_audit_event, record_experiment, record_trial
from tlm.llm import DeterministicLocalLLM, append_audit_log, train_validation_feedback
from tlm.research import run_research_bar_validation
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

        self.assertEqual(summary["experiment"]["status"], "completed")
        self.assertEqual(len(summary["trials"]), 1)
        self.assertEqual(summary["trials"][0]["trial_id"], "exp_db_trial_0000")


if __name__ == "__main__":
    unittest.main()
