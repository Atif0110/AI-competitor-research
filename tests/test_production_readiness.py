from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import app
from app.config import settings
from app.llm.client import _resolve_provider_order


def test_api_key_guard_fails_closed_when_live(monkeypatch):
    from app.api import _require_api_key
    from fastapi import HTTPException
    monkeypatch.setattr(settings, "api_key_required", True)
    monkeypatch.setattr(settings, "api_key", "acr_test_secret")
    try:
        _require_api_key("wrong")
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 401
    _require_api_key("acr_test_secret")


def test_health_exposes_storage_and_provider_chain(monkeypatch):
    monkeypatch.setattr(settings, "api_key_required", False)
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert "storage_backend" in body
    assert "active_llm_provider" in body
    assert "llm_provider_chain" in body


def test_cors_header_for_frontend(monkeypatch):
    monkeypatch.setattr(settings, "api_key_required", False)
    client = TestClient(app)
    response = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"

def test_provider_auto_selection(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "auto")
    monkeypatch.setattr(settings, "apinex_api_key", None)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "groq_api_key", None)
    assert _resolve_provider_order() == ["anthropic"]
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    assert _resolve_provider_order() == ["anthropic", "openai"]
    monkeypatch.setattr(settings, "llm_provider", "openai")
    assert _resolve_provider_order() == ["openai", "anthropic"]
    assert _resolve_provider_order()[-1] == "anthropic"


def test_free_tier_providers_lead_the_chain(monkeypatch):
    """Free allowances must be attempted before metered providers."""
    monkeypatch.setattr(settings, "llm_provider", "auto")
    monkeypatch.setattr(settings, "apinex_api_key", "sk-apinex-test")
    monkeypatch.setattr(settings, "groq_api_key", "gsk-test")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    order = _resolve_provider_order()
    assert order == ["apinex", "groq", "anthropic", "openai"]
    assert settings.provider_tiers == {
        "apinex": "free", "groq": "free", "anthropic": "paid", "openai": "paid",
    }


def test_forced_provider_leads_then_free_fallbacks(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "apinex_api_key", "sk-apinex-test")
    monkeypatch.setattr(settings, "groq_api_key", "gsk-test")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr(settings, "openai_api_key", None)
    assert _resolve_provider_order() == ["anthropic", "apinex", "groq"]


def test_forced_provider_without_key_raises(monkeypatch):
    from app.llm.client import LLMError
    monkeypatch.setattr(settings, "llm_provider", "apinex")
    monkeypatch.setattr(settings, "apinex_api_key", None)
    with pytest.raises(LLMError):
        _resolve_provider_order()


def test_health_reports_free_tier_and_models(monkeypatch):
    monkeypatch.setattr(settings, "api_key_required", False)
    client = TestClient(app)
    body = client.get("/health").json()
    assert "provider_tiers" in body
    assert "llm_models" in body and "apinex" in body["llm_models"]
    assert "llm_provider_chain" in body


def test_provider_quota_error_triggers_cooldown(monkeypatch):
    """A rate-limited provider must be skipped, not retried on every call."""
    from app.llm.client import LLMClient, LLMError

    monkeypatch.setattr(settings, "llm_provider", "auto")
    monkeypatch.setattr(settings, "apinex_api_key", "sk-apinex-test")
    monkeypatch.setattr(settings, "groq_api_key", "gsk-test")

    calls = {"apinex": 0, "groq": 0}

    class _RateLimited:
        name = "apinex"

        def complete(self, messages):
            calls["apinex"] += 1
            raise LLMError("apinex http 429: quota exceeded")

    class _Good:
        name = "groq"

        def complete(self, messages):
            calls["groq"] += 1
            return "ok"

    client = LLMClient.__new__(LLMClient)
    client._providers = [("apinex", _RateLimited()), ("groq", _Good())]
    client._current = 0
    client.calls = 0
    client.provider_attempts = 0
    client.fallbacks = []
    client.last_error = None
    client._fallback_since = None
    client._cooldowns = {}
    client.cooldown_seconds = 60

    assert client.complete("s", "u") == "ok"
    assert client.complete("s", "u") == "ok"
    # apinex failed once, then stayed in cooldown for the second call
    assert calls == {"apinex": 1, "groq": 2}
    assert "apinex" in client.telemetry()["cooldown_providers"]


def test_report_run_id_sanitised_before_fs():
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    client = TestClient(app)
    assert client.get("/reports/..%2F..%2Fetc%2Fpasswd").status_code in (404, 422)
    assert client.get("/reports/not..valid..run").status_code == 422
    assert client.get("/reports/does-not-exist-run").status_code == 404


def test_evidence_content_endpoint_is_routable():
    """/evidence/content must not be shadowed by /evidence/{run_id}."""
    monkey = None
    client = TestClient(app)
    response = client.get("/evidence/content?source_url=https://example.com/p")
    assert response.status_code == 404  # routed to the endpoint, evidence missing
    assert response.json()["detail"] == "evidence not found"
    assert client.get("/evidence/bad..run").status_code == 422
