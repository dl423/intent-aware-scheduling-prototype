"""Run the gateway on a fixed workload with a simulated clock and mock provider."""

from __future__ import annotations

import asyncio
import html
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .clock import SimulatedClock
from .config import Settings
from .execution import ExecutionResult
from .gateway import Gateway
from .models import (
    AskPolicy,
    DecisionRecord,
    ExecutionTemplate,
    FleetState,
    ModelTier,
    PriorityClass,
    ReasoningEffort,
    TaskContract,
    WorkEstimate,
)
from .policy import MODEL_LADDER, PolicyEngine, estimate_cost
from .providers import LUNA_MODEL, MockProvider, TERRA_MODEL
from .scheduler import ScheduledTask, Scheduler
from .store import TaskStore
from .workload import DEFAULT_BURSTS, WorkloadItem, absolute_times, generate_workload


START = datetime(2026, 7, 12, tzinfo=timezone.utc)
STEP = timedelta(minutes=10)
CAPACITY = 3
STATIC = "static"
CONTRACT_FIFO = "contract-fifo"
INTENT_AWARE = "intent-aware"
RUN_MODES = (STATIC, CONTRACT_FIFO, INTENT_AWARE)

# Assumed duration of one provider-call stage, before adjustment for reasoning
# effort. All configurations use these values. Drafts generated in parallel
# share one stage because their durations overlap.
ASSUMED_MODEL_STAGE_HOURS = {
    LUNA_MODEL: 0.12,
    TERRA_MODEL: 0.25,
}
ASSUMED_EFFORT_MULTIPLIER = {
    "low": 0.8,
    "medium": 1.0,
    "high": 1.3,
}


@dataclass
class Outcome:
    item: WorkloadItem
    submitted_at: datetime
    started_at: datetime
    completed_at: datetime
    deadline: datetime
    cost_usd: float
    task_calls: int
    overhead_calls: int
    task_cost_usd: float
    overhead_cost_usd: float
    modeled_execution_cost_usd: float
    model: str
    effort: str
    template: str
    priority: str
    events: list[str]

    @property
    def queue_wait_hours(self) -> float:
        return (self.started_at - self.submitted_at).total_seconds() / 3600

    @property
    def deadline_met(self) -> bool:
        return self.completed_at <= self.deadline


HUMAN_WAITING_PERSONAS = frozenset({"urgent-executive", "interactive-collaborator"})


@dataclass(frozen=True)
class TracePoint:
    instant: datetime
    in_flight: int
    ready_queue: int
    ready_human_waiting: int = 0
    ready_patient: int = 0
    allocated_human_waiting: int = 0
    allocated_patient: int = 0


class StaticBaselinePolicy:
    """Intent-blind Terra/medium/self-check policy used with FIFO queueing."""

    def decide(
        self,
        contract: TaskContract,
        fleet: FleetState,
        work: WorkEstimate | None = None,
    ) -> DecisionRecord:
        template = ExecutionTemplate.SELF_CHECK
        return DecisionRecord(
            model_tier=ModelTier.TERRA,
            model_name=MODEL_LADDER[ModelTier.TERRA].api_name,
            reasoning_effort=ReasoningEffort.MEDIUM,
            priority_class=PriorityClass.STANDARD,
            execution_template=template,
            parallel_drafts=1,
            scheduled_at=fleet.now,
            ask_policy=AskPolicy.ONE_BATCHED_CHECKPOINT,
            estimated_cost_usd=estimate_cost(ModelTier.TERRA, template, work),
            policy_rule="intent-blind-static",
        )


class ContractFifoPolicy:
    """Contract-selected execution with one immediate FIFO scheduling class."""

    def __init__(self) -> None:
        self.contract_policy = PolicyEngine()

    def decide(
        self,
        contract: TaskContract,
        fleet: FleetState,
        work: WorkEstimate | None = None,
    ) -> DecisionRecord:
        decision = self.contract_policy.decide(contract, fleet, work)
        return decision.model_copy(
            update={
                "priority_class": PriorityClass.STANDARD,
                "scheduled_at": fleet.now,
                "policy_rule": f"{decision.policy_rule}; scheduling=fifo-immediate",
            }
        )


def assumed_service_duration(result: ExecutionResult) -> timedelta:
    """Calculate simulated service duration from task and candidate-selection calls."""
    effort = ASSUMED_EFFORT_MULTIPLIER[result.metrics.reasoning_effort]
    task_stage = ASSUMED_MODEL_STAGE_HOURS[result.metrics.model] * effort
    if result.metrics.template == ExecutionTemplate.SELF_CHECK.value:
        task_stage *= 2
    # Parallel drafts execute concurrently, so they remain one task stage.
    selection_stage = sum(
        ASSUMED_MODEL_STAGE_HOURS[response.model] * ASSUMED_EFFORT_MULTIPLIER["low"]
        for response in result.provider_responses
        if response.role == "candidate_selection"
    )
    return timedelta(hours=task_stage + selection_stage)


def _settings() -> Settings:
    return Settings(
        provider="mock",
        database_path=Path(":memory:"),
        capacity=CAPACITY,
        intent_model=LUNA_MODEL,
        intent_fallback_model=TERRA_MODEL,
        candidate_model=LUNA_MODEL,
        candidate_fallback_model=TERRA_MODEL,
        judge_model=LUNA_MODEL,
        judge_fallback_model=TERRA_MODEL,
        scheduler_interval_seconds=0.1,
    )


def _explicit(item: WorkloadItem) -> dict[str, Any]:
    arrival, deadline = absolute_times(item, START)
    return {
        "deadline": deadline,
        "interactive": item.persona in HUMAN_WAITING_PERSONAS,
        "quality_floor": item.quality,
        "max_cost_usd": item.max_cost_usd,
        "attention_profile": item.attention,
    }


async def simulate_pipeline(
    items: list[WorkloadItem],
    *,
    mode: str,
) -> tuple[list[Outcome], dict[str, float], dict[str, Any], list[TracePoint]]:
    """Drive Gateway, policy, scheduler, execution, and mock provider together."""

    if mode not in RUN_MODES:
        raise ValueError(f"mode must be one of {RUN_MODES}")
    clock = SimulatedClock(START)
    provider = MockProvider()
    if mode == STATIC:
        policy: Any | None = StaticBaselinePolicy()
    elif mode == CONTRACT_FIFO:
        policy = ContractFifoPolicy()
    else:
        policy = None
    scheduler = Scheduler(
        capacity=CAPACITY,
        clock=clock,
        reservations={
            PriorityClass.INTERACTIVE.value: 0,
            PriorityClass.STANDARD.value: 0,
            PriorityClass.DEFERRED_BATCH.value: 0,
        },
        queue_discipline="edf" if mode == INTENT_AWARE else "fifo",
        enable_escalation=mode == INTENT_AWARE,
    )
    gateway = Gateway(
        _settings(),
        provider=provider,
        store=TaskStore(":memory:"),
        clock=clock,
        policy=policy,
        scheduler=scheduler,
        auto_execute=False,
    )
    arrivals = deque(sorted(items, key=lambda item: item.arrival_hour))
    by_id: dict[str, WorkloadItem] = {}
    ids_by_workload: dict[str, str] = {}
    running: dict[str, tuple[datetime, ScheduledTask, ExecutionResult]] = {}
    outcomes: list[Outcome] = []
    utilization: dict[str, float] = defaultdict(float)
    trace: list[TracePoint] = []
    nudged: set[str] = set()
    end = START + timedelta(
        hours=max(item.arrival_hour + item.deadline_hours for item in items) + 48
    )

    try:
        while (arrivals or gateway.scheduler.queued_count or running) and clock.now() <= end:
            now = clock.now()
            for task_id, (finished, scheduled, result) in list(running.items()):
                if finished > now:
                    continue
                gateway.finalize_dispatched(scheduled, result)
                record = gateway.get(task_id)
                item = by_id[task_id]
                accounting = record["accounting"]
                outcomes.append(
                    Outcome(
                        item=item,
                        submitted_at=datetime.fromisoformat(record["submitted_at"]),
                        started_at=scheduled.started_at or now,
                        completed_at=now,
                        deadline=datetime.fromisoformat(record["contract"]["deadline"]),
                        cost_usd=float(accounting["total_cost_usd"]),
                        task_calls=int(accounting["task_calls"]),
                        overhead_calls=int(accounting["overhead_calls"]),
                        task_cost_usd=float(accounting["task_cost_usd"]),
                        overhead_cost_usd=float(accounting["overhead_cost_usd"]),
                        modeled_execution_cost_usd=float(
                            record["decision"]["estimated_cost_usd"]
                        ),
                        model=record["metrics"]["model"],
                        effort=record["metrics"]["reasoning_effort"],
                        template=record["metrics"]["template"],
                        priority=record["effective_priority"],
                        events=[event["event"] for event in record["transitions"]],
                    )
                )
                del running[task_id]

            while arrivals and START + timedelta(hours=arrivals[0].arrival_hour) <= now:
                item = arrivals.popleft()
                record = await gateway.submit(item.request_text, _explicit(item))
                by_id[record["id"]] = item
                ids_by_workload[item.id] = record["id"]

            for item in items:
                if item.nudge_after_hours is None or item.id in nudged:
                    continue
                signal_at = START + timedelta(hours=item.arrival_hour + item.nudge_after_hours)
                task_id = ids_by_workload.get(item.id)
                task = gateway.scheduler.get(task_id) if task_id else None
                if task and task.state == "queued" and now >= signal_at:
                    await gateway.nudge(task_id, "Any update?")
                    nudged.add(item.id)

            for scheduled in await gateway.tick():
                result = await gateway.run_dispatched(scheduled)
                running[scheduled.task_id] = (
                    now + assumed_service_duration(result),
                    scheduled,
                    result,
                )

            utilization[_period(now)] += (
                gateway.scheduler.in_flight_count * STEP.total_seconds() / 3600
            )
            ready_ids = gateway.scheduler.ready_task_ids
            ready_human_waiting = sum(
                by_id[task_id].persona in HUMAN_WAITING_PERSONAS for task_id in ready_ids
            )
            # The simulator holds a task's peak call capacity until machine
            # completion, including candidate selection. This is allocated
            # capacity, not stage-by-stage provider or hardware utilization.
            allocated_human_waiting = sum(
                scheduled.capacity_units
                for task_id, (_, scheduled, _) in running.items()
                if by_id[task_id].persona in HUMAN_WAITING_PERSONAS
            )
            trace.append(
                TracePoint(
                    instant=now,
                    in_flight=gateway.scheduler.in_flight_count,
                    ready_queue=len(ready_ids),
                    ready_human_waiting=ready_human_waiting,
                    ready_patient=len(ready_ids) - ready_human_waiting,
                    allocated_human_waiting=allocated_human_waiting,
                    allocated_patient=(
                        gateway.scheduler.in_flight_count - allocated_human_waiting
                    ),
                )
            )
            clock.advance(STEP)

        denominators = _period_capacity_hours(START, clock.now(), CAPACITY)
        rates = {
            period: utilization[period] / denominators[period] if denominators[period] else 0.0
            for period in ("peak", "valley", "shoulder")
        }
        run_accounting = {
            key: sum(getattr(outcome, key) for outcome in outcomes)
            for key in (
                "task_calls",
                "overhead_calls",
                "task_cost_usd",
                "overhead_cost_usd",
                "modeled_execution_cost_usd",
                "cost_usd",
            )
        }
        return outcomes, rates, run_accounting, trace
    finally:
        await gateway.close()


async def transition_probe() -> tuple[str, str]:
    """Exercise nudge revision and deadline aging through Gateway and Scheduler."""
    clock = SimulatedClock(START)
    gateway = Gateway(
        _settings(),
        provider=MockProvider(),
        store=TaskStore(":memory:"),
        clock=clock,
        auto_execute=False,
    )
    try:
        nudged = await gateway.submit(
            "Prepare a high quality note by tonight.",
            {
                "quality_floor": "high",
                "deadline": START + timedelta(hours=8),
                "max_cost_usd": 2,
                "attention_profile": "ask-freely",
            },
        )
        await gateway.nudge(nudged["id"], "Any update?")
        nudge_reason = gateway.get(nudged["id"])["transitions"][-1]["reason"]

        aging = await gateway.submit(
            "Prepare an overnight research note.",
            {
                "quality_floor": "high",
                "deadline": START + timedelta(hours=8),
                "max_cost_usd": 2,
                "attention_profile": "batch-questions",
            },
        )
        clock.advance(hours=7, minutes=5)
        gateway.scheduler.escalate()
        aging_events = gateway.get(aging["id"])["transitions"]
        escalation_reason = next(
            event["reason"]
            for event in reversed(aging_events)
            if event["event"] == "promoted"
        )
        return nudge_reason, escalation_reason
    finally:
        await gateway.close()


def _period(instant: datetime) -> str:
    if 9 <= instant.hour < 18:
        return "peak"
    if 0 <= instant.hour < 6:
        return "valley"
    return "shoulder"


def _period_capacity_hours(start: datetime, end: datetime, capacity: int) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    cursor = start
    while cursor < end:
        totals[_period(cursor)] += capacity * STEP.total_seconds() / 3600
        cursor += STEP
    return totals


def _percentile(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round((len(ordered) - 1) * percentile))
    return ordered[index]


def summaries(outcomes: list[Outcome]) -> dict[str, dict[str, float]]:
    groups: dict[str, list[Outcome]] = defaultdict(list)
    groups["overall"] = outcomes
    for outcome in outcomes:
        groups[outcome.item.persona].append(outcome)
        if outcome.item.persona in HUMAN_WAITING_PERSONAS:
            groups["human-waiting"].append(outcome)
        else:
            groups["patient"].append(outcome)
    result = {}
    for name, group in groups.items():
        waits = [outcome.queue_wait_hours for outcome in group]
        result[name] = {
            "tasks": len(group),
            "deadline_pct": 100 * sum(outcome.deadline_met for outcome in group) / len(group),
            "cost": sum(outcome.cost_usd for outcome in group),
            "wait_p50": median(waits),
            "wait_p95": _percentile(waits, 0.95),
            "task_calls": sum(outcome.task_calls for outcome in group),
            "overhead_calls": sum(outcome.overhead_calls for outcome in group),
            "task_cost": sum(outcome.task_cost_usd for outcome in group),
            "overhead_cost": sum(outcome.overhead_cost_usd for outcome in group),
            "modeled_execution_cost": sum(
                outcome.modeled_execution_cost_usd for outcome in group
            ),
        }
    return result


def render_report(
    outcomes: dict[str, list[Outcome]],
    utilization: dict[str, dict[str, float]],
    accounting: dict[str, dict[str, Any]],
    traces: dict[str, list[TracePoint]],
) -> str:
    labels = {
        STATIC: "static",
        CONTRACT_FIFO: "contract+FIFO",
        INTENT_AWARE: "intent-aware",
    }
    rows = []
    for mode in RUN_MODES:
        summary = summaries(outcomes[mode])
        rows.append(
            f"{labels[mode]:15} deadlines all {summary['overall']['deadline_pct']:5.1f}%  "
            f"urgent {summary['urgent-executive']['deadline_pct']:5.1f}%  "
            f"patient {summary['patient']['deadline_pct']:5.1f}% | "
            f"wait p95 human {summary['human-waiting']['wait_p95']:4.2f}h  "
            f"patient {summary['patient']['wait_p95']:5.2f}h | "
            f"modeled execution ${summary['overall']['modeled_execution_cost']:.4f}"
        )
    detail = []
    for mode in RUN_MODES:
        detail.append(
            f"{labels[mode]:15} max ready queue {max(point.ready_queue for point in traces[mode]):2d}  "
            f"peak utilization {utilization[mode]['peak']:.1%}  "
            f"valley utilization {utilization[mode]['valley']:.1%}  "
            f"calls task {accounting[mode]['task_calls']:.0f} overhead {accounting[mode]['overhead_calls']:.0f}"
        )
    return "\n".join(
        [
            "Deterministic congested case study",
            "The contract+FIFO and intent-aware runs use identical model, effort, and execution choices.",
            "Service time and execution cost use the documented fixed assumptions.",
            "Deadlines end at machine completion; output quality and requester attention are not measured.",
            "",
            *rows,
            "",
            *detail,
        ]
    )


async def collect_case_study(
    bursts: int = DEFAULT_BURSTS,
) -> tuple[
    str,
    dict[str, list[Outcome]],
    dict[str, list[TracePoint]],
]:
    items = generate_workload(bursts)
    outcomes: dict[str, list[Outcome]] = {}
    utilization: dict[str, dict[str, float]] = {}
    accounting: dict[str, dict[str, Any]] = {}
    traces: dict[str, list[TracePoint]] = {}
    for mode in RUN_MODES:
        run_outcomes, run_utilization, run_accounting, run_trace = await simulate_pipeline(
            items, mode=mode
        )
        outcomes[mode] = run_outcomes
        utilization[mode] = run_utilization
        accounting[mode] = run_accounting
        traces[mode] = run_trace
    report = render_report(outcomes, utilization, accounting, traces)
    return report, outcomes, traces


async def run_case_study(bursts: int = DEFAULT_BURSTS) -> str:
    report, _, _ = await collect_case_study(bursts)
    return report


def run_demo(
    bursts: int = DEFAULT_BURSTS,
    html_path: str | None = None,
) -> str:
    report, _, _ = asyncio.run(collect_case_study(bursts))
    if html_path:
        Path(html_path).write_text(
            "<!doctype html><meta charset='utf-8'><title>Scheduler evaluation</title>"
            f"<pre>{html.escape(report)}</pre>",
            encoding="utf-8",
        )
    return report
