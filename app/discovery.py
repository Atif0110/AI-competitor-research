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
import time
from typing import Iterable, List
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.config import settings
from app.url_safety import is_public_http_url, safe_http_get

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Discovery limits
# ---------------------------------------------------------------------------

MAX_DISCOVERY_CANDIDATES = 24
FIRECRAWL_MAP_LIMIT = 200
DISCOVERY_REQUEST_TIMEOUT_SECONDS = 4
DISCOVERY_SITEMAP_DOCUMENT_LIMIT = 8


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
    "workspace",
    "workspaces",
    "dashboard",
    "admin",
    "account",
    "accounts",
    "signin",
    "signup",
    "register",
    "oauth",
    "authorize",
}

_NON_PUBLIC_PATH_SEGMENTS = {
    "p",
    "w",
    "s",
    "page",
    "pages",
    "users",
    "user",
    "members",
    "member",
    "settings",
    "templates",
}

_NON_PUBLIC_HOST_PREFIXES = {
    "app.",
    "admin.",
    "dashboard.",
    "accounts.",
    "account.",
    "auth.",
    "login.",
    "signin.",
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

_HIGH_SIGNAL_PATH_SEGMENTS = {
    "pricing",
    "plans",
    "product",
    "products",
    "features",
    "solutions",
    "enterprise",
    "compare",
    "comparison",
    "alternatives",
    "platform",
    "capabilities",
    "use-cases",
    "usecase",
    "services",
    "business",
    "teams",
    "shop",
    "store",
    "buy",
    "collection",
    "collections",
    "catalog",
    "category",
    "categories",
    "smartphone",
    "smartphones",
    "phone",
    "phones",
}

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


def _is_non_public_host(url: str) -> bool:
    """Return True for common application/admin/auth subdomains."""
    hostname = _hostname(url)

    if not hostname:
        return False

    return any(
        hostname.startswith(prefix)
        for prefix in _NON_PUBLIC_HOST_PREFIXES
    )


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

    if any(
        segment in _NON_PUBLIC_PATH_SEGMENTS
        for segment in segments
    ):
        return True

    if _is_non_public_host(url):
        return True

    return False


def is_product_url(url: str) -> bool:
    """Return whether a URL is a plausible competitive-intelligence page.

    The function name is retained for backwards compatibility.
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


def _focus_score(url: str, focus_products: Iterable[str]) -> int:
    path = "/".join(_path_segments(url))
    tokens = set(re.findall(r"[a-z0-9]+", path))
    score = 0
    for focus in focus_products:
        focus_tokens = [token for token in re.findall(r"[a-z0-9]+", focus.lower()) if len(token) > 2]
        if not focus_tokens:
            continue
        score += 2000 * sum(token in tokens for token in focus_tokens)
        compact = "-".join(focus_tokens)
        if compact and compact in path:
            score += 1000
    return score


def _candidate_score(url: str, website: str, focus_products: Iterable[str] = ()) -> int:
    """Score URLs so high-signal commercial pages are processed first."""
    parsed = urlparse(url)
    segments = _path_segments(url)

    score = 0

    # Pricing pages are the highest-value input for a structured offer run.
    # Rank them ahead of generic home/product pages so the per-entity budget
    # does not get spent on pages that cannot contain comparable prices.
    if any(segment in {"pricing", "plans"} for segment in segments):
        score += 1500

    if any(segment in {"product", "products", "item", "items"} for segment in segments):
        score += 250

    if canonical_url(url) == canonical_url(website):
        # Keep the supplied page in consideration, while ranking discovered
        # product and store pages above a generic homepage.
        score += 100

    if not segments:
        score += 50

    score += sum(
        100
        for segment in segments
        if segment in _HIGH_SIGNAL_PATH_SEGMENTS
    )

    score -= len(segments) * 3

    if parsed.query:
        score -= 5

    score += _focus_score(url, focus_products)

    return score


def _rank_and_limit_candidates(
    urls: Iterable[str],
    website: str,
    limit: int = MAX_DISCOVERY_CANDIDATES,
    focus_products: Iterable[str] = (),
) -> List[str]:
    """Canonicalize, dedupe, rank and hard-cap candidate URLs."""
    candidates = dedupe_urls(urls)

    candidates = [
        url
        for url in candidates
        if _is_extractable_candidate(url)
    ]

    candidates.sort(
        key=lambda url: (
            -_candidate_score(url, website, focus_products),
            len(url),
            url,
        )
    )

    return candidates[:limit]


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

    # Do not fetch the homepage here. Discovery immediately checks robots.txt
    # and sitemaps, and the old preliminary full-page GET could block for the
    # full request timeout before useful work even started.
    logger.info("starting discovery for %s (seed host: %s)", website, sorted(hosts))

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
    focus_products: Iterable[str] = (),
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
            "limit": FIRECRAWL_MAP_LIMIT,
        },
        headers={
            "Authorization": (
                f"Bearer {settings.firecrawl_api_key}"
            ),
            "Content-Type": "application/json",
        },
        timeout=min(settings.request_timeout_seconds, 10),
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

    result = _rank_and_limit_candidates(
        candidates,
        website,
        focus_products=focus_products,
    )

    logger.info(
        "firecrawl discovered %d usable candidates "
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

    response = safe_http_get(
        url,
        timeout=min(settings.request_timeout_seconds, DISCOVERY_REQUEST_TIMEOUT_SECONDS),
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
    focus_products: Iterable[str] = (),
) -> List[str]:
    """Discover candidate intelligence URLs through sitemap files."""
    allowed_hosts = _resolve_allowed_hosts(
        website
    )
    started = time.monotonic()

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
            or len(visited) >= DISCOVERY_SITEMAP_DOCUMENT_LIMIT
            or time.monotonic() - started >= DISCOVERY_REQUEST_TIMEOUT_SECONDS * 6
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

            if len(urls) >= MAX_DISCOVERY_CANDIDATES * 10:
                return

    for sitemap in sitemap_urls:
        process(sitemap)
        if len(visited) >= DISCOVERY_SITEMAP_DOCUMENT_LIMIT or time.monotonic() - started >= DISCOVERY_REQUEST_TIMEOUT_SECONDS * 6:
            break

    # The supplied website itself is always a candidate.
    if is_product_url(website):
        urls.insert(
            0,
            website,
        )

    result = _rank_and_limit_candidates(
        urls,
        website,
        focus_products=focus_products,
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
    focus_products: Iterable[str] = (),
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

    return _rank_and_limit_candidates(
        seeded,
        website,
        focus_products=focus_products,
    )


def _discover_search_urls(website: str, focus_products: Iterable[str]) -> List[str]:
    """Use a bounded keyless site query to find product pages outside sitemaps."""
    from app.research.sources import DuckDuckGoSearch

    host = _hostname(website)
    focus = " ".join(str(value).strip() for value in focus_products if str(value).strip())[:160]
    query = f"site:{host} {focus} price buy product".strip()
    try:
        hits = DuckDuckGoSearch(timeout=4).search(query, limit=8)
    except Exception as exc:
        logger.info("site search failed for %s: %s", host, type(exc).__name__)
        return []
    return [
        hit.url for hit in hits
        if hit.url
        and is_public_http_url(hit.url)
        and _same_domain(hit.url, website)
        and _is_extractable_candidate(hit.url)
    ]


# ---------------------------------------------------------------------------
# Public discovery entry point
# ---------------------------------------------------------------------------


def discover_urls_for_entity(
    website: str,
    focus_products: Iterable[str] = (),
) -> List[str]:
    """Discover candidate research URLs using multiple strategies.

    Combine focused keyless search, Firecrawl, and sitemap results. Search
    catches product pages missing from sitemaps; sitemaps provide breadth.

    The extractor remains responsible for deciding whether a scraped page
    actually contains a valid commercial ProductOffer.
    """

    # --------------------------------------------------------------
    focus_products = tuple(focus_products)
    found: list[str] = []

    # Search is cheap, keyless, and useful when the URL slug differs from the
    # phrase supplied by the user. Results remain on the requested host.
    found.extend(_discover_search_urls(website, focus_products))

    # Firecrawl supplements search rather than short-circuiting sitemaps.
    # --------------------------------------------------------------

    if settings.firecrawl_api_key and len(found) < 3:
        try:
            firecrawl_found = discover_firecrawl(
                website,
                focus_products=focus_products,
            )

            if firecrawl_found:
                logger.info(
                    "using %d URLs discovered by Firecrawl for %s",
                    len(firecrawl_found),
                    website,
                )
                found.extend(firecrawl_found)
            else:
                logger.warning("Firecrawl returned no usable URLs for %s", website)

        except Exception as exc:
            logger.warning(
                "Firecrawl discovery failed for %s: %s",
                website,
                exc,
            )

    # --------------------------------------------------------------
    # Sitemaps add broad coverage after search, unless search already returned
    # enough candidates to fill the per-company page budget with headroom.
    # --------------------------------------------------------------

    if len(found) < 8:
        try:
            found.extend(discover_sitemaps(website, focus_products=focus_products))

        except Exception as exc:
            logger.warning("sitemap discovery failed for %s: %s", website, exc)

    # --------------------------------------------------------------
    # 3. Last-resort conventional paths
    # --------------------------------------------------------------

    if not found:
        seeded = _seed_common_intelligence_urls(website, focus_products=focus_products)
        logger.warning("no search or sitemap URLs for %s; using %d conventional candidates", website, len(seeded))
        found.extend(seeded)

    return _rank_and_limit_candidates(
        found, website, focus_products=focus_products
    )
