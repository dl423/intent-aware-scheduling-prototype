# Reading generated results

Run `uv run python experiments/reproduce.py` from the repository root. The exporter preserves a new run under `results/` and prints its location.

| File | Meaning |
|---|---|
| `report.txt`, `report.html` | Summary for all three modes |
| `checks.json`, `checks.md` | Explicit comparisons and pass/fail outcomes |
| `manifest.json` | Public repository revision, source and fixture hashes, configuration, and run timestamps |
| `workload.json` | The 72 synthetic requests and their supplied contracts |
| `<mode>/tasks.md` | Per-task start, completion, deadline, wait, and modeled cost |
| `<mode>/trace.md` | Allocated slots and ready queues at ten-minute ticks |
| `<mode>/sim-NNN.json` | Task decisions, transitions, mock responses, and outcome |
| `<mode>/table-derivation.json` | Counts, sorted waits, percentile index, and cost sum |
| `<mode>/outcomes.json`, `traces.json` | Full machine-readable outcomes and traces |
| `<mode>/provider-calls.json`, `accounting.json` | Mock calls and accounting totals |

D1 and D2 are fixed simulated dates, July 12 and 13, 2026. Database event timestamps and run timestamps use real execution time. Do not subtract timestamps from different clocks.

Queue wait is start minus submission. Completion on the deadline counts as meeting it. Human-waiting p95 selects index `round((n-1)*0.95)` from sorted waits. For 32 human-waiting tasks this is the 30th value. Modeled execution costs use policy estimates; mock call accounting includes extraction and judging overhead and is a separate quantity.

The bundled reference fixtures contain table values and decoded Figure 4 coordinates snapped to the ten-minute grid. They omit private manuscript text and historical logs. Comparisons establish reproduction of this deterministic case, not general scheduling superiority. Generated outputs are local and gitignored; inspect them before choosing to distribute any run.
