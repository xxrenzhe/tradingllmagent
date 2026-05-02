#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tlm.storage import write_json


DEFAULT_REPORTS = {
    "expanded_2r": Path("reports/nq_expanded_high_edge_walk_forward_2r_gate_2026-05-02.json"),
    "expanded_70wr_2r": Path("reports/nq_expanded_high_edge_walk_forward_70wr_2r_hard_gate_2026-05-02.json"),
    "smc_objective": Path("reports/nq_smc_lqem_ce_v1_objective_gates_2026-05-02.json"),
    "tick_coverage": Path("reports/nq_expanded_high_edge_mbp1_quote_coverage_audit_2026-05-02.json"),
    "execution_stress": Path("reports/nq_expanded_high_edge_execution_stress_mbp1_full_2026-05-02.json"),
    "vol_objective": Path("reports/nq_vol_execution_70wr_2r_objective_audit_2026-05-02.json"),
    "mbp1_microstructure_2r": Path("reports/nq_mbp1_microstructure_2r_search_2026-05-03.json"),
    "mbp1_microstructure_2r_reversal": Path("reports/nq_mbp1_microstructure_2r_reversal_search_2026-05-03.json"),
    "mbp1_microstructure_2r_medium": Path("reports/nq_mbp1_microstructure_2r_medium_candidate_index_search_2026-05-03.json"),
    "mbp1_microstructure_2r_full": Path("reports/nq_mbp1_microstructure_2r_full_grid_candidate_index_search_2026-05-03.json"),
    "assessment": Path("reports/black_box_70pct_winrate_2r_strategy_assessment_2026-05-02.md"),
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit completion of the 70% win-rate / 2R live-ready strategy objective.")
    parser.add_argument("--output", type=Path, default=Path("reports/70wr_2r_objective_completion_audit_2026-05-02.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("reports/70wr_2r_objective_completion_audit_2026-05-02.md"))
    args = parser.parse_args(argv)

    evidence = load_evidence(DEFAULT_REPORTS)
    payload = build_completion_audit(evidence)
    write_json(args.output, payload)
    args.markdown_output.write_text(render_markdown(payload), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "achieved": payload["decision"]["achieved"]}, indent=2))
    return 0


def load_evidence(paths: dict[str, Path]) -> dict[str, Any]:
    loaded: dict[str, Any] = {}
    for key, path in paths.items():
        if path.suffix == ".json":
            loaded[key] = json.loads(path.read_text(encoding="utf-8"))
        else:
            loaded[key] = path.read_text(encoding="utf-8")
    return loaded


def build_completion_audit(evidence: dict[str, Any]) -> dict[str, Any]:
    expanded_2r = evidence["expanded_2r"]
    expanded_70wr_2r = evidence["expanded_70wr_2r"]
    smc_objective = evidence["smc_objective"]
    tick_coverage = evidence["tick_coverage"]
    execution_stress = evidence["execution_stress"]
    vol_objective = evidence["vol_objective"]
    mbp1_microstructure_2r = evidence.get("mbp1_microstructure_2r")
    mbp1_microstructure_2r_reversal = evidence.get("mbp1_microstructure_2r_reversal")
    mbp1_microstructure_2r_medium = evidence.get("mbp1_microstructure_2r_medium")
    mbp1_microstructure_2r_full = evidence.get("mbp1_microstructure_2r_full")

    requirements = [
        requirement(
            "win_rate_70pct",
            "Strategy must demonstrate at least 70% win rate after costs.",
            passed=False,
            evidence=[
                {
                    "artifact": str(DEFAULT_REPORTS["expanded_70wr_2r"]),
                    "finding": expanded_70wr_2r["summary"]["decision"],
                },
                {
                    "artifact": str(DEFAULT_REPORTS["smc_objective"]),
                    "finding": gate_findings(smc_objective, ("full_history_win_rate_ge_target", "walk_forward_test_win_rate_ge_target")),
                },
                {
                    "artifact": str(DEFAULT_REPORTS["vol_objective"]),
                    "finding": gate_findings(vol_objective, ("vol_prescreen_has_70pct_win_rate",)),
                },
                {
                    "artifact": str(DEFAULT_REPORTS["mbp1_microstructure_2r"]),
                    "finding": microstructure_finding(mbp1_microstructure_2r),
                },
                {
                    "artifact": str(DEFAULT_REPORTS["mbp1_microstructure_2r_reversal"]),
                    "finding": microstructure_finding(mbp1_microstructure_2r_reversal),
                },
                {
                    "artifact": str(DEFAULT_REPORTS["mbp1_microstructure_2r_medium"]),
                    "finding": microstructure_finding(mbp1_microstructure_2r_medium),
                },
                {
                    "artifact": str(DEFAULT_REPORTS["mbp1_microstructure_2r_full"]),
                    "finding": microstructure_finding(mbp1_microstructure_2r_full),
                },
            ],
            gap="No candidate passes the 70% win-rate gate; expanded-high-edge has no train-selected 70%/2R fold, SMC v1 has 0% full-history win rate, and VOL has no >=70% prescreen row.",
        ),
        requirement(
            "reward_2r",
            "Strategy must target and realize a 2R reward profile.",
            passed=False,
            evidence=[
                {
                    "artifact": str(DEFAULT_REPORTS["expanded_2r"]),
                    "finding": expanded_2r["summary"]["decision"],
                },
                {
                    "artifact": str(DEFAULT_REPORTS["smc_objective"]),
                    "finding": gate_findings(smc_objective, ("full_history_net_r_p75_ge_target", "walk_forward_test_net_r_p75_ge_target")),
                },
                {
                    "artifact": str(DEFAULT_REPORTS["vol_objective"]),
                    "finding": gate_findings(vol_objective, ("vol_strategy_reward_profile_ge_2r",)),
                },
                {
                    "artifact": str(DEFAULT_REPORTS["mbp1_microstructure_2r"]),
                    "finding": microstructure_finding(mbp1_microstructure_2r),
                },
                {
                    "artifact": str(DEFAULT_REPORTS["mbp1_microstructure_2r_reversal"]),
                    "finding": microstructure_finding(mbp1_microstructure_2r_reversal),
                },
                {
                    "artifact": str(DEFAULT_REPORTS["mbp1_microstructure_2r_medium"]),
                    "finding": microstructure_finding(mbp1_microstructure_2r_medium),
                },
                {
                    "artifact": str(DEFAULT_REPORTS["mbp1_microstructure_2r_full"]),
                    "finding": microstructure_finding(mbp1_microstructure_2r_full),
                },
            ],
            gap="Strict 2R expanded-high-edge walk-forward fails, SMC v1 net-R gates are negative rather than >= 2R, and VOL generated specs are below 2R.",
        ),
        requirement(
            "non_overfit_walk_forward",
            "Strategy must pass locked train/test or walk-forward validation without post-hoc overfit.",
            passed=False,
            evidence=[
                {
                    "artifact": str(DEFAULT_REPORTS["expanded_70wr_2r"]),
                    "finding": expanded_70wr_2r["summary"]["decision"],
                },
                {
                    "artifact": str(DEFAULT_REPORTS["smc_objective"]),
                    "finding": gate_findings(smc_objective, ("walk_forward_test_win_rate_ge_target", "walk_forward_test_net_r_p75_ge_target")),
                },
                {
                    "artifact": str(DEFAULT_REPORTS["vol_objective"]),
                    "finding": gate_findings(vol_objective, ("vol_final_target_rows_exist",)),
                },
                {
                    "artifact": str(DEFAULT_REPORTS["mbp1_microstructure_2r"]),
                    "finding": microstructure_finding(mbp1_microstructure_2r),
                },
                {
                    "artifact": str(DEFAULT_REPORTS["mbp1_microstructure_2r_reversal"]),
                    "finding": microstructure_finding(mbp1_microstructure_2r_reversal),
                },
                {
                    "artifact": str(DEFAULT_REPORTS["mbp1_microstructure_2r_medium"]),
                    "finding": microstructure_finding(mbp1_microstructure_2r_medium),
                },
                {
                    "artifact": str(DEFAULT_REPORTS["mbp1_microstructure_2r_full"]),
                    "finding": microstructure_finding(mbp1_microstructure_2r_full),
                },
            ],
            gap="No locked walk-forward candidate survives the explicit 70%/2R objective gates; VOL has no final-target rows.",
        ),
        requirement(
            "long_term_profitability",
            "Strategy must show long-term positive profitability across years and holdout periods.",
            passed=False,
            evidence=[
                {
                    "artifact": str(DEFAULT_REPORTS["expanded_2r"]),
                    "finding": expanded_2r["summary"]["decision"],
                },
                {
                    "artifact": str(DEFAULT_REPORTS["smc_objective"]),
                    "finding": {
                        "full_history_net_pnl": smc_objective["full_history"]["metrics"]["net_pnl"],
                        "final_holdout_metrics": (smc_objective.get("final_holdout", {}).get("summary") or {}).get("summary", {}).get("metrics", {}),
                    },
                },
            ],
            gap="Strict 2R expanded-high-edge fails positive years; SMC v1 full-history net PnL is negative and final holdout has no trades.",
        ),
        requirement(
            "black_box_tick_test",
            "Provided Databento tick/quote window must be normalized, audited, and used for black-box execution evidence.",
            passed=bool(tick_coverage["decision"]["passed"]),
            evidence=[
                {
                    "artifact": str(DEFAULT_REPORTS["tick_coverage"]),
                    "finding": {
                        "decision": tick_coverage["decision"],
                        "expected_quote_days": tick_coverage["expected_quote_days"],
                        "analyzed_quote_days": tick_coverage["analyzed_quote_days"],
                        "validated_trade_count": tick_coverage["validated_trade_count"],
                    },
                },
            ],
            gap=None if tick_coverage["decision"]["passed"] else "Tick/quote coverage audit did not pass.",
        ),
        requirement(
            "execution_cost_stress",
            "Execution validation must survive cost stress before live readiness.",
            passed=bool(execution_stress["decision"]["passed"]),
            evidence=[
                {
                    "artifact": str(DEFAULT_REPORTS["execution_stress"]),
                    "finding": execution_stress["decision"],
                },
            ],
            gap="Quote replay passes for the available tick window, but bar-level cost stress still fails.",
        ),
        requirement(
            "live_ready",
            "Strategy must be directly usable in live trading with promotion gates passed.",
            passed=False,
            evidence=[
                {
                    "artifact": str(DEFAULT_REPORTS["assessment"]),
                    "finding": "Main assessment rejects live promotion.",
                },
                {
                    "artifact": str(DEFAULT_REPORTS["execution_stress"]),
                    "finding": execution_stress["decision"],
                },
            ],
            gap="Core objective gates and cost-stress gates fail, so live readiness is not established.",
        ),
    ]
    achieved = all(row["passed"] for row in requirements)
    return {
        "artifact": "70wr_2r_objective_completion_audit",
        "objective": "Train a 70% win-rate, 2R, non-overfit, live-ready, long-term profitable, black-box-tested NQ/MNQ strategy using data/bars and data/raw/databento/GLBX-20260502-QG6TRKVV9Q.zip.",
        "success_criteria": [row["requirement"] for row in requirements],
        "requirements": requirements,
        "decision": {
            "achieved": achieved,
            "reason": None if achieved else "At least one required objective gate remains unmet; do not promote or mark complete.",
            "failed_requirements": [row["id"] for row in requirements if not row["passed"]],
        },
    }


def requirement(
    requirement_id: str,
    text: str,
    *,
    passed: bool,
    evidence: list[dict[str, Any]],
    gap: str | None,
) -> dict[str, Any]:
    return {
        "id": requirement_id,
        "requirement": text,
        "passed": bool(passed),
        "evidence": evidence,
        "gap": gap,
    }


def gate_findings(report: dict[str, Any], names: Sequence[str]) -> list[dict[str, Any]]:
    gates = {gate["name"]: gate for gate in report.get("objective_gates", [])}
    return [gates[name] for name in names if name in gates]


def microstructure_finding(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {"status": "missing"}
    selected = report.get("selected") or {}
    return {
        "decision": report.get("decision"),
        "target": report.get("target"),
        "coverage": report.get("coverage"),
        "selected_spec": selected.get("spec"),
        "selected_train": selected.get("train"),
        "selected_test": selected.get("test"),
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 70% Win-Rate / 2R Objective Completion Audit",
        "",
        f"- Achieved: `{payload['decision']['achieved']}`",
        f"- Reason: {payload['decision']['reason'] or 'All requirements passed.'}",
        "",
        "## Checklist",
        "",
    ]
    for row in payload["requirements"]:
        status = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- {row['id']}: {status} - {row['requirement']}")
        if row["gap"]:
            lines.append(f"  Gap: {row['gap']}")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
