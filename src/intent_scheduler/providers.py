"""LLM provider adapters.

The scheduler's deterministic core depends only on :class:`ProviderAdapter`.
The mock keeps tests and the default demo offline, while ``OpenAIProvider``
loads credentials lazily and uses the Responses API in real mode.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol


TERRA_MODEL = "gpt-5.6-terra"
LUNA_MODEL = "gpt-5.6-luna"

# Public list prices in USD per million input/output tokens. Environment
# overrides allow billing changes to be adopted without a code release.
DEFAULT_PRICE_PER_MILLION: dict[str, tuple[float, float]] = {
    TERRA_MODEL: (2.5, 15.0),
    LUNA_MODEL: (1.0, 6.0),
}


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    text: str
    model: str
    role: str = "task_execution"
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    latency_seconds: float = 0.0


class ProviderAdapter(Protocol):
    async def complete(
        self,
        prompt: str,
        *,
        model: str,
        reasoning_effort: str = "medium",
        json_schema: Mapping[str, Any] | None = None,
        schema_name: str = "response",
        role: str = "task_execution",
    ) -> ProviderResponse:
        """Return one completion, optionally constrained to strict JSON."""


class MockProvider:
    """Deterministic offline provider with optional scripted responses.

    Scripted values are consumed FIFO. Without scripts, the adapter recognizes
    the intent and quality-judge schemas and otherwise emits a stable task
    response. Token counts and cost are deterministic approximations intended
    for scheduler tests, not provider billing reconciliation.
    """

    def __init__(
        self,
        *,
        latency_seconds: float = 0.0,
        input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
        price_per_million: Mapping[str, tuple[float, float]] | None = None,
        scripted_responses: list[str | Mapping[str, Any]] | None = None,
    ) -> None:
        self.latency_seconds = latency_seconds
        self.price_per_million = dict(price_per_million or DEFAULT_PRICE_PER_MILLION)
        self.fixed_rates = (
            (input_cost_per_million, output_cost_per_million)
            if input_cost_per_million is not None and output_cost_per_million is not None
            else None
        )
        self.scripted_responses = list(scripted_responses or [])
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        prompt: str,
        *,
        model: str,
        reasoning_effort: str = "medium",
        json_schema: Mapping[str, Any] | None = None,
        schema_name: str = "response",
        role: str = "task_execution",
    ) -> ProviderResponse:
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "schema_name": schema_name if json_schema else None,
                "role": role,
            }
        )
        if self.latency_seconds:
            await asyncio.sleep(self.latency_seconds)

        if self.scripted_responses:
            value = self.scripted_responses.pop(0)
            text = value if isinstance(value, str) else json.dumps(value)
        elif json_schema and schema_name == "intent_contract":
            text = json.dumps(self._mock_intent(prompt))
        elif json_schema and schema_name == "quality_judgment":
            text = json.dumps({"score": 4, "rationale": "The response addresses the stated goal."})
        elif json_schema and schema_name == "candidate_selection":
            text = json.dumps({"winner": 1, "rationale": "Candidate 1 is the clearest response."})
        else:
            text = self._mock_task_response(prompt)

        input_tokens = _estimate_tokens(prompt)
        output_tokens = _estimate_tokens(text)
        rates = self.fixed_rates or self.price_per_million.get(model, (0.0, 0.0))
        cost = (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000
        self.calls[-1].update(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )
        return ProviderResponse(
            text=text,
            model=model,
            role=role,
            usage=ProviderUsage(input_tokens, output_tokens, cost),
            latency_seconds=self.latency_seconds,
        )

    @staticmethod
    def _mock_intent(prompt: str) -> dict[str, Any]:
        request = prompt.rsplit("USER REQUEST:", 1)[-1].lower()
        quality = None
        if any(word in request for word in ("critical", "mission-critical", "production")):
            quality = "critical"
        elif any(word in request for word in ("high quality", "thorough", "research")):
            quality = "high"
        elif any(word in request for word in ("rough", "draft", "quick sketch")):
            quality = "draft"

        deadline_mode = "unspecified"
        deadline_at = None
        if any(word in request for word in ("now", "asap", "waiting", "urgent")):
            deadline_mode = "interactive"

        attention = None
        if any(phrase in request for phrase in ("do not interrupt", "don't interrupt", "no questions")):
            attention = "do-not-interrupt"
        elif any(phrase in request for phrase in ("ask me", "consult me", "check with me")):
            attention = "ask-freely"

        cost_match = re.search(r"(?:under|budget(?: is)?|maximum|at most)\s*\$\s*(\d+(?:\.\d+)?)", request)
        return {
            "quality_floor": quality,
            "deadline_mode": deadline_mode,
            "deadline_at": deadline_at,
            "cost_cap_usd": float(cost_match.group(1)) if cost_match else None,
            "attention_profile": attention,
        }

    @staticmethod
    def _mock_task_response(prompt: str) -> str:
        if "CANDIDATE RESPONSES" in prompt:
            return "Candidate 1"
        return "Mock completion: " + prompt[-240:].strip()


class OpenAIProvider:
    """OpenAI Responses API adapter.

    ``OPENAI_API_KEY`` is loaded from the process environment, or from a
    local ``.env`` via python-dotenv when installed. No credential is read at
    import time or retained by this module.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        price_per_million: Mapping[str, tuple[float, float]] | None = None,
        client: Any | None = None,
    ) -> None:
        if client is None:
            try:
                from dotenv import load_dotenv

                project_dir = Path(__file__).resolve().parents[2]
                load_dotenv(project_dir / ".env", override=False)
            except ImportError:
                pass
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover - dependency error path
                raise RuntimeError("Real mode requires the 'openai' package") from exc
            client = AsyncOpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        self.client = client
        self.price_per_million = dict(price_per_million or _prices_from_env())
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        prompt: str,
        *,
        model: str = TERRA_MODEL,
        reasoning_effort: str = "medium",
        json_schema: Mapping[str, Any] | None = None,
        schema_name: str = "response",
        role: str = "task_execution",
    ) -> ProviderResponse:
        import time

        call_record = {
            "model": model,
            "reasoning_effort": reasoning_effort,
            "schema_name": schema_name if json_schema is not None else None,
            "role": role,
        }
        self.calls.append(call_record)
        request: dict[str, Any] = {
            "model": model,
            "input": prompt,
            "reasoning": {"effort": reasoning_effort},
        }
        if json_schema is not None:
            request["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": dict(json_schema),
                    "strict": True,
                }
            }

        started = time.perf_counter()
        response = await self.client.responses.create(**request)
        elapsed = time.perf_counter() - started
        text = getattr(response, "output_text", "")
        usage_obj = getattr(response, "usage", None)
        input_tokens = int(getattr(usage_obj, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage_obj, "output_tokens", 0) or 0)
        rates = self.price_per_million.get(model)
        cost = 0.0
        if rates:
            cost = (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000
        call_record.update(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_seconds=elapsed,
        )
        return ProviderResponse(
            text=text,
            model=getattr(response, "model", model),
            role=role,
            usage=ProviderUsage(input_tokens, output_tokens, cost),
            latency_seconds=elapsed,
        )


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _prices_from_env() -> dict[str, tuple[float, float]]:
    """Load optional pricing without baking unverified prices into the code."""

    result = dict(DEFAULT_PRICE_PER_MILLION)
    for model, prefix in ((TERRA_MODEL, "TERRA"), (LUNA_MODEL, "LUNA")):
        input_price = os.getenv(f"OPENAI_{prefix}_INPUT_USD_PER_MILLION")
        output_price = os.getenv(f"OPENAI_{prefix}_OUTPUT_USD_PER_MILLION")
        if input_price is not None and output_price is not None:
            result[model] = (float(input_price), float(output_price))
    return result
