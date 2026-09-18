"""Execution templates and post-completion quality assessment."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .providers import LUNA_MODEL, TERRA_MODEL, ProviderAdapter, ProviderResponse, ProviderUsage


QUALITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 5},
        "rationale": {"type": "string"},
    },
    "required": ["score", "rationale"],
}

CANDIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "winner": {"type": "integer", "minimum": 1},
        "rationale": {"type": "string"},
    },
    "required": ["winner", "rationale"],
}


class QualityJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=1, le=5)
    rationale: str


class CandidateSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    winner: int = Field(ge=1)
    rationale: str


@dataclass(frozen=True, slots=True)
class ExecutionMetrics:
    input_tokens: int
    output_tokens: int
    cost_usd: float
    wall_seconds: float
    queue_wait_seconds: float
    model: str
    reasoning_effort: str
    template: str
    provider_calls: int
    task_calls: int
    overhead_calls: int
    task_tokens: int
    overhead_tokens: int
    task_cost_usd: float
    overhead_cost_usd: float

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    output: str
    quality_score: int
    quality_rationale: str
    metrics: ExecutionMetrics
    provider_responses: tuple[ProviderResponse, ...] = field(default_factory=tuple)
    fallback_events: tuple[dict[str, str], ...] = field(default_factory=tuple)


class ExecutionEngine:
    """Run a request through the policy-selected execution structure."""

    def __init__(
        self,
        provider: ProviderAdapter,
        *,
        candidate_model: str = LUNA_MODEL,
        candidate_fallback_model: str | None = TERRA_MODEL,
        judge_model: str = LUNA_MODEL,
        judge_fallback_model: str | None = TERRA_MODEL,
        parallel_drafts: int = 3,
    ) -> None:
        if parallel_drafts < 2:
            raise ValueError("parallel_drafts must be at least 2")
        self.provider = provider
        self.candidate_model = candidate_model
        self.candidate_fallback_model = candidate_fallback_model
        self.judge_model = judge_model
        self.judge_fallback_model = judge_fallback_model
        self.parallel_drafts = parallel_drafts

    async def execute(
        self,
        request_text: str,
        decision: Any,
        *,
        queue_wait_seconds: float = 0.0,
    ) -> ExecutionResult:
        started = time.perf_counter()
        model = str(_value(getattr(decision, "model_name")))
        effort = str(_value(getattr(decision, "reasoning_effort", "medium")))
        template = _normalize_template(getattr(decision, "execution_template", "single-pass"))
        ask_policy = str(_value(getattr(decision, "ask_policy", "batch-questions")))
        parallel_drafts = int(getattr(decision, "parallel_drafts", self.parallel_drafts))
        if template == "parallel-drafts+judge" and parallel_drafts < 2:
            raise ValueError("parallel execution decisions require at least 2 drafts")
        responses: list[ProviderResponse] = []
        fallback_events: list[dict[str, str]] = []

        base_prompt = _task_prompt(request_text, ask_policy)
        if template == "single-pass":
            response = await self.provider.complete(
                base_prompt, model=model, reasoning_effort=effort, role="task_execution"
            )
            responses.append(response)
            output = response.text
        elif template == "single-pass+self-check":
            first = await self.provider.complete(
                base_prompt, model=model, reasoning_effort=effort, role="task_execution"
            )
            responses.append(first)
            check_prompt = f"""Revise the candidate only where needed to satisfy the original goal.
Check factual consistency, omissions, and instruction following. Return only
the final answer.

ORIGINAL GOAL:
{base_prompt}

CANDIDATE:
{first.text}"""
            checked = await self.provider.complete(
                check_prompt, model=model, reasoning_effort=effort, role="task_execution"
            )
            responses.append(checked)
            output = checked.text
        elif template == "parallel-drafts+judge":
            draft_calls = [
                self.provider.complete(
                    base_prompt + f"\n\nProduce independent candidate {index + 1}.",
                    model=model,
                    reasoning_effort=effort,
                    role="task_execution",
                )
                for index in range(parallel_drafts)
            ]
            # Wait for every draft even when one fails. Releasing the task's
            # capacity while sibling calls still run would exceed the limit.
            draft_results = await asyncio.gather(*draft_calls, return_exceptions=True)
            for result in draft_results:
                if isinstance(result, BaseException):
                    raise result
            drafts = list(draft_results)
            responses.extend(drafts)
            selection, selection_responses, fallback = await self._structured_call(
                prompt=_candidate_prompt(request_text, [draft.text for draft in drafts]),
                primary_model=self.candidate_model,
                fallback_model=self.candidate_fallback_model,
                schema=CANDIDATE_SCHEMA,
                schema_name="candidate_selection",
                role="candidate_selection",
                parser=lambda text: _parse_selection(text, len(drafts)),
            )
            responses.extend(selection_responses)
            if fallback:
                fallback_events.append(fallback)
            output = drafts[selection.winner - 1].text
        else:  # pragma: no cover - normalization guards this
            raise ValueError(f"Unsupported execution template: {template}")

        judgment, quality_responses, fallback = await self._structured_call(
            prompt=_quality_prompt(request_text, output),
            primary_model=self.judge_model,
            fallback_model=self.judge_fallback_model,
            schema=QUALITY_SCHEMA,
            schema_name="quality_judgment",
            role="quality_judgment",
            parser=_parse_quality,
        )
        responses.extend(quality_responses)
        if fallback:
            fallback_events.append(fallback)
        elapsed = time.perf_counter() - started
        usage = _sum_usage(responses)
        task_responses = [response for response in responses if response.role == "task_execution"]
        overhead_responses = [response for response in responses if response.role != "task_execution"]
        task_usage = _sum_usage(task_responses)
        overhead_usage = _sum_usage(overhead_responses)
        metrics = ExecutionMetrics(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
            wall_seconds=elapsed,
            queue_wait_seconds=max(0.0, queue_wait_seconds),
            model=model,
            reasoning_effort=effort,
            template=template,
            provider_calls=len(responses),
            task_calls=len(task_responses),
            overhead_calls=len(overhead_responses),
            task_tokens=task_usage.total_tokens,
            overhead_tokens=overhead_usage.total_tokens,
            task_cost_usd=task_usage.cost_usd,
            overhead_cost_usd=overhead_usage.cost_usd,
        )
        return ExecutionResult(
            output=output,
            quality_score=judgment.score,
            quality_rationale=judgment.rationale,
            metrics=metrics,
            provider_responses=tuple(responses),
            fallback_events=tuple(fallback_events),
        )

    async def _structured_call(
        self,
        *,
        prompt: str,
        primary_model: str,
        fallback_model: str | None,
        schema: dict[str, Any],
        schema_name: str,
        role: str,
        parser: Any,
    ) -> tuple[Any, list[ProviderResponse], dict[str, str] | None]:
        responses: list[ProviderResponse] = []
        response = await self.provider.complete(
            prompt,
            model=primary_model,
            reasoning_effort="low",
            json_schema=schema,
            schema_name=schema_name,
            role=role,
        )
        responses.append(response)
        try:
            return parser(response.text), responses, None
        except ValueError:
            if fallback_model is None or fallback_model == primary_model:
                raise
        fallback_event = {
            "role": role,
            "from_model": primary_model,
            "to_model": fallback_model,
            "reason": "schema-or-semantic-validation-failure",
        }
        fallback_response = await self.provider.complete(
            prompt,
            model=fallback_model,
            reasoning_effort="low",
            json_schema=schema,
            schema_name=schema_name,
            role=role,
        )
        responses.append(fallback_response)
        return parser(fallback_response.text), responses, fallback_event


def _task_prompt(request_text: str, ask_policy: str) -> str:
    attention_instruction = {
        "ask-freely": "If information is genuinely required, state one concise clarification question.",
        "may-ask": "If information is genuinely required, state one concise clarification question.",
        "batch-questions": "Batch all essential clarification questions into one checkpoint; otherwise proceed.",
        "one-batched-checkpoint": "Batch all essential clarification questions into one checkpoint; otherwise proceed.",
        "do-not-interrupt": "Do not ask questions. Make explicit, conservative assumptions and self-check them.",
        "best-guess+self-verify": "Do not ask questions. Make explicit, conservative assumptions and self-check them.",
    }.get(ask_policy, "Proceed using reasonable assumptions.")
    return f"""Complete the user's task. {attention_instruction}
Return the useful task result, without discussing scheduling policy.

USER TASK:
{request_text}"""


def _candidate_prompt(goal: str, candidates: list[str]) -> str:
    rendered = "\n\n".join(
        f"CANDIDATE {index + 1}:\n{text}" for index, text in enumerate(candidates)
    )
    return f"""Select the candidate that best fulfills the stated goal. Judge
correctness, completeness, instruction following, and clarity. Return strict JSON.

GOAL:
{goal}

CANDIDATE RESPONSES:
{rendered}"""


def _quality_prompt(goal: str, output: str) -> str:
    return f"""Score the response against its own stated goal using this rubric.
1 = unusable or unrelated. 2 = major failures. 3 = adequate with meaningful
defects. 4 = strong with only minor defects. 5 = fully satisfies the goal.
Do not reward verbosity. Return strict JSON with a concise rationale.

GOAL:
{goal}

RESPONSE:
{output}"""


def _parse_quality(text: str) -> QualityJudgment:
    try:
        return QualityJudgment.model_validate_json(text)
    except ValidationError as exc:
        raise ValueError(f"Provider returned an invalid quality judgment: {exc}") from exc


def _parse_selection(text: str, candidate_count: int) -> CandidateSelection:
    try:
        selection = CandidateSelection.model_validate_json(text)
    except ValidationError as exc:
        raise ValueError(f"Provider returned an invalid candidate selection: {exc}") from exc
    if selection.winner > candidate_count:
        raise ValueError("Provider selected a candidate outside the available range")
    return selection


def _sum_usage(responses: list[ProviderResponse]) -> ProviderUsage:
    return ProviderUsage(
        input_tokens=sum(response.usage.input_tokens for response in responses),
        output_tokens=sum(response.usage.output_tokens for response in responses),
        cost_usd=sum(response.usage.cost_usd for response in responses),
    )


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _normalize_template(value: Any) -> str:
    normalized = str(_value(value)).lower().replace("_", "-").replace(" ", "")
    aliases = {
        "single-pass": "single-pass",
        "singlepass": "single-pass",
        "single-pass+self-check": "single-pass+self-check",
        "self-check": "single-pass+self-check",
        "singlepass+self-check": "single-pass+self-check",
        "parallel-drafts+judge": "parallel-drafts+judge",
        "parallel+judge": "parallel-drafts+judge",
        "n-way-parallel+judge": "parallel-drafts+judge",
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported execution template: {value}") from exc
