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
    "You are a general web-research planner. Research may concern any field, "
    "including science, technology, health, public policy, history, culture, "
    "organizations, products, or markets. Given a research objective, produce "
    "a concrete evidence-gathering plan. Reply with JSON only:\n"
    '{"objective": str, "subject": str, "sub_questions": [str], '
    '"search_queries": [str], "keywords": [str], "page_types": [str]}.\n'
    "Rules: sub_questions are specific, balanced, and answerable from web pages "
    "(4-8 of them). Prioritize the user's question. Do not force a product, "
    "pricing, or competitor framing unless the objective asks for it. "
    "search_queries are short keyword queries a search engine would accept. "
    "keywords are lowercase single terms worth matching in page text. "
    "page_types must come from: product, pricing, review, docs, company, "
    "article, legal, listing, comparison, other."
)

# Page types a plan may request, in the order the prompt lists them.
_PAGE_TYPES = {t.value for t in PageType}

_COMMON_PAGE_TYPES = [
    PageType.article,
    PageType.other,
    PageType.docs,
    PageType.company,
    PageType.comparison,
    PageType.review,
    PageType.product,
    PageType.pricing,
    PageType.listing,
    PageType.legal,
]

_GENERAL_QUESTIONS = [
    "What is the central question, claim, or issue being investigated?",
    "What are the main findings or conclusions reported by the sources?",
    "What evidence, data, methods, or reasoning support those findings?",
    "Where do credible sources agree, and where do they disagree?",
    "What limitations, uncertainty, or missing evidence should be considered?",
    "What context, causes, or mechanisms help explain the findings?",
    "What has changed over time, and what is the most recent evidence?",
    "What practical implications or open questions follow from the evidence?",
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
    # The API accepts ``topic`` as an alias for the subject in topic and
    # comparison mode. Preserve it when building deterministic plans too.
    topic = request.topic
    if topic:
        return str(topic).strip()
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
                f"Compare {request.company or getattr(request, 'topic', None) or 'the target'} with "
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
    elif not plan.search_queries and request.mode == ResearchMode.compare:
        plan.search_queries = _clean_list(
            [f"{plan.subject} {competitor} {objective}" for competitor in request.competitors[:5]],
            6,
        )
    elif not plan.search_queries and request.mode == ResearchMode.url:
        host = urlparse(request.url or "").netloc
        plan.search_queries = _clean_list(
            [
                f"{host} {request.question}" if request.question else f"{host} background",
                request.question,
                f"{plan.subject} research and context",
            ],
            6,
        )
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
    if request.mode == ResearchMode.compare or request.competitors:
        questions = [
            "What is the main question and what evidence is available for each subject?",
            "What are the most important similarities and differences?",
            "Which claims are supported by strong evidence, and which remain uncertain?",
            "What limitations or source gaps affect the comparison?",
            "What has changed recently that could affect the comparison?",
        ]
        questions.extend(
            f"How does {competitor} compare with {subject or 'the subject'} on the stated research question?"
            for competitor in request.competitors[:3]
        )
    else:
        questions = list(_GENERAL_QUESTIONS)
    if objective and objective.lower() not in {q.lower() for q in questions}:
        questions.insert(0, objective)
    return questions[:8]


def _deterministic_queries(subject: str, objective: str, keywords: List[str]) -> List[str]:
    base = [w for w in re.findall(r"[A-Za-z0-9+.\-']{2,}", objective)][:8]
    query_terms = [word for word in base if word.lower() not in {
        "what", "which", "where", "when", "does", "do", "did", "the", "and", "for", "from", "with", "about", "should", "could", "would",
    }]
    queries = []
    if subject:
        queries.append(subject)
    if query_terms:
        queries.append(" ".join(query_terms[:6]))
    if keywords:
        queries.append(" ".join(keywords[:5]) + " evidence")
    if subject:
        queries.append(f"{subject} research findings")
    return _clean_list(queries, 6)
