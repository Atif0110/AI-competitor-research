"""Deep-research contracts.

Everything the research layer produces is one of these models, so the API,
the report builder and the chat layer all read the same shapes.

Citation model
--------------
Every stored page gets a stable per-run reference (``S1``, ``S2``, ...). A
section inherits its page's reference and adds its heading anchor. Answers and
report prose cite those references as ``[S1]``, which is what makes a claim
traceable back to the exact page text that was captured during the run.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PageType(str, Enum):
    """What a crawled page is, after classification."""

    product = "product"
    pricing = "pricing"
    review = "review"
    docs = "docs"
    company = "company"
    article = "article"
    legal = "legal"
    listing = "listing"
    comparison = "comparison"
    other = "other"


class ResearchMode(str, Enum):
    """How a run collects evidence."""

    url = "url"
    topic = "topic"
    compare = "compare"


class ResearchStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    partial = "partial"
    failed = "failed"


class ResearchRequest(BaseModel):
    """What the user asked to research."""

    mode: ResearchMode = ResearchMode.url
    url: Optional[str] = None
    question: str = Field(default="", max_length=2000)
    company: Optional[str] = Field(default=None, max_length=200)
    competitors: List[str] = Field(default_factory=list, max_length=10)
    max_pages: Optional[int] = Field(default=None, ge=1, le=60)
    max_depth: Optional[int] = Field(default=None, ge=0, le=4)
    follow_external: bool = True
    use_search: bool = True
    extract_reviews: bool = True
    use_llm: bool = True
    extra_seeds: List[str] = Field(default_factory=list, max_length=20)


class SearchHit(BaseModel):
    """One external search result."""

    url: str
    title: str = ""
    snippet: str = ""
    origin: str = "search"


class ResearchPlan(BaseModel):
    """Planner output: what to look for and which pages to open."""

    objective: str = ""
    subject: str = ""
    sub_questions: List[str] = Field(default_factory=list)
    search_queries: List[str] = Field(default_factory=list)
    seed_urls: List[str] = Field(default_factory=list)
    page_types: List[PageType] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list)
    max_pages: int = 12
    max_depth: int = 2
    planner: str = "deterministic"

    def questions(self, limit: int = 8) -> List[str]:
        return [q for q in self.sub_questions if q][:limit]


class PageDocument(BaseModel):
    """A fetched page: raw content plus the metadata research needs."""

    url: str
    final_url: str = ""
    status: str = "fetched"
    http_status: Optional[int] = None
    title: str = ""
    description: str = ""
    text: str = ""
    markdown: str = ""
    links: List[str] = Field(default_factory=list)
    depth: int = 0
    fetched_at: datetime = Field(default_factory=_utcnow)
    content_hash: str = ""
    scraper: str = ""
    error: Optional[str] = None
    page_type: PageType = PageType.other
    relevance: float = 0.0
    source_ref: str = ""
    origin: str = "crawl"

    @property
    def usable(self) -> bool:
        return bool(self.text and self.text.strip())


class ResearchSection(BaseModel):
    """One retrievable unit of page content (chat/RAG granularity)."""

    section_id: Optional[int] = None
    page_id: Optional[int] = None
    run_id: str = ""
    source_ref: str = ""
    url: str = ""
    title: str = ""
    heading: str = ""
    content: str = ""
    index: int = 0
    word_count: int = 0
    page_type: PageType = PageType.other
    created_at: datetime = Field(default_factory=_utcnow)


class Fact(BaseModel):
    """A single structured claim extracted from a page, with its evidence."""

    page_id: Optional[int] = None
    source_ref: str = ""
    url: str = ""
    subject: str = Field(default="", max_length=200)
    question: str = ""
    statement: str = Field(min_length=1)
    value: Optional[str] = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    kind: str = "fact"
    quote: str = ""


class Citation(BaseModel):
    """A resolved reference used by an answer or a report."""

    ref: str
    url: str
    title: str = ""
    heading: str = ""
    snippet: str = ""


class ResearchGap(BaseModel):
    """Something the run tried to answer but could not support."""

    question: str
    reason: str


class ResearchCoverage(BaseModel):
    """Honest accounting of what the run did and did not do."""

    pages_discovered: int = 0
    pages_fetched: int = 0
    pages_failed: int = 0
    pages_skipped: int = 0
    external_sources: int = 0
    search_queries: int = 0
    sections: int = 0
    facts: int = 0
    characters: int = 0
    search_backend: Optional[str] = None
    budget_exhausted: bool = False
    llm_enabled: bool = False
    provider: Optional[str] = None


class DeepResearchResult(BaseModel):
    """Everything a completed (or partial) research run produced."""

    run_id: str
    status: ResearchStatus = ResearchStatus.completed
    mode: ResearchMode = ResearchMode.url
    request: ResearchRequest = Field(default_factory=ResearchRequest)
    question: str = ""
    plan: ResearchPlan = Field(default_factory=ResearchPlan)
    sections: List[ResearchSection] = Field(default_factory=list)
    facts: List[Fact] = Field(default_factory=list)
    citations: List[Citation] = Field(default_factory=list)
    report_markdown: str = ""
    findings: List[str] = Field(default_factory=list)
    gaps: List[ResearchGap] = Field(default_factory=list)
    coverage: ResearchCoverage = Field(default_factory=ResearchCoverage)
    pages: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    duration_s: float = 0.0
    created_at: datetime = Field(default_factory=_utcnow)

    def source_map(self) -> Dict[str, Citation]:
        return {c.ref: c for c in self.citations}


# ----------------------------------------------------------------------
# research chat
# ----------------------------------------------------------------------
class ChatMessage(BaseModel):
    role: str = "user"  # user | assistant
    content: str = ""
    citations: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)


class ChatAnswer(BaseModel):
    session_id: str = ""
    run_id: str = ""
    message: str = ""
    citations: List[Dict[str, Any]] = Field(default_factory=list)
    sources_used: int = 0
    answerable: bool = True
    provider: str = ""
    created_at: datetime = Field(default_factory=_utcnow)