"""Queue tasks according to their contracts and policy decisions.

The scheduler manages priority classes, start eligibility, and capacity. Prompt
construction and provider calls are handled by other gateway components.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import count
from threading import RLock
from typing import Any, Callable, Mapping

from .clock import Clock, SystemClock
from .models import AdmissionResult, Provenance


INTERACTIVE = "interactive"
STANDARD = "standard"
DEFERRED_BATCH = "deferred-batch"
PRIORITY_ORDER = (INTERACTIVE, STANDARD, DEFERRED_BATCH)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("scheduler datetimes must be timezone-aware")
    return value.astimezone(timezone.utc)


def _class_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    normalized = str(raw).lower().replace("_", "-")
    aliases = {"batch": DEFERRED_BATCH, "deferred": DEFERRED_BATCH}
    normalized = aliases.get(normalized, normalized)
    if normalized not in PRIORITY_ORDER:
        raise ValueError(f"unknown priority class: {value!r}")
    return normalized


def _updated(model: Any, **changes: Any) -> Any:
    """Copy a dataclass or Pydantic model without coupling to either library."""

    if hasattr(model, "model_copy"):
        return model.model_copy(update=changes)
    try:
        return replace(model, **changes)
    except (TypeError, ValueError):
        for key, value in changes.items():
            setattr(model, key, value)
        return model


@dataclass
class ScheduledTask:
    """A task's contract, policy decision, and scheduling state."""

    task_id: str
    contract: Any
    decision: Any
    enqueued_at: datetime | None = None
    spent_cost_usd: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    effective_priority: str | None = None
    state: str = "new"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    transitions: list[dict[str, Any]] = field(default_factory=list)
    _generation: int = field(default=0, repr=False)
    _queue_order: int | None = field(default=None, repr=False)

    @property
    def deadline(self) -> datetime:
        return _utc(self.contract.deadline)

    @property
    def priority_class(self) -> str:
        return self.effective_priority or _class_value(self.decision.priority_class)

    @property
    def scheduled_at(self) -> datetime | None:
        value = getattr(self.decision, "scheduled_at", None)
        return _utc(value) if value is not None else None

    @property
    def estimated_cost_usd(self) -> float:
        return float(getattr(self.decision, "estimated_cost_usd", 0.0))

    @property
    def capacity_units(self) -> int:
        """Peak concurrent provider calls required by the execution template."""

        raw_template = getattr(self.decision, "execution_template", "")
        template = str(getattr(raw_template, "value", raw_template))
        if template == "parallel-drafts+judge":
            return max(1, int(getattr(self.decision, "parallel_drafts", 1)))
        return 1

    @property
    def remaining_budget_usd(self) -> float:
        cap = getattr(self.contract, "max_cost_usd", None)
        return math.inf if cap is None else max(0.0, float(cap) - self.spent_cost_usd)


class CostBudgetExceeded(ValueError):
    """Raised when policy's estimated task cost exceeds the user's cap."""


class Scheduler:
    """Schedule tasks by priority class and, by default, earliest deadline.

    Class reservations protect slots for classes with eligible queued work.
    When a class has no eligible tasks, other classes can use those slots.
    Running calls are not preempted, so reservations affect subsequent
    dispatches. Queued tasks can move to a higher class as deadlines approach.
    """

    def __init__(
        self,
        capacity: int = 6,
        *,
        reservations: Mapping[str, int] | None = None,
        escalation_window: timedelta = timedelta(hours=1),
        deferred_load_threshold: int | float = 0.5,
        clock: Clock | None = None,
        queue_discipline: str = "edf",
        enable_escalation: bool = True,
        decision_hook: Callable[[ScheduledTask], Any] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        supplied = (
            reservations
            if reservations is not None
            else {INTERACTIVE: 1, STANDARD: int(capacity > 1), DEFERRED_BATCH: 0}
        )
        self.reservations = {name: 0 for name in PRIORITY_ORDER}
        for name, amount in supplied.items():
            normalized = _class_value(name)
            if amount < 0:
                raise ValueError("reservations cannot be negative")
            self.reservations[normalized] = int(amount)
        if sum(self.reservations.values()) > capacity:
            raise ValueError("reservations cannot exceed global capacity")
        if escalation_window < timedelta(0):
            raise ValueError("escalation window cannot be negative")
        self.escalation_window = escalation_window
        if isinstance(deferred_load_threshold, float):
            if not 0 < deferred_load_threshold <= 1:
                raise ValueError("fractional deferred threshold must be in (0, 1]")
            self.deferred_limit = max(1, math.ceil(capacity * deferred_load_threshold))
        else:
            if not 1 <= deferred_load_threshold <= capacity:
                raise ValueError("deferred threshold must be between 1 and capacity")
            self.deferred_limit = deferred_load_threshold
        self.clock = clock or SystemClock()
        if queue_discipline not in {"edf", "fifo"}:
            raise ValueError("queue_discipline must be 'edf' or 'fifo'")
        self.queue_discipline = queue_discipline
        self.enable_escalation = enable_escalation
        self.decision_hook = decision_hook
        self._queues: dict[str, list[tuple[float, int, int, str]]] = {
            name: [] for name in PRIORITY_ORDER
        }
        self._tasks: dict[str, ScheduledTask] = {}
        self._in_flight: dict[str, ScheduledTask] = {}
        self._sequence = count()
        self._lock = RLock()

    @property
    def in_flight_count(self) -> int:
        return sum(task.capacity_units for task in self._in_flight.values())

    @property
    def queued_count(self) -> int:
        return sum(task.state == "queued" for task in self._tasks.values())

    @property
    def ready_queue_count(self) -> int:
        """Return queued tasks that are eligible to dispatch at the current time."""

        return len(self.ready_task_ids)

    @property
    def ready_task_ids(self) -> list[str]:
        """Return ids of queued tasks that are eligible to dispatch at the current time."""

        now = self.clock.now()
        return [
            task.task_id
            for task in self._tasks.values()
            if task.state == "queued" and self._ready(task, now)
        ]

    @property
    def load(self) -> float:
        return self.in_flight_count / self.capacity

    def get(self, task_id: str) -> ScheduledTask | None:
        return self._tasks.get(task_id)

    def admit(self, task: ScheduledTask) -> AdmissionResult:
        """Check the contract budget without mutating scheduler state."""

        estimate = Decimal(str(task.estimated_cost_usd))
        cap = Decimal(str(getattr(task.contract, "max_cost_usd")))
        remaining = Decimal(str(task.remaining_budget_usd))
        if task.estimated_cost_usd < 0:
            return AdmissionResult(
                admitted=False,
                estimated_cost_usd=max(Decimal(0), estimate),
                cost_cap_usd=cap,
                reason="estimated cost cannot be negative",
            )
        if task.estimated_cost_usd > task.remaining_budget_usd + 1e-12:
            return AdmissionResult(
                admitted=False,
                estimated_cost_usd=estimate,
                cost_cap_usd=cap,
                reason=(
                    f"estimated cost ${task.estimated_cost_usd:.4f} exceeds remaining "
                    f"budget ${task.remaining_budget_usd:.4f}"
                ),
            )
        return AdmissionResult(
            admitted=True,
            estimated_cost_usd=estimate,
            cost_cap_usd=cap,
            reason=f"estimated cost fits with ${remaining:.4f} remaining",
        )

    def enqueue(self, task: ScheduledTask) -> ScheduledTask:
        """Admit and enqueue a task, raising on duplicate IDs or budget failure."""

        with self._lock:
            if task.task_id in self._tasks and self._tasks[task.task_id].state not in {
                "cancelled",
                "completed",
                "rejected",
            }:
                raise ValueError(f"task {task.task_id!r} is already active")
            admission = self.admit(task)
            if not admission.admitted:
                task.state = "rejected"
                task.transitions.append(
                    {"at": self.clock.now(), "event": "rejected", "reason": admission.reason}
                )
                self._tasks[task.task_id] = task
                raise CostBudgetExceeded(admission.reason)
            now = self.clock.now()
            task.enqueued_at = task.enqueued_at or now
            if task._queue_order is None:
                task._queue_order = next(self._sequence)
            task.state = "queued"
            task.effective_priority = task.priority_class
            task.transitions.append({"at": now, "event": "enqueued", "class": task.priority_class})
            self._tasks[task.task_id] = task
            self._push(task)
            return task

    # Naming used by some gateway implementations.
    submit = enqueue

    def _push(self, task: ScheduledTask) -> None:
        task._generation += 1
        sequence = next(self._sequence)
        order = (
            task.deadline.timestamp()
            if self.queue_discipline == "edf"
            else float(task._queue_order or 0)
        )
        heapq.heappush(
            self._queues[task.priority_class],
            (order, sequence, task._generation, task.task_id),
        )

    def _valid_entry(self, class_name: str, generation: int, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        return bool(
            task
            and task.state == "queued"
            and task.priority_class == class_name
            and task._generation == generation
        )

    def _ready(self, task: ScheduledTask, now: datetime) -> bool:
        return task.scheduled_at is None or task.scheduled_at <= now

    def _has_ready(self, class_name: str, now: datetime) -> bool:
        return any(
            self._valid_entry(class_name, generation, task_id)
            and self._ready(self._tasks[task_id], now)
            for _, _, generation, task_id in self._queues[class_name]
        )

    def _pop_ready(self, class_name: str, now: datetime) -> ScheduledTask | None:
        queue = self._queues[class_name]
        deferred: list[tuple[float, int, int, str]] = []
        selected: ScheduledTask | None = None
        while queue:
            entry = heapq.heappop(queue)
            _, _, generation, task_id = entry
            if not self._valid_entry(class_name, generation, task_id):
                continue
            task = self._tasks[task_id]
            if self._ready(task, now):
                selected = task
                break
            deferred.append(entry)
        for entry in deferred:
            heapq.heappush(queue, entry)
        return selected

    def _can_use_slot(self, class_name: str, now: datetime, units: int) -> bool:
        if units > self.capacity or self.in_flight_count + units > self.capacity:
            return False
        if class_name == DEFERRED_BATCH and self.in_flight_count >= self.deferred_limit:
            return False
        remaining_after = self.capacity - self.in_flight_count - units
        protected = 0
        counts = self.in_flight_by_class()
        for other in PRIORITY_ORDER:
            if other == class_name or not self._has_ready(other, now):
                continue
            protected += max(0, self.reservations[other] - counts[other])
        return remaining_after >= protected

    def escalate(self) -> list[ScheduledTask]:
        """Age queued work upward as its deadline enters the urgency window."""

        with self._lock:
            now = self.clock.now()
            promoted: list[ScheduledTask] = []
            for task in list(self._tasks.values()):
                if task.state != "queued":
                    continue
                if task.priority_class == STANDARD and task.deadline - now <= self.escalation_window:
                    target = INTERACTIVE
                elif task.priority_class == DEFERRED_BATCH and task.deadline <= now:
                    target = INTERACTIVE
                elif (
                    task.priority_class == DEFERRED_BATCH
                    and task.deadline - now <= self.escalation_window
                ):
                    target = STANDARD
                else:
                    continue
                self._promote(task, target, "deadline-escalation", now)
                promoted.append(task)
            return promoted

    def _promote(self, task: ScheduledTask, target: str, reason: str, now: datetime) -> None:
        old = task.priority_class
        target = _class_value(target)
        if PRIORITY_ORDER.index(target) >= PRIORITY_ORDER.index(old):
            return
        task.effective_priority = target
        # Promotion overrides a future low-load window. The task is now ready.
        if getattr(task.decision, "scheduled_at", None) is not None:
            task.decision = _updated(task.decision, scheduled_at=now)
        task.transitions.append(
            {"at": now, "event": "promoted", "from": old, "to": target, "reason": reason}
        )
        self._push(task)

    def nudge(self, task_id: str) -> ScheduledTask:
        """Revise the contract with revealed human-blocked-now intent."""

        with self._lock:
            task = self._tasks[task_id]
            provenance = getattr(task.contract, "provenance", None)
            if provenance is not None and hasattr(provenance, "interactive"):
                provenance = _updated(provenance, interactive=Provenance.REVEALED)
                task.contract = _updated(
                    task.contract,
                    interactive=True,
                    provenance=provenance,
                )
            else:
                task.contract = _updated(task.contract, interactive=True)
            old = task.priority_class
            task.transitions.append(
                {
                    "at": self.clock.now(),
                    "event": "revealed-intent",
                    "from": old,
                    "to": INTERACTIVE,
                    "reason": "user-nudge",
                }
            )
            if task.state == "in-flight":
                task.effective_priority = INTERACTIVE
            return task

    promote = nudge

    def update_deadline(self, task_id: str, deadline: datetime) -> ScheduledTask:
        with self._lock:
            deadline = _utc(deadline)
            task = self._tasks[task_id]
            provenance = getattr(task.contract, "provenance", None)
            if provenance is not None and hasattr(provenance, "deadline"):
                explicit = type(provenance.deadline)("explicit")
                provenance = _updated(provenance, deadline=explicit)
                task.contract = _updated(
                    task.contract, deadline=deadline, provenance=provenance
                )
            else:
                task.contract = _updated(task.contract, deadline=deadline)
            task.transitions.append(
                {"at": self.clock.now(), "event": "deadline-updated", "deadline": deadline}
            )
            if task.state == "queued":
                self._push(task)
                self.escalate()
            return task

    def update_decision(
        self,
        task_id: str,
        decision: Any,
        *,
        event: str = "replanned",
        reason: str | None = None,
    ) -> ScheduledTask:
        """Install a fresh deterministic policy decision for queued work."""

        with self._lock:
            task = self._tasks[task_id]
            if task.state != "queued":
                return task
            old = task.priority_class
            task.decision = decision
            task.effective_priority = _class_value(decision.priority_class)
            task.transitions.append(
                {
                    "at": self.clock.now(),
                    "event": event,
                    "from": old,
                    "to": task.priority_class,
                    "reason": reason,
                }
            )
            self._push(task)
            return task

    def next_task(self) -> ScheduledTask | None:
        """Reserve the task's peak call capacity and return the next runnable task."""

        with self._lock:
            if self.enable_escalation:
                self.escalate()
            now = self.clock.now()
            for class_name in PRIORITY_ORDER:
                while self._has_ready(class_name, now):
                    task = self._pop_ready(class_name, now)
                    if task is None:
                        break
                    prior_decision = task.decision
                    if self.decision_hook is not None:
                        task.decision = self.decision_hook(task)
                    admission = self.admit(task)
                    if not admission.admitted or task.capacity_units > self.capacity:
                        reason = admission.reason if not admission.admitted else (
                            f"task requires {task.capacity_units} slots but capacity is {self.capacity}"
                        )
                        task.state = "rejected"
                        task.transitions.append({"at": now, "event": "rejected", "reason": reason})
                        continue
                    if not self._can_use_slot(class_name, now, task.capacity_units):
                        task.decision = prior_decision
                        self._push(task)
                        break
                    task.state = "in-flight"
                    task.started_at = now
                    if self.decision_hook is not None:
                        task.transitions.append(
                            {
                                "at": now,
                                "event": "dispatch-redecision",
                                "before": prior_decision,
                                "after": task.decision,
                            }
                        )
                    task.transitions.append({"at": now, "event": "dispatched", "class": class_name})
                    self._in_flight[task.task_id] = task
                    return task
            return None

    dispatch = next_task

    def dispatch_ready(self) -> list[ScheduledTask]:
        dispatched: list[ScheduledTask] = []
        while self.in_flight_count < self.capacity:
            task = self.next_task()
            if task is None:
                break
            dispatched.append(task)
        return dispatched

    def complete(self, task_id: str, *, actual_cost_usd: float = 0.0) -> ScheduledTask:
        if actual_cost_usd < 0:
            raise ValueError("actual cost cannot be negative")
        with self._lock:
            task = self._in_flight.pop(task_id)
            task.spent_cost_usd += actual_cost_usd
            task.completed_at = self.clock.now()
            task.state = "completed"
            task.transitions.append(
                {
                    "at": task.completed_at,
                    "event": "completed",
                    "actual_cost_usd": actual_cost_usd,
                    "budget_exceeded": task.remaining_budget_usd == 0
                    and task.spent_cost_usd > float(getattr(task.contract, "max_cost_usd", math.inf)),
                }
            )
            return task

    release = complete

    def fail(self, task_id: str, *, reason: str) -> ScheduledTask:
        """Release reserved slots and retain an inspectable failure transition."""
        with self._lock:
            task = self._in_flight.pop(task_id)
            task.state = "failed"
            task.completed_at = self.clock.now()
            task.transitions.append({"at": task.completed_at, "event": "failed", "reason": reason})
            return task

    def cancel(self, task_id: str) -> bool:
        """Cancel queued work. Running provider calls are not preempted."""

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.state != "queued":
                return False
            task.state = "cancelled"
            task.transitions.append({"at": self.clock.now(), "event": "cancelled"})
            return True

    def in_flight_by_class(self) -> dict[str, int]:
        counts = {name: 0 for name in PRIORITY_ORDER}
        for task in self._in_flight.values():
            counts[task.priority_class] += task.capacity_units
        return counts

    def queue_depths(self) -> dict[str, int]:
        return {
            name: sum(
                task.state == "queued" and task.priority_class == name
                for task in self._tasks.values()
            )
            for name in PRIORITY_ORDER
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "capacity": self.capacity,
            "in_flight": self.in_flight_count,
            "load": self.load,
            "in_flight_by_class": self.in_flight_by_class(),
            "queued_by_class": self.queue_depths(),
        }


# Descriptive alias used in documentation.
IntentAwareScheduler = Scheduler
