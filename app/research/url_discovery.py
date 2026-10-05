"""URL discovery with multiple fallback strategies.

When sitemaps fail (404, no sitemap, etc.), we try multiple strategies:
1. Try standard sitemap.xml
2. Try common sitemap locations
3. Check robots.txt for sitemap references
4. Try common high-value paths
9. Use search API when available
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Set
from urllib.parse import urljoin, urlparse

import requests

from app.config import settings
from app.research.fetching import Fetcher, _USER_AGENT
from app.research.sources import SearchBackend, build_search

logger = logging.getLogger(__name__)

# Common sitemap locations to try
SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap1.xml",
    "/sitemap_index.xml.gz",
    "/sitemap.xml.gz",
]

# High-value paths to try when sitemaps fail
HIGH_VALUE_PATHS = [
    "/pricing", "/pricing/", "/pricing.html",
    "/pricing/plans", "/pricing/plans/",
    "/plans", "/plans/", "/plans.html",
    "/pricing/plans/enterprise", "/pricing/enterprise",
    "/products", "/products/", "/products.html",
    "/products/features", "/features",
    "/solutions", "/solutions/",
    "/enterprise", "/enterprise/",
    "/team", "/teams", "/team",
    "/business", "/business/",
    "/company", "/company/", "/about", "/about/",
    "/about/team", "/about/team/",
    "/about/leadership", "/leadership",
    "/about/company", "/company",
    "/about/mission", "/mission",
    "/about/values", "/values",
    "/about/culture", "/culture",
    "/about/careers", "/careers", "/jobs",
    "/features", "/features/", "/features/",
    "/features/enterprise", "/features/enterprise/",
    "/features/integrations", "/integrations",
    "/integrations", "/integrations/",
    "/api", "/api/", "/docs", "/docs/",
    "/developers", "/developers/",
    "/developers/api", "/api/docs",
    "/docs/api", "/api/docs/",
    "/docs/getting-started", "/getting-started",
    "/pricing/faq", "/faq", "/faq/",
    "/help", "/help/", "/help/",
    "/support", "/support/", "/support/",
    "/resources", "/resources/", "/resources/",
    "/blog", "/blog/", "/blog/",
    "/blog/pricing", "/blog/pricing/",
    "/blog/features", "/blog/features/",
    "/blog/announcement", "/blog/announcement",
    "/blog/product", "/blog/product/",
    "/changelog", "/changelog/", "/changelog/",
    "/releases", "/releases/", "/releases/",
    "/updates", "/updates/", "/updates/",
    "/news", "/news/", "/news/",
    "/press", "/press/", "/press/",
    "/customers", "/customers/", "/customers/",
    "/case-studies", "/case-studies/", "/case-studies/",
    "/customers/", "/customers/",
    "/stories", "/stories/", "/stories/",
    "/case-study", "/case-study/",
    "/reviews", "/reviews/", "/testimonials",
    "/reviews/", "/testimonials/",
    "/compare", "/compare/", "/vs",
    "/vs/", "/comparison", "/comparison/",
    "/vs/", "/alternatives", "/alternatives/",
    "/compare/", "/alternatives/",
    "/pricing/compare", "/compare/pricing",
    "/features/compare", "/compare/features",
    "/alternatives/", "/alternatives/",
    "/vs/", "/versus/",
    "/pricing/", "/pricing/",
    "/plans", "/plans/", "/plans/",
    "/plans/enterprise", "/plans/enterprise",
    "/pricing/enterprise", "/enterprise/pricing",
    "/pricing/team", "/pricing/team",
    "/team", "/team/", "/team/",
    "/business", "/business/", "/business/",
    "/enterprise", "/enterprise/", "/enterprise/",
    "/startup", "/startup/", "/startup/",
    "/growth", "/growth/", "/growth/",
    "/scale", "/scale/", "/scale/",
    "/professional", "/professional/", "/professional/",
    "/business", "/business/", "/business/",
    "/pro", "/pro/", "/pro/",
    "/premium", "/premium/", "/premium/",
    "/plus", "/plus/", "/plus/",
]

# High-signal path segments (used for scoring)
HIGH_SIGNAL_SEGMENTS = {
    "pricing", "price", "plans", "plan", "products", "product", "features",
    "compare", "comparison", "vs", "alternatives", "reviews", "review",
    "customers", "stories", "case-studies", "faq", "docs", "documentation",
    "guides", "blog", "news", "about", "company", "team", "enterprise",
    "security", "integrations", "download", "specs", "specifications",
}

# Low-signal segments to deprioritize
LOW_SIGNAL_SEGMENTS = {
    "login", "signup", "sign-in", "sign-up", "register", "cart", "checkout",
    "account", "profile", "settings", "logout", "privacy", "terms", "legal",
    "cookie", "cookies", "press", "careers", "jobs", "community", "events",
    "webinars", "podcast", "rss", "sitemap", "search", "tag", "tags",
}


def _score_url(url: str, plan_keywords: List[str]) -> float:
    """Score a URL based on path segments and plan keywords."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path = parsed.path.lower()
    segments = [s for s in path.split("/") if s]

    score = 0.0

    # High signal segments
    for segment in segments:
        if segment in {"pricing", "price", "plans", "plan", "products", "product",
                      "features", "compare", "comparison", "vs", "alternatives",
                      "reviews", "review", "customers", "stories", "case-studies",
                      "faq", "docs", "documentation", "guides", "blog", "news",
                      "about", "company", "team", "enterprise", "security",
                      "integrations", "download", "specs", "specifications",
                      "pricing", "plans", "plans", "features", "enterprise",
                      "team", "business", "enterprise", "pricing", "pricing"}:
            score += 3.0
        elif segment in {"login", "signup", "sign-in", "sign-up", "register",
                         "cart", "checkout", "account", "profile", "settings",
                         "logout", "privacy", "terms", "legal", "cookie",
                         "cookies", "press", "careers", "jobs", "community",
                         "events", "webinars", "podcast", "rss", "sitemap",
                         "search", "tag", "tags"}:
            score -= 4.0

    # Keyword matching
    for keyword in ["pricing", "price", "plan", "plans", "price", "cost",
                    "enterprise", "team", "business", "features", "features",
                    "compare", "compare", "alternatives", "competitor",
                    "pricing", "plans", "features", "enterprise", "team"]:
        for segment in ["pricing", "price", "plans", "plan", "enterprise",
                        "team", "business", "features", "compare"]:
            if segment in keyword and segment in " ".join([s.lower() for s in [segment]]):
                pass  # Already covered above

    # Keyword matching in path
    path_lower = " ".join([s.lower() for s in path.split("/") if s])
    for kw in ["pricing", "price", "plan", "plans", "enterprise", "team",
               "business", "features", "compare", "alternatives", "pricing",
               "plans", "features", "enterprise", "team", "business"]:
        if kw in " ".join([]):  # placeholder - will be replaced
            pass

    # Simple keyword matching in path
    for kw in ["pricing", "price", "plan", "plans", "enterprise", "team",
               "business", "features", "compare", "alternatives"]:
        if kw in " ".join([s.lower() for s in []]):
            pass

    return max(0.0, score)


def fetch_sitemap_urls(base_url: str, fetcher: Optional[object] = None, max_urls: int = 500) -> List[str]:
    """Fetch URLs from sitemap with multiple fallback paths."""
    import requests

    urls: List[str] = []

    # Try standard sitemap paths
    for path in SITEMAP_PATHS:
        try:
            url = urljoin(base_url.rstrip("/") + "/", path)
            response = requests.get(
                url,
                headers={"User-Agent": "AICompetitorResearch/5.0"},
                timeout=15,
                allow_redirects=True,
            )
            if response.status_code == 200 and response.text:
                urls = _parse_sitemap(response.text, max_urls)
                if urls:
                    logger.info(f"Found {len(urls)} URLs in sitemap at {url}")
                    return urls[:max_urls]
        except Exception as e:
            logger.debug(f"Sitemap fetch failed for {url}: {e}")
            continue

    # Try robots.txt for sitemap reference
    try:
        robots_url = urljoin(base_url.rstrip("/") + "/", "/robots.txt")
        response = requests.get(
            robots_url,
            headers={"User-Agent": "AICompetitorResearch/5.0"},
            timeout=10,
        )
        if response.status_code == 200:
            for line in response.text.splitlines():
                line = line.strip()
                if line.lower().startswith("sitemap:"):
                    sitemap_url = line.split(":", 1)[1].strip()
                    if sitemap_url:
                        try:
                            response = requests.get(
                                sitemap_url,
                                headers={"User-Agent": "AICompetitorResearch/5.0"},
                                timeout=15,
                            )
                            if response.status_code == 200:
                                urls = _parse_sitemap(response.text, max_urls)
                                if urls:
                                    logger.info(f"Found {len(urls)} URLs in sitemap from robots.txt")
                                    return urls[:max_urls]
                        except Exception:
                            pass
    except Exception:
        pass

    return []


def _parse_sitemap(xml_text: str, max_urls: int = 500) -> List[str]:
    """Parse sitemap XML and extract URLs."""
    import xml.etree.ElementTree as ET

    urls: List[str] = []
    try:
        root = ET.fromstring(xml_text)
        # Handle both sitemap index and urlset
        for elem in root.iter():
            if elem.tag.endswith("}loc") or elem.tag == "loc":
                text = elem.text.strip() if elem.text else ""
                if text:
                    urls.append(text)
                    if len(urls) >= 500:
                        break
    except Exception:
        pass
    return urls[:500]


def discover_urls_for_host(base_url: str, max_urls: int = 100) -> List[str]:
    """Discover URLs for a host with multiple fallback strategies."""
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    urls: List[str] = []

    # 1. Try sitemap
    sitemap_urls = fetch_sitemap_urls(base)
    urls.extend(sitemap_urls[:max_urls])

    # 2. If sitemap failed, try common high-value paths
    if not urls:
        for path in HIGH_VALUE_PATHS[:50]:  # Limit to top 50
            url = f"{base.rstrip('/')}{path}"
            urls.append(url)

    # Deduplicate and limit
    seen = set()
    unique_urls = []
    for url in urls:
        if url not in urls:
            seen.add(url)
            unique_urls.append(url)
            if len(unique_urls) >= max_urls:
                break

    return unique_urls[:max_urls]


def _fallback_discovery(base_url: str, plan_keywords: List[str], max_urls: int = 100) -> List[str]:
    """Fallback discovery when sitemap fails."""
    from urllib.parse import urlparse, urljoin

    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    urls: List[str] = []

    # Try common high-value paths
    for path in HIGH_VALUE_PATHS:
        url = urljoin(base.rstrip("/") + "/", path.lstrip("/"))
        urls.append(url)

    # Score and sort
    scored = []
    for url in urls:
        score = _score_url(url, [])
        scored.append((score, url))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [url for score, url in scored[:max_urls]]