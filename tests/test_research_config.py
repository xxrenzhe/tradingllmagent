from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from tlm.cli import build_parser
from tlm.research_config import (
    PRIMARY_COST_MODEL,
    PRIMARY_RESEARCH_SYMBOL,
    PRIMARY_RESEARCH_TIMEFRAME,
    PRIMARY_TOP_STRATEGY_OBJECTIVE,
    ExecutionValidationPolicy,
    RankingPolicy,
    ResearchPolicy,
    ValidationWindowPolicy,
    primary_research_policy,
)
from tlm.research_pipeline import ResearchPipeline
from tlm.strategy import parse_strategy_spec

from test_strategy_backtest import base_spec


class PrimaryResearchPolicyTests(unittest.TestCase):
    def test_primary_research_policy_uses_nq_cme_defaults(self) -> None:
        policy = primary_research_policy()

        self.assertEqual(policy.symbol, PRIMARY_RESEARCH_SYMBOL)
        self.assertEqual(policy.timeframe, PRIMARY_RESEARCH_TIMEFRAME)
        self.assertEqual(policy.cost_model, PRIMARY_COST_MODEL)
        self.assertEqual(policy.top_strategy_objective, PRIMARY_TOP_STRATEGY_OBJECTIVE)
        self.assertIsInstance(policy.validation, ValidationWindowPolicy)
        self.assertIsInstance(policy.execution, ExecutionValidationPolicy)
        self.assertIsInstance(policy.ranking, RankingPolicy)
        self.assertEqual(policy.execution.default_execution_mode, "bar_then_tick")

    def test_primary_pipeline_forces_policy_defaults_into_runner(self) -> None:
        captured = {}

        def fake_runner(**kwargs):
            captured.update(kwargs)
            return []

        legacy_spec = parse_strategy_spec({**base_spec(), "symbol": "NQmain", "cost_model": "default"})
        policy = ResearchPolicy()
        with tempfile.TemporaryDirectory() as temp_dir:
            result = ResearchPipeline(policy=policy, runner=fake_runner).run(
                seed_spec=legacy_spec,
                data_root=Path(temp_dir) / "data",
                experiments_root=Path(temp_dir) / "experiments",
                experiment_db=Path(temp_dir) / "research.sqlite3",
                config_dir=Path("configs"),
                date_from=date(2025, 1, 1),
                date_to=date(2025, 12, 31),
                experiment_id="primary_policy_test",
            )

        self.assertEqual(captured["seed_spec"].symbol, PRIMARY_RESEARCH_SYMBOL)
        self.assertEqual(captured["seed_spec"].timeframe, PRIMARY_RESEARCH_TIMEFRAME)
        self.assertEqual(captured["seed_spec"].cost_model, PRIMARY_COST_MODEL)
        self.assertEqual(captured["symbol_config"].alias, PRIMARY_RESEARCH_SYMBOL)
        self.assertEqual(captured["execution_mode"], policy.execution.default_execution_mode)
        self.assertEqual(captured["train_days"], policy.validation.train_days)
        self.assertEqual(captured["final_holdout_days"], policy.validation.final_holdout_days)
        self.assertEqual(result.policy["ranking"]["primary_objective"], PRIMARY_TOP_STRATEGY_OBJECTIVE)

    def test_cli_exposes_primary_research_entrypoint_without_symbol_override(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "research",
                "primary-run",
                "--spec",
                "strategies/example.json",
                "--from",
                "2025-01-01",
                "--to",
                "2025-12-31",
            ]
        )

        self.assertEqual(args.research_command, "primary-run")
        self.assertFalse(hasattr(args, "symbol"))
        self.assertFalse(hasattr(args, "execution_mode"))


if __name__ == "__main__":
    unittest.main()
