from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from intent_scheduler.models import (
    AskPolicy,
    AttentionProfile,
    ContractProvenance,
    ExecutionTemplate,
    FleetState,
    ModelTier,
    PriorityClass,
    Provenance,
    QualityFloor,
    ReasoningEffort,
    TaskContract,
    WorkEstimate,
)
from intent_scheduler.policy import PolicyEngine, check_admission, estimate_cost


NOW = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)


def contract(
    quality: QualityFloor,
    *,
    slack: timedelta = timedelta(hours=2),
    interactive: bool = False,
    attention: AttentionProfile = AttentionProfile.ASK_FREELY,
    cap: str = "10.00",
) -> TaskContract:
    provenance = ContractProvenance(
        quality_floor=Provenance.EXPLICIT,
        deadline=Provenance.EXPLICIT,
        max_cost_usd=Provenance.EXPLICIT,
        attention_profile=Provenance.EXPLICIT,
    )
    return TaskContract(
        quality_floor=quality,
        deadline=NOW + slack,
        interactive=interactive,
        max_cost_usd=Decimal(cap),
        attention_profile=attention,
        provenance=provenance,
    )


@pytest.mark.parametrize(
    ("quality", "tier", "effort", "template"),
    [
        (QualityFloor.DRAFT, ModelTier.LUNA, ReasoningEffort.LOW, ExecutionTemplate.SINGLE_PASS),
        (QualityFloor.STANDARD, ModelTier.LUNA, ReasoningEffort.MEDIUM, ExecutionTemplate.SINGLE_PASS),
        (QualityFloor.HIGH, ModelTier.TERRA, ReasoningEffort.HIGH, ExecutionTemplate.SELF_CHECK),
        (QualityFloor.CRITICAL, ModelTier.TERRA, ReasoningEffort.HIGH, ExecutionTemplate.PARALLEL_JUDGE),
    ],
)
def test_readable_quality_policy_table(quality, tier, effort, template):
    decision = PolicyEngine().decide(contract(quality), FleetState(now=NOW))
    assert (decision.model_tier, decision.reasoning_effort, decision.execution_template) == (
        tier,
        effort,
        template,
    )


def test_patient_task_is_valley_filled_when_window_is_safe():
    valley = NOW + timedelta(hours=7)
    decision = PolicyEngine().decide(
        contract(QualityFloor.HIGH, slack=timedelta(hours=24)),
        FleetState(now=NOW, in_flight=7, capacity=8, next_low_load_at=valley),
    )
    assert decision.priority_class is PriorityClass.DEFERRED_BATCH
    assert decision.scheduled_at == valley


def test_low_load_window_too_near_deadline_is_not_used():
    decision = PolicyEngine().decide(
        contract(QualityFloor.HIGH, slack=timedelta(hours=8)),
        FleetState(now=NOW, next_low_load_at=NOW + timedelta(hours=7, minutes=30)),
    )
    assert decision.scheduled_at == NOW


def test_interactive_signal_protects_quality_under_load():
    decision = PolicyEngine().decide(
        contract(QualityFloor.CRITICAL, interactive=True),
        FleetState(now=NOW, in_flight=9, capacity=8),
    )
    assert decision.priority_class is PriorityClass.INTERACTIVE
    assert decision.execution_template is ExecutionTemplate.PARALLEL_JUDGE
    assert decision.degradation_notes == []


def test_high_load_degrades_parallel_breadth_before_model_tier():
    decision = PolicyEngine().decide(
        contract(QualityFloor.CRITICAL),
        FleetState(now=NOW, in_flight=7, capacity=8),
    )
    assert decision.model_tier is ModelTier.TERRA
    assert decision.execution_template is ExecutionTemplate.SELF_CHECK
    assert decision.parallel_drafts == 1
    assert "parallel breadth" in decision.degradation_notes[0]


def test_attention_policy_and_machine_assurance_substitution():
    decision = PolicyEngine().decide(
        contract(
            QualityFloor.DRAFT,
            attention=AttentionProfile.DO_NOT_INTERRUPT,
        ),
        FleetState(now=NOW),
    )
    assert decision.ask_policy is AskPolicy.BEST_GUESS_SELF_VERIFY
    assert decision.execution_template is ExecutionTemplate.SELF_CHECK
    assert decision.estimated_cost_usd == estimate_cost(ModelTier.LUNA, ExecutionTemplate.SELF_CHECK)


def test_cost_estimate_scales_with_execution_structure():
    work = WorkEstimate(input_tokens=10_000, output_tokens=2_000)
    single = estimate_cost(ModelTier.TERRA, ExecutionTemplate.SINGLE_PASS, work)
    checked = estimate_cost(ModelTier.TERRA, ExecutionTemplate.SELF_CHECK, work)
    parallel = estimate_cost(
        ModelTier.TERRA,
        ExecutionTemplate.PARALLEL_JUDGE,
        work,
        parallel_drafts=3,
    )
    assert checked == single * 2
    assert parallel == single * 4


def test_admission_rejects_estimate_over_contract_cap():
    task = contract(QualityFloor.CRITICAL, cap="0.000001")
    decision = PolicyEngine().decide(task, FleetState(now=NOW))
    result = check_admission(task, decision)
    assert result.admitted is False
    assert result.estimated_cost_usd > result.cost_cap_usd


def test_contract_rejects_naive_deadline():
    with pytest.raises(ValidationError, match="timezone-aware"):
        TaskContract(
            quality_floor=QualityFloor.STANDARD,
            deadline=datetime(2026, 7, 12, 13),
            max_cost_usd=Decimal("1"),
            attention_profile=AttentionProfile.ASK_FREELY,
            provenance=ContractProvenance(
                quality_floor=Provenance.DEFAULT,
                deadline=Provenance.DEFAULT,
                max_cost_usd=Provenance.DEFAULT,
                attention_profile=Provenance.DEFAULT,
            ),
        )
