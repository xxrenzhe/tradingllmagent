from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .config import CostModelConfig
from .variants import stable_hash
from .validation import ValidationPlan


def code_version(repo_root: Path | None = None) -> str:
    root = repo_root or Path.cwd()
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def config_snapshot(config_dir: Path) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for name in ["symbols.yaml", "costs.yaml", "llm.yaml"]:
        path = config_dir / name
        if path.exists():
            snapshot[name] = path.read_text(encoding="utf-8")
    return snapshot


def config_snapshot_hash(config_dir: Path) -> str:
    return stable_hash(config_snapshot(config_dir))


def fold_definition_hash(plan: ValidationPlan) -> str:
    return stable_hash(plan.to_dict())


def cost_model_hash(cost_model: CostModelConfig) -> str:
    return stable_hash(cost_model.to_dict())


def research_snapshot(
    plan: ValidationPlan,
    cost_model: CostModelConfig,
    config_dir: Path = Path("configs"),
    random_seed: int = 0,
    repo_root: Path | None = None,
    experiment_id: str | None = None,
    strategy_spec_hash: str | None = None,
    prompt_hash: str | None = None,
    data_version_hash: str | None = None,
    llm_model: str = "local-deterministic-template",
    llm_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config_snapshot(config_dir)
    return {
        "experiment_id": experiment_id,
        "strategy_spec_hash": strategy_spec_hash,
        "prompt_hash": prompt_hash,
        "llm_model": llm_model,
        "llm_parameters": llm_parameters or {},
        "data_version_hash": data_version_hash,
        "code_version": code_version(repo_root),
        "config_snapshot": config,
        "config_snapshot_hash": stable_hash(config),
        "cost_model_hash": cost_model_hash(cost_model),
        "fold_definition_hash": fold_definition_hash(plan),
        "random_seed": random_seed,
    }
