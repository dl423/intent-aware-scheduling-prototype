from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from intent_scheduler.clock import SimulatedClock
from intent_scheduler.models import (
    AttentionProfile,
    ContractProvenance,
    Provenance,
    QualityFloor,
    TaskContract,
)
from intent_scheduler.scheduler import (
    DEFERRED_BATCH,
    INTERACTIVE,
    STANDARD,
    CostBudgetExceeded,
    ScheduledTask,
    Scheduler,
)


UTC = timezone.utc
START = datetime(2026, 7, 12, 8, tzinfo=UTC)


@dataclass(frozen=True)
class Decision:
    priority_class: str
    scheduled_at: datetime
    estimated_cost_usd: float = 1.0
    execution_template: str = "single-pass"
    parallel_drafts: int = 1


def task(
    task_id: str,
    clock: SimulatedClock,
    *,
    priority: str = STANDARD,
    deadline_hours: float = 4,
    scheduled_hours: float = 0,
    estimate: float = 1,
    cap: float | None = 10,
) -> ScheduledTask:
    now = clock.now()
    contract = TaskContract(
        quality_floor=QualityFloor.STANDARD,
        deadline=now + timedelta(hours=deadline_hours),
        max_cost_usd=cap or 10,
        attention_profile=AttentionProfile.ASK_FREELY,
        provenance=ContractProvenance(
            quality_floor=Provenance.EXPLICIT,
            deadline=Provenance.EXPLICIT,
            max_cost_usd=Provenance.EXPLICIT,
            attention_profile=Provenance.EXPLICIT,
        ),
    )
    return ScheduledTask(
        task_id,
        contract,
        Decision(priority, now + timedelta(hours=scheduled_hours), estimate),
    )


def test_simulated_clock_is_timezone_safe_and_monotonic() -> None:
    clock = SimulatedClock(START)
    assert clock.advance(timedelta(hours=3)) == START + timedelta(hours=3)
    assert clock.advance(minutes=0.5) == START + timedelta(hours=3, seconds=30)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set(datetime(2026, 7, 13))


def test_edf_ordering_within_each_class_and_class_ordering() -> None:
    clock = SimulatedClock(START)
    scheduler = Scheduler(capacity=4, reservations={}, clock=clock)
    scheduler.enqueue(task("standard-late", clock, deadline_hours=5))
    scheduler.enqueue(task("interactive", clock, priority=INTERACTIVE, deadline_hours=8))
    scheduler.enqueue(task("standard-early", clock, deadline_hours=2))

    assert scheduler.next_task().task_id == "interactive"
    assert scheduler.next_task().task_id == "standard-early"
    assert scheduler.next_task().task_id == "standard-late"


def test_reservation_prevents_interactive_from_consuming_standard_share() -> None:
    clock = SimulatedClock(START)
    scheduler = Scheduler(
        capacity=3,
        reservations={INTERACTIVE: 1, STANDARD: 1},
        clock=clock,
    )
    scheduler.enqueue(task("interactive-1", clock, priority=INTERACTIVE))
    scheduler.enqueue(task("interactive-2", clock, priority=INTERACTIVE))
    scheduler.enqueue(task("interactive-3", clock, priority=INTERACTIVE))
    scheduler.enqueue(task("standard", clock))

    dispatched = scheduler.dispatch_ready()
    assert [item.task_id for item in dispatched] == [
        "interactive-1",
        "interactive-2",
        "standard",
    ]
    assert scheduler.in_flight_count == 3


def test_parallel_template_reserves_peak_provider_call_concurrency() -> None:
    clock = SimulatedClock(START)
    scheduler = Scheduler(capacity=4, reservations={}, clock=clock)
    parallel = task("parallel", clock, priority=INTERACTIVE)
    parallel.decision = Decision(
        INTERACTIVE,
        clock.now(),
        execution_template="parallel-drafts+judge",
        parallel_drafts=3,
    )
    scheduler.enqueue(parallel)
    scheduler.enqueue(task("standard", clock))

    assert scheduler.next_task() is parallel
    assert scheduler.in_flight_count == 3
    assert scheduler.next_task().task_id == "standard"
    assert scheduler.in_flight_count == 4


def test_admission_control_rejects_estimate_above_remaining_budget() -> None:
    clock = SimulatedClock(START)
    scheduler = Scheduler(clock=clock)
    over = task("over", clock, estimate=2.01, cap=2)

    result = scheduler.admit(over)
    assert not result.admitted
    assert "exceeds remaining budget" in result.reason
    with pytest.raises(CostBudgetExceeded):
        scheduler.enqueue(over)
    assert over.state == "rejected"


def test_deferred_task_escalates_and_ignores_future_low_load_window() -> None:
    clock = SimulatedClock(START)
    scheduler = Scheduler(
        capacity=2,
        escalation_window=timedelta(hours=1),
        clock=clock,
    )
    deferred = task(
        "overnight",
        clock,
        priority=DEFERRED_BATCH,
        deadline_hours=3,
        scheduled_hours=8,
    )
    scheduler.enqueue(deferred)
    assert scheduler.next_task() is None

    clock.advance(timedelta(hours=2, minutes=1))
    promoted = scheduler.escalate()
    assert promoted == [deferred]
    assert deferred.priority_class == STANDARD
    assert deferred.scheduled_at == clock.now()
    assert scheduler.next_task() is deferred
    assert deferred.transitions[-2]["reason"] == "deadline-escalation"


def test_nudge_revises_contract_as_revealed_intent() -> None:
    clock = SimulatedClock(START)
    scheduler = Scheduler(clock=clock)
    deferred = task(
        "research",
        clock,
        priority=DEFERRED_BATCH,
        deadline_hours=20,
        scheduled_hours=6,
    )
    scheduler.enqueue(deferred)

    scheduler.nudge("research")
    assert deferred.contract.interactive is True
    assert deferred.contract.provenance.interactive is Provenance.REVEALED
    assert deferred.priority_class == DEFERRED_BATCH
    assert deferred.transitions[-1]["event"] == "revealed-intent"


def test_deferred_work_only_fills_capacity_below_load_threshold() -> None:
    clock = SimulatedClock(START)
    scheduler = Scheduler(
        capacity=4,
        reservations={},
        deferred_load_threshold=0.5,
        clock=clock,
    )
    scheduler.enqueue(task("standard", clock))
    scheduler.enqueue(task("batch-1", clock, priority=DEFERRED_BATCH))
    scheduler.enqueue(task("batch-2", clock, priority=DEFERRED_BATCH))

    assert scheduler.next_task().task_id == "standard"
    assert scheduler.next_task().task_id == "batch-1"
    assert scheduler.next_task() is None
    scheduler.complete("standard")
    assert scheduler.next_task().task_id == "batch-2"


def test_future_scheduled_earlier_deadline_does_not_block_ready_peer() -> None:
    clock = SimulatedClock(START)
    scheduler = Scheduler(capacity=2, reservations={}, clock=clock)
    scheduler.enqueue(
        task("not-ready", clock, deadline_hours=2, scheduled_hours=1)
    )
    scheduler.enqueue(task("ready", clock, deadline_hours=3))

    assert scheduler.next_task().task_id == "ready"


def test_deadline_update_reorders_edf_queue() -> None:
    clock = SimulatedClock(START)
    scheduler = Scheduler(capacity=2, reservations={}, clock=clock)
    scheduler.enqueue(task("first-before-update", clock, deadline_hours=2))
    scheduler.enqueue(task("new-first", clock, deadline_hours=4))

    scheduler.update_deadline("new-first", START + timedelta(hours=1))
    assert scheduler.next_task().task_id == "new-first"


def test_cancel_is_lazy_and_does_not_dispatch_cancelled_heap_entry() -> None:
    clock = SimulatedClock(START)
    scheduler = Scheduler(clock=clock)
    scheduler.enqueue(task("cancelled", clock))
    assert scheduler.cancel("cancelled")
    assert not scheduler.cancel("cancelled")
    assert scheduler.next_task() is None


def test_standard_task_ages_ahead_of_far_deadline_interactive_work() -> None:
    clock = SimulatedClock(START)
    scheduler = Scheduler(capacity=1, reservations={}, clock=clock)
    scheduler.enqueue(
        task(
            "far-interactive",
            clock,
            priority=INTERACTIVE,
            deadline_hours=8,
        )
    )
    near = task(
        "near-standard",
        clock,
        priority=STANDARD,
        deadline_hours=0.5,
    )
    scheduler.enqueue(near)

    assert scheduler.next_task() is near
    assert near.priority_class == INTERACTIVE
    assert any(event.get("reason") == "deadline-escalation" for event in near.transitions)
