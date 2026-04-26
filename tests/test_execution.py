from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tlm.api import build_execution_intent_response
from tlm.execution import create_execution_intent, evaluate_risk, submit_paper_shadow


def sample_intent_payload() -> dict:
    return {
        "mode": "paper_shadow",
        "account": "Sim101",
        "instrument": "NQ 06-26",
        "action": "buy",
        "quantity": 1,
        "order_type": "market",
        "bracket": {"stop": 18800.0, "limit": 18820.0},
        "source_strategy": {
            "strategy_spec_hash": "abc123",
            "strategy_freeze_id": "freeze_001",
            "module_id": "opening_range_breakout",
        },
        "reason": "test intent",
        "risk_profile": {
            "allowed_accounts": ["Sim101"],
            "allowed_instruments": ["NQ 06-26"],
            "max_quantity": 1,
            "require_bracket": True,
        },
    }


class ExecutionIntentTests(unittest.TestCase):
    def test_execution_intent_passes_paper_shadow_risk_gate(self) -> None:
        response = build_execution_intent_response(sample_intent_payload())

        self.assertEqual(response["intent"]["status"], "risk_approved")
        self.assertTrue(response["risk"]["passed"])
        self.assertEqual(response["intent"]["account"], "Sim101")
        self.assertEqual(response["intent"]["source_strategy"]["module_id"], "opening_range_breakout")

    def test_risk_gate_rejects_missing_bracket_and_disabled_live(self) -> None:
        payload = sample_intent_payload()
        payload["mode"] = "live"
        payload["bracket"] = {}
        payload["quantity"] = 2
        intent = create_execution_intent(payload)
        risk = evaluate_risk(intent, payload["risk_profile"])

        self.assertFalse(risk["passed"])
        self.assertIn("live_profile_disabled", risk["reasons"])
        self.assertIn("live_quantity_above_micro_limit", risk["reasons"])
        self.assertIn("bracket_required", risk["reasons"])

    def test_paper_shadow_appends_audit_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "execution_audit.jsonl"
            response = submit_paper_shadow(sample_intent_payload(), audit_path)
            events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(response["audit_path"], str(audit_path))
        self.assertEqual(events[0]["event_type"], "paper_shadow_intent")
        self.assertEqual(events[0]["risk"]["decision"], "risk_approved")


if __name__ == "__main__":
    unittest.main()
