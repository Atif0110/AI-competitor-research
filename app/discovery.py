"""URL discovery and canonical URL deduplication.

Discovery supports both ecommerce/product sites and SaaS/software companies.

Flow:
    competitor site -> Firecrawl /map OR sitemap chain
                    -> remove obvious non-research pages
                    -> canonicalize + dedupe
                    -> optional focus-product filtering in the pipeline

The discovery layer is intentionally broader than product extraction.
Discovery finds candidate intelligence pages; the LLM extractor decides
whether a scraped page actually contains a valid commercial observation.

Sitemap chain:
    robots.txt -> Sitemap declarations -> sitemap index -> child sitemaps
                 -> candidate URLs
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Iterable, List
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------

# These paths are generally not useful for competitive product intelligence.
# They are filtered before scraping so we do not waste extraction calls.
_NON_RESEARCH_PATH_SEGMENTS = {
    "about",
    "author",
    "authors",
    "blog",
    "careers",
    "career",
    "changelog",
    "community",
    "contact",
    "customers",
    "customer-stories",
    "docs",
    "documentation",
    "faq",
    "help",
    "legal",
    "login",
    "logout",
    "news",
    "press",
    "privacy",
    "resources",
    "security",
    "status",
    "support",
    "terms",
    "webinars",
}

# These are especially important for this application's ProductOffer
# extractor. An integration page can contain product-like language but is not
# itself a commercial product offer.
_NON_OFFER_PATH_SEGMENTS = {
    "connections",
    "integration",
    "integrations",
}

# Conventional commercial paths used as a last-resort discovery fallback.
# These are candidates, not guaranteed valid pages.
_COMMON_INTELLIGENCE_PATHS = (
    "pricing",
    "plans",
    "product",
    "products",
    "features",
    "solutions",
    "enterprise",
    "compare",
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

_SITEMAP_TAG = re.compile(
    r"<loc>\s*(.*?)\s*</loc>",
    re.I | re.S,
)

_ROBOTS_SITEMAP = re.compile(
    r"(?im)^\s*Sitemap:\s*(.+)$",
)


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------


def canonical_url(url: str) -> str:
    """Normalize a URL so the same page always hashes identically."""
    u = urlparse(url.strip())

    scheme = u.scheme.lower() or "https"
    host = u.netloc.lower()
    path = u.path.rstrip("/") or "/"

    keep = [
        (key, value)
        for key, value in parse_qsl(
            u.query,
            keep_blank_values=True,
        )
        if key.lower() not in _DROP_QUERY
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
    return hashlib.sha256(
        canonical_url(url).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------


def _path_segments(url: str) -> list[str]:
    """Return normalized non-empty URL path segments."""
    path = urlparse(url).path

    return [
        segment.lower()
        for segment in path.split("/")
        if segment
    ]


def _is_non_research_url(url: str) -> bool:
    """Return True for pages that should not enter research scraping."""
    segments = _path_segments(url)

    if not segments:
        return False

    if any(
        segment in _NON_RESEARCH_PATH_SEGMENTS
        for segment in segments
    ):
        return True

    if any(
        segment in _NON_OFFER_PATH_SEGMENTS
        for segment in segments
    ):
        return True

    return False


def is_product_url(url: str) -> bool:
    """Return whether a URL is a plausible competitive-intelligence page.

    The function name is retained for backwards compatibility.

    Historically this function required specific product-like path patterns.
    That was too restrictive for SaaS websites, whose commercial pages can
    have arbitrary URL structures. The new behavior therefore means:

        valid HTTP(S) URL
        + not an obvious non-research/non-offer page

    The actual LLM extractor remains responsible for deciding whether the
    page contains a valid ProductOffer.
    """
    parsed = urlparse(url)

    if parsed.scheme.lower() not in {
        "http",
        "https",
    }:
        return False

    if not parsed.netloc:
        return False

    return not _is_non_research_url(url)


def _is_extractable_candidate(url: str) -> bool:
    """Return whether a URL is worth sending through product extraction."""
    return is_product_url(url)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def dedupe_urls(urls: Iterable[str]) -> List[str]:
    """Deduplicate URLs using their canonical URL hash."""
    seen = set()
    output: list[str] = []

    for url in urls:
        if not isinstance(url, str):
            continue

        if not url.strip():
            continue

        canonical = canonical_url(url)

        if not canonical.startswith(
            (
                "http://",
                "https://",
            )
        ):
            continue

        url_key = url_hash(canonical)

        if url_key in seen:
            continue

        seen.add(url_key)
        output.append(canonical)

    return output


# ---------------------------------------------------------------------------
# Domain handling
# ---------------------------------------------------------------------------


def _hostname(url: str) -> str:
    """Return a normalized hostname."""
    return (
        urlparse(url)
        .netloc
        .lower()
        .split(":", 1)[0]
    )


def _same_domain(
    url: str,
    website: str,
    allowed_hosts: set[str] | None = None,
) -> bool:
    """Allow the target hostname, subdomains and known redirect host."""
    candidate = _hostname(url)

    if not candidate:
        return False

    hosts = {
        _hostname(website),
    }

    if allowed_hosts:
        hosts.update(
            host.lower()
            for host in allowed_hosts
            if host
        )

    for host in hosts:
        if (
            candidate == host
            or candidate.endswith("." + host)
        ):
            return True

    return False


def _resolve_allowed_hosts(
    website: str,
) -> set[str]:
    """Resolve the supplied website and collect its final hostname.

    This handles sites where the user enters an old/alternate official
    hostname and the current commercial site redirects elsewhere.

    Example:
        notion.so -> notion.com
    """
    hosts = {
        _hostname(website),
    }

    if not hosts:
        return hosts

    try:
        import requests

        response = requests.get(
            website,
            timeout=settings.request_timeout_seconds,
            headers={
                "User-Agent": "cintel-research/1.0",
            },
            allow_redirects=True,
        )

        final_host = _hostname(
            response.url
        )

        if final_host:
            hosts.add(final_host)

        logger.info(
            "resolved discovery hosts for %s: %s",
            website,
            sorted(hosts),
        )

    except Exception as exc:
        logger.info(
            "could not resolve redirect host for %s: %s",
            website,
            exc,
        )

    return hosts


# ---------------------------------------------------------------------------
# Firecrawl
# ---------------------------------------------------------------------------


def _firecrawl_link_url(
    link,
) -> str | None:
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
    value = getattr(
        link,
        "url",
        None,
    )

    if isinstance(value, str):
        return value.strip() or None

    return None


def discover_firecrawl(
    website: str,
) -> List[str]:
    """Discover candidate intelligence URLs through Firecrawl /map."""
    import requests

    if not settings.firecrawl_api_key:
        raise RuntimeError(
            "FIRECRAWL_API_KEY not set"
        )

    allowed_hosts = _resolve_allowed_hosts(
        website
    )

    response = requests.post(
        f"{settings.firecrawl_base_url}/map",
        json={
            "url": website,
            "limit": 200,
        },
        headers={
            "Authorization": (
                f"Bearer {settings.firecrawl_api_key}"
            ),
            "Content-Type": "application/json",
        },
        timeout=settings.request_timeout_seconds,
    )

    if response.status_code != 200:
        raise RuntimeError(
            "firecrawl map http "
            f"{response.status_code}"
        )

    payload = response.json()

    raw_links = payload.get(
        "links"
    ) or []

    candidates: list[str] = []

    for raw_link in raw_links:
        url = _firecrawl_link_url(
            raw_link
        )

        if not url:
            continue

        if not _same_domain(
            url,
            website,
            allowed_hosts,
        ):
            continue

        if not _is_extractable_candidate(
            url
        ):
            continue

        candidates.append(url)

    # Always consider the supplied website itself.
    # Homepage content can contain pricing/product intelligence and is useful
    # when a site has an unusual URL structure.
    if is_product_url(website):
        candidates.insert(
            0,
            website,
        )

    result = dedupe_urls(
        candidates
    )

    logger.info(
        "firecrawl discovered %d candidate URLs "
        "from %d returned links for %s",
        len(result),
        len(raw_links),
        website,
    )

    return result


# ---------------------------------------------------------------------------
# HTTP / sitemap helpers
# ---------------------------------------------------------------------------


def _fetch(url: str) -> str:
    """Fetch a URL and return its text."""
    import requests

    response = requests.get(
        url,
        timeout=settings.request_timeout_seconds,
        headers={
            "User-Agent": "cintel-research/1.0",
        },
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"http {response.status_code} for {url}"
        )

    return response.text


def _loc_links(
    text: str,
) -> List[str]:
    """Extract sitemap <loc> URLs."""
    return [
        link.strip()
        for link in _SITEMAP_TAG.findall(
            text
        )
    ]


# ---------------------------------------------------------------------------
# Sitemap discovery
# ---------------------------------------------------------------------------


def discover_sitemaps(
    website: str,
) -> List[str]:
    """Discover candidate intelligence URLs through sitemap files."""
    allowed_hosts = _resolve_allowed_hosts(
        website
    )

    sitemap_urls: List[str] = []

    try:
        robots = _fetch(
            website.rstrip("/")
            + "/robots.txt"
        )

        sitemap_urls = [
            sitemap.strip()
            for sitemap in _ROBOTS_SITEMAP.findall(
                robots
            )
        ]

        logger.info(
            "robots.txt declared %d sitemap(s) for %s",
            len(sitemap_urls),
            website,
        )

    except Exception as exc:
        logger.info(
            "no robots.txt for %s (%s) — "
            "defaulting to /sitemap.xml",
            website,
            exc,
        )

    if not sitemap_urls:
        sitemap_urls = [
            website.rstrip("/")
            + "/sitemap.xml"
        ]

    urls: List[str] = []
    visited = set()

    def process(
        sitemap: str,
        depth: int = 0,
    ) -> None:
        sitemap = sitemap.strip()

        if not sitemap:
            return

        # Keep sitemap traversal bounded.
        if (
            sitemap in visited
            or depth > 2
        ):
            return

        visited.add(sitemap)

        try:
            text = _fetch(
                sitemap
            )

        except Exception as exc:
            logger.warning(
                "sitemap fetch failed %s: %s",
                sitemap,
                exc,
            )
            return

        for loc in _loc_links(
            text
        ):
            loc = loc.strip()

            if not loc:
                continue

            if (
                loc.lower().endswith(
                    ".xml"
                )
                or "sitemap" in loc.lower()
            ):
                process(
                    loc,
                    depth + 1,
                )
                continue

            if not _same_domain(
                loc,
                website,
                allowed_hosts,
            ):
                continue

            if not _is_extractable_candidate(
                loc
            ):
                continue

            urls.append(loc)

    for sitemap in sitemap_urls:
        process(sitemap)

    # The supplied website itself is always a candidate.
    if is_product_url(website):
        urls.insert(
            0,
            website,
        )

    result = dedupe_urls(
        urls
    )

    logger.info(
        "sitemap discovery found %d candidate URLs for %s",
        len(result),
        website,
    )

    return result


# ---------------------------------------------------------------------------
# Last-resort commercial URL seeding
# ---------------------------------------------------------------------------


def _seed_common_intelligence_urls(
    website: str,
) -> List[str]:
    """Generate a small set of conventional commercial URLs.

    This is only used when Firecrawl and sitemap discovery produce no
    candidates. A missing page is harmless because the scraper will reject
    it normally.
    """
    parsed = urlparse(
        website
    )

    if not parsed.netloc:
        return []

    base = urlunparse(
        (
            parsed.scheme
            or "https",
            parsed.netloc,
            "",
            "",
            "",
            "",
        )
    ).rstrip("/")

    seeded = [
        f"{base}/{path}"
        for path in _COMMON_INTELLIGENCE_PATHS
    ]

    # Include the homepage as the first candidate.
    seeded.insert(
        0,
        base,
    )

    return dedupe_urls(
        seeded
    )


# ---------------------------------------------------------------------------
# Public discovery entry point
# ---------------------------------------------------------------------------


def discover_urls_for_entity(
    website: str,
) -> List[str]:
    """Discover candidate research URLs using multiple strategies.

    Strategy:

    1. Firecrawl Map.
    2. Sitemap discovery if Firecrawl has no useful candidates.
    3. Common commercial URL seeds as a final fallback.

    The extractor remains responsible for deciding whether a scraped page
    actually contains a valid commercial ProductOffer.
    """

    # --------------------------------------------------------------
    # 1. Firecrawl
    # --------------------------------------------------------------

    if settings.firecrawl_api_key:
        try:
            found = discover_firecrawl(
                website
            )

            if found:
                logger.info(
                    "using %d URLs discovered by Firecrawl for %s",
                    len(found),
                    website,
                )

                return found

            logger.warning(
                "Firecrawl returned no usable URLs for %s; "
                "falling back to sitemap discovery",
                website,
            )

        except Exception as exc:
            logger.warning(
                "Firecrawl discovery failed for %s: %s",
                website,
                exc,
            )

    # --------------------------------------------------------------
    # 2. Sitemap
    # --------------------------------------------------------------

    try:
        found = discover_sitemaps(
            website
        )

        if found:
            logger.info(
                "using %d URLs discovered through sitemap for %s",
                len(found),
                website,
            )

            return found

    except Exception as exc:
        logger.warning(
            "sitemap discovery failed for %s: %s",
            website,
            exc,
        )

    # --------------------------------------------------------------
    # 3. Last-resort conventional paths
    # --------------------------------------------------------------

    seeded = _seed_common_intelligence_urls(
        website
    )

    logger.warning(
        "no discovered URLs for %s; "
        "using %d conventional intelligence URL candidates",
        website,
        len(seeded),
    )

    return seeded
