"""Run the default experiment, save its records, and check reference values.

The exporter uses the same workload and sequence of simulation calls as
collect_case_study. A temporary Gateway subclass captures task records and
provider responses before the in-memory store closes. It leaves the policy,
scheduling, execution, and simulated-clock behavior unchanged.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import html
import json
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from intent_scheduler import simulation as sim


def encode(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    raise TypeError(type(value).__name__)


def write_json(path, value):
    path.write_text(json.dumps(value, default=encode, indent=2) + '\n')


def time_label(t):
    day = (t.date() - sim.START.date()).days + 1
    return f'D{day} {t:%H:%M}'


def save_mode_tables(folder, mode, outcomes, traces, captured):
    by_workload = {r['workload_id']: r for r in captured['tasks']}
    lines = [f'# Per-task results for {mode}', '',
             'Fresh simulation output. All times are UTC. D1 is July 12, 2026; D2 is July 13. Wait is submission-to-start time. Cost is modeled execution cost, not mock-provider accounting. Rows are in workload order, not completion order.', '',
             '| Task | Persona | Submitted | Started | Completed | Deadline | Wait (min) | Met? | Modeled cost ($) |',
             '|---|---|---|---|---|---|---|---|---|']
    for o in sorted(outcomes, key=lambda o: o.item.id):
        lines.append(f'| {o.item.id} | {o.item.persona} | {time_label(o.submitted_at)} | {time_label(o.started_at)} | {time_label(o.completed_at)} | {time_label(o.deadline)} | {round(o.queue_wait_hours*60)} | {"yes" if o.deadline_met else "no"} | {o.modeled_execution_cost_usd:.4f} |')
        task_data = by_workload[o.item.id]
        write_json(folder / f'{o.item.id}.json', {'outcome': o, 'queue_wait_minutes': round(o.queue_wait_hours*60), 'deadline_met': o.deadline_met, **task_data})
    (folder / 'tasks.md').write_text('\n'.join(lines)+'\n')
    trace_lines = [f'# Time trace for {mode}', '',
                   'One row per ten-minute tick. Allocated values count reserved provider-call slots. Ready values count tasks whose eligibility time has arrived. Future-eligible tasks are excluded from ready counts. Empty midnight-to-first-arrival rows remain in traces.json but are omitted here.', '',
                   '| UTC time | Allocated slots | Human slots | Patient slots | Ready tasks | Ready human | Ready patient |',
                   '|---|---|---|---|---|---|---|']
    for t in traces:
        if t.instant.hour < 9 and t.instant.date() == sim.START.date():
            continue
        trace_lines.append(f'| {time_label(t.instant)} | {t.in_flight} | {t.allocated_human_waiting} | {t.allocated_patient} | {t.ready_queue} | {t.ready_human_waiting} | {t.ready_patient} |')
    (folder / 'trace.md').write_text('\n'.join(trace_lines)+'\n')


async def main():
    began = datetime.now(timezone.utc)
    runs = ROOT / 'results'
    runs.mkdir(parents=True, exist_ok=True)
    run = runs / ('run-' + began.strftime('%Y%m%dT%H%M%S%fZ'))
    run.mkdir()
    items = sim.generate_workload(8)
    write_json(run / 'workload.json', items)
    base_gateway = sim.Gateway
    captures = []

    class RecordingGateway(base_gateway):
        async def submit(self, request_text, explicit):
            record = await super().submit(request_text, explicit)
            if not hasattr(self, '_audit_extraction'):
                self._audit_extraction = {}
            self._audit_extraction[record['id']] = list(self.intent.provider_responses)
            return record

        async def run_dispatched(self, scheduled):
            result = await super().run_dispatched(scheduled)
            if not hasattr(self, '_audit_execution'):
                self._audit_execution = {}
            self._audit_execution[scheduled.task_id] = result
            return result

        async def close(self):
            records = list(self._records)
            assert len(records) == len(items)
            tasks = []
            for item, task_id in zip(items, records):
                record = self.get(task_id)
                assert record['request_text'] == item.request_text
                assert datetime.fromisoformat(record['contract']['deadline']) == sim._explicit(item)['deadline']
                tasks.append({'workload_id': item.id, 'runtime_task_id': task_id,
                              'record': record,
                              'extraction_responses': self._audit_extraction.get(task_id, []),
                              'execution_result': self._audit_execution.get(task_id)})
            captures.append({'tasks': tasks, 'provider_calls': list(self.provider.calls)})
            await super().close()

    outcomes, rates, accounting, traces = {}, {}, {}, {}
    sim.Gateway = RecordingGateway
    try:
        for mode in sim.RUN_MODES:
            outcomes[mode], rates[mode], accounting[mode], traces[mode] = await sim.simulate_pipeline(items, mode=mode)
            folder = run / mode
            folder.mkdir()
            write_json(folder / 'outcomes.json', outcomes[mode])
            write_json(folder / 'traces.json', traces[mode])
            write_json(folder / 'provider-calls.json', captures[-1]['provider_calls'])
            write_json(folder / 'summary.json', sim.summaries(outcomes[mode]))
            write_json(folder / 'accounting.json', accounting[mode])
            save_mode_tables(folder, mode, outcomes[mode], traces[mode], captures[-1])
    finally:
        sim.Gateway = base_gateway

    report = sim.render_report(outcomes, rates, accounting, traces)
    (run / 'report.txt').write_text(report+'\n')
    (run / 'report.html').write_text('<!doctype html><meta charset="utf-8"><title>Current prototype experiment</title><pre>'+html.escape(report)+'</pre>\n')
    write_json(run / 'allocation-rates.json', rates)
    table_rows = json.loads((ROOT / 'experiments/expected/table.json').read_text())
    checks = []
    def check(name, passed, details):
        checks.append({'check':name,'passed':bool(passed),'details':details})
    labels = dict(zip(sim.RUN_MODES, ['Static reference','Contract+FIFO','Intent-aware']))
    for mode, values in outcomes.items():
        urgent = [o for o in values if o.item.persona=='urgent-executive']
        human = [o for o in values if o.item.persona in sim.HUMAN_WAITING_PERSONAS]
        sorted_waits = sorted(round(o.queue_wait_hours*60) for o in human)
        idx=round((len(sorted_waits)-1)*0.95)
        summary={'submitted':len(items),'completed':len(values),
                 'critical_met':sum(o.completed_at<=o.deadline for o in urgent),'critical_total':len(urgent),
                 'all_met':sum(o.completed_at<=o.deadline for o in values),
                 'sorted_human_wait_minutes':sorted_waits,'p95_zero_based_index':idx,
                 'p95_minutes':sorted_waits[idx],
                 'modeled_cost':sum(Decimal(str(o.modeled_execution_cost_usd)) for o in values)}
        write_json(run / mode / 'table-derivation.json',summary)
        actual=(str(summary['critical_met']),str(len(urgent)),str(summary['all_met']),str(len(values)),f"{sorted_waits[idx]/60:.2f}",f"{summary['modeled_cost']:.4f}")
        check(f'{mode}: all paper table fields',actual==tuple(table_rows[labels[mode]]),{'paper':table_rows[labels[mode]],'fresh':actual})
        check(f'{mode}: all 72 submitted tasks completed',len(values)==len(items)==72 and len({o.item.id for o in values})==72, {'completed':len(values)})
        patients=[o for o in values if o.item.persona not in sim.HUMAN_WAITING_PERSONAS]
        check(f'{mode}: all 40 patient deadlines met',len(patients)==40 and all(o.deadline_met for o in patients),{'patient_count':len(patients)})
        valid_allocation=True
        for t in traces[mode]:
            active=[o for o in values if o.started_at<=t.instant<o.completed_at]
            slots=sum(3 if o.template=='parallel-drafts+judge' else 1 for o in active)
            human_slots=sum(3 if o.template=='parallel-drafts+judge' else 1 for o in active if o.item.persona in sim.HUMAN_WAITING_PERSONAS)
            valid_allocation &= (slots==t.in_flight<=sim.CAPACITY and human_slots==t.allocated_human_waiting and slots-human_slots==t.allocated_patient)
        check(f'{mode}: trace allocation matches task intervals',valid_allocation,{'trace_points':len(traces[mode])})
        if mode!=sim.INTENT_AWARE:
            starts=[o.started_at for o in sorted(values,key=lambda o:o.item.id)]
            check(f'{mode}: FIFO start order',starts==sorted(starts),{})
    def choices(mode):
        return {o.item.id:(o.model,o.effort,o.template,o.modeled_execution_cost_usd) for o in outcomes[mode]}
    check('Contract-based per-task execution controls and costs match',choices(sim.CONTRACT_FIFO)==choices(sim.INTENT_AWARE),{})
    cost_static=sum(Decimal(str(o.modeled_execution_cost_usd)) for o in outcomes[sim.STATIC])
    cost_aware=sum(Decimal(str(o.modeled_execution_cost_usd)) for o in outcomes[sim.INTENT_AWARE])
    reduction=(1-cost_aware/cost_static)*100
    check('Modeled cost reduction rounds to paper 42.2%',f'{reduction:.1f}'=='42.2',{'percent':str(reduction)})
    decoded=json.loads((ROOT/'experiments/expected/figure.json').read_text())
    for mode in (sim.CONTRACT_FIFO,sim.INTENT_AWARE):
        observed=Counter((o.submitted_at.isoformat(),round(o.queue_wait_hours*60)) for o in outcomes[mode] if o.item.persona in sim.HUMAN_WAITING_PERSONAS)
        drawn=Counter((p['arrival_utc'],p['wait_minutes']) for p in decoded['human_waiting'][mode]['points'])
        check(f'{mode}: all 32 figure panel (a) markers match',observed==drawn,{'raw_markers':sum(observed.values()),'figure_markers':sum(drawn.values())})
        completed=Counter(o.completed_at.isoformat() for o in outcomes[mode] if o.item.persona not in sim.HUMAN_WAITING_PERSONAS)
        drawn_completions=Counter({p['completion_utc']:p['new_completions'] for p in decoded['patient_completions'][mode]})
        check(f'{mode}: every patient step in figure panel (b) matches',completed==drawn_completions,{'raw_completions':sum(completed.values())})
        if mode==sim.INTENT_AWARE:
            patients=[o for o in outcomes[mode] if o.item.persona not in sim.HUMAN_WAITING_PERSONAS]
            check('Intent-aware patient execution starts at Day 2 02:00 and ends at 10:00',time_label(min(o.started_at for o in patients))=='D2 02:00' and time_label(max(o.completed_at for o in patients))=='D2 10:00',{})
    write_json(run/'checks.json',checks)
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, capture_output=True)
    manifest={
        'run_started_utc':began.isoformat(),'run_finished_utc':datetime.now(timezone.utc).isoformat(),
        'git_revision':revision.stdout.strip() if revision.returncode == 0 else None,
        'python':platform.python_version(),'provider':'MockProvider; no network calls',
        'command':'uv run python experiments/reproduce.py',
        'experiment_equivalent':'intent-scheduler demo --bursts 8',
        'capture_method':'Temporary Gateway subclass records submissions, execution results, and final task views before in-memory store closes. Core simulation, scheduling, policy, and execution rules unchanged.',
        'bursts':8,'capacity':sim.CAPACITY,'step_seconds':sim.STEP.total_seconds(),
        'simulated_start':sim.START.isoformat(),
        'source_hashes':{str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'src/intent_scheduler').glob('*.py'))},
        'exporter_sha256':sha256(Path(__file__).read_bytes()).hexdigest(),
        'dependency_lock_sha256':sha256((ROOT/'uv.lock').read_bytes()).hexdigest(),
        'scope':'Checks establish agreement for this synthetic workload, not real-provider performance or quality.',
    }
    manifest['expected_hashes'] = {p.name: sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'experiments/expected').glob('*.json'))}
    write_json(run/'manifest.json',manifest)
    lines=['# Fresh-run checks against the paper','',f"Source revision `{manifest['git_revision']}`. Run began {began.isoformat()}.",'',
           'The experiment used only MockProvider. Checks use fresh per-task timestamps and costs, and compare against the bundled table fixture and decoded rounded figure coordinates. No figure or paper values were rewritten.','',
           '| Check | Result |','|---|---|']
    for c in checks: lines.append(f"| {c['check']} | {'PASS' if c['passed'] else 'FAIL'} |")
    (run/'checks.md').write_text('\n'.join(lines)+'\n')
    print(report)
    print(f'\nChecks: {sum(c["passed"] for c in checks)}/{len(checks)} passed')
    print('SAVED_RUN='+str(run))
    if not all(c['passed'] for c in checks):
        raise SystemExit('Some paper comparisons failed; inspect checks.json.')


if __name__=='__main__':
    asyncio.run(main())
