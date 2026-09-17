# Intent-Aware Scheduling Prototype

A single-node research prototype that maps task requirements over quality, deadline, cost, and attention into execution choices and scheduling decisions. The deterministic case study compares static execution and FIFO scheduling, contract-selected execution with FIFO, and contract-selected execution with intent-aware scheduling.

## Quickstart

Python 3.11 or newer and uv are required. Run from this repository's root.

```bash
uv sync --extra dev
uv run intent-scheduler demo --bursts 8
```

The experiment uses a deterministic mock provider. It needs no API key and makes no external provider calls. Installing dependencies may require network access.

To save per-task records, traces, and a checked report:

```bash
uv run python experiments/reproduce.py
```

Outputs go into a new timestamped directory under `results/`, which is gitignored. The exporter checks all 72 outcomes per mode, capacity accounting, FIFO ordering, equivalent contract-based execution choices, and agreement with the bundled table and figure reference values. It exits unsuccessfully if any check fails. The fixtures are expected values, not independent validation of the model.

## Expected results

| Mode | Critical deadlines | All deadlines | Human-waiting p95 queue wait | Modeled execution cost |
|---|---|---|---|---|
| Static | 1/8 | 52/72 | 3.83 h | $1.8720 |
| Contract-FIFO | 1/8 | 56/72 | 3.50 h | $1.0816 |
| Intent-aware | 8/8 | 72/72 | 1.00 h | $1.0816 |

All 40 patient tasks meet their deadlines in each mode. Intent-aware scheduling increases their p95 queue wait from 4.00 to 19.33 hours relative to contract-FIFO. These are results for a fixed synthetic workload with assumed service durations and configured costs. They do not establish real-provider performance, delivered quality, extraction accuracy, or requester experience.

## Navigation

- [Architecture and experiment assumptions](docs/architecture.md)
- [Reading generated results](docs/results.md)
- [Optional service usage](docs/service.md)
- [Implementation](src/intent_scheduler/) and [tests](tests/)
- [Expected table values](experiments/expected/table.json) and [figure values](experiments/expected/figure.json)

## Tests

```bash
uv run pytest
```

The tests use mock or stub providers. The separate `validate-real` command makes paid provider calls and is not required to reproduce the experiment.

## Scope

Queues are in process; SQLite records are not used to restore queued tasks after restart. There is no durable multi-step workflow or resumable human checkpoint protocol. The policy's quality settings are not calibrated quality guarantees. This repository contains the paper prototype only.

## License

This project is released under the [MIT License](LICENSE).
