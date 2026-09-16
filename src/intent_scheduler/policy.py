"""Deterministic intent-to-actuator policy and cost admission helpers.

The quality table is intentionally data, not an opaque scoring function.  It
is the inspectable contract decomposition at the semantic-transparency
boundary.  Load handling changes execution structure or reasoning effort
before model capability, so incidental congestion does not silently violate a
declared quality floor.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal

from .models import (
    AdmissionResult,
    AskPolicy,
    AttentionProfile,
    DecisionRecord,
    ExecutionTemplate,
    FleetState,
    ModelTier,
    PriorityClass,
    QualityFloor,
    ReasoningEffort,
    TaskContract,
    WorkEstimate,
)


@dataclass(frozen=True)
class ModelSpec:
    tier: ModelTier
    api_name: str
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal


# Provider identifiers and list prices verified 2026-07-12 against:
# https://developers.openai.com/api/docs/models/gpt-5.6-luna
# https://developers.openai.com/api/docs/models/gpt-5.6-terra
# Prices are USD per million input/output tokens. The higher tier is
# deliberately absent from this paper companion.
MODEL_LADDER: dict[ModelTier, ModelSpec] = {
    ModelTier.LUNA: ModelSpec(ModelTier.LUNA, "gpt-5.6-luna", Decimal("1.00"), Decimal("6.00")),
    ModelTier.TERRA: ModelSpec(ModelTier.TERRA, "gpt-5.6-terra", Decimal("2.50"), Decimal("15.00")),
}


@dataclass(frozen=True)
class QualityPolicy:
    model_tier: ModelTier
    reasoning_effort: ReasoningEffort
    execution_template: ExecutionTemplate
    parallel_drafts: int = 1


# The declared quality floor selects the minimum model tier. Congestion may
# reduce breadth or effort, but never silently lowers this assigned tier.
QUALITY_POLICY_TABLE: dict[QualityFloor, QualityPolicy] = {
    QualityFloor.DRAFT: QualityPolicy(
        ModelTier.LUNA, ReasoningEffort.LOW, ExecutionTemplate.SINGLE_PASS
    ),
    QualityFloor.STANDARD: QualityPolicy(
        ModelTier.LUNA, ReasoningEffort.MEDIUM, ExecutionTemplate.SINGLE_PASS
    ),
    QualityFloor.HIGH: QualityPolicy(
        ModelTier.TERRA, ReasoningEffort.HIGH, ExecutionTemplate.SELF_CHECK
    ),
    QualityFloor.CRITICAL: QualityPolicy(
        ModelTier.TERRA, ReasoningEffort.HIGH, ExecutionTemplate.PARALLEL_JUDGE, 3
    ),
}


ASK_POLICY_TABLE: dict[AttentionProfile, AskPolicy] = {
    AttentionProfile.ASK_FREELY: AskPolicy.MAY_ASK,
    AttentionProfile.BATCH_QUESTIONS: AskPolicy.ONE_BATCHED_CHECKPOINT,
    AttentionProfile.DO_NOT_INTERRUPT: AskPolicy.BEST_GUESS_SELF_VERIFY,
}


HIGH_LOAD_FRACTION = 0.85
INTERACTIVE_SLACK = timedelta(minutes=15)
DEFERRED_SLACK = timedelta(hours=6)
DEADLINE_SAFETY_MARGIN = timedelta(hours=1)


def estimate_cost(
    model_tier: ModelTier,
    template: ExecutionTemplate,
    work: WorkEstimate | None = None,
    *,
    parallel_drafts: int = 1,
) -> Decimal:
    """Conservatively estimate request cost for admission control.

    A self-check is modeled as two equivalent calls. Parallel execution uses
    N drafts and one judge. This is intentionally simple and auditable.
    """

    work = work or WorkEstimate()
    spec = MODEL_LADDER[model_tier]
    if template is ExecutionTemplate.SINGLE_PASS:
        calls = 1
    elif template is ExecutionTemplate.SELF_CHECK:
        calls = 2
    else:
        calls = max(2, parallel_drafts + 1)

    per_call = (
        Decimal(work.input_tokens) * spec.input_usd_per_million
        + Decimal(work.output_tokens) * spec.output_usd_per_million
    ) / Decimal(1_000_000)
    return (per_call * calls).quantize(Decimal("0.000001"))


def check_admission(contract: TaskContract, decision: DecisionRecord) -> AdmissionResult:
    admitted = decision.estimated_cost_usd <= contract.max_cost_usd
    if admitted:
        reason = "estimated execution cost is within the task cost cap"
    else:
        reason = "estimated execution cost exceeds the task cost cap"
    return AdmissionResult(
        admitted=admitted,
        estimated_cost_usd=decision.estimated_cost_usd,
        cost_cap_usd=contract.max_cost_usd,
        reason=reason,
    )


def _priority(contract: TaskContract, fleet: FleetState) -> PriorityClass:
    slack = contract.deadline - fleet.now
    if contract.interactive or slack <= INTERACTIVE_SLACK:
        return PriorityClass.INTERACTIVE
    if slack >= DEFERRED_SLACK:
        return PriorityClass.DEFERRED_BATCH
    return PriorityClass.STANDARD


def _degrade_for_load(
    base: QualityPolicy, contract: TaskContract, fleet: FleetState
) -> tuple[QualityPolicy, list[str]]:
    """Apply the explicit congestion degradation path.

    Interactive tasks are protected because congestion is precisely when their
    human-waiting signal matters most. For patient work, verification breadth
    is reduced before reasoning effort. Model tier is never silently lowered.
    """

    if fleet.load_fraction < HIGH_LOAD_FRACTION or contract.interactive:
        return base, []

    if contract.quality_floor is QualityFloor.CRITICAL:
        return (
            replace(base, execution_template=ExecutionTemplate.SELF_CHECK, parallel_drafts=1),
            ["high load: reduced critical parallel breadth before model capability"],
        )
    if contract.quality_floor is QualityFloor.HIGH:
        return (
            replace(base, reasoning_effort=ReasoningEffort.MEDIUM),
            ["high load: reduced high-quality reasoning effort; retained self-check"],
        )
    if contract.quality_floor is QualityFloor.STANDARD:
        return (
            replace(base, reasoning_effort=ReasoningEffort.LOW),
            ["high load: reduced standard reasoning effort; retained Luna tier"],
        )
    return base, ["high load: draft contract already uses the minimum-cost operating point"]


class PolicyEngine:
    """Pure deterministic mapping from contract and fleet snapshot to decision."""

    def decide(
        self,
        contract: TaskContract,
        fleet: FleetState,
        work: WorkEstimate | None = None,
    ) -> DecisionRecord:
        base = QUALITY_POLICY_TABLE[contract.quality_floor]
        selected, notes = _degrade_for_load(base, contract, fleet)
        ask_policy = ASK_POLICY_TABLE[contract.attention_profile]

        # Attention is a requester-supplied resource. If none is available,
        # machine verification substitutes for human review and is costed.
        if (
            contract.attention_profile is AttentionProfile.DO_NOT_INTERRUPT
            and selected.execution_template is ExecutionTemplate.SINGLE_PASS
        ):
            selected = replace(selected, execution_template=ExecutionTemplate.SELF_CHECK)
            notes.append("do-not-interrupt: substituted machine self-check for human attention")

        priority = _priority(contract, fleet)
        scheduled_at = fleet.now
        if priority is PriorityClass.DEFERRED_BATCH and fleet.next_low_load_at is not None:
            latest_safe_start = contract.deadline - DEADLINE_SAFETY_MARGIN
            if fleet.next_low_load_at <= latest_safe_start:
                scheduled_at = fleet.next_low_load_at
                notes.append("deferred to declared low-load window within deadline margin")

        cost = estimate_cost(
            selected.model_tier,
            selected.execution_template,
            work,
            parallel_drafts=selected.parallel_drafts,
        )
        spec = MODEL_LADDER[selected.model_tier]
        return DecisionRecord(
            model_tier=selected.model_tier,
            model_name=spec.api_name,
            reasoning_effort=selected.reasoning_effort,
            priority_class=priority,
            execution_template=selected.execution_template,
            parallel_drafts=selected.parallel_drafts,
            scheduled_at=scheduled_at,
            ask_policy=ask_policy,
            estimated_cost_usd=cost,
            policy_rule=f"quality={contract.quality_floor.value}",
            degradation_notes=notes,
        )
