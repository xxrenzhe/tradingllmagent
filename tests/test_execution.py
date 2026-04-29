from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tlm.api import build_execution_intent_response
from tlm.execution import (
    build_execution_intent_response_with_registry,
    build_paper_shadow_run,
    build_gateway_command,
    create_execution_state_record,
    create_execution_intent,
    default_paper_shadow_risk_profile,
    evaluate_live_readiness,
    evaluate_risk,
    get_risk_profile,
    load_risk_profile_registry,
    transition_execution_state,
    validate_risk_profile,
    write_risk_profile_registry,
    submit_paper_shadow,
)


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
        "correlation_id": "corr_test",
        "expected_spread_ticks": 1,
        "expected_max_slippage_ticks": 1,
        "risk_profile": {
            "allowed_accounts": ["Sim101"],
            "allowed_instruments": ["NQ 06-26"],
            "max_quantity": 1,
            "require_bracket": True,
            "max_spread_ticks": 2,
            "max_slippage_ticks": 2,
        },
    }


class ExecutionIntentTests(unittest.TestCase):
    def test_execution_intent_passes_paper_shadow_risk_gate(self) -> None:
        response = build_execution_intent_response(sample_intent_payload())

        self.assertEqual(response["intent"]["status"], "risk_approved")
        self.assertTrue(response["risk"]["passed"])
        self.assertEqual(response["intent"]["account"], "Sim101")
        self.assertEqual(response["intent"]["correlation_id"], "corr_test")
        self.assertEqual(response["intent"]["schema_version"], 1)
        self.assertEqual(response["intent"]["protocol_version"], "execution.v1")
        self.assertEqual(response["intent"]["source_strategy"]["module_id"], "opening_range_breakout")

    def test_risk_profile_registry_roundtrip_and_intent_resolution(self) -> None:
        profile = {
            **default_paper_shadow_risk_profile(),
            "profile_id": "sim_nq_one_lot",
            "allowed_accounts": ["Sim101"],
            "allowed_instruments": ["NQ 06-26"],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_path = Path(temp_dir) / "risk_profiles.json"
            registry = write_risk_profile_registry(registry_path, [profile])
            loaded = load_risk_profile_registry(registry_path)
            resolved = get_risk_profile(registry_path, "sim_nq_one_lot")
            payload = sample_intent_payload()
            payload.pop("risk_profile")
            payload["risk_profile_id"] = "sim_nq_one_lot"
            response = build_execution_intent_response_with_registry(payload, registry_path)

        self.assertEqual(registry["profile_count"], 1)
        self.assertEqual(loaded["profiles"][0]["profile_id"], "sim_nq_one_lot")
        self.assertEqual(resolved["max_quantity"], 1)
        self.assertEqual(response["risk_profile_id"], "sim_nq_one_lot")
        self.assertTrue(response["risk"]["passed"])

    def test_risk_profile_validation_rejects_incomplete_profiles(self) -> None:
        validation = validate_risk_profile({"profile_id": "bad", "max_quantity": 0})

        self.assertFalse(validation["valid"])
        self.assertIn("max_quantity_must_be_positive", validation["errors"])
        self.assertIn("missing_allowed_accounts", validation["errors"])

    def test_risk_gate_rejects_missing_bracket_and_disabled_live(self) -> None:
        payload = sample_intent_payload()
        payload["mode"] = "live"
        payload["bracket"] = {}
        payload["quantity"] = 2
        intent = create_execution_intent(payload)
        risk = evaluate_risk(intent, payload["risk_profile"])

        self.assertFalse(risk["passed"])
        self.assertIn("live_profile_disabled", risk["reasons"])
        self.assertIn("quantity_exceeds_limit", risk["reasons"])
        self.assertIn("live_quantity_above_profile_limit", risk["reasons"])
        self.assertIn("bracket_required", risk["reasons"])

    def test_risk_gate_rejects_stale_event_and_spread_conditions(self) -> None:
        payload = sample_intent_payload()
        payload["expected_spread_ticks"] = 4
        payload["risk_profile"] = {
            **payload["risk_profile"],
            "data_stale": True,
            "event_blackout": True,
            "daily_loss_limit_reached": True,
            "max_spread_ticks": 2,
        }
        intent = create_execution_intent(payload)
        risk = evaluate_risk(intent, payload["risk_profile"])

        self.assertFalse(risk["passed"])
        self.assertIn("data_stale", risk["reasons"])
        self.assertIn("event_blackout", risk["reasons"])
        self.assertIn("daily_loss_limit_reached", risk["reasons"])
        self.assertIn("spread_exceeds_limit", risk["reasons"])

    def test_gateway_command_requires_approved_risk(self) -> None:
        payload = sample_intent_payload()
        intent = create_execution_intent(payload)
        risk = evaluate_risk(intent, payload["risk_profile"])
        command = build_gateway_command(intent, risk)

        self.assertEqual(command["type"], "marketOrder")
        self.assertEqual(command["correlation_id"], "corr_test")
        self.assertEqual(command["idempotency_key"], intent.idempotency_key)
        self.assertEqual(command["strategy_freeze_id"], "freeze_001")
        self.assertEqual(command["risk_decision_id"], risk["risk_decision_id"])

    def test_execution_state_machine_requires_approval_before_submission(self) -> None:
        payload = sample_intent_payload()
        payload["intent_id"] = "intent_state"
        intent = create_execution_intent(payload)
        risk = evaluate_risk(intent, payload["risk_profile"])
        command = build_gateway_command(intent, risk)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "execution.sqlite3"
            state = create_execution_state_record(db_path, intent, risk)

            with self.assertRaisesRegex(ValueError, "approved status requires"):
                transition_execution_state(db_path, intent.intent_id, "approved", event_type="human_approval")
            pending = transition_execution_state(
                db_path,
                intent.intent_id,
                "pending_human_approval",
                event_type="approval_required",
            )
            approved = transition_execution_state(
                db_path,
                intent.intent_id,
                "approved",
                event_type="human_approval",
                payload={"approval_id": "approval_1", "approver": "operator", "reason": "paper sim"},
            )
            submitted = transition_execution_state(
                db_path,
                intent.intent_id,
                "submitted",
                event_type="gateway_command",
                payload={"command": command},
            )

        self.assertEqual(state["status"], "risk_approved")
        self.assertEqual(pending["status"], "pending_human_approval")
        self.assertEqual(approved["human_approvals"][0]["approval_id"], "approval_1")
        self.assertEqual(submitted["status"], "submitted")
        self.assertEqual(submitted["audit_events"][-1]["event_type"], "gateway_command")

    def test_execution_state_machine_enforces_terminal_and_gateway_updates(self) -> None:
        payload = sample_intent_payload()
        payload["intent_id"] = "intent_terminal"
        payload["risk_profile"] = {**payload["risk_profile"], "event_blackout": True}
        intent = create_execution_intent(payload)
        risk = evaluate_risk(intent, payload["risk_profile"])

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "execution.sqlite3"
            rejected = create_execution_state_record(db_path, intent, risk)
            with self.assertRaisesRegex(ValueError, "terminal intent"):
                transition_execution_state(
                    db_path,
                    intent.intent_id,
                    "approved",
                    event_type="human_approval",
                    payload={"approval_id": "approval_bad", "approver": "operator"},
                )

        self.assertEqual(rejected["status"], "risk_rejected")

    def test_execution_filled_status_requires_gateway_or_order_update(self) -> None:
        payload = sample_intent_payload()
        payload["intent_id"] = "intent_fill"
        intent = create_execution_intent(payload)
        risk = evaluate_risk(intent, payload["risk_profile"])
        command = build_gateway_command(intent, risk)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "execution.sqlite3"
            create_execution_state_record(db_path, intent, risk)
            transition_execution_state(
                db_path,
                intent.intent_id,
                "approved",
                event_type="automation_profile",
                payload={"automation_profile_id": "paper_shadow_auto"},
            )
            transition_execution_state(
                db_path,
                intent.intent_id,
                "submitted",
                event_type="gateway_command",
                payload={"command": command},
            )
            accepted = transition_execution_state(
                db_path,
                intent.intent_id,
                "accepted",
                event_type="gateway_update",
                payload={"update_id": "update_accepted"},
            )
            with self.assertRaisesRegex(ValueError, "requires gateway/order/reconciliation"):
                transition_execution_state(
                    db_path,
                    intent.intent_id,
                    "filled",
                    event_type="manual_override",
                )
            filled = transition_execution_state(
                db_path,
                intent.intent_id,
                "filled",
                event_type="order_update",
                payload={"update_id": "update_filled", "filled_qty": 1},
            )

        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(filled["status"], "filled")

    def test_paper_shadow_appends_audit_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "execution_audit.jsonl"
            response = submit_paper_shadow(sample_intent_payload(), audit_path)
            events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(response["audit_path"], str(audit_path))
        self.assertEqual(events[0]["event_type"], "paper_shadow_intent")
        self.assertEqual(events[0]["risk"]["decision"], "risk_approved")
        self.assertFalse(events[0]["paper_shadow_run"]["live_gateway_command_created"])

    def test_paper_shadow_run_records_replayable_fill_and_drift(self) -> None:
        payload = {
            **sample_intent_payload(),
            "intent_id": "intent_fixed",
            "idempotency_key": "idem_fixed",
            "market_snapshot": {
                "snapshot_time": "2026-04-27T13:30:00",
                "bid": 19000.0,
                "ask": 19000.5,
                "spread_ticks": 2,
                "tick_size": 0.25,
            },
            "slippage_model": {"tick_size": 0.25, "slippage_ticks": 1},
            "backtest_costs": {"expected_spread_ticks": 1, "expected_slippage_ticks": 0.5},
        }

        first = build_paper_shadow_run(payload)
        second = build_paper_shadow_run(payload)
        run = first["paper_shadow_run"]

        self.assertEqual(first["intent"]["status"], "risk_approved")
        self.assertEqual(run["strategy_spec_hash"], "abc123")
        self.assertEqual(run["module_id"], "opening_range_breakout")
        self.assertFalse(run["blocked"])
        self.assertEqual(run["hypothetical_fill"]["fill_price"], 19000.75)
        self.assertEqual(run["drift_report"]["spread_drift_ticks"], 1)
        self.assertEqual(run["drift_report"]["slippage_drift_ticks"], 0.5)
        self.assertEqual(run["replay_key"], second["paper_shadow_run"]["replay_key"])
        self.assertFalse(run["live_gateway_command_created"])

    def test_paper_shadow_blocks_without_hypothetical_fill_when_risk_rejected(self) -> None:
        payload = sample_intent_payload()
        payload["risk_profile"] = {**payload["risk_profile"], "event_blackout": True}

        run = build_paper_shadow_run(payload)["paper_shadow_run"]

        self.assertTrue(run["blocked"])
        self.assertIn("event_blackout", run["blocked_reasons"])
        self.assertIsNone(run["hypothetical_fill"])
        self.assertFalse(run["live_gateway_command_created"])

    def test_live_readiness_blocks_without_external_validation(self) -> None:
        micro_live = evaluate_live_readiness(
            "micro_live",
            {
                "trade_count": 20,
                "manual_approval_audited": True,
                "broker_side_protection": True,
                "unresolved_incident_count": 0,
            },
        )
        controlled_live = evaluate_live_readiness(
            "controlled_live",
            {
                "sample_trades": 100,
                "strategy_profile_whitelisted": True,
                "kill_switch_verified": True,
                "high_risk_drift_count": 0,
            },
        )

        self.assertFalse(micro_live["passed"])
        self.assertIn("external_nt8_validated", micro_live["reasons"])
        self.assertFalse(controlled_live["passed"])
        self.assertIn("external_broker_validated", controlled_live["reasons"])

    def test_live_readiness_passes_when_all_gates_are_met(self) -> None:
        result = evaluate_live_readiness(
            "paper_shadow",
            {
                "trading_days": 10,
                "replay_consistent": True,
                "p95_spread_slippage_drift": 0.5,
                "max_allowed_drift": 1.0,
            },
        )

        self.assertTrue(result["passed"])
        self.assertEqual(result["decision"], "ready")


if __name__ == "__main__":
    unittest.main()
