# Reading the Results

Run the reproduction script from the repository root.

```bash
uv run python experiments/reproduce.py
```

The script saves each run in a new timestamped directory under `results/` and prints its location. Start with `report.txt` or `report.html` for the comparison and `checks.md` for verification outcomes.

## Saved Files

`<mode>` is `static`, `contract-fifo`, or `intent-aware`.

| File | Contents |
|---|---|
| `report.txt`, `report.html` | Summary for all three configurations |
| `checks.json`, `checks.md` | Comparisons and pass or fail results |
| `manifest.json` | Repository revision, source and reference hashes, experiment configuration, and run timestamps |
| `workload.json` | The 72 requests and their explicit contracts |
| `<mode>/tasks.md` | Task submission, start, completion, deadline, queue wait, and modeled cost |
| `<mode>/trace.md` | Reserved slots and ready-task counts at ten-minute ticks |
| `<mode>/sim-NNN.json` | One task's decisions, transitions, mock responses, and outcome |
| `<mode>/table-derivation.json` | Counts, sorted waits, percentile index, and cost sum for the results table |
| `<mode>/outcomes.json`, `<mode>/traces.json` | Complete task outcomes and queue traces |
| `<mode>/provider-calls.json`, `<mode>/accounting.json` | Mock calls and accounting totals |

## Timing and Cost

Task times use the simulated clock. D1 and D2 denote July 12 and July 13, 2026. Database events and run timestamps use actual execution time, so they cannot be subtracted from simulated timestamps to calculate task durations.

Queue wait runs from submission to execution start. Completion on or before the deadline counts as meeting it. To calculate human-waiting p95, the script sorts the waits of the `n` tasks whose requesters are designated as waiting and selects zero-based index `round((n-1)*0.95)`. For 32 tasks, this is the 30th value.

The results table sums policy estimates of execution cost. Mock accounting totals the calls made during the run, including extraction and judging overhead. These are separate cost measures.

## Reproduction Checks

The script checks completion of all 72 tasks per configuration, slot allocation, FIFO ordering, and matching execution choices between the contract-based configurations. It also compares results with the bundled table values and decoded Figure 4 coordinates, rounded to the ten-minute grid. The reference files contain numerical values without manuscript text or historical logs.

Passing these checks establishes reproduction of this case, without independently validating its assumptions or demonstrating general scheduling superiority. Generated outputs are excluded from Git. Review their contents before distributing a run.
