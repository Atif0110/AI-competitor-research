import pytest
from pydantic import ValidationError

from app.api import DeepResearchRequest
from app.schemas import Competitor, CompetitorTarget
from app.url_safety import is_public_http_url, safe_http_get


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",
    "http://10.2.3.4/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://localhost/",
    "http://service.internal/",
    "http://user:pass@example.com/",
    "http://example.com:8080/",
    "file:///etc/passwd",
])
def test_non_public_urls_rejected(url):
    assert not is_public_http_url(url)


def test_public_website_is_allowed_and_normalized():
    target = CompetitorTarget(company="Acme", website="acme.example.com")
    assert target.website == "https://acme.example.com"


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost/"])
def test_competitor_private_site_rejected(url):
    with pytest.raises(ValidationError):
        Competitor(name="Internal", website=url)


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost/"])
def test_deep_research_private_url_rejected(url):
    with pytest.raises(ValidationError):
        DeepResearchRequest(mode="url", url=url)


def test_safe_get_blocks_private_redirect(monkeypatch):
    class Response:
        is_redirect = True
        is_permanent_redirect = False
        headers = {"Location": "http://169.254.169.254/latest/meta-data/"}

    def fake_get(*args, **kwargs):
        return Response()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(ValueError, match="blocked"):
        safe_http_get("https://example.com/")
