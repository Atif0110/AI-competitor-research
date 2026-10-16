from app.llm.client import LLMClient
from app.storage.db import OfferStore


def test_competitive_job_and_run_usage_survive_store_recreation(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'history.db'}"
    store = OfferStore(database_url=database_url)
    store.save_job("run-123", {
        "job_id": "run-123",
        "status": "running",
        "target": {"company": "Acme"},
        "events": [{"event": "scrape_succeeded"}],
    })
    store.record_run({
        "run_id": "run-123",
        "started_at": "2026-10-16T12:00:00Z",
        "mode": "live",
        "target_company": "Acme",
        "runtime_s": 12.5,
        "llm_calls": 2,
        "estimated_tokens": 345,
    })

    restarted_store = OfferStore(database_url=database_url)
    assert restarted_store.get_job("run-123")["events"][0]["event"] == "scrape_succeeded"
    metrics = restarted_store.recent_runs(limit=1)[0]
    assert metrics["estimated_tokens"] == 345
    assert metrics["llm_calls"] == 2


def test_jobs_running_at_restart_are_marked_interrupted(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'restart.db'}"
    store = OfferStore(database_url=database_url)
    store.save_job("run-456", {"job_id": "run-456", "status": "running"})

    OfferStore(database_url=database_url).reconcile_jobs_after_restart()
    job = store.get_job("run-456")
    assert job["status"] == "interrupted"
    assert "restarted" in job["error"]


def test_llm_usage_scope_tracks_calls_and_estimates():
    client = LLMClient.__new__(LLMClient)
    client._providers = [("demo", type("Provider", (), {"complete": lambda self, messages: "answer"})())]
    client._current = 0
    client.calls = 0
    client.estimated_tokens = 0
    client.provider_attempts = 0
    client.fallbacks = []
    client.last_error = None
    client._fallback_since = None
    client._cooldowns = {}
    client.cooldown_seconds = 60

    with client.usage_scope():
        assert client.complete("system prompt", "research question") == "answer"
        usage = client.telemetry()
        assert usage["calls"] == 1
        assert usage["provider_attempts"] == 1
        assert usage["estimated_tokens"] > 0
    assert client.telemetry()["calls"] == 1


def test_run_history_api_merges_research_paths_and_reopens_saved_job(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    import app.api as api_module
    from app.config import settings

    store = OfferStore(database_url=f"sqlite:///{tmp_path / 'api-history.db'}")
    store.save_job("comp-789", {
        "job_id": "comp-789", "status": "completed",
        "target": {"company": "Acme"}, "events": [],
        "result": {"duration_s": 8, "metrics": {"llm_calls": 1}},
    })
    store.record_run({
        "run_id": "comp-789", "started_at": "2026-10-16T12:00:00Z",
        "mode": "live", "target_company": "Acme", "runtime_s": 8,
        "llm_calls": 1, "estimated_tokens": 45,
    })

    class Documents:
        @staticmethod
        def list_runs(limit):
            return [{"run_id": "deep-321", "status": "partial", "title": "Pricing review",
                     "created_at": "2026-10-16T13:00:00Z", "coverage": {"pages_fetched": 2}}]

    monkeypatch.setattr(api_module, "_store", store)
    monkeypatch.setattr(api_module, "_deep_pipeline", lambda: SimpleNamespace(documents=Documents()))
    monkeypatch.setattr(settings, "api_key_required", False)
    client = TestClient(api_module.app)

    response = client.get("/research/runs?limit=8")
    assert response.status_code == 200
    runs = response.json()
    assert {(run["kind"], run["run_id"]) for run in runs} == {
        ("competitive", "comp-789"), ("deep", "deep-321")
    }
    reopened = client.get("/research/jobs/comp-789")
    assert reopened.status_code == 200
    assert reopened.json()["result"]["duration_s"] == 8
