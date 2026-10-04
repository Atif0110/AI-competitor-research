"""Research planning.

Turns "research https://acme.com/pricing, how does the enterprise plan compare
to competitors?" into a concrete plan: what to look for, which sub-questions to
answer, which queries to search, and how many pages / how much depth to spend.

The planner is LLM-assisted but never LLM-required: with no provider key (or a
failing provider) the deterministic planner still produces a usable plan, so
the engine never degrades to "vague research".
"""
from __future__ import annotations

import json
import logging
import re
from typing import List, Optional
from urllib.parse import urlparse

from app.config import settings
from app.research.models import PageType, ResearchMode, ResearchPlan, ResearchRequest
from app.storage.documents import tokenize

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a research planner for a competitive-intelligence system. Given a "
    "research objective, produce a concrete plan. Reply with JSON only:\n"
    '{"objective": str, "subject": str, "sub_questions": [str], '
    '"search_queries": [str], "keywords": [str], "page_types": [str]}.\n'
    "Rules: sub_questions are specific and answerable from web pages (4-8 of "
    "them, each answerable with a number, name, limit or feature). "
    "search_queries are short keyword queries a search engine would accept. "
    "keywords are lowercase single terms worth matching in page text. "
    "page_types must come from: product, pricing, review, docs, company, "
    "article, legal, listing, comparison, other."
)

# Page types a plan may request, in the order the prompt lists them.
_PAGE_TYPES = {t.value for t in PageType}

_COMMON_PAGE_TYPES = [
    PageType.company,
    PageType.product,
    PageType.pricing,
    PageType.review,
    PageType.docs,
    PageType.comparison,
    PageType.listing,
]

_QUESTION_STEMS = [
    "What does {subject} actually sell, and to whom?",
    "What are the current prices, plans and limits?",
    "Which features differentiate {subject} from its alternatives?",
    "What do customers complain about, and what do they praise?",
    "What are the terms, guarantees, limits and support commitments?",
    "What integrations, platform support and compliance are offered?",
    "How large is the company and who are its main competitors?",
    "What changed recently: launches, pricing moves or policy updates?",
]


def _clean_list(values, limit: int = 10) -> List[str]:
    out: List[str] = []
    for value in values or []:
        if not isinstance(value, str):
            continue
        text = " ".join(value.split()).strip()
        if text and text not in out:
            out.append(text[:300])
        if len(out) >= limit:
            break
    return out


def _page_types(values) -> List[PageType]:
    out: List[PageType] = []
    for value in values or []:
        try:
            page_type = PageType(str(value).strip().lower())
        except ValueError:
            continue
        if page_type not in out:
            out.append(page_type)
    return out[:len(_PAGE_TYPES)]


def _subject_for(request: ResearchRequest, plan_seed: str = "") -> str:
    if request.company:
        return request.company
    if request.url:
        host = urlparse(request.url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    if request.question:
        words = [w for w in request.question.split() if w]
        return " ".join(words[:6]) or plan_seed or "the subject"
    return plan_seed or "the subject"


def build_plan(
    request: ResearchRequest,
    llm=None,
    max_pages: Optional[int] = None,
    max_depth: Optional[int] = None,
) -> ResearchPlan:
    """Plan a run: LLM when a provider is available, deterministic otherwise."""
    budget_pages = max(1, min(int(max_pages or request.max_pages or settings.deep_max_pages), 60))
    budget_depth = max(0, min(int(max_depth if max_depth is not None else (request.max_depth or settings.deep_max_depth)), 4))

    objective = (request.question or "").strip()
    if not objective:
        if request.mode == ResearchMode.url and request.url:
            objective = f"Research everything relevant on {request.url}"
        elif request.competitors:
            objective = (
                f"Compare {request.company or 'the target'} with "
                f"{', '.join(request.competitors[:4])}"
            )
        else:
            objective = "Research the topic as deeply as the available sources allow"

    llm_plan = _llm_plan(request, objective) if (request.use_llm and llm is not None) else None

    plan = ResearchPlan(
        objective=objective,
        subject=_subject_for(request, (llm_plan.subject if llm_plan else "")),
        sub_questions=llm_plan.sub_questions if llm_plan else [],
        search_queries=llm_plan.search_queries if llm_plan else [],
        seed_urls=_seed_urls(request, llm_plan),
        page_types=llm_plan.page_types if llm_plan else [],
        keywords=llm_plan.keywords if llm_plan else [],
        max_pages=budget_pages,
        max_depth=budget_depth,
        planner=llm_plan.planner if llm_plan else "deterministic",
    )

    if not plan.sub_questions:
        plan.sub_questions = _deterministic_sub_questions(plan.subject, objective, request)
    if not plan.keywords:
        plan.keywords = tokenize(f"{plan.subject} {objective}")[:24]
    if not plan.page_types:
        plan.page_types = _COMMON_PAGE_TYPES
    if not plan.search_queries and request.mode == ResearchMode.topic:
        plan.search_queries = _deterministic_queries(plan.subject, objective, plan.keywords)
    return plan


def _seed_urls(request: ResearchRequest, llm_plan: Optional[ResearchPlan]) -> List[str]:
    seeds: List[str] = []
    if request.url:
        seeds.append(_normalize_seed(request.url))
    for extra in request.extra_seeds:
        seeds.append(_normalize_seed(extra))
    if llm_plan:
        seeds.extend(_normalize_seed(url) for url in llm_plan.seed_urls)
    return [url for url in dict.fromkeys(seeds) if url]


def _normalize_seed(url: str) -> str:
    candidate = (url or "").strip()
    if not candidate:
        return ""
    if "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"


def _llm_plan(request: ResearchRequest, objective: str) -> Optional[ResearchPlan]:
    """Ask the model for a plan; return None when it cannot help."""
    from app.llm.client import LLMError

    try:
        from app.llm.client import LLMClient
    except Exception:  # pragma: no cover - import guard
        return None

    try:
        client = _shared_client()
    except LLMError as exc:
        logger.info("planner LLM unavailable: %s", exc)
        return None
    if client is None or client.provider_name == "demo":
        return None

    user = (
        f"OBJECTIVE:\n{objective}\n\n"
        f"START URL: {request.url or 'none'}\n"
        f"TARGET COMPANY: {request.company or 'none'}\n"
        f"COMPETITORS: {', '.join(request.competitors) or 'none'}\n"
        f"MODE: {request.mode.value}"
    )
    try:
        raw = client.complete(_SYSTEM, user)
        parsed = _parse_plan_json(raw)
    except Exception as exc:
        logger.info("planner LLM call failed: %s", exc)
        return None
    if not parsed:
        return None

    return ResearchPlan(
        objective=str(parsed.get("objective") or objective)[:500],
        subject=str(parsed.get("subject") or "")[:200],
        sub_questions=_clean_list(parsed.get("sub_questions"), 8),
        search_queries=_clean_list(parsed.get("search_queries"), 8),
        seed_urls=_clean_list(parsed.get("seed_urls"), 10),
        keywords=[k.lower() for k in _clean_list(parsed.get("keywords"), 24)],
        page_types=_page_types(parsed.get("page_types")),
        planner="llm",
    )


_CLIENT_SINGLETON = None


def _shared_client():
    global _CLIENT_SINGLETON
    if _CLIENT_SINGLETON is None:
        from app.llm.client import LLMClient

        _CLIENT_SINGLETON = LLMClient()
    return _CLIENT_SINGLETON


def _parse_plan_json(raw: str):
    from app.llm.extractor import StructuredExtractor

    try:
        return StructuredExtractor._parse_json(raw)
    except Exception:
        logger.debug("planner returned non-JSON", exc_info=True)
        return None


def _deterministic_sub_questions(subject: str, objective: str, request: ResearchRequest) -> List[str]:
    questions = [
        stem.format(subject=subject or "the subject")
        for stem in _QUESTION_STEMS
    ]
    if request.competitors:
        for competitor in request.competitors[:3]:
            questions.append(
                f"How does {competitor} compare with {subject or 'the target'} on price, features and positioning?"
            )
    if objective and objective.lower() not in {q.lower() for q in questions}:
        questions.insert(0, objective)
    return questions[:8]


def _deterministic_queries(subject: str, objective: str, keywords: List[str]) -> List[str]:
    base = [w for w in re.findall(r"[A-Za-z0-9+.\-']{2,}", objective)][:8]
    queries = []
    if subject:
        queries.append(subject)
    if base:
        queries.append(" ".join(base[:6]))
    if keywords:
        queries.append(" ".join(keywords[:4]) + " review")
    queries.append(f"{subject} pricing" if subject else objective[:60])
    return _clean_list(queries, 6)