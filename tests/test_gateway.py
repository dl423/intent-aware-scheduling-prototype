from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from intent_scheduler.clock import SimulatedClock
from intent_scheduler.config import Settings
from intent_scheduler.gateway import Gateway
from intent_scheduler.models import Provenance
from intent_scheduler.providers import MockProvider
from intent_scheduler.store import TaskStore


START = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)


def settings(capacity: int = 6) -> Settings:
    return Settings(
        provider="mock",
        database_path=Path(":memory:"),
        capacity=capacity,
    )


@pytest.mark.asyncio
async def test_gateway_uses_injected_clock_and_preserves_revealed_intent_on_deadline_change():
    clock = SimulatedClock(START)
    gateway = Gateway(
        settings(),
        provider=MockProvider(),
        store=TaskStore(":memory:"),
        clock=clock,
        auto_execute=False,
    )
    try:
        submitted = await gateway.submit(
            "Write a rough note by tomorrow.",
            {
                "quality_floor": "draft",
                "deadline": START + timedelta(hours=24),
                "max_cost_usd": 1,
                "attention_profile": "ask-freely",
            },
        )
        assert datetime.fromisoformat(submitted["submitted_at"]) == START

        await gateway.nudge(submitted["id"], "Any update?")
        clock.advance(minutes=10)
        updated = await gateway.update_deadline(
            submitted["id"], START + timedelta(hours=6)
        )

        assert updated["contract"]["interactive"] is True
        assert updated["contract"]["provenance"]["interactive"] == Provenance.REVEALED.value
        assert updated["effective_priority"] == "interactive"
        assert updated["decision"]["priority_class"] == "interactive"
        assert updated["decision_history"][-1]["phase"] == "deadline-replan"
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_dispatch_redecision_uses_current_load_and_records_both_decisions():
    clock = SimulatedClock(START)
    gateway = Gateway(
        settings(capacity=8),
        provider=MockProvider(),
        store=TaskStore(":memory:"),
        clock=clock,
        auto_execute=False,
    )
    try:
        blocker_ids = []
        for index in range(7):
            blocker = await gateway.submit(
                f"Interactive draft {index}",
                {
                    "quality_floor": "draft",
                    "deadline": START + timedelta(hours=1),
                    "interactive": True,
                    "max_cost_usd": 1,
                    "attention_profile": "ask-freely",
                },
            )
            blocker_ids.append(blocker["id"])
        assert len(await gateway.tick()) == 7
        assert gateway.scheduler.in_flight_count == 7

        patient = await gateway.submit(
            "Prepare a high quality research note by tomorrow.",
            {
                "quality_floor": "high",
                "deadline": START + timedelta(hours=24),
                "max_cost_usd": 2,
                "attention_profile": "batch-questions",
            },
        )
        assert patient["decision"]["reasoning_effort"] == "medium"

        for task_id in blocker_ids:
            gateway.scheduler.complete(task_id)

        clock.advance(hours=14)
        dispatched = await gateway.tick()
        assert [task.task_id for task in dispatched] == [patient["id"]]
        record = gateway.get(patient["id"])
        assert record["decision_history"][0]["decision"]["reasoning_effort"] == "medium"
        assert record["decision_history"][-1]["decision"]["reasoning_effort"] == "high"
        assert record["transitions"][-2]["event"] == "dispatch-redecision"
    finally:
        await gateway.close()
