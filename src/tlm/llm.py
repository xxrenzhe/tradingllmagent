from __future__ import annotations

import copy
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .strategy import StrategySpec, parse_strategy_spec
from .variants import stable_hash


TRAIN_VALIDATION_ONLY_KEYS = {"fold", "train_metrics", "validation_metrics"}


@dataclass(frozen=True)
class LLMStrategyProposal:
    model: str
    prompt: str
    response: str
    prompt_hash: str
    response_hash: str
    strategy: StrategySpec
    metadata: dict[str, Any] | None = None

    def audit_record(self) -> dict[str, Any]:
        return {
            "created_at": datetime.now(UTC).isoformat(),
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "response_hash": self.response_hash,
            "prompt": self.prompt,
            "response": self.response,
            "strategy_name": self.strategy.name,
            "strategy_spec_hash": stable_hash(self.strategy.raw),
            "metadata": self.metadata or {},
        }


def build_strategy_prompt(
    seed_spec: StrategySpec,
    feedback: list[dict[str, Any]] | None = None,
    max_candidates: int = 1,
) -> str:
    payload = {
        "task": "Generate bounded Strategy Spec v0 candidates for deterministic local backtesting.",
        "constraints": [
            "Return JSON only.",
            "Do not generate executable code.",
            "Do not use martingale, loss doubling, infinite DCA, or unbounded grid logic.",
            "Every candidate must keep hard stop loss, take profit, max holding time, and max position.",
            "Use bounded parameter ranges only.",
            "Optimize from train and validation feedback only; test and final holdout details are hidden.",
        ],
        "max_candidates": max_candidates,
        "seed_strategy": seed_spec.raw,
        "train_validation_feedback": feedback or [],
    }
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def train_validation_feedback(results: list[Any]) -> list[dict[str, Any]]:
    feedback = []
    for result in results:
        fold_feedback = [
            {key: value for key, value in fold.items() if key in TRAIN_VALIDATION_ONLY_KEYS}
            for fold in result.fold_results
        ]
        feedback.append(
            {
                "experiment_id": result.experiment_id,
                "strategy_name": result.strategy_name,
                "strategy_spec_hash": result.strategy_spec_hash,
                "variant_parameters": result.variant_parameters,
                "folds": fold_feedback,
            }
        )
    return feedback


def load_train_validation_feedback(
    experiments_root: Path,
    experiment_id: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    feedback = []
    for result_path in sorted(experiments_root.glob("*/leaderboard.json"), reverse=True):
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        payload_experiment_id = payload.get("experiment_id", result_path.parent.name)
        if experiment_id and not (
            payload_experiment_id == experiment_id
            or payload_experiment_id.startswith(f"{experiment_id}_")
        ):
            continue
        feedback.append(train_validation_feedback_from_payload(payload))
        if len(feedback) >= max(limit, 0):
            break
    return feedback


def train_validation_feedback_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "experiment_id": payload.get("experiment_id"),
        "strategy_name": payload.get("strategy_name"),
        "strategy_spec_hash": payload.get("strategy_spec_hash"),
        "variant_parameters": payload.get("variant_parameters", {}),
        "folds": [
            {key: value for key, value in fold.items() if key in TRAIN_VALIDATION_ONLY_KEYS}
            for fold in payload.get("fold_results", [])
        ],
    }


class DeterministicLocalLLM:
    def __init__(self, model: str = "local-deterministic-template") -> None:
        self.model = model

    def propose(self, seed_spec: StrategySpec, feedback: list[dict[str, Any]] | None = None) -> LLMStrategyProposal:
        prompt = build_strategy_prompt(seed_spec, feedback=feedback)
        raw = self._mutate_seed(seed_spec.raw, proposal_index=len(feedback or []))
        response = json.dumps({"strategy_spec": raw}, indent=2, sort_keys=True)
        strategy = parse_strategy_spec(raw)
        return LLMStrategyProposal(
            model=self.model,
            prompt=prompt,
            response=response,
            prompt_hash=stable_hash(prompt),
            response_hash=stable_hash(response),
            strategy=strategy,
            metadata={"provider": "deterministic"},
        )

    def _mutate_seed(self, raw_seed: dict[str, Any], proposal_index: int = 0) -> dict[str, Any]:
        raw = copy.deepcopy(raw_seed)
        base_name = str(raw["name"]).split("_llm_proposal_")[0]
        raw["name"] = f"{base_name}_llm_proposal_{proposal_index:04d}"
        raw["market_hypothesis"] = (
            f"{raw['market_hypothesis']} Candidate is generated by the local deterministic "
            f"LLM adapter for audit-safe strategy search round {proposal_index}."
        )
        parameters = raw.setdefault("parameters", {})
        if "opening_range_minutes" in parameters:
            candidate_windows = [[3, 5, 15], [5, 15, 30], [10, 20, 30]]
            parameters["opening_range_minutes"] = {
                "values": candidate_windows[proposal_index % len(candidate_windows)]
            }
        return raw


class OpenAICompatibleLLM:
    def __init__(
        self,
        model: str,
        parameters: dict[str, Any] | None = None,
        transport: Any | None = None,
    ) -> None:
        self.model = model
        self.parameters = parameters or {}
        self.base_url = str(self.parameters.get("base_url", "https://api.openai.com/v1")).rstrip("/")
        self.api_key_env = str(self.parameters.get("api_key_env", "OPENAI_API_KEY"))
        self.api_key = self.parameters.get("api_key") or os.environ.get(self.api_key_env)
        self.timeout_seconds = float(self.parameters.get("timeout_seconds", 60))
        self.transport = transport or self._post_json
        if not self.api_key:
            raise ValueError(f"Missing API key. Set {self.api_key_env} or llm_parameters.api_key.")

    def propose(
        self,
        seed_spec: StrategySpec,
        feedback: list[dict[str, Any]] | None = None,
    ) -> LLMStrategyProposal:
        prompt = build_strategy_prompt(seed_spec, feedback=feedback)
        request_payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You generate only bounded Strategy Spec JSON for a local backtest system. "
                        "Never include executable code or hidden test/final-holdout analysis."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            **self.completion_parameters(),
        }
        response_payload = self.transport(
            f"{self.base_url}/chat/completions",
            request_payload,
            self.request_headers(),
            self.timeout_seconds,
        )
        response = extract_chat_content(response_payload)
        strategy = parse_llm_response(response)
        return LLMStrategyProposal(
            model=self.model,
            prompt=prompt,
            response=response,
            prompt_hash=stable_hash(prompt),
            response_hash=stable_hash(response),
            strategy=strategy,
            metadata={
                "provider": "openai_compatible",
                "base_url": self.base_url,
                "usage": response_payload.get("usage", {}),
            },
        )

    def completion_parameters(self) -> dict[str, Any]:
        reserved = {
            "provider",
            "api_key",
            "api_key_env",
            "base_url",
            "timeout_seconds",
        }
        return {key: value for key, value in self.parameters.items() if key not in reserved}

    def request_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM request failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM request failed: {exc}") from exc


def create_llm_adapter(
    model: str,
    parameters: dict[str, Any] | None = None,
    transport: Any | None = None,
):
    parameters = parameters or {}
    provider = parameters.get("provider")
    if provider in {None, "", "deterministic"} and model.startswith("local-"):
        return DeterministicLocalLLM(model=model)
    if provider == "deterministic":
        return DeterministicLocalLLM(model=model)
    if provider in {None, "openai", "openai_compatible"}:
        return OpenAICompatibleLLM(model=model, parameters=parameters, transport=transport)
    raise ValueError(f"Unsupported LLM provider: {provider}")


def extract_chat_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("LLM response missing choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM response missing message content")
    return content


def parse_llm_response(response: str) -> StrategySpec:
    try:
        payload = json.loads(response)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM response must be JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("strategy_spec"), dict):
        raise ValueError("LLM response must contain a strategy_spec object")
    return parse_strategy_spec(payload["strategy_spec"])


def append_audit_log(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
