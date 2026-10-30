from fastapi.testclient import TestClient

from app.api import _require_api_key, _require_public_research_or_api_key, app
from app.config import settings


def test_public_workspace_bypasses_visitor_api_key(monkeypatch):
    monkeypatch.setattr(settings, "public_workspace_mode", True)
    monkeypatch.setattr(settings, "api_key_required", True)
    monkeypatch.setattr(settings, "api_key", "server-side-only")

    from starlette.requests import Request

    request = Request({
        "type": "http", "method": "POST", "path": "/research/jobs",
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("testserver", 80), "client": ("127.0.0.1", 12345),
        "http_version": "1.1", "root_path": "",
    })
    _require_public_research_or_api_key(request, None)
    from fastapi import HTTPException
    import pytest
    with pytest.raises(HTTPException) as error:
        _require_api_key(None)
    assert error.value.status_code == 401

    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["public_workspace"] is True
    assert response.json()["auth_required"] is False
    assert TestClient(app).get("/research/runs").status_code == 200


def test_private_workspace_still_requires_api_key(monkeypatch):
    from fastapi import HTTPException
    import pytest

    monkeypatch.setattr(settings, "public_workspace_mode", False)
    monkeypatch.setattr(settings, "api_key_required", True)
    monkeypatch.setattr(settings, "api_key", "server-side-only")

    with pytest.raises(HTTPException) as error:
        _require_api_key(None)
    assert error.value.status_code == 401


def test_private_workspace_protects_read_endpoints(monkeypatch):
    monkeypatch.setattr(settings, "public_workspace_mode", False)
    monkeypatch.setattr(settings, "api_key_required", True)
    monkeypatch.setattr(settings, "api_key", "private-test-key")
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/research/runs").status_code == 401
    assert client.get("/reports").status_code == 401
    assert client.get("/research/runs", headers={"X-API-Key": "private-test-key"}).status_code == 200


def test_research_errors_do_not_echo_provider_details(monkeypatch):
    import app.api as api_module

    monkeypatch.setattr(settings, "public_workspace_mode", True)
    monkeypatch.setattr(settings, "api_key_required", True)

    def fail(_target):
        raise RuntimeError("secret-provider-response-body")

    monkeypatch.setattr(api_module._pipeline, "run", fail)
    response = TestClient(app).post("/research", json={
        "company": "Acme", "website": "https://acme.example.com",
    })
    assert response.status_code == 500
    assert "secret-provider-response-body" not in response.text
    assert "Reference:" in response.json()["detail"]


def test_public_research_rejects_private_network_targets(monkeypatch):
    monkeypatch.setattr(settings, "public_workspace_mode", True)
    monkeypatch.setattr(settings, "api_key_required", True)
    import app.api as api_module
    with api_module._rate_lock:
        api_module._rate_window.clear()
        api_module._public_hour_window.clear()
    client = TestClient(app)
    response = client.post("/research/jobs", json={
        "company": "Internal service",
        "website": "http://127.0.0.1/admin",
    })
    assert response.status_code == 422

    deep = client.post("/research/deep", json={
        "mode": "url", "url": "http://169.254.169.254/latest/meta-data/",
    })
    assert deep.status_code == 422
