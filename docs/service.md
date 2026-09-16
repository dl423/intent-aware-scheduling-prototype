# Optional REST service

The paper experiment runs without this service. For local API exploration:

```bash
uv run intent-scheduler serve
```

Mock mode is the default. Open `http://127.0.0.1:8000/docs` for request schemas. The API supports task submission and inspection, queued-task cancellation, deadline changes, and nudges. SQLite defaults to `scheduler.db`; queues are not restored after restart.

The standalone repository loads only its own `.env` file. Process environment variables take precedence. `.env.example` contains empty credential placeholders. Never commit a populated environment file or service database.

Real-provider mode is optional and incurs provider charges. Configure `OPENAI_API_KEY` and `INTENT_SCHEDULER_PROVIDER=openai` only when intentionally using it. Role model names and fallback names can be set through the `INTENT_SCHEDULER_INTENT_*`, `INTENT_SCHEDULER_CANDIDATE_*`, and `INTENT_SCHEDULER_JUDGE_*` settings shown in [config.py](../src/intent_scheduler/config.py). Task model choices and cost assumptions are configured in the implementation. Verify those settings against your provider before real use. The separate `validate-real` command also makes paid calls; it is not part of the reproduction instructions.
