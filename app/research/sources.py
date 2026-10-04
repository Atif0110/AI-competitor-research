"""External source discovery.

Topic research needs pages the crawler cannot reach by following links, so this
module provides three interchangeable search backends plus a content-fetch
fallback for pages that block us:

1. ``ApiNexSearch``  - the configured APInex gateway (web search tool).
2. ``DuckDuckGoSearch`` - keyless HTML endpoint, the zero-cost default.
3. ``NullSearch``    - honest no-op used when search is unavailable.

Every backend reports which one it is so the report can state where its
sources came from instead of implying broader coverage than it has.
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from html.parser import HTMLParser
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

from app.config import settings
from app.research.fetching import _USER_AGENT, normalize_link
from app.research.models import SearchHit

logger = logging.getLogger(__name__)


class SearchBackend(ABC):
    name = "none"
    available = False

    @abstractmethod
    def search(self, query: str, limit: int = 8) -> List[SearchHit]:
        ...

    def contents(self, url: str) -> Optional[str]:
        """Optional page-extraction fallback; None when unsupported."""
        return None


class NullSearch(SearchBackend):
    name = "none"
    available = False

    def search(self, query: str, limit: int = 8) -> List[SearchHit]:
        return []


class ApiNexSearch(SearchBackend):
    """APInex web tools (POST /v1/tools/web/search).

    Enabled only when an APInex key is configured. Every response shape is
    parsed defensively: an upstream change must degrade to "no results",
    never crash a research run.
    """

    name = "apinex"
    available = True

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key if api_key is not None else settings.apinex_api_key
        self.base_url = (base_url or settings.apinex_base_url).rstrip("/")
        self.enabled = settings.apinex_web_tools

    def _post(self, path: str, payload: dict, timeout: int = 45):
        import requests

        try:
            response = requests.post(
                f"{self.base_url}{path}",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
        except Exception as exc:
            logger.info("apinex tool %s failed: %s", path, exc)
            return None
        if response.status_code >= 400:
            logger.info(
                "apinex tool %s returned %s: %s",
                path,
                response.status_code,
                response.text[:200],
            )
            return None
        try:
            return response.json()
        except ValueError:
            logger.info("apinex tool %s returned non-JSON", path)
            return None

    def search(self, query: str, limit: int = 8) -> List[SearchHit]:
        if not (self.api_key and self.enabled):
            return []
        data = self._post(
            "/tools/web/search",
            {"query": query, "limit": limit, "max_results": limit},
        )
        return _hits_from_payload(data, limit, origin="apinex-search")

    def contents(self, url: str) -> Optional[str]:
        if not (self.api_key and self.enabled):
            return None
        data = self._post("/tools/web/contents", {"urls": [url], "format": "markdown"})
        return _text_from_payload(data)


class _DuckDuckGoParser(HTMLParser):
    """Extract result links/titles from DuckDuckGo's HTML endpoint."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hits: List[SearchHit] = []
        self._in_result = False
        self._href = ""
        self._text: List[str] = []

    def handle_starttag(self, tag, attrs):
        attr = {k.lower(): (v or "") for k, v in attrs}
        classes = attr.get("class", "")
        if tag == "a" and "result__a" in classes:
            self._in_result = True
            self._href = attr.get("href", "")
            self._text = []
        elif tag == "a" and "result__snippet" in classes:
            self._in_result = "snippet"

    def handle_endtag(self, tag):
        if tag == "a" and self._in_result is True:
            title = " ".join(self._text).strip()
            url = self._href
            if url.startswith("//duckduckgo.com/l/") or "duckduckgo.com/l/" in url:
                parsed = parse_qs(urlparse(url).query)
                target = parsed.get("uddg")
                if target:
                    url = target[0]
            if title and url:
                self.hits.append(SearchHit(url=url, title=title, origin="duckduckgo"))
            self._in_result = False
            self._href = ""
            self._text = []

    def handle_data(self, data):
        if self._in_result is True:
            self._text.append(data.strip())


class DuckDuckGoSearch(SearchBackend):
    """Keyless search so topic research still works on a zero budget."""

    name = "duckduckgo"
    available = True

    def __init__(self, timeout: int = 20, max_chars: int = 600):
        self.timeout = timeout
        self.max_chars = max_chars

    def search(self, query: str, limit: int = 8) -> List[SearchHit]:
        import requests

        if not query.strip():
            return []
        try:
            response = requests.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query, "kl": "wt-wt"},
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "text/html",
                },
                timeout=self.timeout,
            )
        except Exception as exc:
            logger.info("duckduckgo search failed: %s", exc)
            return []
        if response.status_code >= 400:
            logger.info("duckduckgo returned %s", response.status_code)
            return []

        parser = _DuckDuckGoParser()
        try:
            parser.feed(response.text)
            parser.close()
        except Exception:
            logger.debug("duckduckgo html parse failed", exc_info=True)

        hits: List[SearchHit] = []
        for hit in parser.hits:
            url = normalize_link(hit.url, "https://duckduckgo.com")
            if not url or url in {h.url for h in hits}:
                continue
            hits.append(
                SearchHit(url=url, title=hit.title[:300], snippet="", origin="duckduckgo")
            )
            if len(hits) >= limit:
                break
        return hits


def _hits_from_payload(data, limit: int, origin: str) -> List[SearchHit]:
    """Parse a web-search payload without assuming one exact shape."""
    if not isinstance(data, dict):
        return []
    candidates = (
        data.get("results")
        or data.get("data")
        or data.get("items")
        or data.get("web")
        or []
    )
    if isinstance(candidates, dict):
        candidates = candidates.get("results") or candidates.get("items") or []
    if not isinstance(candidates, list):
        return []

    hits: List[SearchHit] = []
    for item in candidates:
        if isinstance(item, str):
            url = normalize_link(item, "https://duckduckgo.com")
            if url:
                hits.append(SearchHit(url=url, origin=origin))
            continue
        if not isinstance(item, dict):
            continue
        url = (
            item.get("url")
            or item.get("link")
            or item.get("source_url")
            or item.get("href")
        )
        if not url:
            continue
        normalized = normalize_link(str(url), "https://duckduckgo.com")
        if not normalized:
            continue
        hits.append(
            SearchHit(
                url=normalized,
                title=str(item.get("title") or item.get("name") or "")[:300],
                snippet=str(
                    item.get("snippet") or item.get("description") or item.get("summary") or ""
                )[:600],
                origin=origin,
            )
        )
        if len(hits) >= limit:
            break
    return hits


def _text_from_payload(data) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    candidates = (
        data.get("contents")
        or data.get("results")
        or data.get("data")
        or data.get("content")
        or data.get("markdown")
    )
    if isinstance(candidates, str):
        return candidates
    if isinstance(candidates, list):
        parts = []
        for item in candidates:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                body = (
                    item.get("markdown")
                    or item.get("content")
                    or item.get("text")
                    or item.get("html")
                )
                if isinstance(body, str):
                    parts.append(body)
        return "\n\n".join(p for p in parts if p) or None
    return None


def build_search(enabled: bool = True) -> SearchBackend:
    """Pick the best available search backend.

    Order: APInex web tools (best structured results) -> keyless DuckDuckGo ->
    nothing. Research never fails because search is unavailable; it just
    reports fewer external sources.
    """
    if not enabled:
        return NullSearch()
    if settings.apinex_api_key and settings.apinex_web_tools:
        return ApiNexSearch()
    return DuckDuckGoSearch()


def register_default() -> None:  # pragma: no cover - convenience hook
    """Kept so callers can introspect the default backend choice."""
    logger.debug("default search backend: %s", build_search().name)


_SNIPPET_RE = re.compile(r"\s+")


def clean_snippet(text: str, limit: int = 600) -> str:
    return _SNIPPET_RE.sub(" ", text or "").strip()[:limit]