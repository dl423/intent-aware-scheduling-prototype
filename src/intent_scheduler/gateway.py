from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi.encoders import jsonable_encoder

from .clock import Clock, SystemClock
from .config import Settings
from .execution import ExecutionEngine, ExecutionResult
from .intent import IntentExtractor
from .models import FleetState, PriorityClass, TaskStatus, WorkEstimate
from .policy import PolicyEngine, check_admission
from .providers import MockProvider, OpenAIProvider, ProviderAdapter, ProviderResponse
from .scheduler import CostBudgetExceeded, ScheduledTask, Scheduler
from .store import TaskStore


def next_low_load_window(now: datetime) -> datetime:
    """Return the next 02:00 UTC valley used by the prototype policy."""
    candidate = now.replace(hour=2, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def account_responses(responses: list[ProviderResponse] | tuple[ProviderResponse, ...]) -> dict[str, Any]:
    """Split provider usage into task work and gateway overhead."""
    task = [response for response in responses if response.role == "task_execution"]
    overhead = [response for response in responses if response.role != "task_execution"]

    def totals(items: list[ProviderResponse]) -> tuple[int, float]:
        return (
            sum(response.usage.total_tokens for response in items),
            sum(response.usage.cost_usd for response in items),
        )

    task_tokens, task_cost = totals(task)
    overhead_tokens, overhead_cost = totals(overhead)
    return {
        "task_calls": len(task),
        "overhead_calls": len(overhead),
        "task_tokens": task_tokens,
        "overhead_tokens": overhead_tokens,
        "task_cost_usd": task_cost,
        "overhead_cost_usd": overhead_cost,
        "total_calls": len(task) + len(overhead),
        "total_tokens": task_tokens + overhead_tokens,
        "total_cost_usd": task_cost + overhead_cost,
    }


def merge_accounting(*parts: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "task_calls",
        "overhead_calls",
        "task_tokens",
        "overhead_tokens",
        "task_cost_usd",
        "overhead_cost_usd",
        "total_calls",
        "total_tokens",
        "total_cost_usd",
    )
    return {key: sum(part.get(key, 0) for part in parts) for key in keys}


class Gateway:
    """Coordinate extraction, policy, scheduling, execution, and persistence."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        provider: ProviderAdapter | None = None,
        store: TaskStore | None = None,
        clock: Clock | None = None,
        policy: Any | None = None,
        scheduler: Scheduler | None = None,
        auto_execute: bool = True,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.clock = clock or SystemClock()
        self.provider = provider or self._provider_from_settings()
        self.store = store or TaskStore(self.settings.database_path)
        self.intent = IntentExtractor(
            self.provider,
            model=self.settings.intent_model,
            fallback_model=self.settings.intent_fallback_model,
        )
        self.policy = policy or PolicyEngine()
        self.scheduler = scheduler or Scheduler(
            capacity=self.settings.capacity,
            clock=self.clock,
        )
        self.scheduler.decision_hook = self._dispatch_decision
        self.execution = ExecutionEngine(
            self.provider,
            candidate_model=self.settings.candidate_model,
            candidate_fallback_model=self.settings.candidate_fallback_model,
            judge_model=self.settings.judge_model,
            judge_fallback_model=self.settings.judge_fallback_model,
        )
        self.auto_execute = auto_execute
        self._records: dict[str, dict[str, Any]] = {}
        self._running: dict[str, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()

    def _provider_from_settings(self) -> ProviderAdapter:
        if self.settings.provider == "mock":
            return MockProvider(latency_seconds=0.01)
        if self.settings.provider == "openai":
            return OpenAIProvider()
        raise ValueError("INTENT_SCHEDULER_PROVIDER must be 'mock' or 'openai'")

    def _fleet(self, *, allow_deferral: bool) -> FleetState:
        now = self.clock.now()
        return FleetState(
            now=now,
            in_flight=self.scheduler.in_flight_count,
            capacity=self.scheduler.capacity,
            next_low_load_at=next_low_load_window(now) if allow_deferral else None,
        )

    @staticmethod
    def _work_estimate(request_text: str) -> WorkEstimate:
        return WorkEstimate(
            input_tokens=max(400, len(request_text) // 3),
            output_tokens=800,
        )

    def _dispatch_decision(self, task: ScheduledTask) -> Any:
        """Re-derive load-sensitive knobs immediately before capacity is reserved."""
        decision = self.policy.decide(
            task.contract,
            self._fleet(allow_deferral=False),
            task.metadata.get("work_estimate"),
        )
        # Deadline escalation is scheduler state. Preserve a more urgent class
        # while refreshing model, effort, structure, and cost.
        return decision.model_copy(
            update={
                "priority_class": PriorityClass(task.priority_class),
                "scheduled_at": self.clock.now(),
            }
        )

    async def submit(self, request_text: str, explicit: dict[str, Any]) -> dict[str, Any]:
        now = self.clock.now()
        contract = await self.intent.extract(request_text, explicit=explicit, now=now)
        estimate = self._work_estimate(request_text)
        decision = self.policy.decide(contract, self._fleet(allow_deferral=True), estimate)
        admission = check_admission(contract, decision)
        task_id = str(uuid4())
        extraction_accounting = account_responses(self.intent.provider_responses)
        record = {
            "id": task_id,
            "request_text": request_text,
            "status": (
                TaskStatus.QUEUED.value if admission.admitted else TaskStatus.REJECTED.value
            ),
            "contract": contract,
            "decision": decision,
            "decision_history": [
                {"phase": "submission", "at": now, "decision": decision}
            ],
            "admission": admission,
            "submitted_at": now,
            "output": None,
            "quality": None,
            "metrics": None,
            "accounting": extraction_accounting,
            "fallback_events": list(self.intent.fallback_events),
        }
        self._records[task_id] = record
        if admission.admitted:
            task = ScheduledTask(
                task_id=task_id,
                contract=contract,
                decision=decision,
                metadata={
                    "request_text": request_text,
                    "work_estimate": estimate,
                },
            )
            try:
                self.scheduler.enqueue(task)
            except CostBudgetExceeded:
                record["status"] = TaskStatus.REJECTED.value
        self._persist(task_id, "submitted", {"provenance": contract.provenance})
        if self.auto_execute:
            await self.tick()
        return self.get(task_id)

    async def tick(self) -> list[ScheduledTask]:
        dispatched = self.scheduler.dispatch_ready()
        for scheduled in dispatched:
            record = self._records[scheduled.task_id]
            record["status"] = TaskStatus.RUNNING.value
            record["decision"] = scheduled.decision
            record["decision_history"].append(
                {
                    "phase": "dispatch",
                    "at": self.clock.now(),
                    "decision": scheduled.decision,
                }
            )
            self._persist(
                scheduled.task_id,
                "dispatched",
                {"class": scheduled.priority_class, "decision": scheduled.decision},
            )
            if self.auto_execute:
                task = asyncio.create_task(self._execute(scheduled))
                self._running[scheduled.task_id] = task
        return dispatched

    async def run_scheduler_loop(self) -> None:
        """Wake deferred work and deadline escalation without API traffic."""
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.scheduler_interval_seconds
                )
            except TimeoutError:
                pass

    def _queue_wait(self, scheduled: ScheduledTask) -> float:
        if scheduled.started_at and scheduled.enqueued_at:
            return (scheduled.started_at - scheduled.enqueued_at).total_seconds()
        return 0.0

    async def run_dispatched(self, scheduled: ScheduledTask) -> ExecutionResult:
        """Execute a dispatched task without completing its scheduler entry."""
        return await self.execution.execute(
            self._records[scheduled.task_id]["request_text"],
            scheduled.decision,
            queue_wait_seconds=self._queue_wait(scheduled),
        )

    def finalize_dispatched(self, scheduled: ScheduledTask, result: ExecutionResult) -> None:
        """Complete a dispatched task using a previously produced result."""
        record = self._records[scheduled.task_id]
        self.scheduler.complete(scheduled.task_id, actual_cost_usd=result.metrics.cost_usd)
        execution_accounting = account_responses(result.provider_responses)
        record.update(
            status=TaskStatus.COMPLETED.value,
            output=result.output,
            quality={
                "score": result.quality_score,
                "rationale": result.quality_rationale,
                "proxy": True,
            },
            metrics=result.metrics,
            accounting=merge_accounting(record["accounting"], execution_accounting),
        )
        record["fallback_events"].extend(result.fallback_events)
        self._persist(
            scheduled.task_id,
            "completed",
            {
                "cost_usd": result.metrics.cost_usd,
                "accounting": record["accounting"],
            },
        )

    async def _execute(self, scheduled: ScheduledTask) -> None:
        record = self._records[scheduled.task_id]
        try:
            result = await self.run_dispatched(scheduled)
            self.finalize_dispatched(scheduled, result)
        except Exception as exc:
            record["status"] = TaskStatus.FAILED.value
            record["error"] = str(exc)
            self._persist(scheduled.task_id, "failed", {"error": str(exc)})
        finally:
            self._running.pop(scheduled.task_id, None)
            if self.auto_execute:
                await self.tick()

    async def wait_for_idle(self) -> None:
        while self._running:
            await asyncio.gather(*list(self._running.values()), return_exceptions=True)
            await self.tick()

    def get(self, task_id: str) -> dict[str, Any]:
        if task_id not in self._records:
            persisted = self.store.get(task_id)
            if persisted is None:
                raise KeyError(task_id)
            return persisted
        record = dict(self._records[task_id])
        scheduled = self.scheduler.get(task_id)
        if scheduled is not None:
            record["status"] = scheduled.state if scheduled.state != "in-flight" else "running"
            record["effective_priority"] = scheduled.priority_class
            record["transitions"] = scheduled.transitions
        record["events"] = self.store.events(task_id)
        return jsonable_encoder(record)

    def list(self) -> list[dict[str, Any]]:
        return [self.get(task_id) for task_id in self._records]

    def cancel(self, task_id: str) -> dict[str, Any]:
        if task_id not in self._records:
            raise KeyError(task_id)
        if not self.scheduler.cancel(task_id):
            raise ValueError("only queued tasks can be cancelled")
        self._records[task_id]["status"] = TaskStatus.CANCELLED.value
        self._persist(task_id, "cancelled", {})
        return self.get(task_id)

    def _replan(self, scheduled: ScheduledTask, *, event: str, reason: str) -> ScheduledTask:
        decision = self.policy.decide(
            scheduled.contract,
            self._fleet(allow_deferral=True),
            scheduled.metadata.get("work_estimate"),
        )
        scheduled = self.scheduler.update_decision(
            scheduled.task_id,
            decision,
            event=event,
            reason=reason,
        )
        record = self._records[scheduled.task_id]
        record["decision"] = scheduled.decision
        record["decision_history"].append(
            {"phase": event, "at": self.clock.now(), "decision": scheduled.decision}
        )
        return scheduled

    async def update_deadline(self, task_id: str, deadline: datetime) -> dict[str, Any]:
        if task_id not in self._records:
            raise KeyError(task_id)
        scheduled = self.scheduler.update_deadline(task_id, deadline)
        if scheduled.state == "queued":
            scheduled = self._replan(
                scheduled,
                event="deadline-replan",
                reason="explicit-deadline-change",
            )
        self._records[task_id]["contract"] = scheduled.contract
        self._persist(task_id, "deadline-updated", {"deadline": deadline})
        if self.auto_execute:
            await self.tick()
        return self.get(task_id)

    async def nudge(self, task_id: str, message: str) -> dict[str, Any]:
        if task_id not in self._records:
            raise KeyError(task_id)
        scheduled = self.scheduler.nudge(task_id)
        if scheduled.state == "queued":
            scheduled = self._replan(
                scheduled,
                event="revealed-intent-replan",
                reason="human-blocked-now",
            )
        record = self._records[task_id]
        record["contract"] = scheduled.contract
        self._persist(
            task_id,
            "revealed-intent",
            {
                "signal": "human-blocked-now",
                "message": message,
                "class": scheduled.priority_class,
                "provenance": scheduled.contract.provenance,
            },
        )
        if self.auto_execute:
            await self.tick()
        return self.get(task_id)

    def _persist(self, task_id: str, event: str, details: dict[str, Any]) -> None:
        if task_id in self._records:
            self.store.put(
                task_id,
                str(self._records[task_id]["status"]),
                jsonable_encoder(self._records[task_id]),
            )
        self.store.event(task_id, event, jsonable_encoder(details))

    async def close(self) -> None:
        self._stop.set()
        if self._running:
            await asyncio.gather(*list(self._running.values()), return_exceptions=True)
        self.store.close()
