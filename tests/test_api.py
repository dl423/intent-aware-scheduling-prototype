import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from intent_scheduler.app import app


def test_submit_status_and_deadline_update(tmp_path, monkeypatch):
    monkeypatch.setenv("INTENT_SCHEDULER_PROVIDER", "mock")
    monkeypatch.setenv("INTENT_SCHEDULER_DB", str(tmp_path / "api.db"))
    with TestClient(app) as client:
        assert client.get("/health").json()["provider"] == "mock"
        response = client.post(
            "/tasks",
            json={
                "request_text": "Give me a rough draft when convenient.",
                "quality_floor": "draft",
                "max_cost_usd": 1.0,
                "attention_profile": "ask-freely",
            },
        )
        assert response.status_code == 202
        task_id = response.json()["id"]

        deadline = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        update = client.patch(f"/tasks/{task_id}/deadline", json={"deadline": deadline})
        assert update.status_code == 200
        assert update.json()["contract"]["provenance"]["deadline"] == "explicit"

        status = None
        for _ in range(50):
            status = client.get(f"/tasks/{task_id}").json()
            if status["status"] == "completed":
                break
            time.sleep(0.01)
        assert status["status"] == "completed"
        assert status["quality"]["proxy"] is True
        assert status["metrics"]["model"] == "gpt-5.6-luna"


def test_unknown_task_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv("INTENT_SCHEDULER_DB", str(tmp_path / "api.db"))
    with TestClient(app) as client:
        assert client.get("/tasks/missing").status_code == 404
