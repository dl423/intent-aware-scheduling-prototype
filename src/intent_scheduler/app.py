from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .gateway import Gateway
from .models import AttentionProfile, QualityFloor


class TaskSubmission(BaseModel):
    """Natural-language task plus optional contract fields that override parsing."""

    model_config = ConfigDict(extra="forbid")

    request_text: str = Field(min_length=1, description="The user's task request")
    deadline: datetime | None = Field(
        default=None, description="Timezone-aware absolute completion deadline"
    )
    interactive: bool | None = Field(
        default=None, description="Whether a human is waiting for the result now"
    )
    max_cost_usd: float | None = Field(
        default=None, gt=0, description="Maximum task execution spend in USD"
    )
    quality_floor: QualityFloor | None = Field(
        default=None, description="Minimum requested output quality"
    )
    attention_profile: AttentionProfile | None = Field(
        default=None, description="Permitted clarification and checkpoint behavior"
    )


class DeadlineUpdate(BaseModel):
    """Replacement deadline treated as new explicit intent."""

    model_config = ConfigDict(extra="forbid")
    deadline: datetime = Field(description="Timezone-aware absolute completion deadline")


class Nudge(BaseModel):
    """User signal that the task is blocking a human now."""

    model_config = ConfigDict(extra="forbid")
    message: str = Field(
        default="Any update?", min_length=1, description="Human-blocked-now signal text"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    gateway = Gateway()
    app.state.gateway = gateway
    scheduler_loop = asyncio.create_task(gateway.run_scheduler_loop())
    yield
    await gateway.close()
    await scheduler_loop


app = FastAPI(
    title="Intent-Aware Scheduling Gateway",
    version="0.1.0",
    lifespan=lifespan,
)


def _gateway(request: Request) -> Gateway:
    return request.app.state.gateway


@app.get("/health", summary="Inspect service and fleet health")
def health(request: Request) -> dict[str, Any]:
    gateway = _gateway(request)
    return {"status": "ok", "provider": gateway.settings.provider, "fleet": gateway.scheduler.snapshot()}


@app.post("/tasks", status_code=202, summary="Submit and schedule a task")
async def submit_task(payload: TaskSubmission, request: Request) -> dict[str, Any]:
    explicit = payload.model_dump(exclude={"request_text"}, exclude_none=True)
    return await _gateway(request).submit(payload.request_text, explicit)


@app.get("/tasks", summary="List tasks from this process")
def list_tasks(request: Request) -> list[dict[str, Any]]:
    return _gateway(request).list()


@app.get("/tasks/{task_id}", summary="Inspect task state and results")
def task_status(task_id: str, request: Request) -> dict[str, Any]:
    try:
        return _gateway(request).get(task_id)
    except KeyError as exc:
        raise HTTPException(404, "task not found") from exc


@app.post("/tasks/{task_id}/cancel", summary="Cancel queued work")
def cancel_task(task_id: str, request: Request) -> dict[str, Any]:
    try:
        return _gateway(request).cancel(task_id)
    except KeyError as exc:
        raise HTTPException(404, "task not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.patch("/tasks/{task_id}/deadline", summary="Change deadline and replan")
async def change_deadline(task_id: str, payload: DeadlineUpdate, request: Request) -> dict[str, Any]:
    try:
        return await _gateway(request).update_deadline(task_id, payload.deadline)
    except KeyError as exc:
        raise HTTPException(404, "task not found") from exc


@app.post("/tasks/{task_id}/nudge", summary="Reveal that a human is blocked")
async def nudge_task(task_id: str, payload: Nudge, request: Request) -> dict[str, Any]:
    try:
        return await _gateway(request).nudge(task_id, payload.message)
    except KeyError as exc:
        raise HTTPException(404, "task not found") from exc
