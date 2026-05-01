from __future__ import annotations

import os
import tempfile
import unittest
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import duckdb

from tlm.api import (
    _compact_ibkr_poller_state,
    build_leaderboard_response,
    build_cost_calibration_response,
    build_experiment_artifacts_response,
    build_feature_readiness_response,
    build_nt_export_signal_response,
    build_paper_replay_response,
    build_trigger_gate_memory_response,
    build_trigger_gate_outcome_response,
    build_trigger_gate_report_response,
    build_trigger_gate_schedule_response,
    build_trigger_gate_simulation_response,
    build_vol_overview_response,
    create_app,
    run_ibkr_poll_cycle,
)
from tlm.dukascopy import Tick
from tlm.experiments import load_experiment_audit_logs, load_experiment_summary
from tlm.ibkr_gateway import IbkrContractSpec, IbkrPaperGateway
from tlm.storage import bar_path, normalized_quote_path, normalized_tick_path, write_ticks_parquet
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

    def test_worker_supports_primary_research_task_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "tasks.sqlite3"
            create_task(db_path, "research.primary_run", {}, task_id="task_primary_missing_spec")
            failed = run_task(db_path, "task_primary_missing_spec")

        self.assertEqual(failed["status"], "failed")
        self.assertIn("spec", failed["error"])
        self.assertNotIn("Unsupported task_type", failed["error"])

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

    def test_run_task_executes_nq_data_plan_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            db_path = root / "tasks.sqlite3"
            firstrate_csv = root / "nq.csv"
            firstrate_csv.write_text(
                "timestamp,open,high,low,close,volume\n"
                "2025-03-19 13:30:00,100.0,100.5,99.5,100.0,10\n"
                "2025-03-20 13:30:00,101.0,101.5,100.5,101.0,11\n",
                encoding="utf-8",
            )
            create_task(
                db_path,
                "data.import_firstrate",
                {"symbol": "NQ_1M", "input": str(firstrate_csv), "data_root": str(data_root)},
                task_id="task_import_firstrate",
            )
            imported = run_task(db_path, "task_import_firstrate")

            create_task(
                db_path,
                "data.bar_quality",
                {
                    "symbol": "NQ_1M",
                    "date_from": "2025-03-19",
                    "date_to": "2025-03-20",
                    "data_root": str(data_root),
                },
                task_id="task_bar_quality",
            )
            quality = run_task(db_path, "task_bar_quality")

            split_output = root / "split.json"
            create_task(
                db_path,
                "data.split_manifest",
                {
                    "symbol": "NQ_1M",
                    "date_from": "2025-03-01",
                    "date_to": "2025-03-20",
                    "train_days": 3,
                    "validation_days": 3,
                    "test_days": 3,
                    "step_days": 3,
                    "embargo_days": 0,
                    "final_holdout_days": 3,
                    "data_root": str(data_root),
                    "output": str(split_output),
                },
                task_id="task_split",
            )
            split = run_task(db_path, "task_split")

            quotes_csv = root / "tbbo.csv"
            quotes_csv.write_text(
                "ts_event,bid_px_00,ask_px_00,bid_sz_00,ask_sz_00\n"
                "2025-03-19T13:30:00Z,100.00,100.25,7,9\n"
                "2025-03-19T13:35:00Z,101.00,101.25,8,10\n",
                encoding="utf-8",
            )
            create_task(
                db_path,
                "data.import_databento_quotes",
                {"symbol": "NQ_CME", "input": str(quotes_csv), "data_root": str(data_root)},
                task_id="task_import_quotes",
            )
            quotes = run_task(db_path, "task_import_quotes")

            ohlcv_csv = root / "ohlcv.csv"
            ohlcv_csv.write_text(
                "ts_event,rtype,publisher_id,instrument_id,open,high,low,close,volume,symbol\n"
                "2025-03-19T13:30:00Z,33,1,111,100.00,100.50,99.75,100.25,10,NQH5\n"
                "2025-03-19T13:30:00Z,33,1,222,101.00,101.50,100.75,101.25,20,NQM5\n",
                encoding="utf-8",
            )
            create_task(
                db_path,
                "data.import_databento_ohlcv",
                {"symbol": "NQ_CME", "input": str(ohlcv_csv), "data_root": str(data_root)},
                task_id="task_import_ohlcv",
            )
            ohlcv = run_task(db_path, "task_import_ohlcv")

            backtest_result = root / "backtest.json"
            backtest_result.write_text(
                json.dumps(
                    {
                        "trades": [
                            {
                                "symbol": "NQ_CME",
                                "side": "long",
                                "entry_time": "2025-03-19T13:30:00",
                                "exit_time": "2025-03-19T13:35:00",
                                "entry_price": 100.0,
                                "exit_price": 101.0,
                                "contracts": 1,
                                "gross_pnl": 20.0,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            create_task(
                db_path,
                "data.quote_replay",
                {
                    "symbol": "NQ_CME",
                    "date_from": "2025-03-19",
                    "date_to": "2025-03-19",
                    "data_root": str(data_root),
                    "backtest_result": str(backtest_result),
                },
                task_id="task_quote_replay",
            )
            replay = run_task(db_path, "task_quote_replay")

            firstrate_path_exists = bar_path(data_root, "NQ_1M", "1m", datetime(2025, 3, 19).date()).exists()
            quote_path_exists = normalized_quote_path(data_root, "NQ_CME", datetime(2025, 3, 19).date()).exists()
            ohlcv_path_exists = bar_path(data_root, "NQ_CME", "1m", datetime(2025, 3, 19).date()).exists()
            split_output_exists = split_output.exists()

        self.assertEqual(imported["status"], "completed")
        self.assertTrue(firstrate_path_exists)
        self.assertEqual(quality["status"], "completed")
        self.assertEqual(quality["result"]["rows"], 2)
        self.assertEqual(split["status"], "completed")
        self.assertTrue(split_output_exists)
        self.assertFalse(split["result"]["final_holdout_policy"]["llm_feedback_includes_final_holdout"])
        self.assertEqual(quotes["status"], "completed")
        self.assertTrue(quote_path_exists)
        self.assertEqual(ohlcv["status"], "completed")
        self.assertTrue(ohlcv_path_exists)
        self.assertEqual(replay["status"], "completed")
        self.assertAlmostEqual(replay["result"]["avg_bid_ask_cost_usd"], 10.0)

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

    def test_run_task_executes_target_discovery(self) -> None:
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
                "research.discover_target",
                {
                    "spec": str(seed_path),
                    "date_from": "2025-01-01",
                    "date_to": "2025-02-09",
                    "experiment_id": "target_discovery_task",
                    "data_root": str(data_root),
                    "experiments_root": str(experiments_root),
                    "experiment_db": str(experiment_db),
                    "max_rounds": 1,
                    "trials_per_round": 1,
                    "min_annual_trades": -1,
                    "min_sharpe": -100,
                    "min_win_probability": -1,
                    "min_profit_factor": 0.1,
                    "max_drawdown": 100000,
                    "min_positive_year_ratio": 0,
                    "max_final_holdout_sharpe_decay": 10,
                    "max_target_parameter_combinations": 200,
                    "min_non_overlap_test_folds": 1,
                    "train_days": 5,
                    "validation_days": 5,
                    "test_days": 5,
                    "step_days": 5,
                    "embargo_days": 1,
                    "final_holdout_days": 5,
                    "min_folds": 1,
                    "llm_model": "local-deterministic-template",
                    "llm_parameters": {"temperature": 0},
                },
                task_id="task_target_discovery",
            )
            completed = run_task(db_path, "task_target_discovery")
            summary_path = Path(completed["result"]["summary_path"])
            summary_exists = summary_path.exists()
            summary = load_experiment_summary(experiment_db, "target_discovery_task")
            audit_logs = load_experiment_audit_logs(experiment_db, "target_discovery_task")

        self.assertEqual(completed["status"], "completed")
        self.assertIn(completed["result"]["stop_reason"], {"target_found", "budget_exhausted"})
        self.assertEqual(completed["result"]["total_trials"], 1)
        self.assertTrue(summary_exists)
        self.assertEqual(summary["experiment"]["status"], "completed")
        self.assertEqual(summary["experiment"]["metadata"]["target"]["min_profit_factor"], 0.1)
        self.assertEqual(summary["experiment"]["metadata"]["target"]["max_drawdown"], 100000)
        self.assertEqual(len(summary["trials"]), 1)
        self.assertIn("win_probability_test", summary["trials"][0])
        self.assertEqual(
            {entry["event_type"] for entry in audit_logs},
            {"llm_target_discovery_proposal", "target_discovery_trial_completed"},
        )

    def test_run_task_discovers_target_from_local_seed_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            strategies_root = root / "strategies"
            strategies_root.mkdir()
            (strategies_root / "seed_a.json").write_text(json.dumps(base_spec()), encoding="utf-8")
            seed_b = trend_pullback_spec()
            seed_b["name"] = "seed_pool_trend_pullback"
            (strategies_root / "seed_b.json").write_text(json.dumps(seed_b), encoding="utf-8")
            experiments_root = root / "experiments"
            experiment_db = root / "research.sqlite3"
            db_path = root / "tasks.sqlite3"
            for offset in range(40):
                write_breakout_day(data_root, datetime(2025, 1, 1).date() + timedelta(days=offset))
            create_task(
                db_path,
                "research.discover_target",
                {
                    "symbol": "NQmain",
                    "timeframe": "1m",
                    "strategies_root": str(strategies_root),
                    "date_from": "2025-01-01",
                    "date_to": "2025-02-09",
                    "experiment_id": "target_discovery_seed_pool_task",
                    "data_root": str(data_root),
                    "experiments_root": str(experiments_root),
                    "experiment_db": str(experiment_db),
                    "max_seed_strategies": 1,
                    "max_rounds": 1,
                    "trials_per_round": 1,
                    "min_annual_trades": -1,
                    "min_sharpe": -100,
                    "min_win_probability": -1,
                    "train_days": 5,
                    "validation_days": 5,
                    "test_days": 5,
                    "step_days": 5,
                    "embargo_days": 1,
                    "final_holdout_days": 5,
                    "min_folds": 1,
                    "llm_model": "local-deterministic-template",
                    "llm_parameters": {"temperature": 0},
                },
                task_id="task_target_discovery_seed_pool",
            )
            completed = run_task(db_path, "task_target_discovery_seed_pool")
            summary = load_experiment_summary(experiment_db, "target_discovery_seed_pool_task")

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["seed_selection_report"]["selected_count"], 1)
        self.assertEqual(completed["result"]["total_trials"], 1)
        self.assertEqual(summary["experiment"]["status"], "completed")

    def test_run_task_executes_vol_optimization_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "tasks.sqlite3"
            output_dir = root / "vol_artifacts"
            seed_dir = root / "strategies"
            manifest = root / "manifest.json"
            tasks = [
                ("features.vol_readiness", {"output": str(root / "readiness.json")}, "vol_ready"),
                (
                    "research.vol_seed_search",
                    {"output_dir": str(seed_dir), "manifest_output": str(manifest)},
                    "vol_seed",
                ),
                (
                    "research.vol_cost_stress",
                    {"experiments_root": str(root / "experiments"), "output": str(root / "cost.json")},
                    "vol_cost",
                ),
                ("execution.quote_fill_replay", {"output": str(root / "quote.json")}, "vol_quote"),
                ("paper.vol_shadow_review", {"output": str(root / "paper.json")}, "vol_paper"),
                (
                    "memory.vol_mutation_backfill",
                    {"experiments_root": str(root / "experiments"), "output": str(root / "memory.json")},
                    "vol_memory",
                ),
            ]
            results = {}
            for task_type, payload, task_id in tasks:
                create_task(db_path, task_type, payload, task_id=task_id)
                results[task_type] = run_task(db_path, task_id)
            create_task(
                db_path,
                "research.vol_artifacts",
                {
                    "output_dir": str(output_dir),
                    "experiments_root": str(root / "experiments"),
                    "strategies_root": str(seed_dir),
                },
                task_id="vol_artifacts",
            )
            artifacts = run_task(db_path, "vol_artifacts")
            manifest_exists = manifest.exists()

        self.assertEqual(results["features.vol_readiness"]["result"]["status"], "ready")
        self.assertEqual(results["research.vol_seed_search"]["result"]["count"], 50)
        self.assertEqual(results["research.vol_cost_stress"]["result"]["artifact"], "vol_cost_stress_report")
        self.assertEqual(results["execution.quote_fill_replay"]["result"]["status"], "blocked")
        self.assertEqual(results["paper.vol_shadow_review"]["result"]["status"], "blocked")
        self.assertEqual(results["memory.vol_mutation_backfill"]["result"]["record_count"], 0)
        self.assertEqual(len(artifacts["result"]["artifacts"]), 7)
        self.assertTrue(manifest_exists)


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
        self.assertEqual(replay["replay_attribution"]["artifact"], "paper_shadow_replay_attribution")
        self.assertEqual(replay["replay_attribution"]["strategy_name"], "test_orb")
        self.assertEqual(exported["format"], "oif")
        self.assertEqual(exported["line_count"], 2)
        self.assertIn("PLACE;Sim101;NQ 06-26;BUY;1;MARKET", exported["content"])

    def test_feature_readiness_api_helper_exposes_registry_and_generation_manifest(self) -> None:
        readiness = build_feature_readiness_response()

        self.assertGreaterEqual(readiness["feature_count"], 100)
        self.assertEqual(readiness["feature_count"], len(readiness["features"]))
        self.assertIn("implemented", readiness["by_status"])
        self.assertGreater(readiness["generated_strategy_summary"]["strategy_count"], 0)
        self.assertEqual(
            readiness["generated_strategy_summary"]["strategy_count"],
            len(readiness["generated_strategy_manifest"]["strategies"]),
        )

    def test_ibkr_poll_cycle_syncs_gateway_state(self) -> None:
        class PollerAdapter:
            def connect(self, host: str, port: int, client_id: int) -> dict:
                return {"connected": True}

            def disconnect(self) -> dict:
                return {"connected": False}

            def account_summary(self) -> dict:
                return {"account_id": "DU1234567", "account_type": "individual"}

            def request_contract_details(self, contract: dict) -> dict:
                return {
                    "symbol": contract["symbol"],
                    "tick_size": 0.25,
                    "point_value": 2.0,
                    "exchange": contract["exchange"],
                    "currency": contract["currency"],
                    "local_symbol": "MNQM6",
                    "trading_class": "MNQ",
                }

            def request_market_data(self, contract: dict, timeout_seconds: int = 5) -> dict:
                return {
                    "symbol": contract["symbol"],
                    "bid": 19000.0,
                    "ask": 19000.25,
                    "last": 19000.25,
                    "market_data_type": "delayed",
                    "snapshot_time": datetime.now(UTC).isoformat(),
                }

            def request_positions(self) -> list[dict]:
                return [{"symbol": "MNQ", "quantity": 1, "average_cost": 19000.25, "recorded_at": datetime.now(UTC).isoformat()}]

            def request_account_snapshot(self, account_id: str | None = None) -> dict:
                return {
                    "net_liquidation": 100020.0,
                    "daily_pnl": 20.0,
                    "realized_pnl": 12.5,
                    "unrealized_pnl": 8.0,
                    "drawdown_usage": 0.02,
                    "recorded_at": datetime.now(UTC).isoformat(),
                }

            def submit_bracket_order(self, contract: dict, order: dict) -> dict:
                return {"submitted": True}

            def drain_runtime_events(self) -> dict:
                return {
                    "order_status": [{"order_id": 1, "status": "Filled", "filled": 1, "remaining": 0, "average_fill_price": 19000.25}],
                    "executions": [
                        {
                            "execution_id": "exec-1",
                            "order_id": 1,
                            "symbol": "MNQ",
                            "side": "BUY",
                            "quantity": 1,
                            "fill_price": 19000.25,
                            "commission": 0.47,
                            "realized_pnl": 12.5,
                            "filled_at": datetime.now(UTC).isoformat(),
                        }
                    ],
                }

        gateway = IbkrPaperGateway(adapter=PollerAdapter())
        gateway.register_contract_spec(
            IbkrContractSpec(
                symbol="MNQ",
                sec_type="FUT",
                exchange="CME",
                currency="USD",
                quantity=1,
                last_trade_date_or_contract_month="202506",
                local_symbol="MNQM6",
                trading_class="MNQ",
                expected_tick_size=0.25,
                expected_point_value=2.0,
            )
        )
        gateway.connect()

        result = run_ibkr_poll_cycle(gateway, symbol="MNQ")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(gateway.contract_readiness("MNQ")["status"], "ready")
        self.assertEqual(gateway.market_data_readiness("MNQ")["status"], "ready")
        self.assertEqual(gateway.positions_report()["count"], 1)
        self.assertEqual(gateway.execution_ledger()["fill_count"], 1)

    def test_ibkr_poll_cycle_can_run_decision_loop(self) -> None:
        class PollerAdapter:
            def __init__(self) -> None:
                self.connected = False

            def connect(self, host: str, port: int, client_id: int) -> dict:
                self.connected = True
                return {"connected": True, "host": host, "port": port, "client_id": client_id}

            def disconnect(self) -> dict:
                self.connected = False
                return {"connected": False}

            def account_summary(self) -> dict:
                return {"account_id": "DU1234567", "account_type": "paper"}

            def request_contract_details(self, contract: dict) -> dict:
                return {
                    "symbol": "MNQ",
                    "tick_size": 0.25,
                    "point_value": 2.0,
                    "exchange": "CME",
                    "currency": "USD",
                }

            def request_market_data(self, contract: dict, timeout_seconds: int = 5) -> dict:
                return {
                    "symbol": "MNQ",
                    "bid": 19005.75,
                    "ask": 19006.0,
                    "last": 19006.0,
                    "market_data_type": "real_time",
                    "snapshot_time": datetime.now(UTC).isoformat(),
                }

            def request_positions(self) -> list[dict]:
                return [{"symbol": "MNQ", "quantity": 0, "average_cost": 0.0, "recorded_at": datetime.now(UTC).isoformat()}]

            def request_account_snapshot(self, account_id: str | None = None) -> dict:
                return {
                    "net_liquidation": 100020.0,
                    "daily_pnl": 0.0,
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 0.0,
                    "drawdown_usage": 0.0,
                    "recorded_at": datetime.now(UTC).isoformat(),
                }

            def submit_bracket_order(self, contract: dict, order: dict) -> dict:
                return {"submitted": True}

            def drain_runtime_events(self) -> dict:
                return {"order_status": [], "executions": []}

        gateway = IbkrPaperGateway(adapter=PollerAdapter())
        gateway.connect()
        for minute, last in enumerate((19000.0, 19001.0, 19002.0, 19003.0, 19004.0, 19006.0)):
            gateway.record_market_data(
                {
                    "symbol": "MNQ",
                    "bid": last - 0.25,
                    "ask": last,
                    "last": last,
                    "market_data_type": "real_time",
                    "snapshot_time": (datetime.now(UTC) - timedelta(minutes=5 - minute)).isoformat(),
                }
            )

        review_history: list[dict] = []
        optimizer_history: list[dict] = []
        state = {
            "review_interval_seconds": 0,
            "market_data_history_limit": 100,
            "readiness_max_stale_seconds": 600,
            "strategy": {
                "strategy_id": "mnq_1m_breakout",
                "strategy_spec_hash": "strategy-hash",
                "module_id": "range_breakout",
                "family": "range_breakout",
                "symbol": "MNQ",
                "timeframe": "1m",
                "lookback_bars": 5,
                "breakout_ticks": 1,
                "enabled": True,
                "tick_size": 0.25,
                "stop_loss_ticks": 20,
                "take_profit_ticks": 40,
                "max_holding_minutes": 20,
            },
            "control_state": {
                "mode": "paper",
                "min_confidence": 0.55,
                "max_spread_ticks": 2.0,
                "daily_trade_cap": 6,
                "strategies": {},
                "trade_session": {"start": "09:30", "end": "15:55"},
                "safe_mode": False,
                "kill_switch": False,
            },
        }

        result = run_ibkr_poll_cycle(
            gateway,
            symbol="MNQ",
            review_history=review_history,
            optimizer_history=optimizer_history,
            state=state,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["decision"]["status"], "review_completed")
        self.assertEqual(result["decision"]["review_result_action"], "paper_allow")
        self.assertEqual(len(review_history), 1)
        self.assertEqual(gateway.bracket_order_report()["open_bracket_order_count"], 1)

    def test_ibkr_poll_cycle_tracks_acceptance_counters(self) -> None:
        duplicate_fill_time = "2026-04-30T09:35:00+00:00"

        class PollerAdapter:
            def __init__(self) -> None:
                self.connected = False

            def connect(self, host: str, port: int, client_id: int) -> dict:
                self.connected = True
                return {"connected": True, "host": host, "port": port, "client_id": client_id}

            def disconnect(self) -> dict:
                self.connected = False
                return {"connected": False}

            def account_summary(self) -> dict:
                return {"account_id": "DU1234567", "account_type": "paper"}

            def request_contract_details(self, contract: dict) -> dict:
                return {
                    "symbol": "MNQ",
                    "tick_size": 0.25,
                    "point_value": 2.0,
                    "exchange": "CME",
                    "currency": "USD",
                    "last_trade_date_or_contract_month": "202506",
                    "local_symbol": "MNQM6",
                    "trading_class": "MNQ",
                }

            def request_market_data(self, contract: dict, timeout_seconds: int = 5) -> dict:
                return {
                    "symbol": "MNQ",
                    "bid": 19005.75,
                    "ask": 19006.0,
                    "last": 19006.0,
                    "market_data_type": "real_time",
                    "snapshot_time": datetime.now(UTC).isoformat(),
                }

            def request_positions(self) -> list[dict]:
                return [{"symbol": "MNQ", "quantity": 0, "average_cost": 0.0, "recorded_at": datetime.now(UTC).isoformat()}]

            def request_account_snapshot(self, account_id: str | None = None) -> dict:
                return {
                    "net_liquidation": 100020.0,
                    "daily_pnl": 0.0,
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 0.0,
                    "drawdown_usage": 0.0,
                    "recorded_at": datetime.now(UTC).isoformat(),
                }

            def submit_bracket_order(self, contract: dict, order: dict) -> dict:
                return {"submitted": True, "contract": contract["symbol"], "parent_order_id": order["parent_order_id"]}

            def drain_runtime_events(self) -> dict:
                return {
                    "order_status": [
                        {
                            "order_id": 99,
                            "status": "Filled",
                            "filled": 1,
                            "remaining": 0,
                            "average_fill_price": 19000.25,
                        }
                    ],
                    "executions": [
                        {
                            "execution_id": "dup-exec-1",
                            "order_id": 99,
                            "symbol": "MNQ",
                            "side": "BUY",
                            "quantity": 1,
                            "fill_price": 19000.25,
                            "commission": 0.47,
                            "realized_pnl": 0.0,
                            "filled_at": duplicate_fill_time,
                        }
                    ],
                }

        gateway = IbkrPaperGateway(adapter=PollerAdapter())
        gateway.connect()
        for minute, last in enumerate((19000.0, 19001.0, 19002.0, 19003.0, 19004.0, 19006.0)):
            gateway.record_market_data(
                {
                    "symbol": "MNQ",
                    "bid": last - 0.25,
                    "ask": last,
                    "last": last,
                    "market_data_type": "real_time",
                    "snapshot_time": (datetime.now(UTC) - timedelta(minutes=5 - minute)).isoformat(),
                }
            )

        gateway.record_order_status(
            {
                "order_id": 99,
                "status": "Filled",
                "filled": 1,
                "remaining": 0,
                "average_fill_price": 19000.25,
            }
        )
        gateway.record_execution_fill(
            {
                "execution_id": "dup-exec-1",
                "order_id": 99,
                "symbol": "MNQ",
                "side": "BUY",
                "quantity": 1,
                "fill_price": 19000.25,
                "commission": 0.47,
                "realized_pnl": 0.0,
                "filled_at": duplicate_fill_time,
            }
        )

        review_history: list[dict] = []
        optimizer_history: list[dict] = []
        state = {
            "review_interval_seconds": 0,
            "market_data_history_limit": 100,
            "readiness_max_stale_seconds": 600,
            "auto_submit": True,
            "strategy": {
                "strategy_id": "mnq_1m_breakout",
                "strategy_spec_hash": "strategy-hash",
                "module_id": "range_breakout",
                "family": "range_breakout",
                "symbol": "MNQ",
                "timeframe": "1m",
                "lookback_bars": 5,
                "breakout_ticks": 1,
                "enabled": True,
                "tick_size": 0.25,
                "stop_loss_ticks": 20,
                "take_profit_ticks": 40,
                "max_holding_minutes": 20,
            },
            "control_state": {
                "mode": "paper",
                "min_confidence": 0.55,
                "max_spread_ticks": 2.0,
                "daily_trade_cap": 6,
                "strategies": {},
                "trade_session": {"start": "09:30", "end": "15:55"},
                "safe_mode": False,
                "kill_switch": False,
            },
            "readiness_check_count": 0,
            "review_cycle_count": 0,
            "live_order_attempt_count": 0,
            "duplicate_order_event_count": 0,
        }

        result = run_ibkr_poll_cycle(
            gateway,
            symbol="MNQ",
            review_history=review_history,
            optimizer_history=optimizer_history,
            state=state,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["readiness"]["status"], "ready")
        self.assertEqual(state["readiness_check_count"], 1)
        self.assertEqual(state["review_cycle_count"], 1)
        self.assertEqual(state["live_order_attempt_count"], 0)
        self.assertEqual(state["duplicate_order_event_count"], 0)
        self.assertEqual(result["decision"]["submit_event_type"], "bracket_order_submitted")

    def test_ibkr_poller_state_compacts_recursive_review_payloads(self) -> None:
        state = {
            "enabled": True,
            "latest_review": {
                "review_request": {
                    "created_at": "2026-04-30T14:00:00+00:00",
                    "bars_1m": [{"bar_time": f"t{index}"} for index in range(7)],
                    "signals": [{"signal_id": f"s{index}"} for index in range(12)],
                    "previous_review_summaries": [
                        {"review_request": {"previous_review_summaries": [{"large": "nested"}]}}
                    ],
                    "review_request_hash": "request-hash",
                },
                "review_result": {
                    "action": "no_action",
                    "confidence": "medium",
                    "final_summary": {"decision_reason": "no_action"},
                },
            },
        }

        compact = _compact_ibkr_poller_state(state)

        request = compact["latest_review"]["review_request"]
        self.assertEqual(len(request["bars_1m"]), 5)
        self.assertEqual(len(request["signals"]), 10)
        self.assertLess(len(json.dumps(compact)), 3000)

    def test_feature_readiness_prefers_primary_nq_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            generated = Path(temp_dir) / "strategies" / "generated"
            generated.mkdir(parents=True)
            (generated / "feature_combo_manifest.json").write_text(
                json.dumps(
                    {
                        "method": "deterministic_random_feature_combo",
                        "strategies": [{"name": "legacy"}],
                    }
                ),
                encoding="utf-8",
            )
            (generated / "primary_nq_manifest.json").write_text(
                json.dumps(
                    {
                        "method": "primary_nq_futures_seed",
                        "primary_research_track": True,
                        "candidate_family_set": ["vol_breakout_trend"],
                        "strategies": [{"name": "primary"}],
                    }
                ),
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(temp_dir)
                readiness = build_feature_readiness_response()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(readiness["generated_strategy_summary"]["method"], "primary_nq_futures_seed")
        self.assertEqual(
            readiness["generated_strategy_summary"]["manifest_path"],
            "strategies/generated/primary_nq_manifest.json",
        )
        self.assertTrue(readiness["generated_strategy_summary"]["primary_research_track"])
        self.assertEqual(readiness["generated_strategy_manifest"]["strategies"][0]["name"], "primary")

    def test_build_leaderboard_response_rebuilds_execution_validation_summary_after_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            experiments_root = Path(temp_dir) / "experiments"
            experiments_root.mkdir(parents=True)

            bar_only_payload = {
                "experiment_id": "nq_cme_bar_only",
                "strategy_name": "bar_only_candidate",
                "execution_mode": "bar",
                "gates": {"passed": True, "reasons": []},
                "robustness_score": 0.6,
                "aggregate_validation_metrics": {"net_pnl": 100.0, "sharpe": 1.1},
                "aggregate_test_metrics": {
                    "net_pnl": 600.0,
                    "sharpe": 1.5,
                    "annual_trades": 1200.0,
                    "avg_trade_net_pnl": 1.0,
                },
                "non_overlap_test_metrics": {"net_pnl": 500.0, "sharpe": 1.3, "annual_trades": 1000.0},
                "final_holdout_metrics": {"net_pnl": 320.0},
                "final_holdout_policy": {"strict_freeze_task_implemented": True},
                "promotion_report": {"mode": "bar", "stage": "direct_bar", "promoted": False, "reasons": []},
                "strategy_spec": {"symbol": "NQ_CME"},
            }
            tick_validated_payload = {
                **bar_only_payload,
                "experiment_id": "nq_cme_tick_validated",
                "strategy_name": "tick_validated_candidate",
                "execution_mode": "tick",
                "promotion_report": {"mode": "tick", "stage": "direct_tick", "promoted": True, "reasons": []},
            }
            for payload in (bar_only_payload, tick_validated_payload):
                experiment_dir = experiments_root / payload["experiment_id"]
                experiment_dir.mkdir(parents=True)
                (experiment_dir / "leaderboard.json").write_text(json.dumps(payload), encoding="utf-8")

            filtered = build_leaderboard_response(experiments_root, experiment_id="nq_cme_bar_only")

        self.assertEqual(
            filtered["summary"],
            {"passed": 0, "candidate": 1, "freeze_confirmed": 0, "rejected": 0, "total": 1},
        )
        self.assertEqual(
            filtered["execution_validation_summary"]["overall"],
            {
                "validated": 0,
                "pending_execution_validation": 1,
                "blocked_before_execution": 0,
                "not_required": 0,
                "required": 1,
                "total": 1,
            },
        )
        self.assertIn("1 passed candidates still await execution validation", filtered["message"])

    def test_vol_overview_api_helper_exposes_blocked_execution_stages(self) -> None:
        overview = build_vol_overview_response(Path("missing-experiments"))

        self.assertEqual(overview["feature_readiness"]["status"], "ready")
        self.assertEqual(overview["quote_replay"]["status"], "blocked")
        self.assertEqual(overview["paper_shadow"]["status"], "blocked")
        self.assertEqual(overview["llm_trigger_audit"]["status"], "blocked")
        self.assertIn("strategy_leaderboard", overview)

    def test_fastapi_app_registers_research_console_routes_when_installed(self) -> None:
        try:
            app = create_app()
        except RuntimeError:
            self.skipTest("FastAPI is not installed")
        paths = {route.path for route in app.routes}

        self.assertIn("/api/data/import-firstrate", paths)
        self.assertIn("/api/data/bar-quality", paths)
        self.assertIn("/api/data/split-manifest", paths)
        self.assertIn("/api/data/import-databento-quotes", paths)
        self.assertIn("/api/data/import-databento-tbbo", paths)
        self.assertIn("/api/data/import-databento-ohlcv", paths)
        self.assertIn("/api/data/quote-replay", paths)
        self.assertIn("/api/backtests/tick", paths)
        self.assertIn("/api/experiments/primary-research-runs", paths)
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
        self.assertIn("/api/features/readiness", paths)
        self.assertIn("/api/vol/overview", paths)
        self.assertIn("/api/vol/seed-search", paths)
        self.assertIn("/api/vol/pre-screen", paths)
        self.assertIn("/api/vol/quote-fill-replay", paths)
        self.assertIn("/api/modules/memory", paths)
        self.assertIn("/api/trigger-gate/simulations", paths)
        self.assertIn("/api/trigger-gate/reports", paths)
        self.assertIn("/api/trigger-gate/schedules", paths)
        self.assertIn("/api/trigger-gate/memory", paths)
        self.assertIn("/api/trigger-gate/outcomes", paths)
        self.assertIn("/api/gateways/nt8/order-updates", paths)
        self.assertIn("/api/gateways/nt8/incidents", paths)
        self.assertIn("/api/gateways/ibkr/health", paths)
        self.assertIn("/api/gateways/ibkr/readiness", paths)
        self.assertIn("/api/gateways/ibkr/contracts", paths)
        self.assertIn("/api/gateways/ibkr/contracts/sync", paths)
        self.assertIn("/api/gateways/ibkr/market-data", paths)
        self.assertIn("/api/gateways/ibkr/market-data/sync", paths)
        self.assertIn("/api/gateways/ibkr/connect", paths)
        self.assertIn("/api/gateways/ibkr/safe-mode", paths)
        self.assertIn("/api/gateways/ibkr/bracket-orders", paths)
        self.assertIn("/api/gateways/ibkr/bracket-orders/{bracket_id}/submit", paths)
        self.assertIn("/api/gateways/ibkr/kill-switch", paths)
        self.assertIn("/api/gateways/ibkr/execution-ledger", paths)
        self.assertIn("/api/gateways/ibkr/orders", paths)
        self.assertIn("/api/gateways/ibkr/executions", paths)
        self.assertIn("/api/gateways/ibkr/positions", paths)
        self.assertIn("/api/gateways/ibkr/positions/sync", paths)
        self.assertIn("/api/gateways/ibkr/account-snapshots", paths)
        self.assertIn("/api/gateways/ibkr/account-snapshots/sync", paths)
        self.assertIn("/api/gateways/ibkr/poller", paths)
        self.assertIn("/api/gateways/ibkr/runtime-events/sync", paths)
        self.assertIn("/api/gateways/ibkr/reviews", paths)
        self.assertIn("/api/gateways/ibkr/fast-path-optimizer", paths)
        self.assertIn("/api/ibkr-paper/runs", paths)
        self.assertIn("/api/ibkr-paper/runs/{run_id}", paths)
        self.assertIn("/api/ibkr-paper/reviews", paths)
        self.assertIn("/api/ibkr-paper/reports/{run_id}", paths)

    def test_fastapi_app_can_auto_connect_ibkr_on_startup(self) -> None:
        try:
            from fastapi.testclient import TestClient
        except ModuleNotFoundError:
            self.skipTest("FastAPI is not installed")

        class AutoConnectAdapter:
            def connect(self, host: str, port: int, client_id: int) -> dict:
                return {"connected": True, "host": host, "port": port, "client_id": client_id}

            def disconnect(self) -> dict:
                return {"connected": False}

            def account_summary(self) -> dict:
                return {"account_id": "DU1234567", "account_type": "paper"}

            def request_contract_details(self, contract: dict) -> dict:
                return {}

            def request_market_data(self, contract: dict, timeout_seconds: int = 5) -> dict:
                return {}

            def request_positions(self) -> list[dict]:
                return []

            def request_account_snapshot(self, account_id: str | None = None) -> dict:
                return {}

            def submit_bracket_order(self, contract: dict, order: dict) -> dict:
                return {}

            def cancel_order(self, order_id: int) -> dict:
                return {}

            def submit_flatten_order(self, contract: dict, action: str, quantity: int) -> dict:
                return {}

            def drain_runtime_events(self) -> dict:
                return {"order_status": [], "executions": []}

        with patch.dict(
            os.environ,
            {
                "TLM_IBKR_AUTO_CONNECT": "1",
                "TLM_IBKR_HOST": "127.0.0.1",
                "TLM_IBKR_PORT": "7497",
                "TLM_IBKR_CLIENT_ID": "17",
                "TLM_IBKR_POLLER_ENABLED": "0",
            },
            clear=False,
        ):
            with patch("tlm.api.build_ibkr_gateway_adapter", return_value=AutoConnectAdapter()):
                app = create_app()
                with TestClient(app) as client:
                    health = client.get("/api/gateways/ibkr/health").json()
                    poller = client.get("/api/gateways/ibkr/poller").json()

        self.assertTrue(health["connected"])
        self.assertTrue(health["paper_account_verified"])
        self.assertEqual(poller["startup_connect_event"]["event_type"], "connected")

    def test_ibkr_poller_update_blocks_auto_submit_until_acceptance_ready(self) -> None:
        try:
            from fastapi.testclient import TestClient
        except ModuleNotFoundError:
            self.skipTest("FastAPI is not installed")

        class PollerControlAdapter:
            def connect(self, host: str, port: int, client_id: int) -> dict:
                return {"connected": True, "host": host, "port": port, "client_id": client_id}

            def disconnect(self) -> dict:
                return {"connected": False}

            def account_summary(self) -> dict:
                return {"account_id": "DU1234567", "account_type": "paper"}

            def request_contract_details(self, contract: dict) -> dict:
                return {
                    "symbol": contract["symbol"],
                    "tick_size": 0.25,
                    "point_value": 2.0,
                    "exchange": contract["exchange"],
                    "currency": contract["currency"],
                    "last_trade_date_or_contract_month": "20260618",
                    "local_symbol": "MNQM6",
                    "trading_class": "MNQ",
                }

            def request_market_data(self, contract: dict, timeout_seconds: int = 5) -> dict:
                return {
                    "symbol": contract["symbol"],
                    "bid": 19000.0,
                    "ask": 19000.25,
                    "last": 19000.25,
                    "market_data_type": "delayed",
                    "snapshot_time": datetime.now(UTC).isoformat(),
                }

            def request_positions(self) -> list[dict]:
                return []

            def request_account_snapshot(self, account_id: str | None = None) -> dict:
                return {
                    "net_liquidation": 100000.0,
                    "daily_pnl": 0.0,
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 0.0,
                    "drawdown_usage": 0.0,
                    "recorded_at": datetime.now(UTC).isoformat(),
                }

            def submit_bracket_order(self, contract: dict, order: dict) -> dict:
                return {}

            def cancel_order(self, order_id: int) -> dict:
                return {}

            def submit_flatten_order(self, contract: dict, action: str, quantity: int) -> dict:
                return {}

            def drain_runtime_events(self) -> dict:
                return {"order_status": [], "executions": []}

        with patch.dict(
            os.environ,
            {
                "TLM_IBKR_AUTO_CONNECT": "1",
                "TLM_IBKR_HOST": "127.0.0.1",
                "TLM_IBKR_PORT": "7497",
                "TLM_IBKR_CLIENT_ID": "18",
                "TLM_IBKR_POLLER_ENABLED": "0",
            },
            clear=False,
        ):
            with patch("tlm.api.build_ibkr_gateway_adapter", return_value=PollerControlAdapter()):
                app = create_app()
                with TestClient(app) as client:
                    client.post("/api/gateways/ibkr/contracts/sync")
                    client.post("/api/gateways/ibkr/market-data/sync")
                    blocked = client.post("/api/gateways/ibkr/poller", json={"auto_submit": True}).json()
                    poller = client.get("/api/gateways/ibkr/poller").json()

        self.assertEqual(blocked["status"], "blocked")
        self.assertFalse(blocked["auto_submit"])
        self.assertIn("trading_days<5", blocked["blockers"])
        self.assertFalse(poller["auto_submit"])

    def test_ibkr_poller_update_can_adjust_max_spread_ticks(self) -> None:
        try:
            from fastapi.testclient import TestClient
        except ModuleNotFoundError:
            self.skipTest("FastAPI is not installed")

        with patch.dict(
            os.environ,
            {
                "TLM_IBKR_AUTO_CONNECT": "0",
                "TLM_IBKR_POLLER_ENABLED": "0",
            },
            clear=False,
        ):
            app = create_app()
            with TestClient(app) as client:
                updated = client.post(
                    "/api/gateways/ibkr/poller",
                    json={"max_spread_ticks": 3.0, "reason": "test_spread_tuning"},
                ).json()
                poller = client.get("/api/gateways/ibkr/poller").json()

        self.assertEqual(updated["status"], "updated")
        self.assertEqual(updated["updates"][0]["field"], "max_spread_ticks")
        self.assertEqual(poller["control_state"]["max_spread_ticks"], 3.0)
        self.assertFalse(poller["auto_submit"])

    def test_ibkr_poller_update_force_enables_auto_submit(self) -> None:
        try:
            from fastapi.testclient import TestClient
        except ModuleNotFoundError:
            self.skipTest("FastAPI is not installed")

        class PollerControlAdapter:
            def connect(self, host: str, port: int, client_id: int) -> dict:
                return {"connected": True, "host": host, "port": port, "client_id": client_id}

            def disconnect(self) -> dict:
                return {"connected": False}

            def account_summary(self) -> dict:
                return {"account_id": "DU1234567", "account_type": "paper"}

            def request_contract_details(self, contract: dict) -> dict:
                return {
                    "symbol": contract["symbol"],
                    "tick_size": 0.25,
                    "point_value": 2.0,
                    "exchange": contract["exchange"],
                    "currency": contract["currency"],
                    "last_trade_date_or_contract_month": "20260618",
                    "local_symbol": "MNQM6",
                    "trading_class": "MNQ",
                }

            def request_market_data(self, contract: dict, timeout_seconds: int = 5) -> dict:
                return {
                    "symbol": contract["symbol"],
                    "bid": 19000.0,
                    "ask": 19000.25,
                    "last": 19000.25,
                    "market_data_type": "delayed",
                    "snapshot_time": datetime.now(UTC).isoformat(),
                }

            def request_positions(self) -> list[dict]:
                return []

            def request_account_snapshot(self, account_id: str | None = None) -> dict:
                return {
                    "net_liquidation": 100000.0,
                    "daily_pnl": 0.0,
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 0.0,
                    "drawdown_usage": 0.0,
                    "recorded_at": datetime.now(UTC).isoformat(),
                }

            def submit_bracket_order(self, contract: dict, order: dict) -> dict:
                return {}

            def cancel_order(self, order_id: int) -> dict:
                return {}

            def submit_flatten_order(self, contract: dict, action: str, quantity: int) -> dict:
                return {}

            def drain_runtime_events(self) -> dict:
                return {"order_status": [], "executions": []}

        with patch.dict(
            os.environ,
            {
                "TLM_IBKR_AUTO_CONNECT": "1",
                "TLM_IBKR_HOST": "127.0.0.1",
                "TLM_IBKR_PORT": "7497",
                "TLM_IBKR_CLIENT_ID": "19",
                "TLM_IBKR_POLLER_ENABLED": "0",
            },
            clear=False,
        ):
            with patch("tlm.api.build_ibkr_gateway_adapter", return_value=PollerControlAdapter()):
                app = create_app()
                with TestClient(app) as client:
                    client.post("/api/gateways/ibkr/contracts/sync")
                    client.post("/api/gateways/ibkr/market-data/sync")
                    updated = client.post("/api/gateways/ibkr/poller", json={"auto_submit": True, "force": True}).json()
                    poller = client.get("/api/gateways/ibkr/poller").json()

        self.assertEqual(updated["status"], "updated")
        self.assertTrue(updated["auto_submit"])
        self.assertTrue(updated["force"])
        self.assertTrue(poller["auto_submit"])

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
            "/api/data/import-firstrate",
            "/api/data/bar-quality",
            "/api/data/split-manifest",
            "/api/data/import-databento-quotes",
            "/api/data/import-databento-tbbo",
            "/api/data/import-databento-ohlcv",
            "/api/data/quote-replay",
            "/api/tasks/{task_id}/events",
            "/api/experiments/research-runs",
            "/api/experiments/target-discovery",
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
            "/api/features/readiness",
            "/api/vol/overview",
            "/api/vol/seed-search",
            "/api/vol/pre-screen",
            "/api/vol/quote-fill-replay",
            "/api/gateways/nt8/commands",
            "/api/gateways/nt8/order-updates",
            "/api/gateways/nt8/incidents",
            "/api/gateways/ibkr/health",
            "/api/gateways/ibkr/readiness",
            "/api/gateways/ibkr/contracts",
            "/api/gateways/ibkr/contracts/sync",
            "/api/gateways/ibkr/market-data",
            "/api/gateways/ibkr/market-data/sync",
            "/api/gateways/ibkr/connect",
            "/api/gateways/ibkr/safe-mode",
            "/api/gateways/ibkr/bracket-orders",
            "/api/gateways/ibkr/bracket-orders/{bracket_id}/submit",
            "/api/gateways/ibkr/kill-switch",
            "/api/gateways/ibkr/execution-ledger",
            "/api/gateways/ibkr/orders",
            "/api/gateways/ibkr/executions",
            "/api/gateways/ibkr/positions",
            "/api/gateways/ibkr/positions/sync",
            "/api/gateways/ibkr/account-snapshots",
            "/api/gateways/ibkr/account-snapshots/sync",
            "/api/gateways/ibkr/poller",
            "/api/gateways/ibkr/runtime-events/sync",
            "/api/gateways/ibkr/reviews",
            "/api/gateways/ibkr/fast-path-optimizer",
            "/api/ibkr-paper/runs",
            "/api/ibkr-paper/runs/{run_id}",
            "/api/ibkr-paper/reviews",
            "/api/ibkr-paper/reports/{run_id}",
        ]

        for path in required_paths:
            self.assertIn(path, contract)
        for schema_name in [
            "Task:",
            "ResearchRunRequest:",
            "TargetDiscoveryRequest:",
            "LeaderboardReport:",
            "FeatureReadinessReport:",
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
