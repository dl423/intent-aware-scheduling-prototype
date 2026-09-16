"""Repeatable end-to-end validation against the configured OpenAI account."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .gateway import Gateway
from .providers import LUNA_MODEL, TERRA_MODEL, OpenAIProvider, ProviderAdapter
from .store import TaskStore


def _metrics(record: dict[str, Any]) -> dict[str, Any]:
    metrics = record.get("metrics") or {}
    quality = record.get("quality") or {}
    accounting = record.get("accounting") or {}
    return {
        "status": record["status"],
        "model": metrics.get("model"),
        "template": metrics.get("template"),
        "reasoning_effort": metrics.get("reasoning_effort"),
        "provider_calls": metrics.get("provider_calls"),
        "tokens": (metrics.get("input_tokens") or 0) + (metrics.get("output_tokens") or 0),
        "cost_usd": metrics.get("cost_usd"),
        "task_calls": accounting.get("task_calls"),
        "overhead_calls": accounting.get("overhead_calls"),
        "task_cost_usd": accounting.get("task_cost_usd"),
        "overhead_cost_usd": accounting.get("overhead_cost_usd"),
        "quality_score": quality.get("score"),
        "effective_priority": record.get("effective_priority"),
        "transition_events": [event["event"] for event in record.get("transitions", [])],
        "intent_events": [event["kind"] for event in record.get("events", [])],
        "fallback_events": record.get("fallback_events", []),
    }


async def run_real_validation(
    provider: ProviderAdapter | None = None,
    *,
    per_scenario_timeout_seconds: float = 240,
) -> dict[str, Any]:
    """Exercise admission and every execution template through ``Gateway``.

    Prompts are deliberately short to bound live validation cost. Returned
    data contains scheduling and usage metadata only, never task outputs or
    credentials.
    """

    settings = replace(
        Settings.from_env(),
        provider="openai",
        intent_model=LUNA_MODEL,
        intent_fallback_model=TERRA_MODEL,
        candidate_model=LUNA_MODEL,
        candidate_fallback_model=TERRA_MODEL,
        judge_model=LUNA_MODEL,
        judge_fallback_model=TERRA_MODEL,
    )
    active_provider = provider or OpenAIProvider()
    gateway = Gateway(
        settings,
        provider=active_provider,
        store=TaskStore(":memory:"),
    )
    now = datetime.now(timezone.utc)
    results: dict[str, Any] = {}

    async def timed(coro):
        return await asyncio.wait_for(coro, timeout=per_scenario_timeout_seconds)

    try:
        parsed = await timed(
            gateway.submit(
                "I am waiting now. Give me a rough draft under $1 and do not interrupt me.",
                {},
            )
        )
        await timed(gateway.wait_for_idle())
        parsed_record = gateway.get(parsed["id"])
        parsed_contract = parsed_record["contract"]
        results["luna_intent_matrix"] = {
            "status": parsed_record["status"],
            "quality_floor": parsed_contract["quality_floor"],
            "interactive": parsed_contract["interactive"],
            "max_cost_usd": parsed_contract["max_cost_usd"],
            "attention_profile": parsed_contract["attention_profile"],
            "fallback_events": parsed_record["fallback_events"],
            "adequate": (
                parsed_contract["quality_floor"] == "draft"
                and parsed_contract["interactive"] is True
                and float(parsed_contract["max_cost_usd"]) == 1.0
                and parsed_contract["attention_profile"] == "do-not-interrupt"
            ),
        }

        rejected = await timed(
            gateway.submit(
                "Write one sentence describing EDF scheduling.",
                {
                    "quality_floor": "draft",
                    "deadline": now + timedelta(hours=3),
                    "max_cost_usd": 0.000001,
                    "attention_profile": "ask-freely",
                },
            )
        )
        results["budget_rejection"] = {
            "status": rejected["status"],
            "estimated_cost_usd": rejected["admission"]["estimated_cost_usd"],
            "cost_cap_usd": rejected["admission"]["cost_cap_usd"],
        }

        deferred = await timed(
            gateway.submit(
                "Give three concise bullets explaining why valley filling improves utilization.",
                {
                    "quality_floor": "draft",
                    "deadline": now + timedelta(hours=24),
                    "max_cost_usd": 1.0,
                    "attention_profile": "ask-freely",
                },
            )
        )
        deferred_id = deferred["id"]
        before_nudge = gateway.get(deferred_id)
        await timed(gateway.nudge(deferred_id, "Any update? I am blocked now."))
        await timed(gateway.wait_for_idle())
        after_nudge = gateway.get(deferred_id)
        results["nudge_single_pass"] = {
            "before_priority": before_nudge.get("effective_priority"),
            **_metrics(after_nudge),
        }

        patient = await timed(
            gateway.submit(
                "Write a four-item checklist for validating a deterministic scheduler.",
                {
                    "quality_floor": "high",
                    "deadline": now + timedelta(hours=24),
                    "max_cost_usd": 2.0,
                    "attention_profile": "batch-questions",
                },
            )
        )
        patient_id = patient["id"]
        before_deadline = gateway.get(patient_id)
        await timed(gateway.update_deadline(patient_id, now + timedelta(hours=3)))
        await timed(gateway.wait_for_idle())
        after_deadline = gateway.get(patient_id)
        results["deadline_replan_self_check"] = {
            "before_priority": before_deadline.get("effective_priority"),
            **_metrics(after_deadline),
        }

        critical = await timed(
            gateway.submit(
                "Return exactly three concise risks of ignoring a user's task deadline.",
                {
                    "quality_floor": "critical",
                    "deadline": now + timedelta(minutes=30),
                    "interactive": True,
                    "max_cost_usd": 5.0,
                    "attention_profile": "do-not-interrupt",
                },
            )
        )
        await timed(gateway.wait_for_idle())
        results["interactive_parallel_judge"] = _metrics(gateway.get(critical["id"]))

        calls = getattr(active_provider, "calls", [])
        models = [call.get("model") for call in calls]
        allowed_models = {LUNA_MODEL, TERRA_MODEL}
        task_calls = [call for call in calls if call.get("role") == "task_execution"]
        overhead_calls = [call for call in calls if call.get("role") != "task_execution"]
        results["provider_audit"] = {
            "total_calls": len(models),
            "models": sorted(set(models)),
            "two_tier_only": bool(models) and set(models).issubset(allowed_models),
            "both_tiers_observed": allowed_models.issubset(set(models)),
            "structured_calls": sum(bool(call.get("schema_name")) for call in calls),
            "total_tokens": sum(
                int(call.get("input_tokens", 0)) + int(call.get("output_tokens", 0))
                for call in calls
            ),
            "total_cost_usd": sum(float(call.get("cost_usd", 0)) for call in calls),
            "task_calls": len(task_calls),
            "overhead_calls": len(overhead_calls),
            "task_cost_usd": sum(float(call.get("cost_usd", 0)) for call in task_calls),
            "overhead_cost_usd": sum(
                float(call.get("cost_usd", 0)) for call in overhead_calls
            ),
        }
        results["validation_passed"] = all(
            (
                results["budget_rejection"]["status"] == "rejected",
                results["luna_intent_matrix"]["adequate"] is True,
                results["nudge_single_pass"]["status"] == "completed",
                results["nudge_single_pass"]["effective_priority"] == "interactive",
                results["deadline_replan_self_check"]["status"] == "completed",
                results["deadline_replan_self_check"]["template"]
                == "single-pass+self-check",
                results["interactive_parallel_judge"]["status"] == "completed",
                results["interactive_parallel_judge"]["template"]
                == "parallel-drafts+judge",
                results["provider_audit"]["two_tier_only"] is True,
                results["provider_audit"]["both_tiers_observed"] is True,
                results["provider_audit"]["total_cost_usd"] < 1.0,
            )
        )
        return results
    finally:
        await gateway.close()
