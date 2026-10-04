"""Grounded chat over a stored research run.

The answer is always assembled from sections the run actually captured, and
every claim carries the ``[S#]`` marker of the page it came from. When the
retrieved evidence does not support an answer, the engine says so instead of
padding the reply with model priors.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import List, Optional, Sequence, Tuple

from app.config import settings
from app.research.models import ChatAnswer, Citation, ResearchSection
from app.research.synthesis import (
    _query_terms,
    _variants,
    cited_refs,
    resolve_citations,
)

logger = logging.getLogger(__name__)

# ``client=None`` must mean "answer without a model", not "auto-detect", or
# demo mode would answer every question with a canned product blob.
_UNSET = object()

_CHAT_SYSTEM = (
    "You answer questions about a research run using ONLY the numbered evidence "
    "blocks provided. Cite every claim with its block marker, e.g. [S3]. "
    "If the evidence does not contain the answer, reply exactly: "
    "NO_ANSWER. Never use outside knowledge and never invent numbers."
)


class ResearchChat:
    """Retrieval-augmented chat scoped to one research run."""

    def __init__(self, documents, client=_UNSET) -> None:
        self.documents = documents
        self.llm = _shared_client() if client is _UNSET else client

    # ------------------------------------------------------------------
    def ask(
        self,
        question: str,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        top_k: Optional[int] = None,
    ) -> ChatAnswer:
        question = (question or "").strip()
        if not question:
            return ChatAnswer(message="Ask a question about the captured evidence.", answerable=False)

        run_id, session_id = self._resolve_session(question, run_id, session_id)

        run = self.documents.get_run(run_id) if run_id else None
        if not run:
            return ChatAnswer(
                session_id=session_id or "",
                run_id=run_id or "",
                message=(
                    "No research run is selected. Start a research run first, "
                    "then ask questions about its captured evidence."
                ),
                answerable=False,
            )

        sections, citations = self._retrieve(run_id, question, top_k)
        if not sections:
            return self._not_covered(session_id, run_id, question)

        context = self._build_context(sections, citations)
        history = self._history(session_id, run_id)
        message = self._compose(question, context, history, citations)

        if not message or message.strip().upper().startswith("NO_ANSWER"):
            return self._not_covered(session_id, run_id, question)

        refs = cited_refs(message) or [s.source_ref for s in sections]
        resolved = resolve_citations([r for r in refs if r], citations)

        answer = ChatAnswer(
            session_id=session_id,
            run_id=run_id,
            message=message,
            citations=resolved,
            sources_used=len(sections),
            answerable=True,
            provider=getattr(self.llm, "provider", "") or "demo",
        )
        self._persist(session_id, "user", question)
        self._persist(session_id, "assistant", message, resolved)
        return answer

    def _not_covered(self, session_id: str, run_id: str, question: str) -> ChatAnswer:
        """Persist and return the honest 'evidence does not cover this' reply."""
        answer = ChatAnswer(
            session_id=session_id,
            run_id=run_id,
            message=(
                "The captured evidence does not cover that question. "
                "Try a follow-up run with a wider page budget or a search-enabled plan."
            ),
            citations=[],
            sources_used=0,
            answerable=False,
        )
        self._persist(session_id, "user", question)
        self._persist(session_id, "assistant", answer.message, [])
        return answer

    # ------------------------------------------------------------------
    def _resolve_session(
        self,
        question: str,
        run_id: Optional[str],
        session_id: Optional[str],
    ) -> Tuple[Optional[str], str]:
        if session_id:
            session = self.documents.get_session(session_id)
            if not session:
                # Honour the caller's id: the frontend keeps using it even
                # before the first turn was ever persisted.
                self.documents.create_session(
                    session_id, run_id, title=question[:80]
                )
            elif not run_id:
                run_id = session.get("run_id")
            return run_id, session_id

        if not run_id:
            runs = self.documents.list_runs(limit=1)
            run_id = runs[0]["run_id"] if runs else None
        session_id = uuid.uuid4().hex[:16]
        self.documents.create_session(session_id, run_id, title=question[:80])
        return run_id, session_id

    def _retrieve(
        self,
        run_id: str,
        question: str,
        top_k: Optional[int],
    ) -> Tuple[List[ResearchSection], List[Citation]]:
        k = top_k or settings.chat_top_k
        hits: List[Tuple[ResearchSection, float]] = []
        try:
            hits = self.documents.search_sections(run_id, question, k=max(k * 2, k))
        except Exception:
            logger.debug("section search failed", exc_info=True)
            hits = []
        if not hits:
            hits = self.documents.search_facts_query(run_id, question, k=k)

        ranked = [
            (section, score)
            for section, score in hits
            if score >= settings.chat_min_relevance
        ][:k]

        citations = self.documents.citations_for_run(run_id)
        return [section for section, _ in ranked], citations

    def _build_context(
        self,
        sections: Sequence[ResearchSection],
        citations: Sequence[Citation],
    ) -> str:
        by_ref = {c.ref: c for c in citations}
        blocks: List[str] = []
        used = 0
        for section in sections:
            citation = by_ref.get(section.source_ref)
            if citation is None:
                continue
            heading = section.heading or section.title or citation.title or citation.url
            body = re.sub(r"\s+", " ", section.content).strip()
            block = f"[{section.source_ref}] {heading}\n{body}"
            if used + len(block) > settings.chat_context_chars:
                break
            blocks.append(block)
            used += len(block)
        return "\n\n".join(blocks)

    def _history(self, session_id: Optional[str], run_id: str) -> str:
        if not session_id or settings.chat_history_turns <= 0:
            return ""
        rows = self.documents.messages(session_id, limit=200)
        turns = [
            row
            for row in rows
            if (row.get("role") in ("user", "assistant")) and row.get("content")
        ][-settings.chat_history_turns:]
        if not turns:
            return ""
        lines = [
            f"{'User' if row['role'] == 'user' else 'Assistant'}: {row['content'][:500]}"
            for row in turns
        ]
        return "CONVERSATION SO FAR:\n" + "\n".join(lines)

    def _compose(
        self,
        question: str,
        context: str,
        history: str,
        citations: Sequence[Citation],
    ) -> str:
        prompt = "\n\n".join(part for part in (history, f"EVIDENCE:\n{context}") if part)
        prompt += f"\n\nQUESTION: {question}"

        if self.llm is not None:
            try:
                answer = (self.llm.complete(_CHAT_SYSTEM, prompt) or "").strip()
                if answer and "NO_ANSWER" not in answer.upper():
                    return answer
                if answer:
                    return self._extractive_answer(question, context, citations)
            except Exception:
                logger.debug("llm chat failed; using extractive answer", exc_info=True)
        return self._extractive_answer(question, context, citations)

    def _extractive_answer(
        self,
        question: str,
        context: str,
        citations: Sequence[Citation],
    ) -> str:
        """Evidence-only answer used when no model is available.

        Answering with unrelated-but-similar pricing text is worse than
        admitting a gap, so the evidence must actually cover the question's
        vocabulary before anything is returned.
        """
        if not _covers(question, context):
            return "NO_ANSWER"

        by_ref = {c.ref: c for c in citations}
        lines: List[str] = []
        for block in context.split("\n\n"):
            match = re.match(r"\[([^\]]+)\]\s*(.*?)\n(.*)", block, re.DOTALL)
            if not match:
                continue
            ref, heading, body = match.group(1), match.group(2).strip(), match.group(3)
            snippet = _first_sentences(body, 2)
            if not snippet:
                continue
            prefix = f"{heading}: " if heading else ""
            lines.append(f"- {prefix}{snippet} [{ref}]")
        if not lines:
            return "NO_ANSWER"
        return "\n".join(lines)

    def _persist(
        self,
        session_id: Optional[str],
        role: str,
        content: str,
        citations: Optional[List[dict]] = None,
    ) -> None:
        if not session_id:
            return
        try:
            self.documents.add_message(session_id, role, content, citations or [])
        except Exception:
            logger.debug("could not persist chat message", exc_info=True)


def _first_sentences(text: str, count: int) -> str:
    parts = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text or "").strip())
    return " ".join(parts[:count]).strip()[:400]


def _covers(question: str, context: str, threshold: Optional[float] = None) -> bool:
    """Does the evidence actually speak to this question?"""
    terms = _query_terms(question)
    if not terms:
        return bool(context.strip())
    limit = settings.chat_min_coverage if threshold is None else threshold
    haystack = re.sub(r"\s+", " ", context or "").lower()
    hits = sum(
        1 for term in terms if any(form in haystack for form in _variants(term))
    )
    return hits / len(terms) >= limit


def _shared_client():
    """The pipeline's provider chain, or ``None`` in offline/demo mode."""
    try:
        from app.llm.client import LLMClient

        return LLMClient()
    except Exception:
        logger.debug("no llm client available for chat", exc_info=True)
        return None