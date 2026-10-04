"""Content understanding for research pages.

Three jobs:

1. ``split_sections`` turns a page into retrievable units. The chat layer must
   be able to say "the answer is in the Pricing > Enterprise row of page S4",
   so units are heading-anchored rather than arbitrary fixed windows.
2. ``classify_page`` upgrades the crawler's URL-only guess with body signals.
3. ``FactExtractor`` pulls claims that answer the plan's sub-questions, each
   carrying its source page, a verbatim quote and a confidence.

Every LLM path has a deterministic fallback built on the page text, so a run
still produces structured facts with no provider key at all.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from app.config import settings
from app.research.models import Fact, PageDocument, PageType, ResearchPlan, ResearchSection

logger = logging.getLogger(__name__)

_FACT_SYSTEM = (
    "You extract competitive-intelligence facts from a web page. Reply with JSON only:\n"
    '{"facts": [{"question": str, "statement": str, "value": str|null, '
    '"confidence": 0.0-1.0, "quote": str}]}.\n'
    "Rules:\n"
    "1. Only state facts the page supports; every fact must have a short verbatim "
    "quote copied from the page.\n"
    "2. Answer the given questions when the page supports an answer; skip the rest.\n"
    "3. Prefer concrete values: prices, limits, dates, counts, percentages, names.\n"
    "4. If the page supports nothing relevant, return {\"facts\": []}.\n"
    "5. No markdown fences, no commentary."
)

_REVIEW_SYSTEM = (
    "Extract customer reviews from this page. Reply with JSON only:\n"
    '{"reviews": [{"review_text": str, "rating": number|null, "reviewer": str|null}]}.\n'
    "Only include reviews actually written on the page. Never invent text or ratings. "
    "If there are none, return {\"reviews\": []}."
)

# A price is only matched when its unit is actually present: "per" without a
# noun used to produce dangling values such as "$19 per".
_PRICE_RE = re.compile(
    r"(?:[$£€₹¥]|\b(?:USD|EUR|GBP|INR|JPY|CAD|AUD|SGD|BRL|Rs\.?)\s?)\s?\d[\d,]*(?:\.\d+)?"
    r"(?:\s?(?:(?:/|per|a)\s?(?:month|mo|year|yr|seat|user)s?\b|monthly\b|annually\b|yearly\b)|/(?=\d))?"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|INR|JPY|dollars|euros|pounds|rupees)\b",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\b\d[\d,]*(?:\.\d+)?\s?(?:%|percent|x|ms|gb|tb|mb|hz|hours?|days?|users?|seats?|countries|integrations?)\b", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")

_COMMERCIAL_SIGNALS = ("buy now", "add to cart", "in stock", "out of stock", "free trial", "get started", "subscribe")
_REVIEW_SIGNALS = ("review", "rating", "stars", "testimonial", "customers say", "verified buyer")
_DOCS_SIGNALS = ("installation", "getting started", "api reference", "documentation", "endpoint", "sdk", "how to")
_LEGAL_SIGNALS = ("privacy policy", "terms of service", "cookie", "gdpr", "refund policy")


# ----------------------------------------------------------------------
# sections
# ----------------------------------------------------------------------
def split_sections(
    text: str,
    *,
    chunk_chars: Optional[int] = None,
    overlap: Optional[int] = None,
    min_chars: Optional[int] = None,
) -> List[Tuple[str, str]]:
    """Split page text into (heading, content) units.

    Markdown headings are used when the page has them; otherwise blocks of
    paragraphs are grouped so every unit stays above ``min_chars``.
    """
    chunk_chars = chunk_chars or settings.deep_chunk_chars
    overlap = settings.deep_chunk_overlap if overlap is None else overlap
    min_chars = settings.deep_section_min_chars if min_chars is None else min_chars

    body = (text or "").strip()
    if not body:
        return []

    units: List[Tuple[str, str]] = []
    if re.search(r"^#{1,6}\s", body, re.MULTILINE):
        current_heading = ""
        buffer: List[str] = []
        for block in re.split(r"\n{2,}", body):
            if re.match(r"^#{1,6}\s", block.strip()):
                if buffer and "".join(buffer).strip():
                    units.append((current_heading, "\n\n".join(buffer).strip()))
                current_heading = re.sub(r"^#{1,6}\s*", "", block.strip()).strip()
                buffer = []
                continue
            buffer.append(block.strip())
        if buffer and "".join(buffer).strip():
            units.append((current_heading, "\n\n".join(buffer).strip()))
    else:
        paragraphs = [p.strip() for p in re.split(r"\n{2,}", body) if p.strip()]
        heading = ""
        buffer: List[str] = []
        for paragraph in paragraphs:
            # A short standalone line reads as a heading.
            if len(paragraph) <= 90 and "\n" not in paragraph and not _PRICE_RE.search(paragraph):
                if buffer and "".join(buffer).strip():
                    units.append((heading, "\n\n".join(buffer).strip()))
                heading = paragraph
                buffer = []
                continue
            buffer.append(paragraph)
        if buffer and "".join(buffer).strip():
            units.append((heading, "\n\n".join(buffer).strip()))

    return _merge_and_chunk(units, chunk_chars, overlap, min_chars)


def _merge_and_chunk(
    units: List[Tuple[str, str]],
    chunk_chars: int,
    overlap: int,
    min_chars: int,
) -> List[Tuple[str, str]]:
    merged: List[Tuple[str, str]] = []
    for heading, content in units:
        content = content.strip()
        if not content:
            continue
        if len(content) < min_chars and merged:
            prev_heading, prev_content = merged[-1]
            # Keep the headings of merged units: the citation trail stays
            # navigable instead of collapsing into the first heading only.
            trail = _merge_headings(prev_heading, heading)
            merged[-1] = (trail, f"{prev_content}\n\n{content}".strip())
            continue
        if len(content) <= chunk_chars:
            merged.append((heading, content))
            continue
        # Long unit: split on sentence boundaries with a little overlap.
        sentences = [s for s in _SENTENCE_RE.split(content) if s.strip()]
        piece: List[str] = []
        size = 0
        part = 0
        for sentence in sentences:
            piece.append(sentence.strip())
            size += len(sentence)
            if size >= chunk_chars:
                merged.append((heading if part == 0 else f"{heading} (cont. {part})", " ".join(piece)))
                tail = piece[-1][-overlap:] if overlap else ""
                piece = [tail] if tail else []
                size = len(tail)
                part += 1
        if piece and "".join(piece).strip():
            merged.append((heading if part == 0 else f"{heading} (cont. {part})", " ".join(piece).strip()))
    return [(h, c) for h, c in merged if len(c.strip()) >= 40]


def _merge_headings(first: str, second: str, limit: int = 3) -> str:
    """Combine heading trails of merged sections (``A · B · C``)."""
    parts: List[str] = []
    for heading in (first, second):
        for piece in re.split(r"\s+·\s+|\s+/\s+", heading or ""):
            piece = piece.strip()
            if piece and piece not in parts:
                parts.append(piece)
    if not parts:
        return second or first
    if len(parts) > limit:
        return " · ".join(parts[:limit])
    return " · ".join(parts)


def build_sections(
    document: PageDocument,
    run_id: str,
    page_id: Optional[int],
    source_ref: str,
) -> List[ResearchSection]:
    units = split_sections(document.markdown or document.text)
    sections: List[ResearchSection] = []
    for index, (heading, content) in enumerate(units):
        sections.append(
            ResearchSection(
                run_id=run_id,
                page_id=page_id,
                source_ref=source_ref,
                url=document.final_url or document.url,
                title=document.title,
                heading=heading[:200],
                content=content,
                index=index,
                word_count=len(content.split()),
                page_type=document.page_type,
            )
        )
    return sections


# ----------------------------------------------------------------------
# classification
# ----------------------------------------------------------------------
def classify_page(document: PageDocument) -> PageType:
    """Refine the crawler's URL guess with body signals."""
    if document.page_type in (PageType.pricing, PageType.review, PageType.docs, PageType.comparison):
        return document.page_type
    head = f"{document.title} {document.description} {document.text[:1500]}".lower()
    prices = len(_PRICE_RE.findall(head))
    review_signals = sum(1 for signal in _REVIEW_SIGNALS if signal in head)
    docs_signals = sum(1 for signal in _DOCS_SIGNALS if signal in head)
    legal_signals = sum(1 for signal in _LEGAL_SIGNALS if signal in head)
    commercial = sum(1 for signal in _COMMERCIAL_SIGNALS if signal in head)

    scores = {
        PageType.pricing: prices * 2 + (2 if "pricing" in head else 0),
        PageType.product: commercial * 2 + (1 if "product" in head else 0),
        PageType.review: review_signals * 2 + head.count("review"),
        PageType.docs: docs_signals * 2,
        PageType.legal: legal_signals * 3,
        PageType.comparison: (2 if "compare" in head or " vs " in head else 0)
        + (2 if "alternative" in head else 0),
        PageType.company: (2 if "about us" in head or "our mission" in head else 0),
    }
    best = max(scores.items(), key=lambda item: item[1])
    if best[1] <= 0:
        return document.page_type
    if document.page_type != PageType.other and scores[document.page_type] >= best[1]:
        return document.page_type
    return best[0]


# ----------------------------------------------------------------------
# facts
# ----------------------------------------------------------------------
class FactExtractor:
    """Extracts plan-relevant facts, with or without an LLM."""

    def __init__(self, client=None, enabled: bool = True, max_content_chars: int = 9000):
        self.client = client
        self.enabled = enabled
        self.max_content_chars = max_content_chars

    @property
    def llm_usable(self) -> bool:
        return bool(self.enabled and self.client is not None and getattr(self.client, "provider_name", "demo") != "demo")

    def extract(
        self,
        document: PageDocument,
        plan: ResearchPlan,
        page_id: Optional[int],
        source_ref: str,
        questions: Optional[Sequence[str]] = None,
    ) -> List[Fact]:
        text = document.markdown or document.text
        if not text.strip():
            return []
        target_questions = list(questions or plan.questions(6))
        facts: List[Fact] = []

        if self.llm_usable:
            facts = self._llm_facts(document, target_questions, page_id, source_ref, text)
        if not facts:
            facts = self._textual_facts(document, target_questions, page_id, source_ref, text)

        for fact in facts:
            fact.page_id = page_id
            fact.source_ref = source_ref
            fact.url = document.final_url or document.url
            fact.subject = plan.subject
        return facts

    # -- LLM path ----------------------------------------------------
    def _llm_facts(
        self,
        document: PageDocument,
        questions: Sequence[str],
        page_id: Optional[int],
        source_ref: str,
        text: str,
    ) -> List[Fact]:
        user = (
            f"PAGE URL: {document.final_url or document.url}\n"
            f"PAGE TITLE: {document.title}\n\n"
            "QUESTIONS TO ANSWER FROM THIS PAGE:\n"
            + "\n".join(f"- {q}" for q in questions)
            + "\n\nPAGE CONTENT:\n"
            + text[: self.max_content_chars]
        )
        try:
            raw = self.client.complete(_FACT_SYSTEM, user)
            parsed = _json_or_none(raw)
        except Exception as exc:
            logger.info("fact extraction failed for %s: %s", document.url, exc)
            return []
        if not parsed:
            return []
        items = parsed.get("facts")
        if not isinstance(items, list):
            return []

        facts: List[Fact] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            statement = str(item.get("statement") or "").strip()
            if not statement:
                continue
            quote = str(item.get("quote") or "").strip()
            # An unbacked claim is worse than no claim.
            if not quote or not _quote_in_page(quote, text):
                continue
            confidence = item.get("confidence")
            facts.append(
                Fact(
                    page_id=page_id,
                    source_ref=source_ref,
                    url=document.final_url or document.url,
                    subject=document.title,
                    question=str(item.get("question") or "")[:300],
                    statement=statement[:800],
                    value=_stringify(item.get("value")),
                    confidence=_clamp(confidence),
                    kind="fact",
                    quote=quote[:400],
                )
            )
        return facts[:12]

    # -- deterministic path -----------------------------------------
    def _textual_facts(
        self,
        document: PageDocument,
        questions: Sequence[str],
        page_id: Optional[int],
        source_ref: str,
        text: str,
    ) -> List[Fact]:
        """Sentence-level extraction that needs no model.

        Keeps sentences carrying concrete, checkable values (prices, limits,
        percentages) that also match the plan's terms.
        """
        terms = self._query_terms(questions)
        facts: List[Fact] = []
        seen: set = set()

        for sentence in _iter_sentences(text)[:600]:
            candidate = sentence.strip()
            if len(candidate) < 30 or len(candidate) > 400:
                continue
            values = _PRICE_RE.findall(candidate) + _NUMBER_RE.findall(candidate)
            if not values:
                continue
            lowered = candidate.lower()
            if terms and not any(term in lowered for term in terms):
                continue
            key = lowered[:120]
            if key in seen:
                continue
            seen.add(key)
            facts.append(
                Fact(
                    page_id=page_id,
                    source_ref=source_ref,
                    url=document.final_url or document.url,
                    subject=document.title,
                    question="",
                    statement=candidate,
                    value=(values[0] if values else None),
                    confidence=0.55 if len(values) > 1 else 0.45,
                    kind="observed_value",
                    quote=candidate[:300],
                )
            )
            if len(facts) >= 8:
                break
        return facts

    @staticmethod
    def _query_terms(questions: Sequence[str]) -> List[str]:
        from app.storage.documents import tokenize

        terms: List[str] = []
        for question in questions:
            for token in tokenize(question):
                if token not in terms:
                    terms.append(token)
        return [t for t in terms if len(t) > 3][:40]


class ReviewExtractor:
    """Pull customer reviews off research pages for review intelligence."""

    def __init__(self, client=None, enabled: bool = True, max_per_page: int = 8):
        self.client = client
        self.enabled = enabled
        self.max_per_page = max_per_page

    @property
    def llm_usable(self) -> bool:
        return bool(self.enabled and self.client is not None and getattr(self.client, "provider_name", "demo") != "demo")

    def extract(self, document: PageDocument, product_name: Optional[str] = None):
        from app.schemas import Region, Review

        text = document.markdown or document.text
        head = text.lower()
        if not text.strip() or not any(signal in head for signal in _REVIEW_SIGNALS):
            return []

        region = _region_from_text(head)
        source_url = document.final_url or document.url
        product = product_name or _product_name(document)

        if self.llm_usable:
            user = (
                f"PAGE URL: {source_url}\nPAGE TITLE: {document.title}\n\n"
                f"PAGE CONTENT:\n{text[:8000]}"
            )
            try:
                parsed = _json_or_none(self.client.complete(_REVIEW_SYSTEM, user))
            except Exception:
                parsed = None
            if parsed:
                reviews = []
                for item in parsed.get("reviews") or []:
                    if not isinstance(item, dict):
                        continue
                    body = str(item.get("review_text") or "").strip()
                    if not body:
                        continue
                    rating = item.get("rating")
                    try:
                        rating = float(rating) if rating is not None else None
                    except (TypeError, ValueError):
                        rating = None
                    reviews.append(
                        Review(
                            product_name=product[:200],
                            region=region,
                            rating=rating,
                            review_text=body[:1500],
                            reviewer=(str(item.get("reviewer")) or None),
                            source_url=source_url,
                        )
                    )
                    if len(reviews) >= self.max_per_page:
                        break
                if reviews:
                    return reviews
        return []


def _region_from_text(head: str) -> Region:
    from app.schemas import Region

    for token, region in (
        ("united states", Region.US), ("usa", Region.US), ("usd", Region.US),
        ("united kingdom", Region.UK), ("uk", Region.UK), ("gbp", Region.UK),
        ("india", Region.IN), ("inr", Region.IN), ("rupees", Region.IN),
        ("japan", Region.JP), ("jpy", Region.JP),
        ("canada", Region.CA), ("australia", Region.AU),
        ("singapore", Region.SG), ("brazil", Region.BR), ("euro", Region.EU),
    ):
        if token in head:
            return region
    return Region.US


def _product_name(document: PageDocument) -> str:
    title = (document.title or "").split("|")[0].split("-")[0].strip()
    return title[:200] or "Unknown Product"


def _iter_sentences(text: str) -> List[str]:
    """Split page text into clean prose sentences.

    Markdown structure is removed first: heading lines are page structure, not
    claims, and bullet markers must never leak into an extracted statement.
    """
    cleaned = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]+.*$", "", text or "", flags=re.MULTILINE)
    cleaned = re.sub(r"^[ \t]{0,3}[-*+][ \t]+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    flat = re.sub(r"\s*\n\s*", " ", cleaned)
    return [s.strip() for s in _SENTENCE_RE.split(flat) if s.strip()]


def _quote_in_page(quote: str, text: str) -> bool:
    """Verify a quoted snippet really appears in the page text."""
    needle = re.sub(r"\s+", " ", quote).strip().lower()
    if not needle:
        return False
    haystack = re.sub(r"\s+", " ", text).lower()
    if needle in haystack:
        return True
    # Long quotes may be interrupted by markdown bullets; try the tail too.
    parts = [p for p in re.split(r"[^\w%$€£₹¥.,%-]+", needle) if len(p) > 3]
    if len(parts) >= 4:
        return all(part in haystack for part in parts[:8])
    return False


def _json_or_none(raw: str):
    from app.llm.extractor import StructuredExtractor

    try:
        parsed = StructuredExtractor._parse_json(raw)
    except Exception:
        logger.debug("non-JSON LLM output in research extraction", exc_info=True)
        return None
    return parsed if isinstance(parsed, dict) else None


def _clamp(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return round(max(0.0, min(1.0, number)), 2)


def _stringify(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        text = str(value).strip()
        return text[:200] or None
    if isinstance(value, (list, tuple)):
        parts = [_stringify(v) for v in value]
        joined = ", ".join(p for p in parts if p)
        return joined[:200] or None
    if isinstance(value, dict):
        try:
            return json.dumps(value)[:200]
        except (TypeError, ValueError):
            return None
    return str(value)[:200]


def facts_by_question(facts: Iterable[Fact]) -> Dict[str, List[Fact]]:
    grouped: Dict[str, List[Fact]] = {}
    for fact in facts:
        key = (fact.question or "").strip() or "observed values"
        grouped.setdefault(key, []).append(fact)
    return grouped