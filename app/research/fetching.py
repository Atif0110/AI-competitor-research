"""Page fetching and HTML understanding for deep research.

Why not the scraping chain in ``app/scrapers``? That chain is built to pull
commercial offers off product pages and returns markdown. Deep research needs
three more things from the same fetch:

1. the page's outbound links, so the crawler can go deeper;
2. heading structure, so content can be stored as retrievable sections;
3. a readable text rendering that keeps headings and list items.

This module therefore does its own stdlib-HTML parsing and reuses the proxy
pool / timeout configuration from the scraping layer.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import logging
import re
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urldefrag, urljoin, urlparse

from app.config import settings
from app.scrapers.base import ProxyPool

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AICompetitorResearch/5.0"
)

_SKIP_SCHEMES = {"mailto", "tel", "javascript", "data", "sms", "ftp", "file"}

_ASSET_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css", ".js",
    ".json", ".xml", ".pdf", ".zip", ".gz", ".mp4", ".webm", ".mp3", ".woff",
    ".woff2", ".ttf", ".eot", ".dmg", ".exe", ".csv", ".xlsx", ".docx",
}

# Boilerplate containers: their text is navigation, not research content.
# The parser below owns its own tag/class tables (see _ContentParser).
_NON_TEXT_EXTENSIONS = _ASSET_EXTENSIONS | {".php", ".asp", ".aspx", ".jsp"}


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: Optional[int] = None
    html: str = ""
    final_url: str = ""
    content_type: str = ""
    elapsed_ms: int = 0
    proxy_used: Optional[str] = None
    error: Optional[str] = None
    scraper: str = "http"


@dataclass
class ParsedPage:
    title: str = ""
    description: str = ""
    text: str = ""
    markdown: str = ""
    links: List[str] = field(default_factory=list)


class Block:
    """One structural unit of page content."""

    __slots__ = ("kind", "text")

    def __init__(self, kind: str, text: str):
        self.kind = kind
        self.text = text

    @property
    def heading_level(self) -> int:
        return int(self.kind[1:]) if self.kind.startswith("h") and self.kind[1:].isdigit() else 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Block({self.kind!r}, {self.text[:40]!r})"


class _ContentParser(HTMLParser):
    """Turns messy HTML into an ordered list of content blocks.

    A block model (rather than a flat string) is what makes heading-anchored
    sections possible later, and it keeps paragraph boundaries, list items and
    headings from running together the way naive tag-stripping does.
    """

    HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    BLOCK_TAGS = {"p", "div", "section", "article", "blockquote", "tr", "pre", "figcaption"}
    SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "head"}
    BOILERPLATE_TAGS = {"nav", "footer", "header", "aside", "form"}
    BOILERPLATE_MARKERS = (
        "nav", "menu", "footer", "header", "sidebar", "breadcrumb", "cookie",
        "newsletter", "subscribe", "promo", "advert", "social", "share",
        "skip-link", "site-footer", "site-nav",
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[Block] = []
        self.links: List[Tuple[str, str]] = []
        self.title = ""
        self.description = ""
        self._buffer: List[str] = []
        self._skip_depth = 0
        self._boilerplate_depth = 0
        self._in_title = False
        self._heading_tag: Optional[str] = None
        self._heading_buffer: List[str] = []
        self._link_href: Optional[str] = None
        self._link_text: List[str] = []
        self._stack: List[Tuple[str, bool]] = []

    # ------------------------------------------------------------------
    @property
    def _ignored(self) -> bool:
        return self._skip_depth > 0 or self._boilerplate_depth > 0

    def _append_inline(self, text: str) -> None:
        if self._ignored or not text or not text.strip():
            return
        self._buffer.append(text)

    def _flush(self, kind: str = "p") -> None:
        """Close the current inline run into a block of the given kind."""
        if self._ignored:
            self._buffer = []
            return
        text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
        self._buffer = []
        if not text:
            return

        last = self.blocks[-1] if self.blocks else None
        # Inline content that follows a paragraph or a list item continues it;
        # a new list item or heading always starts its own block.
        if last is not None and kind in ("p", "inline") and last.kind in ("p", "li"):
            if len(last.text) + len(text) < 4000:
                last.text = f"{last.text} {text}".strip()
                return
        self.blocks.append(Block(kind, text))

    def _flush_heading(self) -> None:
        if self._ignored or not self._heading_buffer:
            self._heading_buffer = []
            return
        text = re.sub(r"\s+", " ", "".join(self._heading_buffer)).strip()
        self._heading_buffer = []
        if text:
            self._flush("inline")  # close any trailing inline run first
            self.blocks.append(Block(self._heading_tag or "h2", text))

    # ------------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attr = {k.lower(): (v or "") for k, v in attrs}

        if tag in self.SKIP_TAGS:
            self._stack.append((tag, "skip"))
            self._skip_depth += 1
            return
        if self._skip_depth:
            self._stack.append((tag, "skip"))
            return

        if tag == "a":
            # Tracked before the boilerplate check: nav/footer links are the
            # main discovery path to a site's commercial pages.
            self._stack.append((tag, "anchor"))
            self._link_href = attr.get("href", "")
            self._link_text = []
            return

        if tag in self.BOILERPLATE_TAGS or self._looks_boilerplate(attr):
            self._stack.append((tag, "boilerplate"))
            self._boilerplate_depth += 1
            return

        if self._boilerplate_depth:
            self._stack.append((tag, "content"))
            return

        self._stack.append((tag, "content"))

        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            self._read_meta(attr)
        elif tag in self.HEADING_TAGS:
            self._flush_heading()
            self._flush("inline")
            self._heading_tag = tag
            self._heading_buffer = []
        elif tag == "br":
            self._flush("inline")
        elif tag == "li":
            self._flush("inline")
            self._flush("li")

    def _read_meta(self, attr: dict) -> None:
        name = (attr.get("name") or attr.get("property") or "").lower()
        content = (attr.get("content") or "").strip()
        if not content:
            return
        if name in ("description", "og:description") and not self.description:
            self.description = content
        elif name == "og:title" and not self.title:
            self.title = content

    @staticmethod
    def _looks_boilerplate(attr: dict) -> bool:
        marker = " ".join(
            [attr.get("class", ""), attr.get("id", ""), attr.get("role", "")]
        ).lower()
        if not marker:
            return False
        return any(hint in marker for hint in _ContentParser.BOILERPLATE_MARKERS)

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag.lower() == "meta" and not self._skip_depth:
            self._read_meta({k.lower(): (v or "") for k, v in attrs})

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        # An anchor is recorded before the boilerplate guard: navigation and
        # footer links are exactly how a pricing page gets discovered, so they
        # must survive even though their surrounding text is dropped.
        if tag == "a":
            if self._link_href:
                self.links.append(
                    (
                        self._link_href,
                        re.sub(r"\s+", " ", "".join(self._link_text)).strip(),
                    )
                )
            self._link_href = None
            self._link_text = []

        # Unwind to the matching open tag: unbalanced markup must not leave the
        # skip counters stuck and swallow the rest of the document.
        matched_counted = None
        while self._stack:
            open_tag, counted = self._stack.pop()
            if open_tag == tag:
                matched_counted = counted
                break
            if counted == "skip":
                self._skip_depth = max(0, self._skip_depth - 1)
            elif counted == "boilerplate":
                self._boilerplate_depth = max(0, self._boilerplate_depth - 1)

        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if matched_counted in ("boilerplate",) or (
            matched_counted is None and tag in self.BOILERPLATE_TAGS
        ):
            self._boilerplate_depth = max(0, self._boilerplate_depth - 1)
            return
        if self._skip_depth or self._boilerplate_depth:
            return

        if tag == "title":
            self._in_title = False
        elif tag in self.HEADING_TAGS:
            self._flush_heading()
            self._heading_tag = None
        elif tag in self.BLOCK_TAGS:
            self._flush("p")
        elif tag == "li":
            self._flush("li")
        elif tag in ("ul", "ol", "table", "tbody", "thead", "tr"):
            self._flush("inline")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title = (self.title + " " + data).strip()
            return
        if self._link_href is not None:
            self._link_text.append(data)
        if self._ignored:
            return
        if self._heading_tag:
            # Heading text is emitted once, by _flush_heading().
            self._heading_buffer.append(data)
            return
        self._append_inline(data)

    def close(self) -> None:  # noqa: D102
        super().close()
        self._flush_heading()
        self._flush("p")

    # ------------------------------------------------------------------
    @property
    def text(self) -> str:
        lines: List[str] = []
        for block in self.blocks:
            if block.kind == "li":
                lines.append(f"- {block.text}")
            else:
                lines.append(block.text)
        joined = "\n".join(lines)
        joined = re.sub(r"[ \t]{2,}", " ", joined)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()

    @property
    def markdown(self) -> str:
        parts: List[str] = []
        for block in self.blocks:
            if block.heading_level:
                parts.append(f"\n{'#' * min(block.heading_level, 6)} {block.text}\n\n")
            elif block.kind == "li":
                parts.append(f"- {block.text}\n")
            else:
                parts.append(f"{block.text}\n\n")
        joined = "".join(parts)
        joined = re.sub(r"[ \t]{2,}", " ", joined)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


def parse_page(html: str, base_url: str) -> ParsedPage:
    """Extract title, description, readable text and absolute links."""
    parser = _ContentParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        logger.debug("html parse stopped early for %s", base_url, exc_info=True)

    title = re.sub(r"\s+", " ", html_lib.unescape(parser.title)).strip()
    description = re.sub(r"\s+", " ", html_lib.unescape(parser.description)).strip()
    links = []
    seen: Set[str] = set()
    for href, _anchor in parser.links:
        absolute = normalize_link(href, base_url)
        if absolute and absolute not in seen:
            seen.add(absolute)
            links.append(absolute)

    return ParsedPage(
        title=title[:300],
        description=description[:600],
        text=parser.text,
        markdown=parser.markdown,
        links=links,
    )


def normalize_link(href: str, base_url: str) -> Optional[str]:
    """Absolute, canonical http(s) link or None when it is not crawlable."""
    if not href:
        return None
    href = html_lib.unescape(href.strip())
    if not href or href.startswith("#"):
        return None
    scheme = urlparse(href).scheme.lower()
    if scheme in _SKIP_SCHEMES:
        return None
    if scheme and scheme not in ("http", "https"):
        return None
    absolute, _fragment = urldefrag(urljoin(base_url, href))
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    path = (parsed.path or "/").lower()
    if any(path.endswith(ext) for ext in _NON_TEXT_EXTENSIONS):
        return None
    if re.search(r"/(wp-admin|wp-json|feed|tag|category|author|feed/|xmlrpc)", path):
        return None
    # Strip tracking parameters so the same page is not crawled twice.
    kept = [
        (k, v)
        for k, v in parse_qsl_safe(parsed.query)
        if not k.lower().startswith(("utm_", "gclid", "fbclid", "ref_", "mc_"))
    ]
    query = "&".join(f"{k}={v}" for k, v in kept)
    return _rebuild(parsed.scheme, parsed.netloc, parsed.path, query)


def parse_qsl_safe(query: str) -> List[Tuple[str, str]]:
    from urllib.parse import parse_qsl

    try:
        return parse_qsl(query, keep_blank_values=False)
    except ValueError:
        return []


def _rebuild(scheme: str, netloc: str, path: str, query: str) -> str:
    url = f"{scheme}://{netloc}{path or '/'}"
    return f"{url}?{query}" if query else url


def looks_like_document(url: str) -> bool:
    """False for obvious binary/asset URLs so budgets are spent on real pages."""
    path = urlparse(url).path.lower()
    return not any(path.endswith(ext) for ext in _NON_TEXT_EXTENSIONS)


class RobotsGate:
    """Per-host robots.txt cache honouring DEEP_RESPECT_ROBOTS."""

    def __init__(self, enabled: bool = True, user_agent: str = _USER_AGENT):
        self.enabled = enabled
        self.user_agent = user_agent
        self._cache: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str, session=None) -> bool:
        if not self.enabled:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            parser = self._cache.get(origin, "missing")
        if parser == "missing":
            parser = self._load(origin, session)
            with self._lock:
                self._cache[origin] = parser
        if parser is None:
            return True  # no robots.txt or unreadable -> allowed
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def _load(self, origin: str, session) -> Optional[urllib.robotparser.RobotFileParser]:
        import requests

        try:
            response = requests.get(
                f"{origin}/robots.txt",
                headers={"User-Agent": self.user_agent},
                timeout=10,
            )
        except Exception:
            logger.debug("robots.txt unreachable for %s", origin, exc_info=True)
            return None
        if response.status_code >= 400:
            return None
        parser = urllib.robotparser.RobotFileParser()
        try:
            parser.parse(response.text.splitlines())
        except Exception:
            return None
        return parser


class Fetcher:
    """HTTP fetcher with retries, politeness delays and proxy rotation."""

    def __init__(
        self,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        respect_robots: Optional[bool] = None,
        per_host_delay: Optional[float] = None,
        proxies: Optional[List[str]] = None,
        proxy_by_region: Optional[Dict[str, List[str]]] = None,
    ):
        import requests

        self.timeout = timeout or settings.request_timeout_seconds
        self.max_retries = max_retries if max_retries is not None else settings.max_retries
        self.per_host_delay = (
            settings.deep_per_host_delay if per_host_delay is None else per_host_delay
        )
        self.proxy_pool = ProxyPool(
            proxies if proxies is not None else settings.proxy_urls,
            proxy_by_region if proxy_by_region is not None else settings.proxy_regions,
        )
        self.robots = RobotsGate(
            settings.deep_respect_robots if respect_robots is None else respect_robots
        )
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self._last_hit: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _wait_for_host(self, host: str) -> None:
        if self.per_host_delay <= 0:
            return
        with self._lock:
            last = self._last_hit.get(host, 0.0)
            wait = (last + self.per_host_delay) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.monotonic()

    def fetch(self, url: str, region: Optional[str] = None) -> FetchResult:
        host = urlparse(url).netloc
        if not self.robots.allowed(url, self._session):
            return FetchResult(url=url, ok=False, status=None, error="blocked by robots.txt")

        proxy = self.proxy_pool.next(region) if self.proxy_pool.enabled else None
        proxies = {"http": proxy, "https": proxy} if proxy else None
        last_error: Optional[str] = None
        started = time.monotonic()

        for attempt in range(1, max(1, self.max_retries) + 1):
            self._wait_for_host(host)
            try:
                response = self._session.get(
                    url,
                    timeout=self.timeout,
                    proxies=proxies,
                    allow_redirects=True,
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.debug("fetch attempt %d failed for %s: %s", attempt, url, exc)
                time.sleep(min(4.0, 0.75 * attempt))
                continue

            content_type = response.headers.get("Content-Type", "")
            final_url = str(response.url)

            if response.status_code >= 400:
                last_error = f"http {response.status_code}"
                if response.status_code in (401, 403, 404, 410):
                    # Pointless to retry: the page is blocked or gone.
                    break
                time.sleep(min(4.0, 0.75 * attempt))
                continue

            if "html" not in content_type.lower() and "xml" not in content_type.lower():
                return FetchResult(
                    url=url, ok=True, status=response.status_code, final_url=final_url,
                    content_type=content_type, proxy_used=proxy, scraper="http",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

            return FetchResult(
                url=url,
                ok=True,
                status=response.status_code,
                html=response.text,
                final_url=final_url,
                content_type=content_type,
                proxy_used=proxy,
                scraper="http",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        return FetchResult(
            url=url,
            ok=False,
            status=None,
            error=last_error or "fetch failed",
            proxy_used=proxy,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest()