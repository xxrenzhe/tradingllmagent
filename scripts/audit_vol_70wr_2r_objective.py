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
from tlm.strategy import StrategySpecError, load_strategy_spec
from tlm.variants import stable_hash


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit VOL strategies against 70% win-rate, 2R, and full tick-window gates.")
    parser.add_argument("--leaderboard", type=Path, default=Path("experiments/vol_execution_artifacts/vol_strategy_leaderboard.json"))
    parser.add_argument("--strategies-root", type=Path, default=Path("strategies"))
    parser.add_argument("--tick-coverage", type=Path, default=Path("reports/nq_expanded_high_edge_mbp1_quote_coverage_audit_2026-05-02.json"))
    parser.add_argument("--quote-replay", type=Path, default=Path("experiments/vol_execution_artifacts/vol_quote_replay_report.json"))
    parser.add_argument("--paper-shadow", type=Path, default=Path("experiments/vol_execution_artifacts/vol_paper_shadow_review.json"))
    parser.add_argument("--min-win-rate", type=float, default=0.70)
    parser.add_argument("--min-reward-r", type=float, default=2.0)
    parser.add_argument("--output", type=Path, default=Path("reports/nq_vol_execution_70wr_2r_objective_audit_2026-05-02.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("reports/nq_vol_execution_70wr_2r_objective_audit_2026-05-02.md"))
    args = parser.parse_args(argv)

    payload = build_vol_objective_audit(
        leaderboard=load_json(args.leaderboard),
        strategy_paths=sorted(args.strategies_root.glob("nq_vol_execution_*.yaml")),
        tick_coverage=load_json(args.tick_coverage),
        quote_replay=load_json(args.quote_replay) if args.quote_replay.exists() else {},
        paper_shadow=load_json(args.paper_shadow) if args.paper_shadow.exists() else {},
        min_win_rate=args.min_win_rate,
        min_reward_r=args.min_reward_r,
        evidence_paths={
            "leaderboard": args.leaderboard,
            "strategies_root": args.strategies_root,
            "tick_coverage": args.tick_coverage,
            "quote_replay": args.quote_replay,
            "paper_shadow": args.paper_shadow,
        },
    )
    write_json(args.output, payload)
    args.markdown_output.write_text(render_markdown(payload), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "passed": payload["decision"]["passed"]}, indent=2))
    return 0


def build_vol_objective_audit(
    *,
    leaderboard: dict[str, Any],
    strategy_paths: Sequence[Path],
    tick_coverage: dict[str, Any],
    quote_replay: dict[str, Any],
    paper_shadow: dict[str, Any],
    min_win_rate: float,
    min_reward_r: float,
    evidence_paths: dict[str, Path],
) -> dict[str, Any]:
    rows = list(leaderboard.get("rows") or [])
    strategy_cards = [strategy_reward_card(path, min_reward_r) for path in strategy_paths]
    leaderboard_candidates = [
        row
        for row in rows
        if row.get("win_probability_test") is not None
        and float(row["win_probability_test"]) >= min_win_rate
        and float(row.get("net_pnl_test") or 0.0) > 0
    ]
    two_r_strategies = [row for row in strategy_cards if row["min_reward_r"] is not None and row["min_reward_r"] >= min_reward_r]
    final_target_rows = list(leaderboard.get("final_target_leaderboard") or [])

    gates = [
        gate(
            "vol_prescreen_has_70pct_win_rate",
            bool(leaderboard_candidates),
            {
                "min_required_win_rate": min_win_rate,
                "strategy_count": len(rows),
                "passing_strategy_count": len(leaderboard_candidates),
                "best_win_probability": max((float(row.get("win_probability_test") or 0.0) for row in rows), default=None),
                "best_rows": compact_rows(leaderboard_candidates[:10]),
            },
        ),
        gate(
            "vol_strategy_reward_profile_ge_2r",
            bool(two_r_strategies),
            {
                "min_required_reward_r": min_reward_r,
                "strategy_file_count": len(strategy_cards),
                "two_r_strategy_count": len(two_r_strategies),
                "min_reward_r_by_strategy": [
                    {
                        "strategy_name": row["strategy_name"],
                        "path": row["path"],
                        "min_reward_r": row["min_reward_r"],
                        "fixed_exit_r": row["fixed_exit_r"],
                        "grammar_exit_r": row["grammar_exit_r"],
                    }
                    for row in strategy_cards
                ],
            },
        ),
        gate(
            "vol_final_target_rows_exist",
            bool(final_target_rows),
            {
                "target_in_report": leaderboard.get("target"),
                "final_target_row_count": len(final_target_rows),
                "summary": leaderboard.get("summary"),
            },
        ),
        gate(
            "full_recent_tick_window_analyzed",
            bool((tick_coverage.get("decision") or {}).get("passed")),
            {
                "tick_coverage_artifact": str(evidence_paths["tick_coverage"]),
                "date_from": tick_coverage.get("date_from"),
                "date_to": tick_coverage.get("date_to"),
                "expected_quote_days": tick_coverage.get("expected_quote_days"),
                "analyzed_quote_days": tick_coverage.get("analyzed_quote_days"),
                "validated_trade_count": tick_coverage.get("validated_trade_count"),
                "decision": tick_coverage.get("decision"),
            },
        ),
        gate(
            "vol_quote_replay_ready",
            quote_replay.get("status") == "ready_for_review",
            {
                "quote_replay_artifact": str(evidence_paths["quote_replay"]),
                "status": quote_replay.get("status"),
                "missing_requirements": quote_replay.get("missing_requirements", []),
            },
        ),
        gate(
            "vol_paper_shadow_ready",
            paper_shadow.get("status") == "ready_for_review",
            {
                "paper_shadow_artifact": str(evidence_paths["paper_shadow"]),
                "status": paper_shadow.get("status"),
                "missing_requirements": paper_shadow.get("missing_requirements", []),
                "summary": paper_shadow.get("summary", {}),
            },
        ),
    ]
    passed = all(row["passed"] for row in gates)
    return {
        "artifact": "vol_execution_70wr_2r_objective_audit",
        "schema_version": 1,
        "objective": "Audit generated VOL execution-aware strategies against 70% win-rate, 2R reward, and full recent tick-window evidence.",
        "evidence": {key: str(path) for key, path in evidence_paths.items()},
        "leaderboard_hash": stable_hash(leaderboard),
        "strategy_spec_hash": stable_hash(strategy_cards),
        "target": {
            "min_win_rate": min_win_rate,
            "min_reward_r": min_reward_r,
            "requires_full_recent_tick_window": True,
            "requires_quote_replay": True,
            "requires_paper_shadow": True,
        },
        "summary": {
            "leaderboard_rows": len(rows),
            "strategy_file_count": len(strategy_cards),
            "leaderboard_70pct_win_rate_rows": len(leaderboard_candidates),
            "strategy_files_with_2r_reward": len(two_r_strategies),
            "final_target_rows": len(final_target_rows),
            "tick_window": {
                "date_from": tick_coverage.get("date_from"),
                "date_to": tick_coverage.get("date_to"),
                "expected_quote_days": tick_coverage.get("expected_quote_days"),
                "analyzed_quote_days": tick_coverage.get("analyzed_quote_days"),
            },
        },
        "gates": gates,
        "decision": {
            "passed": passed,
            "reason": None if passed else "VOL family does not satisfy every 70% win-rate, 2R, execution-evidence gate.",
            "failed_gates": [row["name"] for row in gates if not row["passed"]],
        },
    }


def strategy_reward_card(path: Path, min_reward_r: float) -> dict[str, Any]:
    try:
        spec = load_strategy_spec(path)
    except StrategySpecError as error:
        return {
            "path": str(path),
            "strategy_name": path.stem,
            "status": "invalid",
            "error": str(error),
            "fixed_exit_r": None,
            "grammar_exit_r": None,
            "min_reward_r": None,
            "meets_2r": False,
        }
    raw = spec.raw
    fixed_exit_r = reward_r_from_exit(raw.get("exit", {}), "stop_loss", "take_profit")
    grammar_exit_r = reward_r_from_exit((raw.get("signal_grammar") or {}).get("exit", {}), "stop", "take_profit")
    reward_values = [value for value in (fixed_exit_r, grammar_exit_r) if value is not None]
    min_observed = min(reward_values) if reward_values else None
    return {
        "path": str(path),
        "strategy_name": spec.name,
        "status": "ready",
        "strategy_family": spec.strategy_family,
        "fixed_exit_r": fixed_exit_r,
        "grammar_exit_r": grammar_exit_r,
        "min_reward_r": min_observed,
        "meets_2r": min_observed is not None and min_observed >= min_reward_r,
    }


def reward_r_from_exit(exit_spec: dict[str, Any], stop_key: str, take_profit_key: str) -> float | None:
    stop = exit_spec.get(stop_key)
    take_profit = exit_spec.get(take_profit_key)
    if not isinstance(stop, dict) or not isinstance(take_profit, dict):
        return None
    stop_value = numeric_exit_value(stop)
    take_profit_value = numeric_exit_value(take_profit)
    if stop_value is None or take_profit_value is None or stop_value <= 0:
        return None
    return take_profit_value / stop_value


def numeric_exit_value(config: dict[str, Any]) -> float | None:
    value = config.get("value", config.get("multiple"))
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compact_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "strategy_name": row.get("strategy_name"),
            "strategy_family": row.get("strategy_family"),
            "trade_count": row.get("trade_count"),
            "net_pnl_test": row.get("net_pnl_test"),
            "win_probability_test": row.get("win_probability_test"),
            "final_target_passed": row.get("final_target_passed"),
        }
        for row in rows
    ]


def gate(name: str, passed: bool, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "evidence": evidence}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# VOL 70% Win-Rate / 2R Objective Audit",
        "",
        f"- Passed: `{payload['decision']['passed']}`",
        f"- Reason: {payload['decision']['reason'] or 'All gates passed.'}",
        f"- Tick window: {payload['summary']['tick_window']['date_from']} to {payload['summary']['tick_window']['date_to']}",
        f"- Tick quote days analyzed: {payload['summary']['tick_window']['analyzed_quote_days']} / {payload['summary']['tick_window']['expected_quote_days']}",
        "",
        "## Gates",
        "",
    ]
    for row in payload["gates"]:
        status = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- {row['name']}: {status}")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
