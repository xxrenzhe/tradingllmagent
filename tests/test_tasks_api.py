from __future__ import annotations

import tempfile
import unittest
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from tlm.api import (
    build_cost_calibration_response,
    build_experiment_artifacts_response,
    build_nt_export_signal_response,
    build_paper_replay_response,
    build_trigger_gate_memory_response,
    build_trigger_gate_outcome_response,
    build_trigger_gate_report_response,
    build_trigger_gate_schedule_response,
    build_trigger_gate_simulation_response,
    create_app,
)
from tlm.dukascopy import Tick
from tlm.experiments import load_experiment_audit_logs, load_experiment_summary
from tlm.storage import normalized_tick_path, write_ticks_parquet
from tlm.tasks import (
    append_task_log,
    claim_queued_task,
    cancel_task,
    create_task,
    encode_sse_event,
    get_task,
    get_task_logs,
    list_tasks,
    next_queued_task,
    task_event_snapshot,
    task_snapshot_sse,
    update_task,
)
from tlm.worker import run_next_queued_task, run_task
from test_paper_nt import sample_result
from test_strategy_backtest import base_spec, trend_pullback_spec
from test_validation_leaderboard import write_breakout_day


class TaskStoreTests(unittest.TestCase):
    def test_task_lifecycle_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.sqlite3"
            task = create_task(
                db_path,
                "research.run",
                {"symbol": "NQmain", "max_trials": 2},
                task_id="task_1",
            )
            append_task_log(db_path, "task_1", "queued for worker")
            running = update_task(db_path, "task_1", "running")
            completed = update_task(db_path, "task_1", "completed", result={"trials": 2})
            logs = get_task_logs(db_path, "task_1")
            tasks = list_tasks(db_path)

        self.assertEqual(task["status"], "queued")
        self.assertEqual(running["status"], "running")
        self.assertEqual(completed["result"], {"trials": 2})
        self.assertEqual(len(tasks), 1)
        self.assertTrue(any(log["message"] == "queued for worker" for log in logs))

    def test_cancel_is_idempotent_for_terminal_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.sqlite3"
            create_task(db_path, "data.download", {"symbol": "NQmain"}, task_id="task_2")
            cancelled = cancel_task(db_path, "task_2")
            cancelled_again = cancel_task(db_path, "task_2")

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled_again["status"], "cancelled")

    def test_get_task_rejects_unknown_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(KeyError):
                get_task(Path(temp_dir) / "tasks.sqlite3", "missing")

    def test_task_sse_snapshot_includes_task_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.sqlite3"
            create_task(db_path, "research.run", {"symbol": "NQmain"}, task_id="task_sse")
            append_task_log(db_path, "task_sse", "worker started")
            update_task(db_path, "task_sse", "completed", result={"trials": 1})
            snapshot = task_event_snapshot(db_path, "task_sse")
            sse = task_snapshot_sse(db_path, "task_sse")
            encoded = encode_sse_event("task", snapshot["task"])

        self.assertEqual(snapshot["task"]["status"], "completed")
        self.assertTrue(any(log["message"] == "worker started" for log in snapshot["logs"]))
        self.assertIn("event: task", sse)
        self.assertIn("event: log", sse)
        self.assertIn('"status": "completed"', encoded)

    def test_run_task_executes_strategy_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.sqlite3"
            create_task(
                db_path,
                "strategy.validate",
                {"spec": "strategies/example_opening_range_breakout.yaml"},
                task_id="task_validate",
            )
            completed = run_task(db_path, "task_validate")
            logs = get_task_logs(db_path, "task_validate")

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["strategy_family"], "opening_range_breakout")
        self.assertTrue(any(log["message"] == "Completed strategy.validate" for log in logs))

    def test_run_task_executes_trigger_gate_simulation_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "tasks.sqlite3"
            output_dir = root / "trigger_gate"
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
                    }
                ],
            }
            create_task(
                db_path,
                "trigger_gate.simulate",
                {
                    "target_frequency_pool": pool,
                    "from": "2026-04-25",
                    "to": "2026-04-26",
                    "output_dir": str(output_dir),
                    "enable_llm": True,
                },
                task_id="task_trigger_gate_sim",
            )
            completed_sim = run_task(db_path, "task_trigger_gate_sim")
            create_task(
                db_path,
                "trigger_gate.report",
                {"output_dir": str(output_dir)},
                task_id="task_trigger_gate_report",
            )
            completed_report = run_task(db_path, "task_trigger_gate_report")
            logs = get_task_logs(db_path, "task_trigger_gate_sim")

        self.assertEqual(completed_sim["status"], "completed")
        self.assertEqual(completed_sim["result"]["mode"], "llm_enabled")
        self.assertEqual(completed_report["status"], "completed")
        self.assertEqual(completed_report["result"]["llm_calls_match_triggers"], True)
        self.assertTrue(any(log["message"] == "Completed trigger_gate.simulate" for log in logs))

    def test_run_next_queued_task_claims_and_executes_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.sqlite3"
            create_task(
                db_path,
                "strategy.validate",
                {"spec": "strategies/example_opening_range_breakout.yaml"},
                task_id="task_next",
            )
            queued = next_queued_task(db_path)
            claimed = claim_queued_task(db_path, "task_next")
            second_claim = claim_queued_task(db_path, "task_next")
            completed = run_task(db_path, "task_next")
            no_work = run_next_queued_task(db_path)

        self.assertEqual(queued["task_id"], "task_next")
        self.assertEqual(claimed["status"], "running")
        self.assertIsNone(second_claim)
        self.assertEqual(completed["status"], "completed")
        self.assertIsNone(no_work)

    def test_run_task_marks_unknown_task_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.sqlite3"
            create_task(db_path, "unknown.task", {}, task_id="task_unknown")
            failed = run_task(db_path, "task_unknown")
            logs = get_task_logs(db_path, "task_unknown")

        self.assertEqual(failed["status"], "failed")
        self.assertIn("Unsupported task_type", failed["error"])
        self.assertTrue(any(log["level"] == "error" for log in logs))

    def test_run_task_builds_bars_from_local_tick_parquet(self) -> None:
        day = datetime(2025, 3, 19, 13, tzinfo=UTC)
        ticks = [
            Tick(day + timedelta(milliseconds=100), 100.00, 100.20, 2.0, 1.0),
            Tick(day + timedelta(seconds=1), 100.10, 100.30, 4.0, 3.0),
            Tick(day + timedelta(minutes=1, milliseconds=100), 100.50, 100.80, 8.0, 7.0),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            db_path = root / "tasks.sqlite3"
            tick_path = normalized_tick_path(data_root, "NQmain", day.date())
            write_ticks_parquet(tick_path, "NQmain", ticks)
            create_task(
                db_path,
                "data.build_bars",
                {
                    "symbol": "NQmain",
                    "date_from": day.date().isoformat(),
                    "date_to": day.date().isoformat(),
                    "timeframe": "1m",
                    "data_root": str(data_root),
                },
                task_id="task_build_bars",
            )

            completed = run_task(db_path, "task_build_bars")
            output_path = Path(completed["result"]["outputs"][0]["path"])
            create_task(
                db_path,
                "data.build_bars",
                {
                    "symbol": "NQmain",
                    "date_from": day.date().isoformat(),
                    "date_to": day.date().isoformat(),
                    "timeframe": "5m",
                    "data_root": str(data_root),
                },
                task_id="task_build_5m_bars",
            )
            completed_5m = run_task(db_path, "task_build_5m_bars")
            output_5m_path = Path(completed_5m["result"]["outputs"][0]["path"])
            con = duckdb.connect(":memory:")
            try:
                rows = con.execute(
                    "SELECT count(*), min(open), max(close) FROM read_parquet(?)",
                    [str(output_path)],
                ).fetchone()
                rows_5m = con.execute(
                    "SELECT count(*), min(open), max(close) FROM read_parquet(?)",
                    [str(output_5m_path)],
                ).fetchone()
            finally:
                con.close()
            output_exists = output_path.exists()
            output_5m_exists = output_5m_path.exists()

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["rows"], 2)
        self.assertTrue(output_exists)
        self.assertEqual(rows[0], 2)
        self.assertAlmostEqual(rows[1], 100.1)
        self.assertAlmostEqual(rows[2], 100.65)
        self.assertEqual(completed_5m["status"], "completed")
        self.assertEqual(completed_5m["result"]["rows"], 1)
        self.assertTrue(output_5m_exists)
        self.assertEqual(rows_5m[0], 1)
        self.assertAlmostEqual(rows_5m[1], 100.1)
        self.assertAlmostEqual(rows_5m[2], 100.65)

    def test_run_task_rejects_non_tick_data_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.sqlite3"
            create_task(
                db_path,
                "data.download",
                {
                    "symbol": "NQmain",
                    "from": "2025-03-19",
                    "to": "2025-03-19",
                    "granularity": "1m",
                },
                task_id="task_download_invalid",
            )
            failed = run_task(db_path, "task_download_invalid")

        self.assertEqual(failed["status"], "failed")
        self.assertIn("Only granularity=tick", failed["error"])

    def test_run_task_executes_multi_family_research(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            experiments_root = root / "experiments"
            experiment_db = root / "research.sqlite3"
            db_path = root / "tasks.sqlite3"
            opening_path = root / "opening.json"
            trend_path = root / "trend.json"
            opening_path.write_text(json.dumps(base_spec()), encoding="utf-8")
            trend_path.write_text(json.dumps(trend_pullback_spec()), encoding="utf-8")
            for offset in range(40):
                write_breakout_day(data_root, datetime(2025, 1, 1).date() + timedelta(days=offset))
            create_task(
                db_path,
                "research.run",
                {
                    "specs": [str(opening_path), str(trend_path)],
                    "date_from": "2025-01-01",
                    "date_to": "2025-02-09",
                    "experiment_id": "portfolio",
                    "data_root": str(data_root),
                    "experiments_root": str(experiments_root),
                    "experiment_db": str(experiment_db),
                    "max_trials_per_family": 2,
                    "family_weights": {
                        "opening_range_breakout": 0.5,
                        "trend_pullback": 1.0,
                    },
                    "train_days": 5,
                    "validation_days": 5,
                    "test_days": 5,
                    "step_days": 5,
                    "embargo_days": 1,
                    "final_holdout_days": 5,
                    "min_folds": 1,
                },
                task_id="task_research_portfolio",
            )
            completed = run_task(db_path, "task_research_portfolio")
            artifacts = build_experiment_artifacts_response(
                experiments_root,
                "portfolio_opening_range_breakout_trial_0000",
            )
            summary = load_experiment_summary(experiment_db, "portfolio")
            audit_logs = load_experiment_audit_logs(experiment_db, "portfolio")

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["trials"], 2)
        self.assertEqual(
            completed["result"]["by_family"],
            {"opening_range_breakout": 1, "trend_pullback": 1},
        )
        family_report = completed["result"]["family_weight_report"]
        self.assertEqual(family_report["status"], "computed_v1")
        self.assertEqual(set(family_report["next_weights"]), {"opening_range_breakout", "trend_pullback"})
        self.assertTrue(
            all(weight >= family_report["min_weight"] for weight in family_report["next_weights"].values())
        )
        self.assertEqual(
            {row["strategy_family"]: row["completed_trials"] for row in family_report["families"]},
            {"opening_range_breakout": 1, "trend_pullback": 1},
        )
        self.assertEqual(
            {row["strategy_family"]: row["requested_quota"] for row in family_report["families"]},
            {"opening_range_breakout": 2, "trend_pullback": 2},
        )
        self.assertEqual(
            {row["strategy_family"]: row["quota"] for row in family_report["families"]},
            {"opening_range_breakout": 1, "trend_pullback": 2},
        )
        self.assertEqual(len(completed["result"]["result_paths"]), 2)
        self.assertTrue(artifacts["trades"])
        self.assertTrue(artifacts["distributions"]["by_direction"])
        self.assertEqual(summary["experiment"]["status"], "completed")
        self.assertEqual(len(summary["trials"]), 2)
        self.assertEqual(summary["experiment"]["metadata"]["trials"], 2)
        self.assertEqual({entry["event_type"] for entry in audit_logs}, {"research_trial_completed"})

    def test_run_task_deduplicates_duplicate_research_specs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            experiments_root = root / "experiments"
            db_path = root / "tasks.sqlite3"
            first_path = root / "opening_first.json"
            duplicate_path = root / "opening_duplicate.json"
            first_payload = base_spec()
            duplicate_payload = base_spec()
            duplicate_payload["name"] = "same_logic_different_name"
            first_path.write_text(json.dumps(first_payload), encoding="utf-8")
            duplicate_path.write_text(json.dumps(duplicate_payload), encoding="utf-8")
            for offset in range(40):
                write_breakout_day(data_root, datetime(2025, 1, 1).date() + timedelta(days=offset))
            create_task(
                db_path,
                "research.run",
                {
                    "specs": [str(first_path), str(duplicate_path)],
                    "date_from": "2025-01-01",
                    "date_to": "2025-02-09",
                    "experiment_id": "dedup",
                    "data_root": str(data_root),
                    "experiments_root": str(experiments_root),
                    "max_trials_per_family": 1,
                    "train_days": 5,
                    "validation_days": 5,
                    "test_days": 5,
                    "step_days": 5,
                    "embargo_days": 1,
                    "final_holdout_days": 5,
                    "min_folds": 1,
                },
                task_id="task_research_dedup",
            )
            completed = run_task(db_path, "task_research_dedup")

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["trials"], 1)
        self.assertEqual(len(completed["result"]["result_paths"]), 1)
        report = completed["result"]["deduplication_report"]
        self.assertEqual(report["input_specs"], 2)
        self.assertEqual(report["unique_specs"], 1)
        self.assertEqual(report["skipped_specs"], 1)
        self.assertEqual(report["skipped"][0]["strategy_name"], "same_logic_different_name")
        self.assertEqual(report["skipped"][0]["strategy_family"], "opening_range_breakout")
        self.assertEqual(report["skipped"][0]["reason"], "duplicate_strategy_logic")
        self.assertTrue(report["skipped"][0]["strategy_logic_hash"])
        self.assertEqual(report["skipped"][0]["duplicate_of"], first_payload["name"])

    def test_run_task_executes_llm_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "tasks.sqlite3"
            experiment_db = root / "research.sqlite3"
            experiments_root = root / "experiments"
            output_path = root / "generated" / "proposal.json"
            seed_path = root / "seed.json"
            seed_path.write_text(json.dumps(base_spec()), encoding="utf-8")
            create_task(
                db_path,
                "research.propose",
                {
                    "spec": str(seed_path),
                    "experiment_id": "proposal_task",
                    "experiments_root": str(experiments_root),
                    "experiment_db": str(experiment_db),
                    "output": str(output_path),
                    "model": "local-deterministic-template",
                    "llm_parameters": {"temperature": 0},
                },
                task_id="task_research_propose",
            )
            completed = run_task(db_path, "task_research_propose")
            audit_path = Path(completed["result"]["audit_log"])
            output_exists = output_path.exists()
            audit_exists = audit_path.exists()

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["experiment_id"], "proposal_task")
        self.assertEqual(completed["result"]["strategy_family"], "opening_range_breakout")
        self.assertTrue(output_exists)
        self.assertTrue(audit_exists)
        self.assertTrue(completed["result"]["prompt_hash"])
        self.assertTrue(completed["result"]["response_hash"])

    def test_run_task_executes_llm_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            experiments_root = root / "experiments"
            experiment_db = root / "research.sqlite3"
            db_path = root / "tasks.sqlite3"
            seed_path = root / "seed.json"
            seed_path.write_text(json.dumps(base_spec()), encoding="utf-8")
            for offset in range(40):
                write_breakout_day(data_root, datetime(2025, 1, 1).date() + timedelta(days=offset))
            create_task(
                db_path,
                "research.iterate",
                {
                    "spec": str(seed_path),
                    "date_from": "2025-01-01",
                    "date_to": "2025-02-09",
                    "experiment_id": "iteration_task",
                    "data_root": str(data_root),
                    "experiments_root": str(experiments_root),
                    "experiment_db": str(experiment_db),
                    "max_trials": 1,
                    "train_days": 5,
                    "validation_days": 5,
                    "test_days": 5,
                    "step_days": 5,
                    "embargo_days": 1,
                    "final_holdout_days": 5,
                    "min_folds": 1,
                    "model": "local-deterministic-template",
                    "llm_parameters": {"temperature": 0},
                },
                task_id="task_research_iterate",
            )
            completed = run_task(db_path, "task_research_iterate")
            proposed_path = Path(completed["result"]["proposal"]["strategy_spec"])
            proposed_exists = proposed_path.exists()
            summary = load_experiment_summary(experiment_db, "iteration_task")
            audit_logs = load_experiment_audit_logs(experiment_db, "iteration_task")

        self.assertEqual(completed["status"], "completed")
        self.assertTrue(proposed_exists)
        self.assertEqual(completed["result"]["research"]["trials"], 1)
        self.assertEqual(summary["experiment"]["status"], "completed")
        self.assertEqual(len(summary["trials"]), 1)
        self.assertEqual(
            {entry["event_type"] for entry in audit_logs},
            {"llm_strategy_proposal", "research_trial_completed"},
        )


class APIImportTests(unittest.TestCase):
    def test_api_module_imports_without_fastapi_installed(self) -> None:
        import tlm.api as api

        self.assertTrue(hasattr(api, "create_app"))
        self.assertTrue(hasattr(api, "app"))

    def test_paper_api_helpers_replay_and_export_without_fastapi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "backtest.json"
            result_path.write_text(json.dumps(sample_result()), encoding="utf-8")
            replay = build_paper_replay_response({"strategy_id": str(result_path)})
            exported = build_nt_export_signal_response(
                {
                    "strategy_id": str(result_path),
                    "format": "oif",
                    "account": "Sim101",
                    "instrument": "NQ 06-26",
                }
            )

        self.assertEqual(replay["ending_equity"], 100_045.0)
        self.assertEqual(exported["format"], "oif")
        self.assertEqual(exported["line_count"], 2)
        self.assertIn("PLACE;Sim101;NQ 06-26;BUY;1;MARKET", exported["content"])

    def test_fastapi_app_registers_research_console_routes_when_installed(self) -> None:
        try:
            app = create_app()
        except RuntimeError:
            self.skipTest("FastAPI is not installed")
        paths = {route.path for route in app.routes}

        self.assertIn("/api/backtests/tick", paths)
        self.assertIn("/api/experiments/proposals", paths)
        self.assertIn("/api/experiments/iterations", paths)
        self.assertIn("/api/experiments/{experiment_id}/audit-logs", paths)
        self.assertIn("/api/experiments/{experiment_id}/artifacts", paths)
        self.assertIn("/api/events", paths)
        self.assertIn("/api/events/context", paths)
        self.assertIn("/api/monitor/report", paths)
        self.assertIn("/api/execution/readiness", paths)
        self.assertIn("/api/execution/approval-queue", paths)
        self.assertIn("/api/execution/risk-profiles", paths)
        self.assertIn("/api/readiness/external-validation", paths)
        self.assertIn("/api/calibration/costs", paths)
        self.assertIn("/api/modules/memory", paths)
        self.assertIn("/api/trigger-gate/simulations", paths)
        self.assertIn("/api/trigger-gate/reports", paths)
        self.assertIn("/api/trigger-gate/schedules", paths)
        self.assertIn("/api/trigger-gate/memory", paths)
        self.assertIn("/api/trigger-gate/outcomes", paths)
        self.assertIn("/api/gateways/nt8/order-updates", paths)
        self.assertIn("/api/gateways/nt8/incidents", paths)

    def test_trigger_gate_api_helper_writes_simulation_artifacts_without_fastapi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = build_trigger_gate_simulation_response(
                {
                    "target_frequency_pool": {
                        "pool_version": "target_frequency_pool.v2",
                        "selected": [
                            {
                                "module_id": "module_a",
                                "strategy_name": "strategy_a",
                                "strategy_spec_hash": "hash_a",
                                "timeframe": "15m",
                                "trades_per_day": 1.0,
                                "proxy_win_rate": 0.61,
                            }
                        ],
                    },
                    "from": "2026-04-25",
                    "to": "2026-04-26",
                    "output_dir": str(root / "trigger_gate"),
                    "enable_llm": True,
                }
            )
            report = build_trigger_gate_report_response({"output_dir": str(root / "trigger_gate")})

        self.assertEqual(manifest["mode"], "llm_enabled")
        self.assertEqual(manifest["llm_call_count"], manifest["trigger_count"])
        self.assertEqual(report["llm_calls_match_triggers"], True)
        self.assertIn("adaptive_recommendations", report)

    def test_trigger_gate_memory_and_outcome_api_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "trigger_gate"
            build_trigger_gate_simulation_response(
                {
                    "target_frequency_pool": {
                        "pool_version": "target_frequency_pool.v2",
                        "selected": [
                            {
                                "module_id": "module_a",
                                "strategy_name": "strategy_a",
                                "strategy_spec_hash": "hash_a",
                                "timeframe": "15m",
                                "trades_per_day": 1.0,
                                "proxy_win_rate": 0.61,
                            }
                        ],
                    },
                    "from": "2026-04-25",
                    "to": "2026-04-26",
                    "output_dir": str(output_dir),
                    "enable_llm": True,
                }
            )
            initial_memory = build_trigger_gate_memory_response({"output_dir": str(output_dir), "limit": 1})
            decision_id = initial_memory["rows"][0]["decision_id"]
            outcome = build_trigger_gate_outcome_response(
                {
                    "output_dir": str(output_dir),
                    "decision_id": decision_id,
                    "outcome_window": "48h",
                    "net_pnl": -25,
                }
            )
            filtered = build_trigger_gate_memory_response(
                {
                    "output_dir": str(output_dir),
                    "decision": "allow",
                    "outcome_label": "allowed_loser",
                }
            )

        self.assertEqual(outcome["final_label"], "allowed_loser")
        self.assertEqual(filtered["row_count"], 1)
        self.assertEqual(filtered["outcome_counts"]["allowed_loser"], 1)

    def test_trigger_gate_schedule_api_helper_builds_standard_windows(self) -> None:
        schedule = build_trigger_gate_schedule_response(
            {
                "target_frequency_pool": {
                    "pool_version": "target_frequency_pool.v2",
                    "selected": [{"strategy_spec_hash": "hash_a", "trades_per_day": 1.0, "proxy_win_rate": 0.61}],
                },
                "as_of": "2026-04-27",
                "output_root": "experiments/trigger_gate/scheduled",
                "enable_llm": True,
            }
        )

        self.assertEqual(schedule["run_count"], 4)
        self.assertEqual(schedule["runs"][-1]["label"], "90d")

    def test_cost_calibration_api_helper_builds_artifact_without_fastapi(self) -> None:
        artifact = build_cost_calibration_response(
            {
                "cost_model": "nq_conservative_v1",
                "samples": [
                    {
                        "source": "paper_shadow",
                        "timestamp": "2026-04-27T13:30:00Z",
                        "instrument": "NQ 06-26",
                        "observed_spread_ticks": 2,
                        "observed_slippage_ticks": 1,
                    }
                ],
                "data_quality_report": {"status": "ok"},
                "proxy_instrument": "USATECHIDXUSD",
                "executable_instrument": "CME_NQ",
            }
        )

        self.assertEqual(artifact["artifact"], "cost_calibration")
        self.assertTrue(artifact["proxy_warning"])
        self.assertEqual(artifact["summary"]["sample_count"], 1)

    def test_openapi_contract_covers_webui_and_runtime_paths(self) -> None:
        contract = Path("docs/openapi/tradingllmagent.openapi.yaml").read_text(encoding="utf-8")
        required_paths = [
            "/api/data/symbols",
            "/api/data/quality",
            "/api/tasks/{task_id}/events",
            "/api/experiments/research-runs",
            "/api/experiments/{experiment_id}/artifacts",
            "/api/reports/leaderboard",
            "/api/events/context",
            "/api/monitor/report",
            "/api/execution/intents",
            "/api/execution/paper-shadow",
            "/api/execution/readiness",
            "/api/execution/approval-queue",
            "/api/execution/risk-profiles",
            "/api/readiness/external-validation",
            "/api/calibration/costs",
            "/api/gateways/nt8/commands",
            "/api/gateways/nt8/order-updates",
            "/api/gateways/nt8/incidents",
        ]

        for path in required_paths:
            self.assertIn(path, contract)
        for schema_name in [
            "Task:",
            "ResearchRunRequest:",
            "LeaderboardReport:",
            "EventCalendar:",
            "MonitorReport:",
            "ReadinessDecision:",
        ]:
            self.assertIn(schema_name, contract)
        for execution_field in [
            "schema_version:",
            "protocol_version:",
            "correlation_id:",
            "idempotency_key:",
        ]:
            self.assertIn(execution_field, contract)


if __name__ == "__main__":
    unittest.main()
