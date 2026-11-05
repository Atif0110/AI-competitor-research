import pytest
from pydantic import ValidationError

from app.api import DeepResearchRequest
from app.schemas import Competitor, CompetitorTarget
from app.url_safety import (
    DNSPinningHTTPProxy,
    DNSPinnedHTTPAdapter,
    _DNSPinnedHTTPConnection,
    _connect_public_socket,
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


def test_public_workspace_keeps_browser_scraper_behind_pinned_proxy(monkeypatch):
    import sys
    import types
    from types import SimpleNamespace

    from app.config import settings
    from app.scrapers.base import ScrapeError
    from app.scrapers.firecrawl import FirecrawlScraper
    from app.scrapers.playwright_scraper import PlaywrightScraper

    monkeypatch.setattr(settings, "public_workspace_mode", True)

    launch_options = {}
    class Page:
        url = "https://example.com/"

        def route(self, _pattern, handler):
            self.route_handler = handler

        def goto(self, *_args, **_kwargs):
            self.goto_options = _kwargs
            return None

        def wait_for_timeout(self, _milliseconds):
            return None

        def inner_text(self, _selector):
            return "Rendered product listing"

    page = Page()

    class Browser:
        def new_page(self):
            return page

        def close(self):
            return None

    class Chromium:
        def launch(self, **kwargs):
            launch_options.update(kwargs)
            return Browser()

    class PlaywrightContext:
        def __enter__(self):
            return SimpleNamespace(chromium=Chromium())

        def __exit__(self, *_args):
            return None

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = PlaywrightContext
    playwright = types.ModuleType("playwright")
    playwright.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    class FakePinnedProxy:
        url = "http://127.0.0.1:45678"

        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(
        "app.scrapers.playwright_scraper.DNSPinningHTTPProxy",
        FakePinnedProxy,
    )
    result = PlaywrightScraper()._fetch_once("https://example.com/", None)

    assert result.markdown == "Rendered product listing"
    assert result.proxy_used is None
    assert launch_options["proxy"] == {
        "server": FakePinnedProxy.url,
        "bypass": "<-loopback>",
    }
    assert launch_options["headless"] is True
    assert launch_options["channel"] == "chromium"
    assert 1_000 <= launch_options["timeout"] <= 20_000
    assert 1_000 <= page.goto_options["timeout"] <= 20_000
    assert "--disable-quic" in launch_options["args"]
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in launch_options["args"]

    class Route:
        def __init__(self, url):
            self.request = SimpleNamespace(url=url)
            self.continued = False
            self.aborted = False

        def continue_(self):
            self.continued = True

        def abort(self):
            self.aborted = True

    public_route = Route("https://cdn.example.com/app.js")
    private_route = Route("http://127.0.0.1/admin")
    page.route_handler(public_route)
    page.route_handler(private_route)
    assert public_route.continued and not public_route.aborted
    assert private_route.aborted and not private_route.continued

    firecrawl = FirecrawlScraper()
    with pytest.raises(ScrapeError, match="Firecrawl is disabled"):
        firecrawl._fetch_once("https://example.com", None)


def test_public_scraper_chain_tries_basic_before_pinned_playwright(monkeypatch):
    from app.config import settings
    from app.scrapers import manager

    monkeypatch.setattr(settings, "public_workspace_mode", True)
    monkeypatch.setattr(settings, "firecrawl_api_key", "")
    monkeypatch.setattr(manager.importlib.util, "find_spec", lambda name: object() if name == "playwright" else None)

    assert manager.build_scraper().names == ["basic", "playwright"]


def test_public_browser_sessions_are_serialized():
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from app.scrapers.playwright_scraper import _browser_slot

    lock = threading.Lock()
    active = 0
    maximum_active = 0

    def use_browser_slot(_index):
        nonlocal active, maximum_active
        with _browser_slot(guarded=True):
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(use_browser_slot, range(4)))

    assert maximum_active == 1


def test_public_browser_failure_falls_back_after_one_attempt(monkeypatch):
    from app.config import settings
    from app.scrapers.base import BaseScraper, RetryPolicy, ScrapeError, ScrapedPage
    from app.scrapers.orchestrator import ScraperOrchestrator

    monkeypatch.setattr(settings, "public_workspace_mode", True)
    monkeypatch.setattr(settings, "demo_mode_enabled", False)
    monkeypatch.setattr(settings, "api_key_required", True)
    monkeypatch.setattr(settings, "rate_limit_min_interval", 0)

    class Browser(BaseScraper):
        name = "playwright"
        public_max_attempts = 1

        def __init__(self):
            self.calls = 0

        def _fetch_once(self, url, proxy, region=None):
            self.calls += 1
            raise ScrapeError("browser timed out")

    class Basic(BaseScraper):
        name = "basic"

        def __init__(self):
            self.calls = 0

        def _fetch_once(self, url, proxy, region=None):
            self.calls += 1
            return ScrapedPage(url=url, markdown="Public page content", region=region)

    browser = Browser()
    basic = Basic()
    orchestrator = ScraperOrchestrator([browser, basic])
    orchestrator.retry_policy = RetryPolicy(max_retries=3, base_seconds=0)

    page, log = orchestrator.fetch("https://example.com/", region="US")

    assert page.markdown == "Public page content"
    assert browser.calls == 1
    assert basic.calls == 1
    assert log.scrapers_tried == ["playwright", "basic"]
    assert log.final_scraper == "basic"


def test_pinned_connect_uses_validated_numeric_address(monkeypatch):
    import app.url_safety as url_safety

    expected_socket = object()
    connected = []
    monkeypatch.setattr(
        url_safety,
        "_resolve_public_addresses",
        lambda host, port: ["93.184.216.34"],
    )
    monkeypatch.setattr(
        url_safety.socket,
        "create_connection",
        lambda address, timeout: connected.append((address, timeout)) or expected_socket,
    )

    assert _connect_public_socket("example.com", 443, timeout=4) is expected_socket
    assert connected == [(("93.184.216.34", 443), 4)]


def test_loopback_browser_proxy_rejects_private_urls_without_connecting():
    import requests

    session = requests.Session()
    session.trust_env = False
    with DNSPinningHTTPProxy() as proxy:
        response = session.get(
            "http://127.0.0.1/admin",
            proxies={"http": proxy.url},
            timeout=2,
        )
    session.close()

    assert response.status_code == 403
    assert "blocked non-public" in response.text


def test_loopback_browser_proxy_rejects_dns_rebinding_answers(monkeypatch):
    import app.url_safety as url_safety
    import requests

    def reject_private_resolution(_host, _port):
        raise ValueError("hostname resolved to a non-public address")

    monkeypatch.setattr(url_safety, "_resolve_public_addresses", reject_private_resolution)
    with DNSPinningHTTPProxy() as proxy:
        response = requests.get(
            "http://example.com/",
            proxies={"http": proxy.url},
            timeout=2,
        )

    assert response.status_code == 502
    assert "upstream request failed" in response.text


def test_loopback_browser_proxy_rejects_rebinding_https_tunnels(monkeypatch):
    import socket
    from urllib.parse import urlsplit

    import app.url_safety as url_safety

    def reject_private_resolution(_host, _port):
        raise ValueError("hostname resolved to a non-public address")

    monkeypatch.setattr(url_safety, "_resolve_public_addresses", reject_private_resolution)
    with DNSPinningHTTPProxy() as proxy:
        parsed = urlsplit(proxy.url)
        with socket.create_connection((parsed.hostname, parsed.port), timeout=2) as client:
            client.sendall(
                b"CONNECT example.com:443 HTTP/1.1\r\n"
                b"Host: example.com:443\r\n\r\n"
            )
            chunks = []
            while True:
                chunk = client.recv(1024)
                if not chunk:
                    break
                chunks.append(chunk)
            response = b"".join(chunks)

    assert response.startswith(b"HTTP/1.1 403 ")
    assert b"blocked unsafe or unresolvable destination" in response


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
