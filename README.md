# Intent-Aware Scheduling Prototype

This companion prototype illustrates how task requirements for quality, deadline, cost, and interruption tolerance can guide execution and scheduling on a single node.

The experiment compares three configurations on the same synthetic workload. The static reference gives every task the same execution settings and serves tasks in first-in, first-out (FIFO) order. Contract-FIFO varies execution settings according to task requirements while retaining FIFO scheduling. Intent-aware scheduling uses those same settings and also prioritizes or defers tasks according to their requirements.

## Quickstart

With Python 3.11 or newer and uv installed, run these commands from the repository root.

```bash
uv sync --extra dev
uv run intent-scheduler demo --bursts 8
```

The deterministic mock provider requires no API key and makes no external calls. Dependency installation may require network access.

To save and check the results, run the reproduction script.

```bash
uv run python experiments/reproduce.py
```

The script saves reports, task records, and queue traces in a timestamped directory under `results/`, which is excluded from Git. It checks the run against the bundled table and figure values and exits with an error if any check fails. The [results guide](docs/results.md) explains the files and checks.

## Expected Results

Queue wait is the time from submission to execution start. The table reports its 95th percentile for tasks whose contracts specify that a person is waiting. Completion on the deadline counts as meeting it.

| Configuration | Critical deadlines met | All deadlines met | Human-waiting p95 queue wait | Modeled execution cost |
|---|---|---|---|---|
| Static | 1/8 | 52/72 | 3.83 h | $1.8720 |
| Contract-FIFO | 1/8 | 56/72 | 3.50 h | $1.0816 |
| Intent-aware | 8/8 | 72/72 | 1.00 h | $1.0816 |

Intent-aware scheduling reduces waits for human-waiting tasks by delaying patient tasks. All 40 patient tasks meet their deadlines in every configuration, although their p95 queue wait rises from 4.00 hours under contract-FIFO to 19.33 hours under intent-aware scheduling.

These results use a fixed synthetic workload with assumed service durations and prices. They do not establish real-provider performance, delivered quality, extraction accuracy, or requester experience.

## Documentation

- [Architecture and experiment assumptions](docs/architecture.md)
- [Reading the results](docs/results.md)
- [REST service and provider configuration](docs/service.md)
- [Source code](src/intent_scheduler/) and [tests](tests/)
- [Reference table values](experiments/expected/table.json) and [figure values](experiments/expected/figure.json)

## Tests

```bash
uv run pytest
```

Tests use mock or stub providers. The optional `validate-real` command makes paid calls and is not required for reproduction.

## Scope

This repository contains the paper prototype only. Queues are held in memory and are not restored from SQLite records after restart. The prototype does not provide durable workflows spanning multiple steps or a protocol for pausing and resuming tasks around human input. Quality settings select execution policies rather than guarantee measured result quality.

## License

Released under the [MIT License](LICENSE).
