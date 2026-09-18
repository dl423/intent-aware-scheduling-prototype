"""Regression tests for task lifecycle, admission, and provider accounting."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from intent_scheduler.app import app
from intent_scheduler.clock import SimulatedClock
from intent_scheduler.config import Settings
from intent_scheduler.gateway import Gateway, next_low_load_window
from intent_scheduler.intent import IntentExtractor
from intent_scheduler.policy import PolicyEngine
from intent_scheduler.providers import MockProvider, LUNA_MODEL
from intent_scheduler.scheduler import Scheduler
from intent_scheduler.store import TaskStore

NOW = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)


def make_gateway(provider=None, policy=None, capacity=3):
    return Gateway(
        Settings(provider='mock', database_path=Path(':memory:'), capacity=capacity),
        provider=provider or MockProvider(), policy=policy,
        store=TaskStore(':memory:'), clock=SimulatedClock(NOW),
        scheduler=Scheduler(capacity=capacity, reservations={}, clock=SimulatedClock(NOW)),
        auto_execute=False,
    )


async def submit(gateway, text='Write a draft', **overrides):
    explicit = dict(quality_floor='draft', deadline=NOW + timedelta(hours=3),
                    interactive=True, max_cost_usd=1, attention_profile='ask-freely')
    explicit.update(overrides)
    return await gateway.submit(text, explicit)


@pytest.mark.asyncio
async def test_failed_execution_releases_capacity_and_persists_failed_state():
    class FailingProvider(MockProvider):
        async def complete(self, prompt, **kwargs):
            if kwargs.get('role') == 'task_execution':
                raise RuntimeError('provider unavailable')
            return await super().complete(prompt, **kwargs)

    gateway = make_gateway(FailingProvider(), capacity=1)
    try:
        first = await submit(gateway)
        second = await submit(gateway)
        scheduled, = await gateway.tick()
        await gateway._execute(scheduled)
        record = gateway.get(first['id'])
        assert record['status'] == 'failed'
        assert gateway.scheduler.in_flight_count == 0
        assert gateway.store.get(first['id'])['status'] == 'failed'
        assert (await gateway.tick())[0].task_id == second['id']
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_concurrent_submissions_account_for_only_their_own_extraction():
    gateway = make_gateway(MockProvider(latency_seconds=0.001))
    try:
        records = await asyncio.gather(*(submit(gateway, f'Draft {i}') for i in range(4)))
        assert [r['accounting']['total_calls'] for r in records] == [1] * 4
        assert sum(r['accounting']['total_tokens'] for r in records) == sum(
            c['input_tokens'] + c['output_tokens'] for c in gateway.provider.calls)
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_mock_concurrent_call_logs_retain_each_calls_usage():
    provider = MockProvider(latency_seconds=0.001)
    responses = await asyncio.gather(*(provider.complete('x' * n, model=LUNA_MODEL)
                                       for n in (10, 100, 1000)))
    assert [(c.get('input_tokens'), c.get('output_tokens'), c.get('cost_usd'))
            for c in provider.calls] == [(r.usage.input_tokens, r.usage.output_tokens,
                                          r.usage.cost_usd) for r in responses]


@pytest.mark.asyncio
async def test_dispatch_rechecks_increased_cost_and_continues_to_next_task():
    class IncreasingCost(PolicyEngine):
        expensive = False
        def decide(self, contract, fleet, work=None):
            decision = super().decide(contract, fleet, work)
            return decision.model_copy(update={'estimated_cost_usd': Decimal('2' if self.expensive else '0.1')})
    policy = IncreasingCost()
    gateway = make_gateway(policy=policy)
    try:
        first = await submit(gateway, max_cost_usd=1)
        second = await submit(gateway, max_cost_usd=3)
        policy.expensive = True
        dispatched = await gateway.tick()
        assert [t.task_id for t in dispatched] == [second['id']]
        record = gateway.get(first['id'])
        assert record['status'] == 'rejected'
        assert record['admission']['admitted'] is False
        assert gateway.store.get(first['id'])['status'] == 'rejected'
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_impossible_parallel_task_does_not_block_ready_peer():
    gateway = make_gateway(capacity=2)
    try:
        oversized = await submit(gateway, quality_floor='critical')
        peer = await submit(gateway)
        assert [t.task_id for t in await gateway.tick()] == [peer['id']]
        assert gateway.get(oversized['id'])['status'] == 'rejected'
    finally:
        await gateway.close()


def test_capacity_one_supports_default_reservations():
    scheduler = Scheduler(capacity=1)
    assert sum(scheduler.reservations.values()) <= 1


@pytest.mark.parametrize('endpoint', ['submit', 'deadline'])
def test_api_rejects_naive_datetimes_with_422(tmp_path, monkeypatch, endpoint):
    monkeypatch.setenv('INTENT_SCHEDULER_PROVIDER', 'mock')
    monkeypatch.setenv('INTENT_SCHEDULER_DB', str(tmp_path / 'api.db'))
    with TestClient(app) as client:
        if endpoint == 'submit':
            response = client.post('/tasks', json={'request_text': 'Draft', 'deadline': '2026-07-13T12:00:00'})
        else:
            response = client.patch('/tasks/missing/deadline', json={'deadline': '2026-07-13T12:00:00'})
        assert response.status_code == 422


@pytest.mark.parametrize('endpoint', ['nudge', 'deadline'])
def test_api_returns_conflict_for_rejected_task_mutation(tmp_path, monkeypatch, endpoint):
    monkeypatch.setenv('INTENT_SCHEDULER_PROVIDER', 'mock')
    monkeypatch.setenv('INTENT_SCHEDULER_DB', str(tmp_path / 'api.db'))
    with TestClient(app) as client:
        record = client.post('/tasks', json={'request_text': 'Draft', 'max_cost_usd': 0.000001}).json()
        assert record['status'] == 'rejected'
        if endpoint == 'nudge':
            response = client.post(f"/tasks/{record['id']}/nudge", json={})
        else:
            response = client.patch(f"/tasks/{record['id']}/deadline", json={'deadline': '2026-07-13T12:00:00Z'})
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_invalid_intent_enum_triggers_fallback():
    provider = MockProvider(scripted_responses=[{'deadline_mode': 'sometime'}])
    extractor = IntentExtractor(provider)
    await extractor.extract('Draft')
    assert len(extractor.fallback_events) == 1
    assert len(extractor.provider_responses) == 2


def test_low_load_window_uses_utc_for_non_utc_clock():
    now = NOW.astimezone(timezone(timedelta(hours=-4)))
    assert next_low_load_window(now) == datetime(2026, 7, 13, 2, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_parallel_failure_waits_for_siblings_before_releasing_capacity():
    class PartialFailure(MockProvider):
        finished = 0
        async def complete(self, prompt, **kwargs):
            if kwargs.get('role') == 'task_execution':
                if 'candidate 1' in prompt:
                    raise RuntimeError('one draft failed')
                await asyncio.sleep(0.01)
                self.finished += 1
            return await super().complete(prompt, **kwargs)
    provider = PartialFailure()
    gateway = make_gateway(provider)
    try:
        record = await submit(gateway, quality_floor='critical')
        scheduled, = await gateway.tick()
        await gateway._execute(scheduled)
        assert provider.finished == 2
        assert gateway.scheduler.in_flight_count == 0
        assert gateway.get(record['id'])['status'] == 'failed'
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_self_check_preserves_attention_instruction():
    gateway = make_gateway()
    try:
        await submit(gateway, attention_profile='do-not-interrupt')
        scheduled, = await gateway.tick()
        await gateway.run_dispatched(scheduled)
        calls = [c for c in gateway.provider.calls if c['role'] == 'task_execution']
        assert len(calls) == 2
        assert all('Do not ask questions.' in c['prompt'] for c in calls)
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_completed_record_retains_transitions_when_read_from_store():
    gateway = make_gateway()
    try:
        submitted = await submit(gateway)
        scheduled, = await gateway.tick()
        result = await gateway.run_dispatched(scheduled)
        gateway.finalize_dispatched(scheduled, result)
        live = gateway.get(submitted['id'])
        gateway._records.clear()
        persisted = gateway.get(submitted['id'])
        assert persisted == live
        assert persisted['transitions'][-1]['event'] == 'completed'
    finally:
        await gateway.close()
