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


def test_evidence_is_retained_for_each_run_even_when_source_text_matches(tmp_path):
    store = OfferStore(database_url=f"sqlite:///{tmp_path / 'per-run-evidence.db'}")
    content = "The Pro plan is $29 per month."
    store.save_evidence("run-one", "https://acme.example/pricing", content)
    store.save_evidence("run-two", "https://acme.example/pricing", content)

    assert len(store.evidence_for_run("run-one")) == 1
    assert len(store.evidence_for_run("run-two")) == 1
    assert store.get_evidence("https://acme.example/pricing", "run-two") == content


def test_saved_chat_history_lists_and_reopens_the_same_conversation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    import app.api as api_module
    from app.config import settings
    from app.research.pipeline import DeepResearchPipeline  # initialize package exports before the document store
    from app.storage.documents import DocumentStore

    database_url = f"sqlite:///{tmp_path / 'saved-chat-history.db'}"
    store = OfferStore(database_url=database_url)
    documents = DocumentStore(database_url=database_url)
    documents.create_session("saved-chat-123", "research-run-123", title="Which plan costs less?")
    documents.add_message("saved-chat-123", "user", "Which plan costs less?")
    documents.add_message("saved-chat-123", "assistant", "Captured evidence does not compare these plans.")
    monkeypatch.setattr(api_module, "_store", store)
    monkeypatch.setattr(api_module, "_deep_pipeline", lambda: SimpleNamespace(documents=documents))
    monkeypatch.setattr(settings, "api_key_required", False)
    client = TestClient(api_module.app)

    listed = client.get("/research/chats?limit=10")
    assert listed.status_code == 200
    assert listed.json()[0]["session_id"] == "saved-chat-123"
    assert listed.json()[0]["message_count"] == 2
    assert "compare" in listed.json()[0]["last_message"]

    reopened = client.get("/research/chat/saved-chat-123")
    assert reopened.status_code == 200
    assert reopened.json()["run_id"] == "research-run-123"
    assert len(reopened.json()["messages"]) == 2


def test_competitive_run_chat_is_scoped_and_cited(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    import app.api as api_module
    from app.config import settings
    from app.schemas import CompetitorTarget
    from app.research.pipeline import DeepResearchPipeline  # initialize package exports before its store module
    from app.storage.documents import DocumentStore

    database_url = f"sqlite:///{tmp_path / 'competitive-chat.db'}"
    store = OfferStore(database_url=database_url)
    store.save_job("chat-run-123", {
        "job_id": "chat-run-123", "status": "completed",
        "target": {"company": "Acme Audio", "website": "https://acme.example"},
        "events": [],
    })
    store.record_run({
        "run_id": "chat-run-123", "started_at": "2026-10-16T12:00:00Z",
        "mode": "live", "target_company": "Acme Audio", "runtime_s": 9,
    })
    store.save_evidence(
        "chat-run-123", "https://acme.example/pricing",
        "Pricing: The Pro plan costs $29 per month and includes unlimited projects.",
    )
    documents = DocumentStore(database_url=database_url)
    monkeypatch.setattr(api_module, "_store", store)
    monkeypatch.setattr(api_module, "_deep_pipeline", lambda: SimpleNamespace(documents=documents))
    monkeypatch.setattr(settings, "api_key_required", False)
    monkeypatch.setattr(settings, "llm_provider", "demo")
    monkeypatch.setattr(settings, "demo_mode_enabled", False)
    client = TestClient(api_module.app)

    response = client.post("/research/chat", json={
        "question": "What does the Pro plan cost per month?",
        "run_id": "chat-run-123", "session_id": "chat-session-123",
    })
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["answerable"] is True
    assert "$29" in answer["message"]
    assert answer["citations"][0]["url"] == "https://acme.example/pricing"
    history = client.get("/research/chat/chat-session-123")
    assert history.status_code == 200
    assert len(history.json()["messages"]) == 2


def test_job_progress_persists_current_phase_and_bounds_event_log(monkeypatch):
    import app.api as api_module

    saved = {}

    class Store:
        @staticmethod
        def save_job(job_id, payload):
            saved["job_id"] = job_id
            saved.update(payload)

    old_jobs = api_module._jobs
    monkeypatch.setattr(api_module, "_jobs", {
        "progress-123": {"job_id": "progress-123", "status": "running",
                          "events": [{"event": f"phase-{i}"} for i in range(220)]}
    })
    monkeypatch.setattr(api_module, "_store", Store())
    try:
        api_module._progress_for_job("progress-123", {"event": "extraction_started"})
        assert len(saved["events"]) == 200
        assert saved["current_event"]["event"] == "extraction_started"
        assert saved["status"] == "running"
    finally:
        api_module._jobs = old_jobs
