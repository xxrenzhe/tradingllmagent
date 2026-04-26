from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tlm.readiness import (
    build_external_validation_artifact,
    evaluate_external_validation,
    write_external_validation_artifact,
)


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


if __name__ == "__main__":
    unittest.main()
