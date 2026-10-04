"""Synthesis: turn evidence into a cited report and honest gaps.

Two writers:

* ``LLMSynthesizer`` produces prose per sub-question using only the facts and
  passages that were actually captured, and must cite them as ``[S1]``.
* ``DeterministicSynthesizer`` writes the same structure from the evidence
  without a model, so a run without provider keys still produces a real,
  sourced dossier instead of a vague summary.

Both produce a ``Synthesis`` with findings, gaps and a markdown report. A gap
is recorded whenever a planned sub-question has no supporting evidence, which
is what keeps the output honest about coverage.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Dict, List, Optional, Sequence

from app.research.models import (
    Citation,
    DeepResearchResult,
    Fact,
    ResearchGap,
    ResearchPlan,
    ResearchSection,
)
from app.storage.documents import tokenize

logger = logging.getLogger(__name__)

_SYNTH_SYSTEM = (
    "You write evidence-backed competitive-intelligence briefs. Use ONLY the "
    "evidence given. Cite every claim with its source marker, e.g. [S2]. "
    "If the evidence does not answer part of the question, say so plainly "
    "instead of guessing. Plain prose or short markdown bullets, no preamble."
)

_CITATION_RE = re.compile(r"\[(S\d+)\]")


class Synthesis:
    def __init__(
        self,
        findings: List[str],
        gaps: List[ResearchGap],
        report_markdown: str,
        cited: bool,
    ):
        self.findings = findings
        self.gaps = gaps
        self.report_markdown = report_markdown
        self.cited = cited


def synthesize(
    result: DeepResearchResult,
    client=None,
) -> Synthesis:
    plan = result.plan
    questions = plan.questions(8)
    facts_by_q: Dict[str, List[Fact]] = defaultdict(list)
    unattributed: List[Fact] = []
    for fact in result.facts:
        if fact.question:
            facts_by_q[fact.question].append(fact)
        else:
            unattributed.append(fact)

    gaps: List[ResearchGap] = []
    evidence_blocks: List[tuple[str, List[Fact], List[ResearchSection]]] = []

    for question in questions:
        facts = _match_facts(question, facts_by_q.get(question, []), result.facts)
        sections = _passages_for(question, result.sections)
        if not facts and not sections:
            gaps.append(
                ResearchGap(
                    question=question,
                    reason="no retrieved evidence in this run covered this question",
                )
            )
            continue
        evidence_blocks.append((question, facts, sections))

    llm_usable = bool(
        client is not None
        and getattr(client, "provider_name", "demo") != "demo"
        and evidence_blocks
    )
    if llm_usable:
        try:
            return _llm_synthesis(result, evidence_blocks, gaps, client)
        except Exception as exc:
            logger.warning("LLM synthesis failed (%s) — deterministic report", exc)

    return deterministic_synthesis(result, evidence_blocks, gaps)


# ----------------------------------------------------------------------
def _variants(term: str) -> set:
    """Simple singular/plural variants so stems match real page wording.

    Generated questions ("What are the pricing plans?") otherwise fail to
    match a page that says "Standard plan costs ...".
    """
    forms = {term}
    if term.endswith("ies") and len(term) > 4:
        forms.add(term[:-3] + "y")
    if term.endswith("ses") and len(term) > 4:
        forms.add(term[:-2])
    if term.endswith("s") and not term.endswith("ss"):
        forms.add(term[:-1])
    else:
        forms.add(term + "s")
    return forms


def _query_terms(question: str) -> set:
    return set(tokenize(question))


def _hits(terms: set, haystack: str) -> int:
    return sum(
        1
        for term in terms
        if any(form in haystack for form in _variants(term))
    )


def _match_facts(question: str, facts: List[Fact], all_facts: List[Fact]) -> List[Fact]:
    if facts:
        return facts
    terms = _query_terms(question)
    if not terms:
        return []
    scored = []
    for fact in all_facts:
        haystack = f"{fact.statement} {fact.question} {fact.value or ''}".lower()
        hits = _hits(terms, haystack)
        if hits >= max(2, len(terms) // 3):
            scored.append((hits, fact))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [fact for _hits, fact in scored[:5]]


def _passages_for(question: str, sections: List[ResearchSection], limit: int = 4) -> List[ResearchSection]:
    from app.storage.documents import lexical_score

    terms = _query_terms(question)
    if not terms:
        return []
    scored = []
    for section in sections:
        haystack = f"{section.heading} {section.title} {section.content}".lower()
        hits = _hits(terms, haystack)
        if hits:
            scored.append((hits + lexical_score(question, haystack), section))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [section for _score, section in scored[:limit]]


# ----------------------------------------------------------------------
def _llm_synthesis(
    result: DeepResearchResult,
    evidence_blocks: List[tuple[str, List[Fact], List[ResearchSection]]],
    gaps: List[ResearchGap],
    client,
) -> Synthesis:
    findings: List[str] = []
    blocks: List[str] = []
    rendered_facts: set = set()

    for question, facts, sections in evidence_blocks:
        lines = [f"QUESTION: {question}", "EVIDENCE:"]
        for fact in facts[:8]:
            value = f" | value: {fact.value}" if fact.value else ""
            lines.append(
                f"- [{fact.source_ref}] {fact.statement}{value} "
                f"(confidence {fact.confidence:.2f})"
            )
            rendered_facts.add(fact.statement[:100])
        for section in sections[:4]:
            snippet = section.content.strip().replace("\n", " ")[:600]
            heading = f"{section.heading} — " if section.heading else ""
            lines.append(f"- [{section.source_ref}] {heading}{snippet}")
        answer = client.complete(_SYNTH_SYSTEM, "\n".join(lines)).strip()
        if not answer:
            continue
        answer = _strip_citations_kept(answer)
        findings.append(f"{question} {answer}".strip())
        blocks.append(f"### {question}\n\n{answer}")

    findings.extend(_deterministic_findings(result.facts, exclude=rendered_facts))
    report = _assemble_report(result, blocks, gaps)
    cited = any(_CITATION_RE.search(f) for f in findings)
    return Synthesis(findings=findings[:24], gaps=gaps, report_markdown=report, cited=cited)


def deterministic_synthesis(
    result: DeepResearchResult,
    evidence_blocks: Optional[List[tuple[str, List[Fact], List[ResearchSection]]]] = None,
    gaps: Optional[List[ResearchGap]] = None,
) -> Synthesis:
    """Evidence-only report. Every line carries its source marker."""
    if evidence_blocks is None or gaps is None:
        plan = result.plan
        gaps = []
        evidence_blocks = []
        for question in plan.questions(8):
            facts = _match_facts(question, [], result.facts)
            sections = _passages_for(question, result.sections)
            if not facts and not sections:
                gaps.append(
                    ResearchGap(
                        question=question,
                        reason="no retrieved evidence in this run covered this question",
                    )
                )
                continue
            evidence_blocks.append((question, facts, sections))

    findings: List[str] = []
    blocks: List[str] = []
    rendered_facts: set = set()
    rendered_phrases: List[set] = []

    for question, facts, sections in evidence_blocks:
        bullets: List[str] = []
        seen: set = set()
        for fact in facts[:6]:
            key = fact.statement[:100]
            if key in seen:
                continue
            seen.add(key)
            value = f" ({fact.value})" if fact.value else ""
            bullets.append(f"- {fact.statement}{value} [{fact.source_ref}]")
            rendered_facts.add(key)
            rendered_phrases.append(_tokens(fact.statement))
        for section in sections[:3]:
            snippet = _first_sentences(section.content, 2)
            if not snippet:
                continue
            # A passage that merely restates a fact already listed above adds
            # length, not evidence.
            snippet_tokens = _tokens(snippet)
            if any(_overlaps(snippet_tokens, phrase) for phrase in rendered_phrases):
                continue
            heading = f"{section.heading}: " if section.heading else ""
            bullets.append(f"- {heading}{snippet} [{section.source_ref}]")
            rendered_phrases.append(snippet_tokens)
        if not bullets:
            gaps.append(
                ResearchGap(
                    question=question,
                    reason="evidence for this question did not contain a usable value",
                )
            )
            continue
        block = "\n".join(bullets)
        blocks.append(f"### {question}\n\n{block}")
        findings.extend(f"{question} {b.lstrip('- ')}" for b in bullets)

    extra = _deterministic_findings(result.facts, exclude=rendered_facts)
    if extra:
        # Facts captured by the extractor must not vanish just because no
        # sub-question matched them lexically.
        blocks.append("### Other captured evidence\n\n" + "\n".join(f"- {line}" for line in extra))
    findings.extend(extra)

    report = _assemble_report(result, blocks, gaps)
    return Synthesis(findings=findings[:24], gaps=gaps, report_markdown=report, cited=True)


def _deterministic_findings(
    facts: Sequence[Fact],
    exclude: Optional[set] = None,
) -> List[str]:
    """Top evidence lines that are not already tied to a sub-question."""
    out: List[str] = []
    exclude = exclude or set()
    for fact in facts:
        if fact.question:
            continue
        if fact.statement[:100] in exclude:
            continue
        value = f" ({fact.value})" if fact.value else ""
        out.append(f"{fact.statement}{value} [{fact.source_ref}]")
        if len(out) >= 8:
            break
    return out


def _tokens(text: str) -> set:
    return {
        word
        for word in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(word) > 3
    }


def _overlaps(first: set, second: set, threshold: float = 0.6) -> bool:
    """True when two phrases mostly repeat the same content words."""
    if not first or not second:
        return False
    shared = len(first & second)
    if not shared:
        return False
    return shared / min(len(first), len(second)) >= threshold


def _assemble_report(
    result: DeepResearchResult,
    blocks: List[str],
    gaps: List[ResearchGap],
) -> str:
    coverage = result.coverage
    lines: List[str] = []
    lines.append(f"# Research report — {result.plan.objective or result.question or 'untitled query'}")
    lines.append("")
    if result.plan.subject:
        lines.append(f"**Subject:** {result.plan.subject}  ")
    lines.append(f"**Mode:** {result.mode.value}  ")
    lines.append(
        f"**Evidence captured:** {coverage.pages_fetched} pages / "
        f"{coverage.sections} sections / {coverage.facts} facts / "
        f"{coverage.characters:,} characters  "
    )
    if coverage.search_backend and coverage.search_backend != "none":
        lines.append(f"**Search backend:** {coverage.search_backend}  ")
    if coverage.provider:
        lines.append(f"**Model:** {coverage.provider}  ")
    if not coverage.llm_enabled:
        lines.append("**Planner/model:** disabled — this report is built directly from captured evidence.")
    lines.append("")

    if blocks:
        lines.append("## What the evidence shows")
        lines.append("")
        for block in blocks:
            lines.append(block)
            lines.append("")

    if gaps:
        lines.append("## Open questions (no supporting evidence captured)")
        lines.append("")
        lines.extend(f"- {gap.question} — {gap.reason}" for gap in gaps)
        lines.append("")

    if result.citations:
        lines.append("## Sources")
        lines.append("")
        for citation in result.citations:
            title = f" — {citation.title}" if citation.title else ""
            lines.append(f"- [{citation.ref}] {citation.url}{title}")
        lines.append("")

    if result.pages:
        lines.append("## Pages visited")
        lines.append("")
        for page in result.pages:
            status = page.get("status", "")
            chars = page.get("chars", 0)
            ref = page.get("source_ref", "")
            lines.append(
                f"- [{ref}] {page.get('url')} ({page.get('page_type')}, {status}, {chars:,} chars)"
            )
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def cited_refs(text: str) -> List[str]:
    """Distinct ``[S#]`` markers used in a body of text, in order."""
    seen: List[str] = []
    for ref in _CITATION_RE.findall(text or ""):
        if ref not in seen:
            seen.append(ref)
    return seen


def resolve_citations(refs: Sequence[str], citations: Sequence[Citation]) -> List[dict]:
    index = {c.ref: c for c in citations}
    resolved = []
    for ref in refs:
        citation = index.get(ref)
        if citation is None:
            continue
        resolved.append(
            {
                "ref": citation.ref,
                "url": citation.url,
                "title": citation.title,
                "heading": citation.heading,
            }
        )
    return resolved


def _first_sentences(text: str, count: int) -> str:
    parts = re.split(r"(?<=[.!?])\s+", " ".join((text or "").split()))
    return " ".join(parts[:count]).strip()[:400]


def _strip_citations_kept(text: str) -> str:
    """Normalise citation markers without removing them."""
    cleaned = re.sub(r"\[\s*(S\d+)\s*\]", r"[\1]", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()