# REST Service

The optional REST service supports API use separately from the paper experiment. Start it from the repository root.

```bash
uv run intent-scheduler serve
```

Mock mode is the default. The [local API documentation](http://127.0.0.1:8000/docs) provides schemas for submitting and inspecting tasks, cancelling queued work, updating deadlines, and nudging a task to indicate that its requester is waiting.

SQLite stores task records in `scheduler.db` by default. Queues remain in memory and are not restored after restart.

## Configuration

The service loads this repository's `.env` file, with process environment variables taking precedence. The [example file](../.env.example) contains empty credential placeholders. Keep credentials and service databases out of version control.

## Real-Provider Mode

Configure `OPENAI_API_KEY` and set `INTENT_SCHEDULER_PROVIDER=openai` to enable paid provider calls. Models and fallbacks for intent extraction, candidate selection, and quality judging use the respective `INTENT_SCHEDULER_INTENT_*`, `INTENT_SCHEDULER_CANDIDATE_*`, and `INTENT_SCHEDULER_JUDGE_*` settings in [config.py](../src/intent_scheduler/config.py).

Task execution models and cost assumptions are defined in the implementation. Check them against the provider before use. The separate `validate-real` command also makes paid calls. Neither real-provider mode nor `validate-real` is needed for reproduction.
