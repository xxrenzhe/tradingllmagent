from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from tlm.trigger_gate import (
    append_trigger_gate_memory,
    build_token_budget_report,
    build_trigger_decision_record,
    build_trigger_evidence_record,
    build_trigger_outcome_record,
    load_trigger_gate_memory,
    run_trigger_gate_simulation,
    validate_trigger_gate_response,
)


class TriggerGateMemoryTests(unittest.TestCase):
    def test_evidence_decision_and_outcome_roundtrip(self) -> None:
        evidence = build_trigger_evidence_record(
            strategy_spec_hash="hash_a",
            module_id="module_a",
            strategy_name="strategy_a",
            timeframe="15m",
            signal_time=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
            signal_features={"entry_signal": True, "score": 0.82},
            market_snapshot={"last_price": 19000.0, "spread": 0.5},
            event_context={"event_state": "normal"},
            risk_pre_gate={"passed": True, "reasons": []},
            pool_version="target_frequency_pool.v2",
            trigger_reason="strategy_signal",
        )
        response = {
            "decision": "allow",
            "risk_level": "low",
            "confidence": 0.72,
            "reasons": ["trend aligned"],
            "invalidation": ["break below vwap"],
            "required_follow_up": [],
            "token_budget_note": "within budget",
        }
        decision = build_trigger_decision_record(
            evidence=evidence,
            model="local-gate",
            prompt_payload={"evidence_id": evidence["evidence_id"]},
            response_payload=response,
            input_tokens=100,
            output_tokens=25,
        )
        outcome = build_trigger_outcome_record(
            decision=decision,
            outcome_window="48h",
            net_pnl=120.0,
            mfe=180.0,
            mae=-40.0,
            paper_fill_id="paper_1",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            append_trigger_gate_memory(root, evidence=evidence, decision=decision, outcome=outcome)
            memory = load_trigger_gate_memory(root)

        self.assertEqual(memory["evidence"][0]["evidence_id"], evidence["evidence_id"])
        self.assertEqual(memory["decisions"][0]["runtime_action"], "paper_intent")
        self.assertEqual(memory["decisions"][0]["total_tokens"], 125)
        self.assertEqual(memory["outcomes"][0]["final_label"], "allowed_winner")

    def test_trigger_gate_response_rejects_live_commands(self) -> None:
        with self.assertRaisesRegex(ValueError, "live execution"):
            validate_trigger_gate_response(
                {
                    "decision": "allow",
                    "risk_level": "low",
                    "confidence": 0.9,
                    "live_gateway_command": {"action": "buy"},
                }
            )

    def test_token_budget_report_counts_decisions_and_budget(self) -> None:
        report = build_token_budget_report(
            [
                {"decision": "allow", "input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
                {"decision": "block", "input_tokens": 80, "output_tokens": 15, "total_tokens": 95},
            ],
            daily_token_budget=200,
        )

        self.assertEqual(report["decision_count"], 2)
        self.assertEqual(report["token_total"], 215)
        self.assertTrue(report["over_budget"])
        self.assertEqual(report["remaining_token_budget"], 0)
        self.assertEqual(report["allow_rate"], 0.5)
        self.assertEqual(report["block_rate"], 0.5)

    def test_trigger_gate_simulation_writes_frequency_only_artifacts(self) -> None:
        pool = {
            "pool_version": "target_frequency_pool.v2",
            "selected": [
                {
                    "module_id": "module_a",
                    "strategy_name": "strategy_a",
                    "strategy_spec_hash": "hash_a",
                    "timeframe": "15m",
                    "trades_per_day": 1.0,
                    "proxy_win_rate": 0.61,
                },
                {
                    "module_id": "module_b",
                    "strategy_name": "strategy_b",
                    "strategy_spec_hash": "hash_b",
                    "timeframe": "15m",
                    "trades_per_day": 1.5,
                    "proxy_win_rate": 0.58,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = run_trigger_gate_simulation(
                target_frequency_pool=pool,
                output_dir=Path(temp_dir),
                replay_start="2026-04-25",
                replay_end="2026-04-26",
                enable_llm=False,
            )
            memory = load_trigger_gate_memory(Path(temp_dir))

        self.assertEqual(manifest["mode"], "frequency_only")
        self.assertEqual(manifest["llm_call_count"], 0)
        self.assertEqual(manifest["trigger_count"], 5)
        self.assertEqual(len(memory["evidence"]), 5)
        self.assertEqual(memory["decisions"], [])

    def test_trigger_gate_simulation_records_llm_decisions_and_tokens(self) -> None:
        pool = {
            "pool_version": "target_frequency_pool.v2",
            "selected": [
                {
                    "module_id": "module_a",
                    "strategy_name": "strategy_a",
                    "strategy_spec_hash": "hash_a",
                    "timeframe": "15m",
                    "trades_per_day": 1.0,
                    "proxy_win_rate": 0.61,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = run_trigger_gate_simulation(
                target_frequency_pool=pool,
                output_dir=Path(temp_dir),
                replay_start="2026-04-25",
                replay_end="2026-04-26",
                enable_llm=True,
                daily_token_budget=10_000,
            )
            memory = load_trigger_gate_memory(Path(temp_dir))

        self.assertEqual(manifest["mode"], "llm_enabled")
        self.assertEqual(manifest["llm_call_count"], manifest["trigger_count"])
        self.assertGreater(manifest["token_budget"]["token_total"], 0)
        self.assertTrue(all(row["decision"] == "allow" for row in memory["decisions"]))


if __name__ == "__main__":
    unittest.main()
