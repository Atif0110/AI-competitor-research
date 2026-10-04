"""Deep crawler.

Breadth-first over links, but budget-aware and relevance-driven:

* the frontier is ordered by how well a URL looks for the plan (path/title
  signals, plan keywords), so the budget is spent on pages that can actually
  answer the sub-questions;
* per-host, per-depth and total page ceilings stop one site (or one run) from
  consuming the whole budget;
* external links are only followed for high-signal paths and never leave the
  approved host set unless the plan asked for it.

Every fetched page is returned as a ``PageDocument`` with evidence metadata
(hash, HTTP status, scraper, depth) attached.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from app.config import settings
from app.research.fetching import (
    Fetcher,
    FetchResult,
    content_hash,
    looks_like_document,
    normalize_link,
    parse_page,
)
from app.research.models import PageDocument, PageType, ResearchPlan

logger = logging.getLogger(__name__)

_HIGH_SIGNAL_SEGMENTS = {
    "pricing", "price", "plans", "plan", "products", "product", "features",
    "compare", "comparison", "vs", "alternatives", "reviews", "review",
    "customers", "stories", "case-studies", "faq", "docs", "documentation",
    "guides", "blog", "news", "about", "company", "team", "enterprise",
    "security", "integrations", "download", "specs", "specifications",
}

_LOW_SIGNAL_SEGMENTS = {
    "login", "signup", "sign-in", "sign-up", "register", "cart", "checkout",
    "account", "profile", "settings", "logout", "privacy", "terms", "legal",
    "cookie", "cookies", "press", "careers", "jobs", "community", "events",
    "webinars", "podcast", "rss", "sitemap", "search", "tag", "tags",
}

_TYPE_SEGMENTS = {
    "pricing": ("pricing", "price", "plans", "plan", "billing", "subscription"),
    "product": ("product", "products", "product/", "shop", "store", "features"),
    "review": ("review", "reviews", "testimonials", "ratings", "customers"),
    "docs": ("docs", "documentation", "guide", "guides", "help", "api", "reference"),
    "company": ("about", "company", "team", "mission", "careers"),
    "comparison": ("compare", "comparison", "vs", "versus", "alternatives"),
    "listing": ("listing", "listings", "directory", "marketplace"),
    "legal": ("privacy", "terms", "legal", "compliance", "dpa"),
    "article": ("blog", "news", "post", "posts", "article"),
}


@dataclass
class CrawlBudget:
    max_pages: int = 12
    max_depth: int = 2
    max_pages_per_host: int = 8
    follow_external: bool = True
    max_external: int = 4

    @classmethod
    def from_plan(cls, plan: ResearchPlan, overrides: Optional[dict] = None) -> "CrawlBudget":
        overrides = overrides or {}
        return cls(
            max_pages=int(overrides.get("max_pages") or plan.max_pages or settings.deep_max_pages),
            max_depth=int(overrides.get("max_depth") if overrides.get("max_depth") is not None else plan.max_depth),
            max_pages_per_host=int(settings.deep_max_pages_per_host),
            follow_external=bool(overrides.get("follow_external", True)),
            max_external=int(overrides.get("max_external", 4)),
        )


@dataclass
class CrawlOutcome:
    pages: List[PageDocument] = field(default_factory=list)
    frontier_seen: int = 0
    external_used: int = 0
    budget_exhausted: bool = False
    errors: List[str] = field(default_factory=list)

    @property
    def fetched(self) -> List[PageDocument]:
        return [p for p in self.pages if p.usable]

    @property
    def failed(self) -> List[PageDocument]:
        return [p for p in self.pages if not p.usable]


def classify_by_url(url: str, title: str = "", snippet: str = "") -> PageType:
    """Cheap, deterministic page classification from URL + title signals."""
    haystack = f"{urlparse(url).path.lower()} {title.lower()} {snippet.lower()}"
    segments = [s for s in urlparse(url).path.lower().split("/") if s]
    for segment in segments:
        if segment in _LOW_SIGNAL_SEGMENTS:
            return PageType.legal if segment in ("privacy", "terms", "legal") else PageType.other
    best: Tuple[int, PageType] = (0, PageType.other)
    for page_type, needles in _TYPE_SEGMENTS.items():
        for needle in needles:
            if needle in haystack:
                score = len(needle)
                if score > best[0]:
                    best = (score, PageType(page_type))
                break
    return best[1]


def url_score(url: str, plan: ResearchPlan) -> float:
    """Frontier priority: how promising is this URL for the plan?"""
    parsed = urlparse(url)
    path = parsed.path.lower()
    segments = [s for s in path.split("/") if s]
    score = 0.0

    if any(segment in _HIGH_SIGNAL_SEGMENTS for segment in segments):
        score += 3.0
    if any(segment in _LOW_SIGNAL_SEGMENTS for segment in segments):
        score -= 4.0

    slug = "-".join(segments)
    for keyword in plan.keywords[:20]:
        if keyword in slug:
            score += 1.5
    for wanted in plan.page_types[:6]:
        if wanted.value in path:
            score += 1.0

    depth = len(segments)
    score -= depth * 0.4
    if path in ("", "/"):
        score += 1.0  # the root page is usually a good overview
    if re.search(r"\.(jpg|png|pdf|zip)$", path):
        score -= 10.0
    return score


class Crawler:
    def __init__(
        self,
        fetcher: Optional[Fetcher] = None,
        budget: Optional[CrawlBudget] = None,
        approved_hosts: Optional[Iterable[str]] = None,
        max_chars: Optional[int] = None,
    ):
        self.fetcher = fetcher or Fetcher()
        self.budget = budget or CrawlBudget()
        self.approved_hosts = {
            h.lower() for h in (approved_hosts or [])
        }
        self.max_chars = max_chars or settings.deep_page_char_budget

    # ------------------------------------------------------------------
    def crawl(
        self,
        seeds: Sequence[str],
        plan: ResearchPlan,
        on_page=None,
    ) -> CrawlOutcome:
        outcome = CrawlOutcome()
        queue: List[Tuple[float, int, str]] = []
        seen: Set[str] = set()
        host_counts: Dict[str, int] = {}
        external_used = 0

        def host_of(url: str) -> str:
            return urlparse(url).netloc.lower()

        def enqueue(url: str, depth: int, priority_bonus: float = 0.0) -> None:
            key = _dedupe_key(url)
            if not key or key in seen:
                return
            if depth > self.budget.max_depth:
                return
            if not looks_like_document(url):
                return
            host = host_of(url)
            external = bool(self.approved_hosts) and host not in self.approved_hosts
            if external:
                if not self.budget.follow_external:
                    return
                if external_used >= self.budget.max_external + depth:
                    return
                if host_counts.get(host, 0) >= 2:
                    return
            seen.add(key)
            outcome.frontier_seen += 1
            queue.append((-(url_score(url, plan) + priority_bonus), depth, url))

        for seed in seeds:
            enqueue(seed, 0, priority_bonus=6.0)

        queue.sort(key=lambda item: (item[0], item[1]))

        while queue:
            if len(outcome.pages) >= self.budget.max_pages:
                outcome.budget_exhausted = True
                break
            _priority, depth, url = queue.pop(0)
            host = host_of(url)
            if host_counts.get(host, 0) >= self.budget.max_pages_per_host:
                continue

            document = self._fetch_one(url, depth, plan)
            outcome.pages.append(document)
            host_counts[host] = host_counts.get(host, 0) + 1

            if host not in self.approved_hosts and self.approved_hosts:
                external_used += 1

            if not document.usable:
                continue
            if on_page:
                try:
                    on_page(document)
                except Exception:
                    logger.debug("page callback failed", exc_info=True)

            if depth < self.budget.max_depth:
                for link in document.links[:60]:
                    enqueue(link, depth + 1)
            if depth == 0 and not document.links and self.budget.max_depth >= 1:
                # A JavaScript-only site exposes no crawlable links at all.
                # Only then are well-known paths worth a speculative request;
                # enqueue() de-duplicates, so this costs nothing extra when a
                # link already reached them.
                host = host_of(url)
                scheme = urlparse(url).scheme
                for segment in ("pricing", "products", "product", "about", "docs"):
                    enqueue(f"{scheme}://{host}/{segment}", 1, priority_bonus=2.0)

            queue.sort(key=lambda item: (item[0], item[1]))

        outcome.external_used = external_used
        return outcome

    # ------------------------------------------------------------------
    def _fetch_one(self, url: str, depth: int, plan: ResearchPlan) -> PageDocument:
        result: FetchResult = self.fetcher.fetch(url)

        if not result.ok:
            return PageDocument(
                url=url,
                final_url=result.final_url or url,
                status="failed",
                error=result.error or "fetch failed",
                depth=depth,
                scraper=result.scraper,
                page_type=classify_by_url(url),
            )

        if not result.html:
            # Not an HTML page (PDF/asset): record it without pretending we
            # read its contents.
            return PageDocument(
                url=url,
                final_url=result.final_url or url,
                status="skipped",
                http_status=result.status,
                depth=depth,
                scraper=result.scraper,
                error=f"unsupported content type {result.content_type or 'unknown'}",
                page_type=PageType.other,
            )

        parsed = parse_page(result.html, result.final_url or url)
        text = parsed.text[: self.max_chars]
        snippet = f"{parsed.description} {parsed.title}"
        page_type = classify_by_url(result.final_url or url, parsed.title, snippet)
        relevance = _relevance(text, f"{parsed.title} {snippet}", plan)

        return PageDocument(
            url=url,
            final_url=result.final_url or url,
            status="fetched",
            http_status=result.status,
            title=parsed.title or (result.final_url or url),
            description=parsed.description,
            text=text,
            markdown=parsed.markdown[: self.max_chars],
            links=parsed.links,
            depth=depth,
            scraper=result.scraper,
            content_hash=content_hash(text),
            page_type=page_type,
            relevance=relevance,
        )


def _relevance(text: str, head: str, plan: ResearchPlan) -> float:
    """0..1 keyword coverage of a page against the plan."""
    keywords = [k for k in plan.keywords[:24] if len(k) > 2]
    if not keywords:
        return 0.0
    haystack = f"{head} {text[:4000]}".lower()
    hits = sum(1 for k in keywords if k in haystack)
    return round(min(1.0, hits / max(3, len(keywords) * 0.5)), 3)


def _dedupe_key(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"/+$", "", (parsed.path or "/").lower())
    return f"{parsed.netloc.lower()}{path}"