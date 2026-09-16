from collections import Counter

import pytest

from intent_scheduler.simulation import (
    CAPACITY,
    CONTRACT_FIFO,
    HUMAN_WAITING_PERSONAS,
    INTENT_AWARE,
    STATIC,
    simulate_pipeline,
    summaries,
    transition_probe,
)
from intent_scheduler.workload import generate_workload


def test_varied_workload_is_deterministic_and_preserves_task_mix():
    items = generate_workload()
    assert items == generate_workload()
    assert len({item.id for item in items}) == len(items) == 72
    assert Counter(item.persona for item in items) == {
        "urgent-executive": 8,
        "interactive-collaborator": 24,
        "overnight-researcher": 8,
        "casual-drafter": 32,
    }
    assert Counter(item.deadline_hours for item in items) == {
        0.5: 8, 2.0: 8, 2.5: 8, 3.0: 8, 18.0: 16, 19.0: 8, 20.0: 8, 21.0: 8,
    }
    arrivals = Counter(round(item.arrival_hour * 60) for item in items)
    assert list(arrivals) == [540, 580, 640, 740, 770, 820, 950, 1010]
    assert list(arrivals.values()) == [7, 12, 12, 6, 13, 10, 6, 6]
    assert generate_workload(3) == items[:31]
    extended = generate_workload(9)
    assert extended[:72] == items
    assert len(extended) == 79
    assert extended[72].arrival_hour == 19.0
    with pytest.raises(ValueError, match="bursts must be positive"):
        generate_workload(0)


@pytest.mark.asyncio
async def test_demo_reports_emergent_queue_cost_and_utilization_properties():
    items = generate_workload()
    static, _, static_accounting, static_trace = await simulate_pipeline(
        items, mode=STATIC
    )
    fifo, fifo_util, fifo_accounting, fifo_trace = await simulate_pipeline(
        items, mode=CONTRACT_FIFO
    )
    aware, aware_util, aware_accounting, aware_trace = await simulate_pipeline(
        items, mode=INTENT_AWARE
    )
    static_summary = summaries(static)
    fifo_summary = summaries(fifo)
    aware_summary = summaries(aware)

    assert len(static) == len(fifo) == len(aware) == len(items) == 72
    # Both baselines must preserve arrival order even when deadlines approach.
    for outcomes in (static, fifo):
        by_arrival = sorted(outcomes, key=lambda o: o.item.id)
        assert [o.started_at for o in by_arrival] == sorted(o.started_at for o in outcomes)
        assert all(o.priority == "standard" and "promoted" not in o.events for o in outcomes)
    assert all(
        o.priority == "interactive"
        for o in aware
        if o.item.persona in HUMAN_WAITING_PERSONAS
    )
    # Cost equality alone would not rule out different execution configurations.
    def execution_choices(outcomes):
        return {
            o.item.id: (o.model, o.effort, o.template, o.modeled_execution_cost_usd)
            for o in outcomes
        }

    assert execution_choices(fifo) == execution_choices(aware)
    assert aware_util["valley"] > fifo_util["valley"]
    assert aware_summary["urgent-executive"]["deadline_pct"] == 100
    assert fifo_summary["urgent-executive"]["deadline_pct"] < 100
    assert aware_summary["patient"]["deadline_pct"] == 100
    assert fifo_summary["patient"]["deadline_pct"] == 100
    assert (
        aware_summary["human-waiting"]["wait_p95"]
        < fifo_summary["human-waiting"]["wait_p95"]
    )
    assert aware_summary["overall"]["deadline_pct"] == 100
    assert (
        aware_summary["overall"]["modeled_execution_cost"]
        == fifo_summary["overall"]["modeled_execution_cost"]
    )
    assert (
        aware_summary["overall"]["modeled_execution_cost"]
        < static_summary["overall"]["modeled_execution_cost"]
    )
    assert max(point.ready_queue for point in fifo_trace) > 0
    assert all(
        point.ready_human_waiting + point.ready_patient == point.ready_queue
        for point in fifo_trace + aware_trace
    )
    assert max(point.ready_human_waiting for point in aware_trace) < max(
        point.ready_human_waiting for point in fifo_trace
    )
    assert aware_accounting["overhead_calls"] > 0
    assert fifo_accounting["overhead_calls"] > 0
    assert static_accounting["overhead_calls"] > 0

    # Independently reconstruct diagnostic allocation from task start/end times.
    for outcomes, trace in ((static, static_trace), (fifo, fifo_trace), (aware, aware_trace)):
        for point in trace:
            active = [o for o in outcomes if o.started_at <= point.instant < o.completed_at]
            expected = {True: 0, False: 0}
            for outcome in active:
                units = 3 if outcome.template == "parallel-drafts+judge" else 1
                expected[outcome.item.persona in HUMAN_WAITING_PERSONAS] += units
            assert point.allocated_human_waiting == expected[True]
            assert point.allocated_patient == expected[False]
            assert point.in_flight == sum(expected.values()) <= CAPACITY


@pytest.mark.asyncio
async def test_demo_exercises_revealed_intent_and_deadline_escalation():
    nudge, escalation = await transition_probe()
    assert nudge == "human-blocked-now"
    assert escalation == "deadline-escalation"
