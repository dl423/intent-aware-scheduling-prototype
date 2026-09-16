"""Shared, provider-independent contracts for the scheduling gateway.

The models in this module deliberately describe task-level intent and policy
outputs.  Provider request objects belong in the adapter layer.  Keeping that
boundary explicit prevents static API knobs from becoming the contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrEnum(str, Enum):
    """A JSON-friendly enum with useful string formatting."""

    def __str__(self) -> str:
        return self.value


class QualityFloor(StrEnum):
    DRAFT = "draft"
    STANDARD = "standard"
    HIGH = "high"
    CRITICAL = "critical"


class AttentionProfile(StrEnum):
    ASK_FREELY = "ask-freely"
    BATCH_QUESTIONS = "batch-questions"
    DO_NOT_INTERRUPT = "do-not-interrupt"


class Provenance(StrEnum):
    EXPLICIT = "explicit"
    PARSED = "parsed"
    REVEALED = "revealed"
    DEFAULT = "default"


class ContractProvenance(BaseModel):
    """Origin of each dimension in the normalized task contract."""

    model_config = ConfigDict(extra="forbid")

    quality_floor: Provenance
    deadline: Provenance
    max_cost_usd: Provenance
    attention_profile: Provenance
    interactive: Provenance = Provenance.DEFAULT

    def __getitem__(self, field: str) -> Provenance:
        """Allow report and API code to consume provenance as a field map."""

        return getattr(self, field)


class TaskContract(BaseModel):
    """Normalized four-dimensional contract consumed by deterministic policy.

    ``interactive`` is a latency interpretation of the deadline dimension,
    rather than a fifth intent dimension.  Interactive contracts still carry
    a concrete deadline so that EDF ordering and attainment metrics remain
    well defined.
    """

    model_config = ConfigDict(extra="forbid")

    quality_floor: QualityFloor
    deadline: datetime
    interactive: bool = False
    max_cost_usd: Decimal = Field(gt=0)
    attention_profile: AttentionProfile
    provenance: ContractProvenance

    @field_validator("deadline")
    @classmethod
    def deadline_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline must be timezone-aware")
        return value


class ModelTier(StrEnum):
    """Configured capability tiers supported by this prototype."""

    LUNA = "luna"
    TERRA = "terra"


class ReasoningEffort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PriorityClass(StrEnum):
    INTERACTIVE = "interactive"
    STANDARD = "standard"
    DEFERRED_BATCH = "deferred-batch"


class ExecutionTemplate(StrEnum):
    SINGLE_PASS = "single-pass"
    SELF_CHECK = "single-pass+self-check"
    PARALLEL_JUDGE = "parallel-drafts+judge"


class AskPolicy(StrEnum):
    MAY_ASK = "may-ask"
    ONE_BATCHED_CHECKPOINT = "one-batched-checkpoint"
    BEST_GUESS_SELF_VERIFY = "best-guess+self-verify"


class FleetState(BaseModel):
    """Small capacity snapshot available to deterministic policy."""

    model_config = ConfigDict(extra="forbid")

    now: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    in_flight: int = Field(default=0, ge=0)
    capacity: int = Field(default=8, gt=0)
    next_low_load_at: datetime | None = None

    @field_validator("now", "next_low_load_at")
    @classmethod
    def times_must_be_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("fleet timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def low_load_window_cannot_be_in_the_past(self) -> FleetState:
        if self.next_low_load_at is not None and self.next_low_load_at < self.now:
            raise ValueError("next_low_load_at cannot precede now")
        return self

    @property
    def load_fraction(self) -> float:
        return self.in_flight / self.capacity


class WorkEstimate(BaseModel):
    """Provider-neutral size estimate used for admission, not final billing."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=1_200, ge=0)
    output_tokens: int = Field(default=800, ge=0)


class DecisionRecord(BaseModel):
    """Auditable result of translating a contract into purchasable knobs."""

    model_config = ConfigDict(extra="forbid")

    model_tier: ModelTier
    model_name: str
    reasoning_effort: ReasoningEffort
    priority_class: PriorityClass
    execution_template: ExecutionTemplate
    parallel_drafts: int = Field(default=1, ge=1)
    scheduled_at: datetime
    ask_policy: AskPolicy
    estimated_cost_usd: Decimal = Field(ge=0)
    policy_rule: str
    degradation_notes: list[str] = Field(default_factory=list)

    @field_validator("scheduled_at")
    @classmethod
    def scheduled_time_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduled_at must be timezone-aware")
        return value


class AdmissionResult(BaseModel):
    admitted: bool
    estimated_cost_usd: Decimal = Field(ge=0)
    cost_cap_usd: Decimal = Field(gt=0)
    reason: str


class TaskStatus(StrEnum):
    REJECTED = "rejected"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskEvent(BaseModel):
    """Append-only event for changes such as nudges and deadline promotion."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def event_time_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value
