import pytest

from intent_scheduler.providers import MockProvider
from intent_scheduler.real_validation import run_real_validation


@pytest.mark.asyncio
async def test_real_validation_matrix_runs_end_to_end_with_mock():
    provider = MockProvider()
    result = await run_real_validation(provider)

    assert result["luna_intent_matrix"]["adequate"] is True
    assert result["budget_rejection"]["status"] == "rejected"
    assert result["nudge_single_pass"]["status"] == "completed"
    assert "revealed-intent" in result["nudge_single_pass"]["intent_events"]
    assert result["deadline_replan_self_check"]["template"] == "single-pass+self-check"
    assert "deadline-replan" in result["deadline_replan_self_check"]["transition_events"]
    assert result["interactive_parallel_judge"]["template"] == "parallel-drafts+judge"
    assert result["provider_audit"]["two_tier_only"] is True
    assert result["provider_audit"]["both_tiers_observed"] is True
    assert result["nudge_single_pass"]["task_calls"] == 1
    assert result["nudge_single_pass"]["overhead_calls"] == 2
    assert result["interactive_parallel_judge"]["task_calls"] == 3
    assert result["interactive_parallel_judge"]["overhead_calls"] == 3
    assert result["provider_audit"]["task_calls"] > 0
    assert result["provider_audit"]["overhead_calls"] > 0
    assert result["validation_passed"] is True
