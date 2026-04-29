from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tlm.readiness import (
    build_external_validation_artifact,
    build_primary_nq_external_readiness,
    evaluate_external_validation,
    write_external_validation_artifact,
    write_primary_nq_external_readiness_artifact,
)
from tlm.storage import normalized_quote_path, write_quotes_parquet


class ExternalValidationReadinessTests(unittest.TestCase):
    def test_paper_shadow_requires_all_coverage_windows(self) -> None:
        decision = evaluate_external_validation(
            "paper_shadow",
            {
                "trading_days": 10,
                "replay_consistent": True,
                "p95_spread_slippage_drift": 0.5,
                "max_allowed_drift": 1.0,
                "coverage_windows": ["open", "midday", "close"],
            },
        )

        self.assertFalse(decision["passed"])
        self.assertEqual(decision["decision"], "blocked")
        self.assertIn("coverage_window:high_impact_event", decision["missing_evidence"])
        self.assertIn("coverage_window:high_volatility", decision["missing_evidence"])

    def test_nt8_sim_requires_external_gateway_evidence(self) -> None:
        decision = evaluate_external_validation(
            "nt8_sim",
            {
                "trading_days": 5,
                "disconnect_reconnect_validated": True,
                "idempotency_validated": True,
                "flatten_validated": True,
                "reconcile_drift_count": 0,
            },
        )

        self.assertFalse(decision["passed"])
        self.assertIn("external_nt8_validated", decision["missing_evidence"])
        self.assertIn("sim_account_only", decision["missing_evidence"])
        self.assertIn("independent_gateway_process", decision["missing_evidence"])

    def test_micro_live_and_controlled_live_pass_only_with_complete_evidence(self) -> None:
        micro_live = evaluate_external_validation(
            "micro_live",
            {
                "external_nt8_validated": True,
                "minimal_position": True,
                "trade_count": 20,
                "manual_approval_audited": True,
                "broker_side_protection": True,
                "unresolved_incident_count": 0,
            },
        )
        controlled_live = evaluate_external_validation(
            "controlled_live",
            {
                "external_broker_validated": True,
                "sample_trades": 100,
                "strategy_profile_whitelisted": True,
                "kill_switch_verified": True,
                "automation_profile_limited": True,
                "high_risk_drift_count": 0,
            },
        )

        self.assertTrue(micro_live["passed"])
        self.assertTrue(controlled_live["passed"])

    def test_external_validation_artifact_roundtrip(self) -> None:
        artifact = build_external_validation_artifact(
            "paper_shadow",
            {
                "trading_days": 10,
                "replay_consistent": True,
                "p95_spread_slippage_drift": 0,
                "max_allowed_drift": 1,
                "coverage_windows": ["open", "midday", "close", "high_volatility", "high_impact_event"],
            },
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "paper_shadow_validation.json"
            write_external_validation_artifact(output, artifact)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["artifact"], "external_validation_evidence")
        self.assertEqual(payload["decision"]["decision"], "ready")
        self.assertTrue(payload["artifact_hash"])

    def test_primary_nq_external_readiness_lists_missing_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = build_primary_nq_external_readiness(
                data_root=root / "data",
                experiments_root=root / "experiments",
                config_dir=Path("configs"),
            )

        self.assertEqual(readiness["artifact"], "primary_nq_external_readiness")
        self.assertEqual(readiness["decision"], "blocked")
        self.assertEqual(readiness["quote_coverage"]["status"], "missing")
        self.assertEqual(readiness["cost_model"]["status"], "frozen_conservative")
        self.assertTrue(readiness["cost_model"]["hash"])
        self.assertIn("representative_nq_cme_quote_data", readiness["missing_external_blockers"])
        self.assertIn("final_holdout_freeze_confirmed_strategy", readiness["missing_external_blockers"])
        self.assertIn("institutional_acceptance_reduced_candidate_volume", readiness["missing_external_blockers"])

    def test_primary_nq_external_readiness_passes_with_quote_freeze_cost_and_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            quote_path = normalized_quote_path(data_root, "NQ_CME", datetime(2026, 4, 29).date())
            write_quotes_parquet(
                quote_path,
                [
                    (
                        "NQ_CME",
                        datetime(2026, 4, 29, 13, 30),
                        19000.0,
                        19000.25,
                        10.0,
                        11.0,
                        19000.125,
                        0.25,
                    )
                ],
            )
            experiment_dir = root / "experiments" / "freeze_ready"
            experiment_dir.mkdir(parents=True)
            (experiment_dir / "leaderboard.json").write_text(
                json.dumps(
                    {
                        "experiment_id": "freeze_ready",
                        "strategy_name": "freeze_ready_candidate",
                        "execution_mode": "tick",
                        "gates": {"passed": True, "reasons": []},
                        "robustness_score": 0.8,
                        "aggregate_validation_metrics": {"net_pnl": 500.0, "sharpe": 2.1},
                        "aggregate_test_metrics": {
                            "net_pnl": 1000.0,
                            "sharpe": 2.5,
                            "annual_trades": 1200.0,
                            "avg_trade_net_pnl": 25.0,
                        },
                        "non_overlap_test_metrics": {"net_pnl": 900.0, "sharpe": 2.0, "annual_trades": 1100.0},
                        "final_holdout_metrics": {"net_pnl": 400.0, "avg_trade_net_pnl": 15.0},
                        "final_holdout_policy": {
                            "strict_freeze_task_implemented": True,
                            "freeze_gate": {"passed": True, "reasons": []},
                        },
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
                ),
                encoding="utf-8",
            )

            readiness = build_primary_nq_external_readiness(
                data_root=data_root,
                experiments_root=root / "experiments",
                config_dir=Path("configs"),
                institutional_acceptance={
                    "reduced_candidate_volume_accepted": True,
                    "accepted_by": "research_committee",
                    "accepted_at": "2026-04-29T00:00:00Z",
                },
            )
            output = root / "artifacts" / "primary_nq_external_readiness.json"
            write_primary_nq_external_readiness_artifact(output, readiness)
            persisted = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(readiness["decision"], "ready")
        self.assertEqual(readiness["quote_coverage"]["file_count"], 1)
        self.assertEqual(readiness["quote_coverage"]["dates"], ["2026-04-29"])
        self.assertEqual(readiness["final_holdout_freeze"]["experiment_ids"], ["freeze_ready"])
        self.assertEqual(readiness["missing_external_blockers"], [])
        self.assertEqual(persisted["artifact_hash"], readiness["artifact_hash"])


if __name__ == "__main__":
    unittest.main()
