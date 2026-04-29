from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .research_config import (
    PRIMARY_COST_MODEL,
    PRIMARY_RESEARCH_SYMBOL,
    PRIMARY_RESEARCH_TIMEFRAME,
    PRIMARY_TOP_STRATEGY_COMPARISON_OBJECTIVES,
    PRIMARY_TOP_STRATEGY_OBJECTIVE,
)
from .variants import stable_hash


def build_research_ledger(
    *,
    result: Any,
    persisted_payload: dict[str, Any],
    quote_reports: Sequence[Path] = (),
    paper_reports: Sequence[Path] = (),
    execution_validation_required: bool,
) -> dict[str, Any]:
    spec = result.strategy_spec
    execution_evidence = persisted_payload.get("execution_evidence", {})
    execution_validation = persisted_payload.get("execution_validation", {})
    validation_plan = result.validation_plan.to_dict()
    pre_screen_report = result.pre_screen_report or {}
    final_holdout_policy = result.final_holdout_policy
    ledger = {
        "schema_version": 1,
        "artifact": "research_ledger",
        "experiment_id": result.experiment_id,
        "strategy_name": result.strategy_name,
        "strategy_spec_hash": result.strategy_spec_hash,
        "prompt_hash": result.prompt_hash,
        "primary_research_policy": {
            "symbol": PRIMARY_RESEARCH_SYMBOL,
            "timeframe": PRIMARY_RESEARCH_TIMEFRAME,
            "cost_model": PRIMARY_COST_MODEL,
            "top_strategy_objective": PRIMARY_TOP_STRATEGY_OBJECTIVE,
            "top_strategy_comparison_objectives": list(PRIMARY_TOP_STRATEGY_COMPARISON_OBJECTIVES),
        },
        "run_context": {
            "symbol": spec.get("symbol"),
            "timeframe": spec.get("timeframe"),
            "strategy_family": spec.get("strategy_family"),
            "execution_mode": result.execution_mode,
            "cost_model": result.cost_model,
            "cost_model_name": result.cost_model.get("name") or result.snapshot.get("cost_model"),
            "data_version_hash": result.data_version_hash,
            "final_holdout_data_version_hash": result.final_holdout_data_version_hash,
        },
        "search_actions": {
            "family": spec.get("strategy_family"),
            "timeframe": spec.get("timeframe"),
            "symbol": spec.get("symbol"),
            "ranking_objective": PRIMARY_TOP_STRATEGY_OBJECTIVE,
            "report_objective": PRIMARY_TOP_STRATEGY_OBJECTIVE,
            "comparison_objectives": list(PRIMARY_TOP_STRATEGY_COMPARISON_OBJECTIVES),
            "cost_model": result.cost_model.get("name") or result.snapshot.get("cost_model"),
            "parameter_grid_hash": result.parameter_grid_hash,
            "parameter_combination_count": result.parameter_combination_count,
            "trial_count": result.trial_count,
            "variant_parameters": result.variant_parameters,
            "pre_screen_thresholds": pre_screen_report.get("thresholds", {}),
            "split_years": split_years(validation_plan),
            "final_holdout_inputs_excluded": True,
            "final_holdout_excluded_from": final_holdout_policy.get("decision_input_policy", {}).get(
                "final_holdout_excluded_from",
                [],
            ),
        },
        "validation_policy": {
            "validation_plan": validation_plan,
            "split_years": split_years(validation_plan),
            "split_boundaries": final_holdout_policy.get("split_boundaries", {}),
            "final_holdout_policy": final_holdout_policy,
            "final_holdout_evaluated": bool(result.final_holdout_data_version_hash),
            "final_holdout_evaluation_phase": final_holdout_policy.get("final_holdout_evaluation_phase"),
            "pre_screen_stage": pre_screen_report.get("stage"),
            "pre_screen_hard_gate_enforced": pre_screen_report.get("hard_gate_enforced", False),
            "pre_screen_thresholds": pre_screen_report.get("thresholds", {}),
            "overlapping_test_folds": result.overlapping_test_folds,
            "non_overlap_test_fold_indexes": result.non_overlap_test_fold_indexes,
        },
        "promotion_policy": {
            "candidate_stage": persisted_payload.get("candidate_stage"),
            "gates": result.gates,
            "hard_gate_report": result.hard_gate_report,
            "promotion_report": result.promotion_report,
            "execution_validation": execution_validation,
            "execution_evidence_status": execution_evidence.get("status"),
            "execution_evidence_missing_requirements": execution_evidence.get("missing_requirements", []),
            "freeze_requires_execution_evidence": execution_validation_required,
            "promotion_uses_final_holdout": final_holdout_policy.get("promotion_uses_final_holdout", False),
            "freeze_gate": final_holdout_policy.get("freeze_gate", {}),
        },
        "decision_audit": {
            "generation_uses_final_holdout": final_holdout_policy.get("generation_uses_final_holdout", False),
            "parameter_selection_uses_final_holdout": final_holdout_policy.get(
                "parameter_selection_uses_final_holdout",
                False,
            ),
            "promotion_uses_final_holdout": final_holdout_policy.get("promotion_uses_final_holdout", False),
            "candidate_ranking_uses_final_holdout": final_holdout_policy.get(
                "candidate_ranking_uses_final_holdout",
                False,
            ),
            "ranking_objective_switching_uses_final_holdout": final_holdout_policy.get(
                "ranking_objective_switching_uses_final_holdout",
                False,
            ),
            "report_iteration_uses_final_holdout": False,
            "final_holdout_evaluated": bool(result.final_holdout_data_version_hash),
            "final_holdout_evaluation_phase": final_holdout_policy.get("final_holdout_evaluation_phase"),
        },
        "evidence_inputs": {
            "quote_reports": [str(path) for path in quote_reports],
            "paper_reports": [str(path) for path in paper_reports],
            "persisted_quote_reports": execution_evidence.get("quote_reports", []),
            "persisted_paper_reports": execution_evidence.get("paper_reports", []),
        },
        "anti_overfit_inputs": {
            "overfitting_report": result.overfitting_report,
            "cost_sensitivity_report": result.cost_sensitivity_report,
            "parameter_stability_report": result.parameter_stability_report,
            "signal_similarity_report": result.signal_similarity_report,
            "validation_to_test_sharpe_decay": result.validation_to_test_sharpe_decay,
            "test_to_holdout_sharpe_decay": result.test_to_holdout_sharpe_decay,
        },
        "reproducibility": {
            "snapshot": result.snapshot,
            "cost_model_hash": result.snapshot.get("cost_model_hash"),
            "fold_definition_hash": result.snapshot.get("fold_definition_hash"),
            "random_seed": result.snapshot.get("random_seed"),
        },
    }
    ledger["ledger_hash"] = stable_hash(ledger)
    return ledger


def split_years(validation_plan: dict[str, Any]) -> dict[str, list[int]]:
    years: dict[str, set[int]] = {
        "train": set(),
        "validation": set(),
        "test": set(),
        "final_holdout": set(),
    }
    for fold in validation_plan.get("folds", []):
        for split in ("train", "validation", "test"):
            years[split].update(_range_years((fold.get(split) or {})))
    years["final_holdout"].update(_range_years(validation_plan.get("final_holdout") or {}))
    return {split: sorted(values) for split, values in years.items()}


def _range_years(date_range: dict[str, Any]) -> set[int]:
    start = str(date_range.get("start") or "")
    end = str(date_range.get("end") or "")
    if len(start) < 4 or len(end) < 4:
        return set()
    start_year = int(start[:4])
    end_year = int(end[:4])
    return set(range(start_year, end_year + 1))
