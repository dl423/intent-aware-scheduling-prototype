from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from intent_scheduler.execution import ExecutionEngine
from intent_scheduler.intent import INTENT_SCHEMA, IntentExtractor
from intent_scheduler.providers import (
    LUNA_MODEL,
    TERRA_MODEL,
    MockProvider,
    OpenAIProvider,
)


def _value(value):
    return getattr(value, "value", value)


@pytest.mark.asyncio
async def test_mock_provider_is_deterministic_and_reports_usage():
    provider = MockProvider()
    first = await provider.complete("hello", model=TERRA_MODEL)
    second = await provider.complete("hello", model=TERRA_MODEL)

    assert first.text == second.text
    assert first.usage.total_tokens > 0
    assert first.usage.cost_usd > 0
    assert [call["model"] for call in provider.calls] == [TERRA_MODEL, TERRA_MODEL]


@pytest.mark.asyncio
async def test_intent_extraction_uses_strict_schema_and_explicit_precedence():
    provider = MockProvider()
    extractor = IntentExtractor(provider)
    now = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
    explicit_deadline = now + timedelta(hours=6)

    contract = await extractor.extract(
        "This is critical and I am waiting now. Do not interrupt me. Budget under $9.",
        explicit={
            "quality_floor": "high",
            "deadline": explicit_deadline,
            "max_cost_usd": 3.0,
        },
        now=now,
    )

    assert _value(contract.quality_floor) == "high"
    assert contract.deadline == explicit_deadline
    assert contract.max_cost_usd == 3.0
    assert _value(contract.attention_profile) == "do-not-interrupt"
    assert contract.interactive is True
    assert _value(contract.provenance.quality_floor) == "explicit"
    assert _value(contract.provenance.attention_profile) == "parsed"
    assert provider.calls[0]["schema_name"] == "intent_contract"
    assert provider.calls[0]["model"] == LUNA_MODEL


@pytest.mark.asyncio
async def test_intent_invalid_json_is_rejected():
    provider = MockProvider(scripted_responses=["not-json"])
    extractor = IntentExtractor(provider, fallback_model=None)
    with pytest.raises(ValueError, match="invalid intent contract"):
        await extractor.extract("Write a report")


@pytest.mark.asyncio
async def test_absent_intent_uses_defaults_with_default_provenance():
    now = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
    contract = await IntentExtractor(MockProvider()).extract(
        "Summarize the supplied content", now=now
    )

    assert _value(contract.quality_floor) == "standard"
    assert contract.deadline == now + timedelta(hours=24)
    assert float(contract.max_cost_usd) == 2.0
    assert _value(contract.attention_profile) == "batch-questions"
    assert _value(contract.provenance.quality_floor) == "default"
    assert _value(contract.provenance.deadline) == "default"
    assert _value(contract.provenance.max_cost_usd) == "default"
    assert _value(contract.provenance.attention_profile) == "default"


@dataclass
class Decision:
    model_name: str = TERRA_MODEL
    reasoning_effort: str = "medium"
    execution_template: str = "single-pass"
    ask_policy: str = "do-not-interrupt"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("template", "expected_calls"),
    [
        ("single-pass", 2),
        ("single-pass+self-check", 3),
        ("parallel-drafts+judge", 5),
    ],
)
async def test_execution_templates_account_for_every_provider_call(template, expected_calls):
    provider = MockProvider()
    engine = ExecutionEngine(provider, parallel_drafts=3)

    result = await engine.execute(
        "Draft a concise migration plan",
        Decision(execution_template=template),
        queue_wait_seconds=2.5,
    )

    assert result.output
    assert result.quality_score == 4
    assert result.metrics.provider_calls == expected_calls
    assert result.metrics.total_tokens > 0
    assert result.metrics.cost_usd > 0
    assert result.metrics.queue_wait_seconds == 2.5
    assert result.metrics.template == template
    assert provider.calls[-1]["model"] == LUNA_MODEL
    assert provider.calls[-1]["schema_name"] == "quality_judgment"
    assert result.metrics.task_calls >= 1
    assert result.metrics.overhead_calls >= 1
    assert result.metrics.task_cost_usd > 0
    assert result.metrics.overhead_cost_usd > 0


@pytest.mark.asyncio
async def test_parallel_judge_rejects_out_of_range_winner():
    provider = MockProvider(
        scripted_responses=[
            "draft one",
            "draft two",
            {"winner": 3, "rationale": "bad index"},
        ]
    )
    engine = ExecutionEngine(
        provider,
        parallel_drafts=2,
        candidate_fallback_model=None,
    )
    with pytest.raises(ValueError, match="outside the available range"):
        await engine.execute(
            "Draft something", Decision(execution_template="parallel-drafts+judge")
        )


class FakeResponses:
    def __init__(self):
        self.request = None

    async def create(self, **kwargs):
        self.request = kwargs
        usage = SimpleNamespace(input_tokens=10, output_tokens=4)
        return SimpleNamespace(output_text='{"score":4,"rationale":"good"}', usage=usage, model=kwargs["model"])


@pytest.mark.asyncio
async def test_openai_adapter_uses_responses_strict_json_shape_and_configured_price():
    responses = FakeResponses()
    provider = OpenAIProvider(
        client=SimpleNamespace(responses=responses),
        price_per_million={TERRA_MODEL: (2.0, 8.0)},
    )
    result = await provider.complete(
        "judge",
        model=TERRA_MODEL,
        reasoning_effort="low",
        json_schema=INTENT_SCHEMA,
        schema_name="intent_contract",
    )

    assert responses.request["reasoning"] == {"effort": "low"}
    assert responses.request["text"]["format"]["strict"] is True
    assert responses.request["text"]["format"]["schema"] == INTENT_SCHEMA
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 4
    assert result.usage.cost_usd == pytest.approx((10 * 2 + 4 * 8) / 1_000_000)


@pytest.mark.asyncio
async def test_intent_schema_failure_falls_back_from_luna_to_terra():
    provider = MockProvider(
        scripted_responses=[
            "not-json",
            {
                "quality_floor": "standard",
                "deadline_mode": "unspecified",
                "deadline_at": None,
                "cost_cap_usd": None,
                "attention_profile": None,
            },
        ]
    )
    extractor = IntentExtractor(provider)
    await extractor.extract("Summarize this")

    assert [call["model"] for call in provider.calls] == [LUNA_MODEL, TERRA_MODEL]
    assert extractor.fallback_events[0]["reason"] == "schema-validation-failure"


@pytest.mark.asyncio
async def test_quality_judge_schema_failure_falls_back_independently():
    provider = MockProvider(
        scripted_responses=[
            "task result",
            "not-json",
            {"score": 4, "rationale": "adequate"},
        ]
    )
    result = await ExecutionEngine(provider).execute("Write one sentence", Decision())

    assert [call["model"] for call in provider.calls] == [
        TERRA_MODEL,
        LUNA_MODEL,
        TERRA_MODEL,
    ]
    assert result.fallback_events[0]["role"] == "quality_judgment"


@pytest.mark.asyncio
async def test_candidate_semantic_failure_falls_back_independently():
    provider = MockProvider(
        scripted_responses=[
            "draft one",
            "draft two",
            {"winner": 3, "rationale": "invalid index"},
            {"winner": 2, "rationale": "valid selection"},
            {"score": 4, "rationale": "adequate"},
        ]
    )
    engine = ExecutionEngine(provider, parallel_drafts=2)
    result = await engine.execute(
        "Draft something",
        Decision(execution_template="parallel-drafts+judge"),
    )

    assert result.output == "draft two"
    assert result.fallback_events[0]["role"] == "candidate_selection"
    assert provider.calls[2]["model"] == LUNA_MODEL
    assert provider.calls[3]["model"] == TERRA_MODEL
