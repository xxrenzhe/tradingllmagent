from __future__ import annotations

import tempfile
import unittest
import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from tlm.research import load_leaderboard_report, run_budgeted_research, write_research_result
from tlm.strategy import parse_strategy_spec
from tlm.strategy_generation import generate_primary_nq_strategy_specs
from tlm.top_strategy_report import _select_top_yearly_strategies

from test_strategy_backtest import base_spec
from test_validation_leaderboard import (
    symbol_config,
    write_breakout_day,
    write_breakout_ticks,
    write_ready_paper_shadow,
    write_ready_quote_report,
)


def _write_leaderboard_payload(experiments_root: Path, payload: dict) -> Path:
    experiment_dir = experiments_root / payload["experiment_id"]
    experiment_dir.mkdir(parents=True)
    path = experiment_dir / "leaderboard.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return experiment_dir


def _ready_primary_payload(
    *,
    experiment_id: str,
    avg_trade_net_pnl: float,
    net_pnl: float,
) -> dict:
    return {
        "experiment_id": experiment_id,
        "strategy_name": f"{experiment_id}_candidate",
        "execution_mode": "tick",
        "gates": {"passed": True, "reasons": []},
        "robustness_score": 0.8,
        "aggregate_validation_metrics": {
            "net_pnl": 700.0,
            "sharpe": 2.3,
            "avg_trade_net_pnl": avg_trade_net_pnl * 0.8,
        },
        "aggregate_test_metrics": {
            "net_pnl": net_pnl,
            "sharpe": 2.8,
            "annual_trades": 1300.0,
            "avg_trade_net_pnl": avg_trade_net_pnl,
        },
        "non_overlap_test_metrics": {
            "net_pnl": net_pnl * 0.9,
            "sharpe": 2.6,
            "annual_trades": 1200.0,
        },
        "final_holdout_metrics": {
            "net_pnl": 320.0,
            "avg_trade_net_pnl": avg_trade_net_pnl * 0.7,
        },
        "final_holdout_policy": {"strict_freeze_task_implemented": True},
        "promotion_report": {
            "mode": "bar_then_tick",
            "stage": "promoted_to_tick",
            "promoted": True,
            "reasons": [],
        },
        "strategy_spec": {"symbol": "NQ_CME"},
    }


def _top_report_from_rows(rows: list[dict]) -> dict:
    replays = []
    for index, row in enumerate(rows):
        replays.append(
            {
                "basket_id": row["experiment_id"],
                "basket_hash": row["experiment_id"],
                "yearly_profitable_candidates": [
                    {
                        "selection_rule": row["experiment_id"],
                        "activation_start_year": 2024,
                        "constituent_edges": [
                            {
                                "scan_type": row["experiment_id"],
                                "horizon_minutes": 60,
                                "session_bucket": "rth",
                                "dow": index,
                                "direction_label": "long",
                            }
                        ],
                        "train_period": {"covered_days": 200},
                        "test_period": {"covered_days": 200},
                        "full_after_activation": {
                            "net_pnl": row["net_pnl_test"],
                            "annual_trades": row["annual_trades_test"],
                            "avg_trade_net_pnl": row["avg_trade_net_pnl_test"],
                            "profit_factor": 1.25,
                            "win_probability": 0.56,
                            "return_to_drawdown": 1.8,
                            "max_drawdown": 120,
                            "cost_stress": [
                                {
                                    "label": "configured_cost_plus_2_ticks",
                                    "net_pnl": 100,
                                }
                            ],
                            "yearly_results": [
                                {"year": 2024, "net_pnl": 120},
                                {"year": 2025, "net_pnl": 80},
                            ],
                        },
                        "test": {
                            "net_pnl": row["net_pnl_test"] * 0.5,
                            "profit_factor": 1.12,
                            "avg_trade_net_pnl": row["avg_trade_net_pnl_test"],
                        },
                        "candidate_stage": row["candidate_stage"],
                        "execution_validation": row["execution_validation"],
                        "execution_evidence": row["execution_evidence"],
                    }
                ],
            }
        )
    return {"symbol": "NQ_CME", "timeframe": "1m", "regime_basket_replays": replays}


class PrimaryResearchPipelineE2ETests(unittest.TestCase):
    def test_primary_pipeline_integration_matrix_reaches_only_execution_validated_nq_cme(self) -> None:
        generated = generate_primary_nq_strategy_specs()
        self.assertEqual(len(generated), 4)
        self.assertEqual({spec["symbol"] for spec in generated}, {"NQ_CME"})
        self.assertTrue(all(spec["generation"]["primary_research_track"] for spec in generated))
        self.assertEqual(
            {spec["generation"]["primary_template"] for spec in generated},
            {
                "opening_range_breakout_retest_with_confirmation",
                "trend_pullback_after_impulse",
                "volatility_expansion_after_compression",
                "regime_filtered_continuation",
            },
        )

        spec = parse_strategy_spec(base_spec())
        primary_symbol = replace(
            symbol_config(),
            alias="NQ_CME",
            provider="firstratedata",
            instrument="NQ",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            experiments_root = root / "experiments"
            for offset in range(40):
                day = date(2025, 1, 1) + timedelta(days=offset)
                write_breakout_day(data_root, day, symbol="NQ_CME")
                write_breakout_ticks(data_root, day, symbol="NQ_CME")

            promoted_dir = experiments_root / "primary_promoted"
            promoted_dir.mkdir(parents=True)
            quote_report = promoted_dir / "quote_replay.json"
            paper_report = promoted_dir / "paper_shadow.jsonl"
            write_ready_quote_report(quote_report)
            write_ready_paper_shadow(paper_report)
            (promoted_dir / "leaderboard.json").write_text(
                json.dumps(_ready_primary_payload(experiment_id="primary_promoted", avg_trade_net_pnl=42.0, net_pnl=900.0)),
                encoding="utf-8",
            )
            lower_expectancy_dir = _write_leaderboard_payload(
                experiments_root,
                _ready_primary_payload(
                    experiment_id="primary_promoted_lower_expectancy",
                    avg_trade_net_pnl=12.0,
                    net_pnl=1500.0,
                ),
            )
            write_ready_quote_report(lower_expectancy_dir / "quote_replay.json")
            write_ready_paper_shadow(lower_expectancy_dir / "paper_shadow.jsonl")
            _write_leaderboard_payload(
                experiments_root,
                {
                    **_ready_primary_payload(
                        experiment_id="primary_bar_validated_only",
                        avg_trade_net_pnl=80.0,
                        net_pnl=1700.0,
                    ),
                    "execution_mode": "bar",
                    "promotion_report": {
                        "mode": "bar",
                        "stage": "direct_bar",
                        "promoted": False,
                        "reasons": [],
                    },
                },
            )
            _write_leaderboard_payload(
                experiments_root,
                {
                    **_ready_primary_payload(
                        experiment_id="primary_tick_missing_evidence",
                        avg_trade_net_pnl=70.0,
                        net_pnl=1600.0,
                    ),
                    "promotion_report": {
                        "mode": "tick",
                        "stage": "direct_tick",
                        "promoted": True,
                        "reasons": [],
                    },
                },
            )
            cfd_proxy_payload = {
                **_ready_primary_payload(
                    experiment_id="nqmain_proxy_high_expectancy",
                    avg_trade_net_pnl=120.0,
                    net_pnl=2400.0,
                ),
                "strategy_spec": {"symbol": "NQmain"},
            }
            _write_leaderboard_payload(experiments_root, cfd_proxy_payload)

            rejected = run_budgeted_research(
                seed_spec=spec,
                symbol_config=primary_symbol,
                data_root=data_root,
                experiment_id="primary_prescreen_rejected",
                date_from=date(2025, 1, 1),
                date_to=date(2025, 1, 20),
                max_trials=1,
                train_days=5,
                validation_days=5,
                test_days=5,
                step_days=5,
                embargo_days=0,
                final_holdout_days=5,
                min_folds=1,
                execution_mode="bar_then_tick",
            )[0]
            write_research_result(experiments_root / rejected.experiment_id / "leaderboard.json", rejected)

            report = load_leaderboard_report(experiments_root)
            top_selected = _select_top_yearly_strategies(
                _top_report_from_rows(report["leaderboard"]),
                top_n=2,
                objective="expectancy_first",
            )

        self.assertEqual(report["leaderboard"][0]["experiment_id"], "primary_promoted")
        self.assertEqual(report["leaderboard"][1]["experiment_id"], "primary_promoted_lower_expectancy")
        self.assertEqual(
            [row["symbol"] for row in report["leaderboard"]],
            ["NQ_CME", "NQ_CME"],
        )
        self.assertEqual(
            [row["execution_validation"]["status"] for row in report["leaderboard"]],
            ["validated", "validated"],
        )
        self.assertEqual(report["leaderboard"][0]["candidate_stage"], "freeze_confirmed_candidate")
        self.assertEqual(report["leaderboard"][0]["execution_evidence"]["status"], "ready_for_promotion")
        by_id = {row["experiment_id"]: row for row in report["candidate_leaderboard"]}
        self.assertEqual(by_id["primary_bar_validated_only"]["candidate_stage"], "bar_validated_candidate")
        self.assertEqual(
            by_id["primary_bar_validated_only"]["execution_validation"]["status"],
            "pending_execution_validation",
        )
        self.assertEqual(by_id["primary_tick_missing_evidence"]["candidate_stage"], "execution_validated_candidate")
        self.assertEqual(by_id["primary_tick_missing_evidence"]["execution_evidence"]["status"], "incomplete_evidence")
        self.assertFalse(by_id["nqmain_proxy_high_expectancy"]["primary_conclusion_eligible"])
        self.assertEqual(report["rejected"][0]["experiment_id"], "primary_prescreen_rejected_trial_0000")
        self.assertEqual(report["rejected"][0]["candidate_stage"], "prescreen_rejected_candidate")
        self.assertEqual(report["rejected"][0]["execution_validation"]["status"], "blocked_before_execution")
        self.assertEqual(report["candidate_stage_summary"]["freeze_confirmed_candidate"], 3)
        self.assertEqual(report["candidate_stage_summary"]["bar_validated_candidate"], 1)
        self.assertEqual(report["candidate_stage_summary"]["execution_validated_candidate"], 1)
        self.assertEqual(report["candidate_stage_summary"]["prescreen_rejected_candidate"], 1)
        self.assertEqual(top_selected[0]["basket_id"], "primary_promoted")


if __name__ == "__main__":
    unittest.main()
