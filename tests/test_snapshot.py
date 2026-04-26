from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from tlm.backtest import default_cost_model
from tlm.config import SymbolConfig
from tlm.snapshot import (
    config_snapshot,
    config_snapshot_hash,
    cost_model_hash,
    fold_definition_hash,
    research_snapshot,
)
from tlm.validation import generate_rolling_folds


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


class SnapshotTests(unittest.TestCase):
    def test_snapshot_hashes_are_stable_for_same_inputs(self) -> None:
        plan = generate_rolling_folds(
            start=date(2025, 1, 1),
            end=date(2025, 2, 9),
            train_days=5,
            validation_days=5,
            test_days=5,
            step_days=5,
            embargo_days=1,
            final_holdout_days=5,
            min_folds=1,
        )
        cost_model = default_cost_model(symbol_config(), "nq_conservative_v1")
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            (config_dir / "symbols.yaml").write_text('{"symbols": {}}\n', encoding="utf-8")
            (config_dir / "costs.yaml").write_text('{"cost_models": {}}\n', encoding="utf-8")
            expected_config_hash = config_snapshot_hash(config_dir)
            expected_config = config_snapshot(config_dir)
            first = research_snapshot(
                plan,
                cost_model,
                config_dir=config_dir,
                random_seed=7,
                experiment_id="exp_001",
                strategy_spec_hash="spec_hash",
                prompt_hash="prompt_hash",
                data_version_hash="data_hash",
                llm_model="local-test-model",
                llm_parameters={"temperature": 0, "max_tokens": 512},
            )
            second = research_snapshot(
                plan,
                cost_model,
                config_dir=config_dir,
                random_seed=7,
                experiment_id="exp_001",
                strategy_spec_hash="spec_hash",
                prompt_hash="prompt_hash",
                data_version_hash="data_hash",
                llm_model="local-test-model",
                llm_parameters={"temperature": 0, "max_tokens": 512},
            )

        self.assertEqual(first, second)
        self.assertEqual(first["fold_definition_hash"], fold_definition_hash(plan))
        self.assertEqual(first["cost_model_hash"], cost_model_hash(cost_model))
        self.assertEqual(first["experiment_id"], "exp_001")
        self.assertEqual(first["strategy_spec_hash"], "spec_hash")
        self.assertEqual(first["prompt_hash"], "prompt_hash")
        self.assertEqual(first["data_version_hash"], "data_hash")
        self.assertEqual(first["llm_model"], "local-test-model")
        self.assertEqual(first["llm_parameters"], {"temperature": 0, "max_tokens": 512})
        self.assertEqual(first["config_snapshot"], expected_config)
        self.assertEqual(first["config_snapshot_hash"], expected_config_hash)


if __name__ == "__main__":
    unittest.main()
