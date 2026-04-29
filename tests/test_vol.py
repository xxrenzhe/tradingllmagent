from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tlm.config import get_cost_model, get_symbol
from tlm.storage import bar_path, write_bars_parquet
from tlm.strategy_generation import VOL_EXECUTION_AWARE_FAMILIES, generate_vol_strategy_specs, write_vol_strategy_specs
from tlm.vol import (
    VOL_ARTIFACT_FILENAMES,
    build_vol_cost_stress_report,
    build_vol_execution_evidence,
    build_vol_feature_readiness,
    build_vol_llm_trigger_audit,
    build_vol_mutation_memory,
    build_vol_paper_shadow_review,
    build_vol_quote_replay_report,
    build_vol_strategy_leaderboard,
    run_vol_prescreen,
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
        audit = build_vol_llm_trigger_audit(leaderboard, memory)

        self.assertEqual(leaderboard["summary"]["generated_seed_count"], len(VOL_EXECUTION_AWARE_FAMILIES))
        self.assertEqual(cost["candidate_count"], 0)
        self.assertEqual(quote["status"], "blocked")
        self.assertEqual(paper["status"], "blocked")
        self.assertEqual(memory["record_count"], 0)
        self.assertEqual(audit["status"], "blocked")

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

    def test_vol_llm_trigger_audit_records_required_review_contract(self) -> None:
        leaderboard = {
            "candidate_leaderboard": [
                {
                    "experiment_id": "vol_prescreen_candidate",
                    "strategy_name": "nq_vol_execution_candidate",
                    "strategy_family": "vol_breakout_trend",
                    "strategy_spec_hash": "candidate_hash",
                    "passed": True,
                    "final_target_passed": False,
                    "annual_trades_test": 1200,
                    "sharpe_test": 1.2,
                    "win_probability_test": 0.54,
                    "reasons": [],
                    "next_round_suggestions": [],
                    "vol_feature_card": {"feature_names": ["bar_volume"]},
                    "strategy_card": {"strategy_family": "vol_breakout_trend"},
                    "execution_card": {"round_trip_cost_usd": 28.0},
                    "data_version_hash": "data_hash",
                    "feature_snapshot_hash": "feature_hash",
                    "cost_model_hash": "cost_hash",
                }
            ],
            "rejected": [
                {
                    "experiment_id": "vol_prescreen_rejected",
                    "strategy_name": "nq_vol_execution_rejected",
                    "strategy_family": "vol_absorption_reversal",
                    "strategy_spec_hash": "rejected_hash",
                    "passed": False,
                    "final_target_passed": False,
                    "annual_trades_test": 120,
                    "sharpe_test": -0.4,
                    "win_probability_test": 0.49,
                    "reasons": ["annual_trades_test"],
                    "next_round_suggestions": ["tighten_volume_threshold"],
                }
            ],
        }

        audit = build_vol_llm_trigger_audit(leaderboard)

        self.assertEqual(audit["status"], "ready_for_llm_review")
        self.assertEqual(audit["record_count"], 2)
        self.assertGreater(audit["token_cost_report"]["estimated_total_tokens"], 0)
        candidate = audit["records"][0]
        self.assertEqual(candidate["trigger_reason"], "candidate_prescreen_complete")
        self.assertEqual(candidate["llm_call_status"], "queued_not_called")
        self.assertIn("request_quote_replay", candidate["allowed_actions"])
        self.assertIn("direct_live_order", candidate["blocked_actions"])
        self.assertEqual(candidate["mutation_outcome"]["status"], "pending_llm_review")
        self.assertIn("input_artifact_hash", candidate)

    def test_vol_quote_replay_report_summarizes_execution_models(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "quote_replay.json"
            report_path.write_text(
                json.dumps(
                    {
                        "artifact": "quote_execution_validation",
                        "trade_count": 2,
                        "validated_trade_count": 2,
                        "avg_bid_ask_cost_usd": 10.0,
                        "execution_models": {
                            "market_order_bid_ask_replay": {},
                            "fixed_conservative": {},
                            "limit_missed_fill": {"fill_rate": 0.5},
                            "adverse_selection": {"5m": {"avg_ticks": 1.25}},
                        },
                    }
                ),
                encoding="utf-8",
            )

            report = build_vol_quote_replay_report(quote_files=[], existing_reports=[report_path])

            self.assertEqual(report["status"], "ready_for_review")
            self.assertEqual(report["missing_requirements"], [])
            self.assertEqual(report["execution_report_summaries"][0]["limit_fill_rate"], 0.5)

    def test_vol_paper_shadow_review_reads_three_day_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "paper_shadow.jsonl"
            events = []
            for index, day in enumerate(("2026-04-27", "2026-04-28", "2026-04-29")):
                events.append(
                    {
                        "event_type": "paper_shadow_intent",
                        "intent": {
                            "intent_id": f"signal_{index}",
                            "source_strategy": {"strategy_spec_hash": "hash", "regime": "trend"},
                            "expected_edge": 12.5,
                        },
                        "risk": {"decision": "risk_approved", "risk_budget_snapshot": {"daily_loss_remaining": 500}},
                        "market_snapshot": {
                            "snapshot_time": f"{day}T13:30:00",
                            "spread_ticks": 2,
                            "adverse_selection_ticks_5m": -1,
                        },
                        "paper_shadow_run": {
                            "paper_shadow_run_id": f"ps_{index}",
                            "intent_id": f"signal_{index}",
                            "blocked": False,
                            "created_at": f"{day}T13:30:00Z",
                            "hypothetical_fill": {"fill_price": 19000 + index},
                            "drift_report": {"observed_spread_ticks": 2},
                        },
                        "llm_diagnosis": {"summary": "ok"},
                        "mutation_proposal": {"allowed_mutations": ["tighten_time_window"], "blocked_mutations": []},
                    }
                )
            report_path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

            report = build_vol_paper_shadow_review([report_path])

            self.assertEqual(report["status"], "ready_for_review")
            self.assertEqual(report["summary"]["trading_day_count"], 3)
            self.assertEqual(report["summary"]["simulated_fill_count"], 3)
            self.assertEqual(report["records"][0]["actual_fill_mode"], "simulated_market")

    def test_vol_execution_evidence_combines_quote_and_paper_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            quote_path = root / "quote_replay.json"
            paper_path = root / "paper_shadow.jsonl"
            quote_path.write_text(
                json.dumps(
                    {
                        "artifact": "quote_execution_validation",
                        "trade_count": 2,
                        "validated_trade_count": 2,
                        "avg_bid_ask_cost_usd": 10.0,
                        "execution_models": {
                            "market_order_bid_ask_replay": {},
                            "fixed_conservative": {},
                            "limit_missed_fill": {"fill_rate": 0.5},
                            "adverse_selection": {"5m": {"avg_ticks": 1.25}},
                        },
                    }
                ),
                encoding="utf-8",
            )
            paper_events = []
            for index, day in enumerate(("2026-04-27", "2026-04-28", "2026-04-29")):
                paper_events.append(
                    {
                        "event_type": "paper_shadow_intent",
                        "intent": {
                            "intent_id": f"signal_{index}",
                            "source_strategy": {"strategy_spec_hash": "hash", "regime": "trend"},
                            "expected_edge": 12.5,
                        },
                        "market_snapshot": {
                            "snapshot_time": f"{day}T13:30:00",
                            "spread_ticks": 2,
                            "adverse_selection_ticks_5m": -1,
                        },
                        "paper_shadow_run": {
                            "paper_shadow_run_id": f"ps_{index}",
                            "intent_id": f"signal_{index}",
                            "blocked": False,
                            "created_at": f"{day}T13:30:00Z",
                            "hypothetical_fill": {"fill_price": 19000 + index},
                        },
                        "llm_diagnosis": {"summary": "ok"},
                        "mutation_proposal": {"allowed_mutations": ["tighten_time_window"], "blocked_mutations": []},
                    }
                )
            paper_path.write_text("\n".join(json.dumps(event) for event in paper_events), encoding="utf-8")

            report = build_vol_execution_evidence(
                quote_reports=[quote_path],
                paper_reports=[paper_path],
            )

        self.assertEqual(report["artifact"], "vol_execution_evidence")
        self.assertEqual(report["status"], "ready_for_promotion")
        self.assertEqual(report["missing_requirements"], [])
        self.assertTrue(report["promotion_gate"]["ready_for_promotion"])
        self.assertEqual(report["quote_replay"]["status"], "ready_for_review")
        self.assertEqual(report["paper_shadow"]["status"], "ready_for_review")

    def test_run_vol_prescreen_writes_populated_leaderboard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            strategies_root = root / "strategies"
            output_dir = root / "artifacts"
            event_calendar = root / "macro_events.json"
            strategy_paths = write_vol_strategy_specs(strategies_root, count=2)
            start = datetime(2025, 1, 2, 13, 30)
            rows = []
            for index in range(80):
                close = 100.0 + index * 0.25
                day = start + timedelta(minutes=index)
                rows.append(
                    (
                        "NQ_CME",
                        day,
                        close - 0.25,
                        close + 0.75,
                        close - 0.5,
                        close,
                        close,
                        close,
                        80 if index > 25 else 20,
                        1.0,
                        1.0,
                        0.0,
                    )
                )
            write_bars_parquet(bar_path(data_root, "NQ_CME", "1m", start.date()), rows)
            event_calendar.write_text(
                json.dumps(
                    {
                        "calendar_id": "test_macro",
                        "events": [
                            {
                                "event_id": "test_event",
                                "name": "Test event",
                                "timestamp_utc": "2025-01-02T13:45:00Z",
                                "importance": "high",
                                "affected_symbols": ["NQ_CME"],
                                "pre_event_minutes": 30,
                                "release_window_minutes": 10,
                                "post_event_minutes": 30,
                                "policy_ref": "high_impact_macro_v1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = run_vol_prescreen(
                strategy_paths=strategy_paths,
                data_root=data_root,
                symbol_config=get_symbol("NQ_CME"),
                cost_model=get_cost_model("nq_conservative_v1"),
                date_from=start.date(),
                date_to=start.date(),
                output_dir=output_dir,
                event_calendar_path=event_calendar,
            )

            leaderboard = report["leaderboard"]
            self.assertEqual(leaderboard["summary"]["total_vol_rows"], 2)
            self.assertEqual(leaderboard["summary"]["generated_seed_count"], 2)
            self.assertTrue((output_dir / "vol_strategy_leaderboard.json").exists())
            self.assertTrue((output_dir / "vol_mutation_memory.json").exists())
            self.assertTrue((output_dir / "vol_llm_trigger_audit.json").exists())
            self.assertIn("family_attribution", leaderboard)
            self.assertIn("data_version_hash", leaderboard["artifact_hashes"])
            self.assertIn("event_calendar_hash", leaderboard["artifact_hashes"])
            self.assertIn("session_attribution", leaderboard["rows"][0])
            self.assertEqual(leaderboard["rows"][0]["event_non_event_view"]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
