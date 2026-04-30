from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tlm.ibkr_soak import compact_soak_sample, collect_soak_sample, run_ibkr_soak_monitor, summarize_soak_sample


class IbkrSoakMonitorTests(unittest.TestCase):
    def test_summarizes_acceptance_and_runtime_status(self) -> None:
        sample = {
            "collected_at": "2026-04-30T12:00:00+00:00",
            "symbol": "MNQ",
            "health": {"status": "paper_armed", "connected": True, "paper_account_verified": True, "safe_mode": False},
            "readiness": {"status": "ready"},
            "market_data": {"status": "ready", "snapshot": {"market_data_type": "delayed"}},
            "bracket_orders": {
                "open_bracket_order_count": 0,
                "completed_bracket_order_count": 1,
                "order_event_count": 24,
            },
            "execution_ledger": {"fill_count": 2, "net_realized_pnl": 16.26, "total_commission": 1.24},
            "incidents": {"count": 0},
            "report": {
                "acceptance_evidence": {
                    "status": "pending",
                    "trading_day_count": 1,
                    "readiness_check_count": 12,
                    "review_cycle_count": 4,
                    "paper_order_lifecycle_event_count": 24,
                    "live_order_attempt_count": 0,
                    "unexplained_duplicate_order_count": 0,
                    "bracket_child_missing_after_accept_count": 0,
                    "incident_count": 0,
                    "missing_requirements": ["trading_days<5"],
                    "gates": {"live_order_attempts": "ready"},
                }
            },
        }

        summary = summarize_soak_sample(sample)

        self.assertEqual(summary["gateway_status"], "paper_armed")
        self.assertEqual(summary["readiness_status"], "ready")
        self.assertEqual(summary["market_data_type"], "delayed")
        self.assertEqual(summary["acceptance_status"], "pending")
        self.assertEqual(summary["paper_order_lifecycle_event_count"], 24)
        self.assertEqual(summary["completed_bracket_order_count"], 1)
        self.assertEqual(summary["missing_requirements"], ["trading_days<5"])

    def test_monitor_writes_sample_and_latest_summary(self) -> None:
        responses = {
            "/api/gateways/ibkr/health": {"status": "paper_armed", "connected": True},
            "/api/gateways/ibkr/readiness?symbol=MNQ&max_stale_seconds=30": {"status": "ready"},
            "/api/gateways/ibkr/market-data?symbol=MNQ&max_stale_seconds=30": {
                "status": "ready",
                "snapshot": {"market_data_type": "delayed"},
            },
            "/api/gateways/ibkr/bracket-orders": {"open_bracket_order_count": 0, "order_event_count": 20},
            "/api/gateways/ibkr/execution-ledger": {"fill_count": 0},
            "/api/gateways/ibkr/incidents": {"count": 0},
            "/api/gateways/ibkr/poller": {"readiness_check_count": 100, "review_cycle_count": 30},
            "/api/ibkr-paper/reports/current": {
                "acceptance_evidence": {
                    "status": "ready",
                    "trading_day_count": 5,
                    "readiness_check_count": 100,
                    "review_cycle_count": 30,
                    "paper_order_lifecycle_event_count": 20,
                    "live_order_attempt_count": 0,
                    "unexplained_duplicate_order_count": 0,
                    "bracket_child_missing_after_accept_count": 0,
                    "missing_requirements": [],
                }
            },
        }

        def fake_fetch(api_base: str, path: str, timeout_seconds: float) -> dict:
            self.assertEqual(api_base, "http://api.test")
            self.assertEqual(timeout_seconds, 2.0)
            return responses[path]

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            summary = run_ibkr_soak_monitor(
                api_base="http://api.test",
                output_dir=output_dir,
                max_samples=1,
                timeout_seconds=2.0,
                fetch_json=fake_fetch,
            )

            self.assertEqual(summary["acceptance_status"], "ready")
            self.assertEqual((output_dir / "latest_summary.json").exists(), True)
            self.assertEqual(len((output_dir / "samples.jsonl").read_text(encoding="utf-8").splitlines()), 1)
            latest = json.loads((output_dir / "latest_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["readiness_check_count"], 100)

    def test_compact_sample_drops_growing_histories(self) -> None:
        sample = {
            "collected_at": "2026-04-30T12:00:00+00:00",
            "symbol": "MNQ",
            "summary": {"acceptance_status": "pending"},
            "poller": {
                "enabled": True,
                "running": True,
                "auto_submit": False,
                "readiness_check_count": 10,
                "review_cycle_count": 2,
                "latest_bars": [{"bar_time": "2026-04-30T12:00:00+00:00"}],
                "latest_review": {"review_result": {"action": "no_action"}},
                "latest_optimizer": {"status": "no_change"},
                "last_result": {"status": "ok", "actions": ["market_data_sync_completed"]},
                "latest_signal": {"signal_class": "none", "reasons": ["no_low_r_match"]},
            },
            "report": {
                "run_id": "current",
                "generated_at": "2026-04-30T12:00:01+00:00",
                "acceptance_evidence": {"status": "pending"},
                "reviews": [{"review_result": {"action": "paper_allow"}}],
                "one_minute_bars": [{"bar_time": "2026-04-30T12:00:00+00:00"}],
            },
        }

        compact = compact_soak_sample(sample)

        self.assertEqual(compact["summary"]["acceptance_status"], "pending")
        self.assertEqual(compact["poller"]["readiness_check_count"], 10)
        self.assertNotIn("latest_bars", compact["poller"])
        self.assertNotIn("latest_review", compact["poller"])
        self.assertNotIn("latest_optimizer", compact["poller"])
        self.assertEqual(compact["report"]["acceptance_evidence"]["status"], "pending")
        self.assertNotIn("reviews", compact["report"])
        self.assertNotIn("one_minute_bars", compact["report"])

    def test_collect_sample_records_endpoint_errors(self) -> None:
        def fake_fetch(api_base: str, path: str, timeout_seconds: float) -> dict:
            if path == "/api/gateways/ibkr/health":
                raise RuntimeError("offline")
            return {}

        sample = collect_soak_sample(
            api_base="http://api.test",
            symbol="MNQ",
            max_stale_seconds=30,
            timeout_seconds=1.0,
            fetch_json=fake_fetch,
        )

        self.assertIn("health", sample["summary"]["endpoint_errors"])


if __name__ == "__main__":
    unittest.main()
