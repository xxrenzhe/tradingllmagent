from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tlm.config import get_symbol
from tlm.strategy_generation import generate_vol_strategy_specs
from tlm.vol import (
    VOL_ARTIFACT_FILENAMES,
    build_vol_cost_stress_report,
    build_vol_feature_readiness,
    build_vol_mutation_memory,
    build_vol_paper_shadow_review,
    build_vol_quote_replay_report,
    build_vol_strategy_leaderboard,
    write_vol_research_artifacts,
)


class VolResearchArtifactTests(unittest.TestCase):
    def test_vol_feature_readiness_requires_executable_volume_features(self) -> None:
        report = build_vol_feature_readiness()

        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["bar_volume_semantics"]["databento_ohlcv_mapping"], "tick_count")
        self.assertFalse(report["execution_data_gap"]["ohlcv_has_real_spread"])
        self.assertEqual(report["missing_features"], [])

    def test_vol_artifact_builders_are_explicit_when_execution_data_is_missing(self) -> None:
        leaderboard = build_vol_strategy_leaderboard(Path("missing-experiments"), specs=generate_vol_strategy_specs())
        cost = build_vol_cost_stress_report(leaderboard, get_symbol("NQ_CME"))
        quote = build_vol_quote_replay_report(quote_files=[])
        paper = build_vol_paper_shadow_review([])
        memory = build_vol_mutation_memory(leaderboard)

        self.assertEqual(leaderboard["summary"]["generated_seed_count"], 5)
        self.assertEqual(cost["candidate_count"], 0)
        self.assertEqual(quote["status"], "blocked")
        self.assertEqual(paper["status"], "blocked")
        self.assertEqual(memory["record_count"], 0)

    def test_write_vol_research_artifacts_outputs_required_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = write_vol_research_artifacts(
                Path(temp_dir),
                experiments_root=Path("missing-experiments"),
                symbol_config=get_symbol("NQ_CME"),
                specs=generate_vol_strategy_specs(),
            )

            self.assertEqual(set(paths), set(VOL_ARTIFACT_FILENAMES))
            for path in paths.values():
                payload = json.loads(Path(path).read_text(encoding="utf-8"))
                self.assertIn("artifact", payload)


if __name__ == "__main__":
    unittest.main()
