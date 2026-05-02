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
from tlm.variants import stable_hash


DEFAULT_EXPANDED_REPORTS = (
    Path("reports/nq_expanded_high_edge_walk_forward_55wr_15r_hard_gate_2026-05-02.json"),
    Path("reports/nq_expanded_high_edge_walk_forward_55wr_15r_net_2026-05-02.json"),
    Path("reports/nq_expanded_high_edge_walk_forward_55wr_15r_floor_2026-05-02.json"),
)
DEFAULT_VOL_REPORT = Path("reports/nq_vol_execution_55wr_15r_objective_audit_2026-05-02.json")
DEFAULT_SMC_REPORT = Path("reports/nq_smc_lqem_ce_v1_55wr_15r_objective_gates_2026-05-02.json")
DEFAULT_LOW_R_REPORT = Path("experiments/profit_mining/low_r_high_frequency_15r_forced_baseline_2019_2026.json")
DEFAULT_LOW_R_SUBSET_REPORT = Path("reports/low_r_15r_subset_search_55wr_wide_2026-05-03.json")
DEFAULT_LOW_R_SUBSET_WALK_FORWARD_REPORT = Path("reports/low_r_15r_subset_walk_forward_55wr_2026-05-03.json")
DEFAULT_TICK_MICROSTRUCTURE_REPORT = Path("reports/nq_tick_microstructure_filter_audit_55wr_15r_2026-05-03.json")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit relaxed 55% win-rate / 1.5R strategy objective evidence.")
    parser.add_argument("--report", action="append", type=Path, help="Expanded high-edge walk-forward report.")
    parser.add_argument("--expanded-report", action="append", type=Path)
    parser.add_argument("--vol-report", type=Path, default=DEFAULT_VOL_REPORT)
    parser.add_argument("--smc-report", type=Path, default=DEFAULT_SMC_REPORT)
    parser.add_argument("--low-r-report", type=Path, default=DEFAULT_LOW_R_REPORT)
    parser.add_argument("--low-r-subset-report", type=Path, default=DEFAULT_LOW_R_SUBSET_REPORT)
    parser.add_argument("--low-r-subset-walk-forward-report", type=Path, default=DEFAULT_LOW_R_SUBSET_WALK_FORWARD_REPORT)
    parser.add_argument("--tick-microstructure-report", type=Path, default=DEFAULT_TICK_MICROSTRUCTURE_REPORT)
    parser.add_argument("--output", type=Path, default=Path("reports/55wr_15r_objective_completion_audit_2026-05-02.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("reports/55wr_15r_objective_completion_audit_2026-05-02.md"))
    args = parser.parse_args(argv)

    paths = tuple(args.expanded_report or args.report or DEFAULT_EXPANDED_REPORTS)
    expanded_reports = [(path, load_json(path)) for path in paths]
    vol_report = (args.vol_report, load_json(args.vol_report)) if args.vol_report.exists() else None
    smc_report = (args.smc_report, load_json(args.smc_report)) if args.smc_report.exists() else None
    low_r_report = (args.low_r_report, load_json(args.low_r_report)) if args.low_r_report.exists() else None
    low_r_subset_report = (
        (args.low_r_subset_report, load_json(args.low_r_subset_report))
        if args.low_r_subset_report.exists()
        else None
    )
    low_r_subset_walk_forward_report = (
        (args.low_r_subset_walk_forward_report, load_json(args.low_r_subset_walk_forward_report))
        if args.low_r_subset_walk_forward_report.exists()
        else None
    )
    tick_microstructure_report = (
        (args.tick_microstructure_report, load_json(args.tick_microstructure_report))
        if args.tick_microstructure_report.exists()
        else None
    )
    payload = build_relaxed_audit(
        expanded_reports,
        vol_report=vol_report,
        smc_report=smc_report,
        low_r_report=low_r_report,
        low_r_subset_report=low_r_subset_report,
        low_r_subset_walk_forward_report=low_r_subset_walk_forward_report,
        tick_microstructure_report=tick_microstructure_report,
    )
    write_json(args.output, payload)
    args.markdown_output.write_text(render_markdown(payload), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "achieved": payload["decision"]["achieved"]}, indent=2))
    return 0


def build_relaxed_audit(
    reports: Sequence[tuple[Path, dict[str, Any]]],
    *,
    vol_report: tuple[Path, dict[str, Any]] | None = None,
    smc_report: tuple[Path, dict[str, Any]] | None = None,
    low_r_report: tuple[Path, dict[str, Any]] | None = None,
    low_r_subset_report: tuple[Path, dict[str, Any]] | None = None,
    low_r_subset_walk_forward_report: tuple[Path, dict[str, Any]] | None = None,
    tick_microstructure_report: tuple[Path, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    expanded_rows = [summarize_expanded_report(path, payload) for path, payload in reports]
    vol_row = summarize_vol_report(*vol_report) if vol_report else None
    smc_row = summarize_smc_report(*smc_report) if smc_report else None
    low_r_row = summarize_low_r_report(*low_r_report) if low_r_report else None
    low_r_subset_row = summarize_low_r_subset_report(*low_r_subset_report) if low_r_subset_report else None
    low_r_subset_walk_forward_row = (
        summarize_low_r_subset_walk_forward_report(*low_r_subset_walk_forward_report)
        if low_r_subset_walk_forward_report
        else None
    )
    tick_microstructure_row = (
        summarize_tick_microstructure_report(*tick_microstructure_report)
        if tick_microstructure_report
        else None
    )
    strategy_family_rows = expanded_rows + [
        row
        for row in (vol_row, smc_row, low_r_row, low_r_subset_row, low_r_subset_walk_forward_row, tick_microstructure_row)
        if row is not None
    ]
    passing = [row for row in strategy_family_rows if row["passed"]]
    best = max(expanded_rows, key=best_sort_key) if expanded_rows else None
    reward_evidence = [
        {"artifact": row["artifact"], "take_profit_r_grid": row["take_profit_r_grid"]}
        for row in expanded_rows
    ]
    if vol_row:
        reward_evidence.append({"artifact": vol_row["artifact"], "reward_gate": vol_row["reward_gate"]})
    if smc_row:
        reward_evidence.append({"artifact": smc_row["artifact"], "reward_gates": smc_row["reward_gates"]})
    if low_r_row:
        reward_evidence.append({"artifact": low_r_row["artifact"], "take_profit_scale": low_r_row["take_profit_scale"]})
    if low_r_subset_row:
        reward_evidence.append({"artifact": low_r_subset_row["artifact"], "forced_take_profit_r": low_r_subset_row["forced_take_profit_r"]})
    if low_r_subset_walk_forward_row:
        reward_evidence.append({"artifact": low_r_subset_walk_forward_row["artifact"], "forced_take_profit_r": low_r_subset_walk_forward_row["forced_take_profit_r"]})
    if tick_microstructure_row:
        reward_evidence.append({"artifact": tick_microstructure_row["artifact"], "min_take_profit_r": tick_microstructure_row["min_take_profit_r"]})
    requirements = [
        requirement(
            "reward_1_5r",
            "Strategy search must force a 1.5R or higher take-profit profile.",
            passed=bool(passing) and any(row["reward_gate_passed"] for row in passing),
            evidence=reward_evidence,
            gap="Expanded searches forced 1.5R, but no full strategy family passes 55%/1.5R gates.",
        ),
        requirement(
            "win_rate_55pct",
            "Out-of-sample walk-forward test years must each reach at least 55% win rate.",
            passed=bool(passing),
            evidence=[win_rate_evidence(row) for row in strategy_family_rows],
            gap="No relaxed strategy family passes the 55% win-rate gate.",
        ),
        requirement(
            "non_overfit_walk_forward",
            "Selection must be train-only and replayed on next unseen test year.",
            passed=bool(passing),
            evidence=[method_evidence(row) for row in strategy_family_rows],
            gap="The protocol is walk-forward, but no candidate survives all gates.",
        ),
        requirement(
            "long_term_profitability",
            "Relaxed strategy must be profitable across all available out-of-sample years.",
            passed=bool(passing),
            evidence=[profitability_evidence(row) for row in strategy_family_rows],
            gap="Relaxed runs still have negative OOS years, no qualifying rows, or negative full-history PnL.",
        ),
        requirement(
            "live_ready",
            "Relaxed candidate must be eligible for execution/tick validation and promotion.",
            passed=False,
            evidence=[{"artifact": row["artifact"], "passed": row["passed"], "decision": row.get("decision")} for row in strategy_family_rows],
            gap="No relaxed candidate passes the objective gates, so there is no candidate to promote to quote replay/live readiness.",
        ),
    ]
    achieved = all(row["passed"] for row in requirements)
    return {
        "artifact": "55wr_15r_objective_completion_audit",
        "schema_version": 1,
        "objective": "Fallback objective: find a 55%+ win-rate, 1.5R+, non-overfit, long-term profitable, live-ready strategy if the 70%/2R target cannot be found.",
        "report_hash": stable_hash(strategy_family_rows),
        "expanded_reports": expanded_rows,
        "vol_report": vol_row,
        "smc_report": smc_row,
        "low_r_report": low_r_row,
        "low_r_subset_report": low_r_subset_row,
        "low_r_subset_walk_forward_report": low_r_subset_walk_forward_row,
        "tick_microstructure_report": tick_microstructure_row,
        "best_expanded_report": best,
        "strategy_family_reports": strategy_family_rows,
        "requirements": requirements,
        "decision": {
            "achieved": achieved,
            "reason": None if achieved else "No relaxed 55% win-rate / 1.5R walk-forward candidate passes all required gates.",
            "failed_requirements": [row["id"] for row in requirements if not row["passed"]],
        },
    }


def summarize_expanded_report(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary", {})
    method = payload.get("method", {})
    take_profit_grid = [float(value) for value in method.get("take_profit_r_grid", [])]
    decision = summary.get("decision", {})
    passed = bool(decision.get("passed"))
    return {
        "family": "expanded_high_edge",
        "artifact": str(path),
        "selection_profile": method.get("selection_profile"),
        "take_profit_r_grid": take_profit_grid,
        "uses_1_5r_only": take_profit_grid == [1.5],
        "reward_gate_passed": take_profit_grid == [1.5],
        "min_train_win_rate": method.get("min_train_win_rate"),
        "min_test_win_rate": method.get("min_test_win_rate"),
        "method": {
            "selection": method.get("selection"),
            "folds": method.get("folds"),
            "selection_profile": method.get("selection_profile"),
        },
        "decision": decision,
        "passed": passed,
        "oos_total_net_pnl": summary.get("oos_total_net_pnl"),
        "oos_total_trades": summary.get("oos_total_trades"),
        "oos_min_year_win_rate": summary.get("oos_min_year_win_rate"),
        "oos_min_year_pnl": summary.get("oos_min_year_pnl"),
    }


def summarize_vol_report(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    gates = {row["name"]: row for row in payload.get("gates", [])}
    reward_gate = gates.get("vol_strategy_reward_profile_ge_2r", {})
    win_gate = gates.get("vol_prescreen_has_70pct_win_rate", {})
    decision = payload.get("decision", {})
    return {
        "family": "vol_execution",
        "artifact": str(path),
        "passed": bool(decision.get("passed")),
        "decision": decision,
        "reward_gate": reward_gate,
        "reward_gate_passed": bool(reward_gate.get("passed")),
        "win_rate_gate": win_gate,
        "win_rate_gate_passed": bool(win_gate.get("passed")),
        "method": {"selection": "VOL prescreen plus reward-profile audit."},
        "oos_total_net_pnl": None,
        "oos_total_trades": None,
        "oos_min_year_win_rate": None,
    }


def summarize_smc_report(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    gates = {row["name"]: row for row in payload.get("objective_gates", [])}
    relevant_names = (
        "full_history_win_rate_ge_target",
        "full_history_net_r_p75_ge_target",
        "walk_forward_test_win_rate_ge_target",
        "walk_forward_test_net_r_p75_ge_target",
        "final_holdout_win_rate_ge_target",
        "final_holdout_net_r_p75_ge_target",
    )
    relevant_gates = [gates[name] for name in relevant_names if name in gates]
    reward_gates = [
        gates[name]
        for name in ("full_history_net_r_p75_ge_target", "walk_forward_test_net_r_p75_ge_target", "final_holdout_net_r_p75_ge_target")
        if name in gates
    ]
    win_gates = [
        gates[name]
        for name in ("full_history_win_rate_ge_target", "walk_forward_test_win_rate_ge_target", "final_holdout_win_rate_ge_target")
        if name in gates
    ]
    passed = bool(relevant_gates) and all(row.get("passed") for row in relevant_gates)
    return {
        "family": "smc_lqem_ce",
        "artifact": str(path),
        "passed": passed,
        "decision": {"passed": passed, "failed_gates": [row["name"] for row in relevant_gates if not row.get("passed")]},
        "reward_gates": reward_gates,
        "reward_gate_passed": bool(reward_gates) and all(row.get("passed") for row in reward_gates),
        "win_rate_gates": win_gates,
        "win_rate_gate_passed": bool(win_gates) and all(row.get("passed") for row in win_gates),
        "method": {"selection": "SMC validation objective gates with relaxed target thresholds."},
        "oos_total_net_pnl": payload.get("full_history", {}).get("metrics", {}).get("net_pnl"),
        "oos_total_trades": payload.get("full_history", {}).get("metrics", {}).get("trade_count"),
        "oos_min_year_win_rate": None,
    }


def summarize_low_r_report(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    row = payload.get("best_full_or_annualized_years") or {}
    metrics = row.get("metrics", {})
    constraints = row.get("constraints", {})
    params = row.get("params", {})
    win_rate = metrics.get("win_rate")
    passed = (
        float(win_rate or 0.0) >= 0.55
        and bool(constraints.get("full_or_annualized_years_gt_min_trades"))
        and int(constraints.get("positive_years") or 0) == len(row.get("yearly_results", []))
    )
    return {
        "family": "low_r_high_frequency_probe",
        "artifact": str(path),
        "passed": passed,
        "decision": {
            "passed": passed,
            "reason": None if passed else "Full-sample low-R probe is below 55% win rate or lacks all-year profitability.",
            "win_rate": win_rate,
            "positive_years": constraints.get("positive_years"),
            "year_count": len(row.get("yearly_results", [])),
        },
        "reward_gate_passed": float(params.get("take_profit_scale") or 0.0) >= 2.0,
        "take_profit_scale": params.get("take_profit_scale"),
        "method": {"selection": "Full-sample low-R high-frequency parameter probe; not walk-forward-selected."},
        "oos_total_net_pnl": metrics.get("net_pnl"),
        "oos_total_trades": metrics.get("trade_count"),
        "oos_min_year_win_rate": win_rate,
    }


def summarize_low_r_subset_report(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    best = payload.get("best_exact") or {}
    metrics = best.get("metrics", {})
    constraints = best.get("constraints", {})
    decision = payload.get("decision", {})
    passed = bool(decision.get("passed"))
    return {
        "family": "low_r_15r_subset_search",
        "artifact": str(path),
        "passed": passed,
        "decision": {
            **decision,
            "best_win_rate": metrics.get("win_rate"),
            "best_positive_years": constraints.get("positive_years"),
            "best_edge_indexes": best.get("edge_indexes"),
        },
        "reward_gate_passed": float((payload.get("target") or {}).get("forced_take_profit_r") or 0.0) >= 1.5,
        "forced_take_profit_r": (payload.get("target") or {}).get("forced_take_profit_r"),
        "method": {"selection": "Approximate low-R subset prefilter plus exact replay of top subsets; not walk-forward-selected."},
        "oos_total_net_pnl": metrics.get("net_pnl"),
        "oos_total_trades": metrics.get("trade_count"),
        "oos_min_year_win_rate": metrics.get("win_rate"),
    }


def summarize_low_r_subset_walk_forward_report(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary", {})
    decision = summary.get("decision", {})
    target = payload.get("target") or {}
    return {
        "family": "low_r_15r_subset_walk_forward",
        "artifact": str(path),
        "passed": bool(decision.get("passed")),
        "decision": {
            **decision,
            "oos_win_rate": summary.get("oos_win_rate"),
            "oos_min_year_win_rate": summary.get("oos_min_year_win_rate"),
            "positive_test_years": summary.get("positive_test_years"),
            "ok_fold_count": summary.get("ok_fold_count"),
        },
        "reward_gate_passed": float(target.get("forced_take_profit_r") or 0.0) >= 1.5,
        "forced_take_profit_r": target.get("forced_take_profit_r"),
        "method": {
            "selection": "Train-only low-R subset selection per fold, exact-replayed on the next unseen test year.",
            "folds": (payload.get("method") or {}).get("folds"),
        },
        "oos_total_net_pnl": summary.get("oos_total_net_pnl"),
        "oos_total_trades": summary.get("oos_total_trades"),
        "oos_min_year_win_rate": summary.get("oos_min_year_win_rate"),
    }


def summarize_tick_microstructure_report(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    target = payload.get("target") or {}
    coverage = payload.get("coverage") or {}
    selected = payload.get("selected_rule") or {}
    decision = payload.get("decision") or {}
    test = selected.get("test") or {}
    passed = bool(decision.get("passed")) and bool(decision.get("live_ready"))
    return {
        "family": "tick_microstructure_filter",
        "artifact": str(path),
        "passed": passed,
        "decision": {
            **decision,
            "objective_passed_but_not_live_ready": bool(decision.get("passed")) and not bool(decision.get("live_ready")),
            "selected_rule": selected.get("name"),
            "selected_test_win_rate": test.get("win_rate"),
            "selected_test_trade_count": test.get("trade_count"),
            "eligible_trade_count": coverage.get("eligible_trade_count"),
        },
        "reward_gate_passed": float(target.get("min_take_profit_r") or 0.0) >= 1.5,
        "min_take_profit_r": target.get("min_take_profit_r"),
        "method": {
            "selection": (payload.get("method") or {}).get("selection"),
            "scope": (payload.get("method") or {}).get("scope"),
            "non_overfit_guardrail": (payload.get("method") or {}).get("non_overfit_guardrail"),
        },
        "oos_total_net_pnl": test.get("net_pnl"),
        "oos_total_trades": test.get("trade_count"),
        "oos_min_year_win_rate": test.get("win_rate"),
    }


def win_rate_evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact": row["artifact"],
        "family": row["family"],
        "decision": row["decision"],
        "oos_min_year_win_rate": row.get("oos_min_year_win_rate"),
    }


def method_evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact": row["artifact"],
        "family": row["family"],
        "method": row.get("method", {}),
        "decision": row["decision"],
    }


def profitability_evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact": row["artifact"],
        "family": row["family"],
        "oos_total_net_pnl": row.get("oos_total_net_pnl"),
        "failed_positive_years": row.get("decision", {}).get("failed_positive_years", []),
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


def best_sort_key(row: dict[str, Any]) -> tuple[float, ...]:
    decision = row["decision"]
    return (
        1.0 if decision.get("passed") else 0.0,
        float(decision.get("win_rate_gate_years") or 0),
        float(decision.get("positive_test_years") or 0),
        float(row.get("oos_total_net_pnl") or 0),
        float(row.get("oos_min_year_win_rate") or 0),
    )


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 55% Win-Rate / 1.5R Objective Completion Audit",
        "",
        f"- Achieved: `{payload['decision']['achieved']}`",
        f"- Reason: {payload['decision']['reason'] or 'All requirements passed.'}",
        "",
        "## Reports",
        "",
    ]
    for row in payload["strategy_family_reports"]:
        lines.append(
            f"- {row['artifact']}: family={row['family']}, passed={row['decision'].get('passed')}, "
            f"profile={row.get('selection_profile')}, oos_net={row['oos_total_net_pnl']}, "
            f"min_win_rate={row['oos_min_year_win_rate']}"
        )
    lines.extend(["", "## Checklist", ""])
    for row in payload["requirements"]:
        status = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- {row['id']}: {status} - {row['requirement']}")
        if row["gap"]:
            lines.append(f"  Gap: {row['gap']}")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
