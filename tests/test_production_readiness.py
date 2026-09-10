from __future__ import annotations

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
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "groq_api_key", None)
    assert _resolve_provider_order() == ["anthropic"]
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    assert _resolve_provider_order() == ["anthropic", "openai"]
    monkeypatch.setattr(settings, "llm_provider", "openai")
    assert _resolve_provider_order() == ["openai", "anthropic"]
    monkeypatch.setattr(settings, "groq_api_key", "gsk-test")
    assert _resolve_provider_order()[-1] == "groq"
