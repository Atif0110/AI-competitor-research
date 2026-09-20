"""URL discovery and canonical URL deduplication.

Discovery supports both ecommerce/product sites and SaaS/software companies.

Flow:
    competitor site -> Firecrawl /map OR sitemap chain
                    -> retain intelligence-relevant URLs
                    -> canonicalize + dedupe
                    -> optional focus-product filtering in the pipeline

Sitemap chain:
    robots.txt -> Sitemap declarations -> sitemap index -> child sitemaps
                 -> relevant URLs

The discovery layer deliberately does not scrape arbitrary website URLs.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Iterable, List
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.config import settings

logger = logging.getLogger(__name__)


# URL path segments that commonly contain competitive-intelligence material.
#
# This intentionally covers:
# - ecommerce/product catalogs
# - SaaS/software products
# - pricing and plans
# - features/capabilities
# - enterprise/solutions/use cases
# - platform pages
# - apps
# - reviews/comparisons
#
# Blog/news/legal/careers/integration URLs are intentionally excluded unless
# they also contain one of the relevant intelligence segments below.
_RELEVANT_PATH_PATTERNS = re.compile(
    r"/("
    r"product|products|p|pd|item|items|dp|"
    r"catalog|collection|collections|"
    r"shop|store|"
    r"pricing|plans|plan|"
    r"features|feature|capabilities|"
    r"solutions|solution|use-cases|usecase|"
    r"enterprise|business|teams|team|"
    r"platform|"
    r"apps|app|"
    r"compare|comparison|comparisons|alternative|alternatives|"
    r"reviews|review|"
    r"detail|details"
    r")(/|$)",
    re.I,
)

_DROP_QUERY = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "igshid",
    "ref",
    "spm",
    "scm",
}

_SITEMAP_TAG = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)
_ROBOTS_SITEMAP = re.compile(r"(?im)^\s*Sitemap:\s*(.+)$")


def canonical_url(url: str) -> str:
    """Normalize a URL so the same page always hashes identically."""
    u = urlparse(url.strip())

    scheme = u.scheme.lower() or "https"
    host = u.netloc.lower()
    path = u.path.rstrip("/") or "/"

    keep = [
        (k, v)
        for k, v in parse_qsl(u.query, keep_blank_values=True)
        if k.lower() not in _DROP_QUERY
    ]

    query = urlencode(keep)

    return urlunparse(
        (
            scheme,
            host,
            path,
            "",
            query,
            "",
        )
    )


def url_hash(url: str) -> str:
    """Return a stable SHA-256 hash for a canonical URL."""
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()


def is_product_url(url: str) -> bool:
    """Return whether a URL looks relevant for competitive research.

    Kept under the existing function name for backwards compatibility with
    existing tests and callers.
    """
    path = urlparse(url).path
    return bool(_RELEVANT_PATH_PATTERNS.search(path))


def dedupe_urls(urls: Iterable[str]) -> List[str]:
    """Deduplicate URLs using their canonical URL hash."""
    seen = set()
    out = []

    for url in urls:
        if not isinstance(url, str) or not url.strip():
            continue

        canonical = canonical_url(url)

        if not canonical.startswith(("http://", "https://")):
            continue

        url_key = url_hash(canonical)

        if url_key in seen:
            continue

        seen.add(url_key)
        out.append(canonical)

    return out


def _same_domain(url: str, website: str) -> bool:
    """Allow the target hostname and its subdomains."""
    a = urlparse(url).netloc.lower().split(":", 1)[0]
    b = urlparse(website).netloc.lower().split(":", 1)[0]

    return a == b or a.endswith("." + b)


def _firecrawl_link_url(link) -> str | None:
    """Extract a URL from Firecrawl v1/v2 Map link formats.

    v1 commonly returns:
        ["https://example.com/page"]

    Newer Map responses can return:
        [{"url": "https://example.com/page", ...}]
    """
    if isinstance(link, str):
        return link.strip() or None

    if isinstance(link, dict):
        value = link.get("url")

        if isinstance(value, str):
            return value.strip() or None

    # Be tolerant of SDK-style objects exposing a `.url` attribute.
    value = getattr(link, "url", None)

    if isinstance(value, str):
        return value.strip() or None

    return None


def discover_firecrawl(website: str) -> List[str]:
    """Firecrawl /map: discover intelligence-relevant site URLs."""
    import requests

    if not settings.firecrawl_api_key:
        raise RuntimeError("FIRECRAWL_API_KEY not set")

    resp = requests.post(
        f"{settings.firecrawl_base_url}/map",
        json={
            "url": website,
            "limit": 200,
        },
        headers={
            "Authorization": f"Bearer {settings.firecrawl_api_key}",
            "Content-Type": "application/json",
        },
        timeout=settings.request_timeout_seconds,
    )

    if resp.status_code != 200:
        raise RuntimeError(f"firecrawl map http {resp.status_code}")

    payload = resp.json()

    raw_links = payload.get("links") or []

    links = []

    for raw_link in raw_links:
        url = _firecrawl_link_url(raw_link)

        if not url:
            continue

        if not _same_domain(url, website):
            continue

        if not is_product_url(url):
            continue

        links.append(url)

    logger.info(
        "firecrawl discovered %d relevant URLs from %d returned links for %s",
        len(links),
        len(raw_links),
        website,
    )

    return dedupe_urls(links)


def _fetch(url: str) -> str:
    """Fetch a URL and return its text."""
    import requests

    resp = requests.get(
        url,
        timeout=settings.request_timeout_seconds,
        headers={
            "User-Agent": "cintel-research/1.0",
        },
    )

    if resp.status_code != 200:
        raise RuntimeError(f"http {resp.status_code} for {url}")

    return resp.text


def _loc_links(text: str) -> List[str]:
    """Extract sitemap <loc> URLs."""
    return [link.strip() for link in _SITEMAP_TAG.findall(text)]


def discover_sitemaps(website: str) -> List[str]:
    """Discover relevant URLs through robots.txt and sitemap files."""
    sitemap_urls: List[str] = []

    try:
        robots = _fetch(
            website.rstrip("/") + "/robots.txt"
        )

        sitemap_urls = [
            sitemap.strip()
            for sitemap in _ROBOTS_SITEMAP.findall(robots)
        ]

        logger.info(
            "robots.txt declared %d sitemap(s) for %s",
            len(sitemap_urls),
            website,
        )

    except Exception as exc:
        logger.info(
            "no robots.txt for %s (%s) — defaulting to /sitemap.xml",
            website,
            exc,
        )

    if not sitemap_urls:
        sitemap_urls = [
            website.rstrip("/") + "/sitemap.xml"
        ]

    urls: List[str] = []
    visited = set()

    def process(sitemap: str, depth: int = 0) -> None:
        sitemap = sitemap.strip()

        if not sitemap:
            return

        # Keep the existing bounded sitemap traversal.
        if sitemap in visited or depth > 1:
            return

        visited.add(sitemap)

        try:
            text = _fetch(sitemap)

        except Exception as exc:
            logger.warning(
                "sitemap fetch failed %s: %s",
                sitemap,
                exc,
            )
            return

        for loc in _loc_links(text):
            loc = loc.strip()

            if not loc:
                continue

            if (
                loc.lower().endswith(".xml")
                or "sitemap" in loc.lower()
            ):
                process(loc, depth + 1)
                continue

            if not _same_domain(loc, website):
                continue

            if is_product_url(loc):
                urls.append(loc)

    for sitemap in sitemap_urls:
        process(sitemap)

    result = dedupe_urls(urls)

    logger.info(
        "sitemap discovery found %d relevant URLs for %s",
        len(result),
        website,
    )

    return result


def discover_urls_for_entity(website: str) -> List[str]:
    """Try Firecrawl first, then fall back to the sitemap chain."""
    if settings.firecrawl_api_key:
        try:
            found = discover_firecrawl(website)

            if found:
                return found

            logger.warning(
                "firecrawl returned no relevant URLs for %s; "
                "falling back to sitemap discovery",
                website,
            )

        except Exception as exc:
            logger.warning(
                "firecrawl discovery failed for %s: %s",
                website,
                exc,
            )

    return discover_sitemaps(website)
