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


def test_private_workspace_still_requires_api_key(monkeypatch):
    from fastapi import HTTPException
    import pytest

    monkeypatch.setattr(settings, "public_workspace_mode", False)
    monkeypatch.setattr(settings, "api_key_required", True)
    monkeypatch.setattr(settings, "api_key", "server-side-only")

    with pytest.raises(HTTPException) as error:
        _require_api_key(None)
    assert error.value.status_code == 401
