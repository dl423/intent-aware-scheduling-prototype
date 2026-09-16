from intent_scheduler.store import TaskStore


def test_store_round_trip_and_events():
    store = TaskStore(":memory:")
    store.put("task-1", "queued", {"id": "task-1", "status": "queued"})
    store.event("task-1", "submitted", {"source": "explicit"})

    assert store.get("task-1")["status"] == "queued"
    assert store.list()[0]["id"] == "task-1"
    assert store.events("task-1")[0]["kind"] == "submitted"
