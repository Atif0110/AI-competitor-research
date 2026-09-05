"""URL discovery stage (Priority 4) + canonical URL deduplication.

    competitor site -> Firecrawl /map OR sitemap chain
                    -> filter product/category URLs
                    -> canonicalize + dedupe by hash

Sitemap chain (review #22):
    robots.txt -> `Sitemap:` declarations -> sitemap index -> child sitemaps
                 -> product URLs
Falls back to GET /sitemap.xml when robots.txt is absent/empty.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Iterable, List
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.config import settings

logger = logging.getLogger(__name__)

# Path segments that indicate a product/catalog page worth scraping.
_PRODUCT_PATTERNS = re.compile(
    r"/(product|products|p|pd|item|items|dp|catalog|collection|collections|"
    r"shop|store|pricing|reviews|detail|details)(/|$)",
    re.I,
)
_DROP_QUERY = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
               "gclid", "fbclid", "igshid", "ref", "spm", "scm"}
_SITEMAP_TAG = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)
_ROBOTS_SITEMAP = re.compile(r"(?im)^\s*Sitemap:\s*(.+)$")


def canonical_url(url: str) -> str:
    """Normalize a URL so the same page always hashes identically."""
    u = urlparse(url.strip())
    scheme = u.scheme.lower() or "https"
    host = u.netloc.lower()
    path = u.path.rstrip("/") or "/"
    keep = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
            if k.lower() not in _DROP_QUERY]
    query = urlencode(keep)
    return urlunparse((scheme, host, path, "", query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()


def is_product_url(url: str) -> bool:
    path = urlparse(url).path
    return bool(_PRODUCT_PATTERNS.search(path))


def dedupe_urls(urls: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for u in urls:
        h = url_hash(u)
        if h in seen:
            continue
        seen.add(h)
        out.append(u)
    return out


def _same_domain(url: str, website: str) -> bool:
    a = urlparse(url).netloc.lower()
    b = urlparse(website).netloc.lower()
    return a == b or a.endswith("." + b)


def discover_firecrawl(website: str) -> List[str]:
    """Firecrawl /map: crawl the site and return discovered links."""
    import requests

    if not settings.firecrawl_api_key:
        raise RuntimeError("FIRECRAWL_API_KEY not set")
    resp = requests.post(
        f"{settings.firecrawl_base_url}/map",
        json={"url": website, "limit": 200},
        headers={"Authorization": f"Bearer {settings.firecrawl_api_key}"},
        timeout=settings.request_timeout_seconds,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"firecrawl map http {resp.status_code}")
    links = (resp.json().get("links") or [])
    return [l for l in links if is_product_url(l) and _same_domain(l, website)]


def _fetch(url: str) -> str:
    import requests

    resp = requests.get(url, timeout=settings.request_timeout_seconds,
                        headers={"User-Agent": "cintel-research/1.0"})
    if resp.status_code != 200:
        raise RuntimeError(f"http {resp.status_code} for {url}")
    return resp.text


def _loc_links(text: str) -> List[str]:
    return [l.strip() for l in _SITEMAP_TAG.findall(text)]


def discover_sitemaps(website: str) -> List[str]:
    """robots.txt -> Sitemap declarations -> index -> child sitemaps (#22)."""
    sitemap_urls: List[str] = []
    try:
        robots = _fetch(website.rstrip("/") + "/robots.txt")
        sitemap_urls = [m.strip() for m in _ROBOTS_SITEMAP.findall(robots)]
        logger.info("robots.txt declared %d sitemap(s) for %s", len(sitemap_urls), website)
    except Exception as e:
        logger.info("no robots.txt for %s (%s) — defaulting to /sitemap.xml", website, e)
    if not sitemap_urls:
        sitemap_urls = [website.rstrip("/") + "/sitemap.xml"]

    urls: List[str] = []
    visited = set()

    def process(smap: str, depth: int = 0) -> None:
        smap = smap.strip()
        if smap in visited or depth > 1:  # one level of index indirection
            return
        visited.add(smap)
        try:
            text = _fetch(smap)
        except Exception as e:
            logger.warning("sitemap fetch failed %s: %s", smap, e)
            return
        for loc in _loc_links(text):
            if loc.lower().endswith(".xml") or "sitemap" in loc.lower():
                process(loc, depth + 1)
            elif is_product_url(loc) and _same_domain(loc, website):
                urls.append(loc)

    for s in sitemap_urls:
        process(s)
    return dedupe_urls(urls)


def discover_urls_for_entity(website: str) -> List[str]:
    """Try Firecrawl map, fall back to the sitemap chain, else fail loudly."""
    if settings.firecrawl_api_key:
        try:
            return dedupe_urls(discover_firecrawl(website))
        except Exception as e:
            logger.warning("firecrawl discovery failed for %s: %s", website, e)
    return discover_sitemaps(website)
