import pytest
from pydantic import ValidationError

from app.api import DeepResearchRequest
from app.schemas import Competitor, CompetitorTarget
from app.url_safety import (
    DNSPinnedHTTPAdapter,
    _DNSPinnedHTTPConnection,
    _resolve_public_addresses,
    create_public_session,
    is_public_http_url,
    public_fetch_guard_enabled,
    safe_http_get,
)


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


def test_research_target_collection_sizes_are_bounded():
    competitors = [
        {"name": f"Company {index}", "website": f"https://company{index}.example.com"}
        for index in range(51)
    ]
    with pytest.raises(ValidationError):
        CompetitorTarget(company="Acme", website="https://acme.example.com", competitors=competitors)

    with pytest.raises(ValidationError):
        DeepResearchRequest(mode="compare", competitors=["Company"] * 11)


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost/"])
def test_competitor_private_site_rejected(url):
    with pytest.raises(ValidationError):
        Competitor(name="Internal", website=url)


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost/"])
def test_deep_research_private_url_rejected(url):
    with pytest.raises(ValidationError):
        DeepResearchRequest(mode="url", url=url)


def test_deep_research_source_urls_are_normalized_and_public_only():
    request = DeepResearchRequest(
        mode="url",
        url="https://example.org",
        source_urls=["arxiv.org/pdf/1234.5678.pdf", "https://example.org/paper.pdf"],
    )
    assert request.source_urls == [
        "https://arxiv.org/pdf/1234.5678.pdf",
        "https://example.org/paper.pdf",
    ]

    with pytest.raises(ValidationError):
        DeepResearchRequest(
            mode="url",
            url="https://example.org",
            source_urls=["http://169.254.169.254/latest/meta-data/"],
        )


def test_safe_get_blocks_private_redirect(monkeypatch):
    class Response:
        is_redirect = True
        is_permanent_redirect = False
        headers = {"Location": "http://169.254.169.254/latest/meta-data/"}

        @staticmethod
        def close():
            return None

    def fake_get(*args, **kwargs):
        return Response()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(ValueError, match="blocked"):
        safe_http_get("https://example.com/")


def test_dns_resolver_rejects_mixed_public_and_private_answers(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(ValueError, match="non-public"):
        _resolve_public_addresses("example.com", 443)


def test_dns_resolver_rejects_ipv6_translation_addresses(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("64:ff9b::a00:1", 443, 0, 0)),
        ],
    )
    with pytest.raises(ValueError, match="non-public"):
        _resolve_public_addresses("example.com", 443)


def test_connection_uses_validated_numeric_address_and_keeps_hostname(monkeypatch):
    import socket
    from urllib3.util import connection as urllib3_connection

    connected = []
    expected_answer = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 80))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [expected_answer])

    sentinel_socket = object()

    def fake_connect(address, timeout, source_address=None, socket_options=None):
        connected.append(address)
        return sentinel_socket

    monkeypatch.setattr(urllib3_connection, "create_connection", fake_connect)
    connection = _DNSPinnedHTTPConnection("example.com", 80, timeout=2)

    assert connection._new_conn() is sentinel_socket
    assert connected == [("93.184.216.34", 80)]
    assert connection.host == "example.com"


def test_public_session_blocks_private_dns_before_opening_socket(monkeypatch):
    import socket
    import requests
    from urllib3.util import connection as urllib3_connection

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80)),
        ],
    )
    connected = []
    monkeypatch.setattr(
        urllib3_connection,
        "create_connection",
        lambda *args, **kwargs: connected.append(args[0]),
    )
    session = create_public_session()
    try:
        with pytest.raises(requests.RequestException, match="blocked unsafe"):
            session.get("http://example.com/", timeout=1)
        assert connected == []
    finally:
        session.close()


def test_public_session_disables_environment_and_external_proxies():
    from requests.exceptions import InvalidProxyURL

    session = create_public_session()
    try:
        assert session.trust_env is False
        assert isinstance(session.get_adapter("https://"), DNSPinnedHTTPAdapter)
        adapter = session.get_adapter("https://")
        assert adapter.poolmanager.connection_from_url("http://example.com").ConnectionCls.__name__ == "_DNSPinnedHTTPConnection"
        assert adapter.poolmanager.connection_from_url("https://example.com").ConnectionCls.__name__ == "_DNSPinnedHTTPSConnection"
        with pytest.raises(InvalidProxyURL, match="disabled"):
            session.get_adapter("https://").proxy_manager_for("http://proxy.example:8080")
    finally:
        session.close()


def test_dns_guard_activates_when_api_is_unauthenticated(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "public_workspace_mode", False)
    monkeypatch.setattr(settings, "api_key_required", True)
    monkeypatch.setattr(settings, "api_key", "configured-test-key")
    monkeypatch.setattr(settings, "demo_mode_enabled", False)
    assert not public_fetch_guard_enabled()

    monkeypatch.setattr(settings, "api_key_required", False)
    assert public_fetch_guard_enabled()


def test_public_workspace_disables_unpinned_browser_and_vendor_scrapers(monkeypatch):
    from app.config import settings
    from app.scrapers.base import ScrapeError
    from app.scrapers.firecrawl import FirecrawlScraper
    from app.scrapers.playwright_scraper import PlaywrightScraper

    monkeypatch.setattr(settings, "public_workspace_mode", True)
    with pytest.raises(ScrapeError, match="Playwright is disabled"):
        PlaywrightScraper()._fetch_once("https://example.com", None)

    firecrawl = FirecrawlScraper()
    with pytest.raises(ScrapeError, match="Firecrawl is disabled"):
        firecrawl._fetch_once("https://example.com", None)


def test_public_workspace_disables_remote_firecrawl_discovery(monkeypatch):
    from app.config import settings
    from app.discovery import discover_firecrawl

    monkeypatch.setattr(settings, "public_workspace_mode", True)
    with pytest.raises(RuntimeError, match="Firecrawl discovery is disabled"):
        discover_firecrawl("https://example.com")


def test_public_workspace_disables_remote_url_contents_provider(monkeypatch):
    from app.config import settings
    from app.research.sources import ApiNexSearch

    monkeypatch.setattr(settings, "public_workspace_mode", True)
    source = ApiNexSearch(api_key="placeholder", base_url="https://api.example.com")
    assert source.contents("https://example.com") is None
