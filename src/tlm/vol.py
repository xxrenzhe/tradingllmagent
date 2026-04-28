from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .config import SymbolConfig
from .feature_catalog import feature_readiness_report
from .research import load_leaderboard_report
from .storage import write_json
from .strategy_generation import VOL_EXECUTION_AWARE_FAMILIES, vol_strategy_generation_manifest


VOL_ARTIFACT_FILENAMES = (
    "vol_feature_readiness.json",
    "vol_strategy_leaderboard.json",
    "vol_cost_stress_report.json",
    "vol_quote_replay_report.json",
    "vol_paper_shadow_review.json",
    "vol_mutation_memory.json",
)

VOL_REQUIRED_FEATURES = (
    "bar_volume",
    "volume_ma_5",
    "volume_ma_20",
    "relative_volume_5",
    "relative_volume_20",
    "volume_percentile_session",
    "volume_spike_flag",
    "multi_timeframe_volume_confirm",
    "volume_price_confirm",
    "volume_absorption_flag",
    "low_volume_filter",
)


def build_vol_feature_readiness() -> dict[str, Any]:
    readiness = feature_readiness_report()
    executable = set(readiness.get("executable_features", []))
    missing = [name for name in VOL_REQUIRED_FEATURES if name not in executable]
    return {
        "schema_version": 1,
        "artifact": "vol_feature_readiness",
        "status": "ready" if not missing else "blocked",
        "bar_volume_semantics": {
            "canonical_name": "bar_volume",
            "databento_ohlcv_mapping": "tick_count",
            "runtime_aliases": ["volume", "volume_1m", "tick_count_1m"],
        },
        "required_features": list(VOL_REQUIRED_FEATURES),
        "missing_features": missing,
        "feature_readiness": readiness,
        "execution_data_gap": {
            "ohlcv_has_real_spread": False,
            "required_for_quote_validation": ["TBBO"],
            "required_for_limit_fill_validation": ["MBP-1"],
        },
    }


def build_vol_strategy_leaderboard(experiments_root: Path, specs: Sequence[dict[str, Any]] | None = None) -> dict[str, Any]:
    report = load_leaderboard_report(experiments_root)
    rows = [_is_vol_row(row) for row in report.get("rows", [])]
    rows = [row for row in rows if row is not None]
    candidate_rows = [_is_vol_row(row) for row in report.get("candidate_leaderboard", [])]
    candidate_rows = [row for row in candidate_rows if row is not None]
    freeze_rows = [_is_vol_row(row) for row in report.get("freeze_confirmed_leaderboard", [])]
    freeze_rows = [row for row in freeze_rows if row is not None]
    manifest = vol_strategy_generation_manifest(list(specs or [])) if specs is not None else None
    return {
        "schema_version": 1,
        "artifact": "vol_strategy_leaderboard",
        "experiments_root": str(experiments_root),
        "target": {
            "min_annual_trades": 1000,
            "min_sharpe": 2,
            "min_win_probability": 0.53,
            "requires_quote_replay": True,
            "requires_paper_shadow": True,
        },
        "summary": {
            "total_vol_rows": len(rows),
            "candidate_vol_rows": len(candidate_rows),
            "freeze_confirmed_vol_rows": len(freeze_rows),
            "generated_seed_count": len(specs or []),
        },
        "seed_manifest": manifest,
        "candidate_leaderboard": candidate_rows,
        "freeze_confirmed_leaderboard": freeze_rows,
        "rejected": [_is_vol_row(row) for row in report.get("rejected", []) if _is_vol_row(row) is not None],
    }


def build_vol_cost_stress_report(
    leaderboard_report: dict[str, Any],
    symbol_config: SymbolConfig,
    extra_round_trip_ticks: Sequence[int] = (0, 1, 2, 4),
) -> dict[str, Any]:
    rows = list(leaderboard_report.get("candidate_leaderboard", []))
    tick_value = symbol_config.tick_size * symbol_config.point_value
    stressed = []
    for row in rows:
        annual_trades = float(row.get("annual_trades_test") or 0.0)
        net_pnl = float(row.get("net_pnl_test") or 0.0)
        stress_rows = []
        for ticks in extra_round_trip_ticks:
            annual_cost_drag = annual_trades * ticks * tick_value
            stress_rows.append(
                {
                    "extra_round_trip_ticks": ticks,
                    "extra_cost_per_trade_usd": ticks * tick_value,
                    "annual_cost_drag_usd": annual_cost_drag,
                    "stressed_net_pnl_proxy": net_pnl - annual_cost_drag,
                    "survives_net_pnl_proxy": net_pnl - annual_cost_drag > 0,
                }
            )
        stressed.append(
            {
                "experiment_id": row.get("experiment_id"),
                "strategy_name": row.get("strategy_name"),
                "strategy_family": row.get("strategy_card", {}).get("strategy_family"),
                "annual_trades_test": annual_trades,
                "net_pnl_test": net_pnl,
                "sharpe_test": row.get("sharpe_test"),
                "stress": stress_rows,
            }
        )
    return {
        "schema_version": 1,
        "artifact": "vol_cost_stress_report",
        "symbol": symbol_config.alias,
        "tick_size": symbol_config.tick_size,
        "point_value": symbol_config.point_value,
        "tick_value_usd": tick_value,
        "assumption": "Proxy stress subtracts extra round-trip ticks from annualized test PnL; quote replay is required before promotion.",
        "candidate_count": len(rows),
        "stressed_candidates": stressed,
    }


def build_vol_quote_replay_report(
    *,
    quote_files: Sequence[Path],
    existing_reports: Sequence[Path] | None = None,
) -> dict[str, Any]:
    existing_quote_files = [str(path) for path in quote_files if path.exists()]
    replay_reports = [str(path) for path in (existing_reports or []) if path.exists()]
    return {
        "schema_version": 1,
        "artifact": "vol_quote_replay_report",
        "status": "ready_for_review" if replay_reports else "blocked",
        "quote_files": existing_quote_files,
        "quote_replay_reports": replay_reports,
        "missing_requirements": [] if replay_reports else ["quote_replay_report"],
        "required_checks": [
            "market_order_bid_ask_replay",
            "spread_distribution",
            "limit_fill_rate",
            "missed_fill_opportunity_cost",
            "adverse_selection_1m_3m_5m_15m",
        ],
    }


def build_vol_paper_shadow_review(paper_reports: Sequence[Path] | None = None) -> dict[str, Any]:
    reports = [str(path) for path in (paper_reports or []) if path.exists()]
    return {
        "schema_version": 1,
        "artifact": "vol_paper_shadow_review",
        "status": "ready_for_review" if reports else "blocked",
        "paper_reports": reports,
        "missing_requirements": [] if reports else ["paper_shadow_trade_log"],
        "required_fields": [
            "signal_id",
            "expected_edge",
            "actual_fill_mode",
            "spread_ticks",
            "adverse_selection_ticks_5m",
            "regime",
            "llm_diagnosis",
            "allowed_mutations",
            "blocked_mutations",
        ],
    }


def build_vol_mutation_memory(leaderboard_report: dict[str, Any]) -> dict[str, Any]:
    rows = list(leaderboard_report.get("candidate_leaderboard", [])) + list(leaderboard_report.get("rejected", []))
    records = []
    for row in rows:
        records.append(
            {
                "experiment_id": row.get("experiment_id"),
                "strategy_name": row.get("strategy_name"),
                "strategy_spec_hash": row.get("strategy_spec_hash"),
                "allowed_mutations": _allowed_mutations(row),
                "blocked_mutations": [
                    "read_final_holdout_before_freeze",
                    "remove_cost_model",
                    "promote_without_quote_replay",
                    "promote_without_paper_shadow",
                ],
                "diagnostic_inputs": {
                    "reasons": row.get("reasons", []),
                    "next_round_suggestions": row.get("next_round_suggestions", []),
                    "cost_sensitivity_report": row.get("cost_sensitivity_report", {}),
                },
            }
        )
    return {
        "schema_version": 1,
        "artifact": "vol_mutation_memory",
        "record_count": len(records),
        "records": records,
    }


def write_vol_research_artifacts(
    output_dir: Path,
    *,
    experiments_root: Path,
    symbol_config: SymbolConfig,
    specs: Sequence[dict[str, Any]] | None = None,
    quote_files: Sequence[Path] = (),
    quote_reports: Sequence[Path] = (),
    paper_reports: Sequence[Path] = (),
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    feature_readiness = build_vol_feature_readiness()
    leaderboard = build_vol_strategy_leaderboard(experiments_root, specs=specs)
    artifacts = {
        "vol_feature_readiness.json": feature_readiness,
        "vol_strategy_leaderboard.json": leaderboard,
        "vol_cost_stress_report.json": build_vol_cost_stress_report(leaderboard, symbol_config),
        "vol_quote_replay_report.json": build_vol_quote_replay_report(
            quote_files=quote_files,
            existing_reports=quote_reports,
        ),
        "vol_paper_shadow_review.json": build_vol_paper_shadow_review(paper_reports),
        "vol_mutation_memory.json": build_vol_mutation_memory(leaderboard),
    }
    paths = {}
    for filename, payload in artifacts.items():
        path = output_dir / filename
        write_json(path, payload)
        paths[filename] = str(path)
    return paths


def _is_vol_row(row: dict[str, Any]) -> dict[str, Any] | None:
    family = row.get("strategy_card", {}).get("strategy_family") or row.get("strategy_family")
    name = str(row.get("strategy_name", ""))
    if family in VOL_EXECUTION_AWARE_FAMILIES or name.startswith("nq_vol_execution_"):
        return row
    return None


def _allowed_mutations(row: dict[str, Any]) -> list[str]:
    reasons = set(row.get("reasons", []))
    suggestions = {str(item).lower() for item in row.get("next_round_suggestions", [])}
    mutations = ["adjust_volume_threshold", "tighten_time_window", "stress_extra_slippage"]
    if "annual_trades_test" in reasons:
        mutations.append("relax_entry_frequency_without_adding_complex_features")
    if "avg_trade_net_pnl" in reasons or any("cost" in item for item in suggestions):
        mutations.append("raise_min_expected_edge")
    if "sharpe_test_aggregate" in reasons:
        mutations.append("add_regime_filter")
    return mutations
