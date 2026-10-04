"""Deep research pipeline.

    plan -> (search) -> crawl -> classify -> extract -> synthesise -> persist

Design rules that matter for reliability:

* **Incremental persistence.** Pages, sections and facts are written as they
  are produced, so a crashed run keeps everything it already proved and a
  re-run with the same ``run_id`` resumes instead of starting over.
* **Evidence before prose.** The report is written last, from stored
  sections, so no claim can exist without a page behind it.
* **No provider key is still a real run.** Planning, crawling, sectioning,
  fact extraction and the report all have deterministic paths.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional, Sequence
from urllib.parse import urlparse

from app.config import settings
from app.research.crawler import CrawlBudget, Crawler, classify_by_url
from app.research.extract import FactExtractor, ReviewExtractor, build_sections, classify_page
from app.research.fetching import Fetcher
from app.research.models import (
    Citation,
    DeepResearchResult,
    PageDocument,
    PageType,
    ResearchCoverage,
    ResearchMode,
    ResearchPlan,
    ResearchRequest,
    ResearchStatus,
)
from app.research.planner import build_plan
from app.research.sources import SearchBackend, build_search
from app.research.synthesis import synthesize
from app.storage.db import OfferStore
from app.storage.documents import DocumentStore
from app.storage.vector import ReviewStore

logger = logging.getLogger(__name__)


class DeepResearchPipeline:
    def __init__(
        self,
        documents: Optional[DocumentStore] = None,
        reviews: Optional[ReviewStore] = None,
        llm=None,
        progress_callback: Optional[Callable[[dict], None]] = None,
        store: Optional[OfferStore] = None,
    ):
        self.documents = documents or DocumentStore()
        self.reviews = reviews or ReviewStore()
        self.llm = llm
        self.store = store
        self.progress_callback = progress_callback
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    @property
    def _provider(self) -> str:
        return getattr(self.llm, "provider_name", "demo") if self.llm else "demo"

    def _progress(self, event: str, **data) -> None:
        if not self.progress_callback:
            return
        payload = {
            "event": event,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **data,
        }
        try:
            self.progress_callback(payload)
        except Exception:
            logger.debug("progress callback failed", exc_info=True)

    # ------------------------------------------------------------------
    def run(
        self,
        request: ResearchRequest,
        run_id: Optional[str] = None,
    ) -> DeepResearchResult:
        run_id = run_id or uuid.uuid4().hex[:12]
        started = time.monotonic()
        errors: List[str] = []

        if request.mode == ResearchMode.url and not request.url:
            request.mode = ResearchMode.topic

        plan = build_plan(
            request,
            llm=self.llm if request.use_llm else None,
            max_pages=request.max_pages,
            max_depth=request.max_depth,
        )
        title = plan.subject or request.question or (request.url or "Research run")

        self.documents.create_run(
            run_id,
            request.mode.value,
            plan.objective,
            request.url,
            title,
        )
        self.documents.update_run(run_id, plan=plan)

        result = DeepResearchResult(
            run_id=run_id,
            status=ResearchStatus.running,
            mode=request.mode,
            request=request,
            question=plan.objective,
            plan=plan,
        )

        self._progress(
            "run_started",
            run_id=run_id,
            mode=request.mode.value,
            subject=plan.subject,
            planner=plan.planner,
        )

        fetcher = Fetcher()
        search = self._search_backend(request)
        source_counter = {"n": 0}

        def next_ref() -> str:
            with self._lock:
                source_counter["n"] += 1
                return f"S{source_counter['n']}"

        try:
            seed_urls = list(plan.seed_urls)
            if request.mode == ResearchMode.compare:
                for competitor in request.competitors[:5]:
                    if competitor:
                        seed_urls.append(f"https://{competitor.replace(' ', '').lower()}")

            if request.use_search and search.available and (
                request.mode != ResearchMode.url or not seed_urls
            ):
                seed_urls.extend(self._search_seeds(search, plan))

            external_seeds = {
                urlparse(url).netloc.lower()
                for url in seed_urls
                if urlparse(url).netloc
            }
            budget = CrawlBudget.from_plan(
                plan,
                {
                    "max_pages": request.max_pages,
                    "max_depth": request.max_depth,
                    "follow_external": request.follow_external,
                },
            )
            crawler = Crawler(
                fetcher=fetcher,
                budget=budget,
                approved_hosts=external_seeds,
            )

            coverage_pages = 0
            coverage_failed = 0
            total_chars = 0
            ref_by_url: Dict[str, str] = {}

            def handle_page(document: PageDocument) -> None:
                nonlocal coverage_pages, coverage_failed, total_chars
                ref = ref_by_url.get(document.url) or next_ref()
                ref_by_url[document.url] = ref
                document.source_ref = ref
                document.page_type = classify_page(document)
                page_id = self.documents.save_page(run_id, document)

                sections = build_sections(document, run_id, page_id, ref)
                self.documents.save_sections(run_id, sections)
                result.sections.extend(sections)
                total_chars += len(document.text)

                facts = self._extract_facts(document, plan, page_id, ref)
                self.documents.save_facts(run_id, facts)
                result.facts.extend(facts)

                if request.extract_reviews:
                    self._store_reviews(document)

                coverage_pages += 1
                self._progress(
                    "page_processed",
                    run_id=run_id,
                    url=document.url,
                    ref=ref,
                    page_type=document.page_type.value,
                    sections=len(sections),
                    facts=len(facts),
                    relevance=document.relevance,
                    pages=coverage_pages,
                )

            self._progress(
                "crawl_started",
                run_id=run_id,
                seeds=len(seed_urls),
                max_pages=budget.max_pages,
                max_depth=budget.max_depth,
            )

            outcome = crawler.crawl(seed_urls, plan, on_page=handle_page)
            coverage_failed = len(outcome.failed)
            errors.extend(outcome.errors)
            for document in outcome.pages:
                if document.usable:
                    continue  # already persisted by handle_page
                if not document.source_ref:
                    # Failed/skipped pages never reach handle_page, but the
                    # report still needs a citable reference for them.
                    document.source_ref = next_ref()
                self.documents.save_page(run_id, document)

            result.coverage = ResearchCoverage(
                pages_discovered=outcome.frontier_seen,
                pages_fetched=coverage_pages,
                pages_failed=coverage_failed,
                pages_skipped=len(
                    [p for p in outcome.pages if p.status == "skipped"]
                ),
                external_sources=outcome.external_used,
                search_queries=len(plan.search_queries),
                sections=len(result.sections),
                facts=len(result.facts),
                characters=total_chars,
                search_backend=search.name if search.available else "none",
                budget_exhausted=outcome.budget_exhausted,
                llm_enabled=self._provider != "demo",
                provider=self._provider,
            )

            result.citations = self.documents.citations_for_run(run_id)
            result.pages = self.documents.pages_for_run(run_id)

            synthesis = synthesize(result, client=self.llm)
            result.findings = synthesis.findings
            result.gaps = synthesis.gaps
            result.report_markdown = synthesis.report_markdown
            # Re-read sections from storage so citations reflect what chat will
            # actually retrieve.
            result.sections = self.documents.sections_for_run(run_id)
            result.citations = self.documents.citations_for_run(run_id)

            status = ResearchStatus.completed
            if coverage_pages == 0:
                status = ResearchStatus.failed
                errors.append("no pages could be fetched for this run")
            elif self._is_incomplete(coverage_pages, coverage_failed, outcome.budget_exhausted):
                status = ResearchStatus.partial

            result.status = status
            result.errors = errors
            result.duration_s = round(time.monotonic() - started, 2)

            self.documents.update_run(
                run_id,
                status=status.value,
                coverage=result.coverage.model_dump(),
                report_markdown=result.report_markdown,
                error="; ".join(errors[:5]) if errors else None,
            )
            self._progress(
                "run_completed",
                run_id=run_id,
                status=status.value,
                pages=result.coverage.pages_fetched,
                sections=result.coverage.sections,
                facts=result.coverage.facts,
                duration_s=result.duration_s,
            )
            return result

        except Exception as exc:
            logger.exception("deep research run %s failed", run_id)
            result.status = ResearchStatus.failed
            result.errors = errors + [str(exc)]
            result.duration_s = round(time.monotonic() - started, 2)
            self.documents.update_run(
                run_id,
                status=ResearchStatus.failed.value,
                coverage=result.coverage.model_dump(),
                error=str(exc)[:500],
            )
            self._progress("run_failed", run_id=run_id, error=str(exc)[:300])
            return result
        finally:
            fetcher.close()

    # ------------------------------------------------------------------
    @staticmethod
    def _is_incomplete(
        pages_fetched: int,
        pages_failed: int,
        budget_exhausted: bool,
    ) -> bool:
        """Decide whether a run deserves the ``partial`` label.

        A single 404 is normal crawling noise and stays ``completed``; the
        label is reserved for runs that visibly missed evidence.
        """
        if pages_fetched <= 0:
            return True
        if pages_fetched >= 3 and pages_failed >= max(2, pages_fetched // 2):
            return True
        # An exhausted budget means the frontier was still full of candidates.
        return bool(budget_exhausted) and pages_fetched <= 1

    # ------------------------------------------------------------------
    def _search_backend(self, request: ResearchRequest) -> SearchBackend:
        if not (request.use_search and settings.deep_search_enabled):
            from app.research.sources import NullSearch

            return NullSearch()
        return build_search(True)

    def _search_seeds(
        self,
        search: SearchBackend,
        plan: ResearchPlan,
        limit: int = 6,
    ) -> List[str]:
        urls: List[str] = []
        for query in plan.search_queries[:3]:
            try:
                hits = search.search(query, limit=limit)
            except Exception:
                logger.debug("search failed for %r", query, exc_info=True)
                continue
            for hit in hits:
                if hit.url and hit.url not in urls:
                    urls.append(hit.url)
            if len(urls) >= limit:
                break
        return urls[:limit]

    def _extract_facts(
        self,
        document: PageDocument,
        plan: ResearchPlan,
        page_id: Optional[int],
        source_ref: str,
    ):
        extractor = FactExtractor(
            client=self.llm,
            enabled=settings.deep_llm_enabled and document.page_type != PageType.legal,
        )
        return extractor.extract(document, plan, page_id, source_ref)

    def _store_reviews(self, document: PageDocument) -> None:
        extractor = ReviewExtractor(client=self.llm, enabled=settings.deep_llm_enabled)
        reviews = extractor.extract(document)
        if not reviews:
            return
        for review in reviews:
            try:
                self.reviews.add(review)
            except Exception:
                logger.debug("could not store review", exc_info=True)

    # ------------------------------------------------------------------
    def load(self, run_id: str) -> Optional[DeepResearchResult]:
        """Rebuild a stored run so the API and chat share one shape."""
        run = self.documents.get_run(run_id)
        if not run:
            return None
        plan_data = run.get("plan") or {}
        try:
            plan = ResearchPlan.model_validate(plan_data)
        except Exception:
            logger.debug("stored plan for %s is unusable", run_id, exc_info=True)
            plan = ResearchPlan()
        coverage_data = run.get("coverage") or {}
        try:
            coverage = ResearchCoverage.model_validate(coverage_data)
        except Exception:
            coverage = ResearchCoverage()

        request = ResearchRequest(
            mode=_safe_mode(run.get("mode")),
            url=run.get("target_url"),
            question=run.get("question") or "",
        )
        try:
            status = ResearchStatus(run.get("status") or "completed")
        except ValueError:
            status = ResearchStatus.completed

        return DeepResearchResult(
            run_id=run_id,
            status=status,
            mode=request.mode,
            request=request,
            question=run.get("question") or "",
            plan=plan,
            sections=self.documents.sections_for_run(run_id),
            facts=self.documents.facts_for_run(run_id),
            citations=self.documents.citations_for_run(run_id),
            report_markdown=run.get("report_markdown") or "",
            coverage=coverage,
            pages=self.documents.pages_for_run(run_id),
            errors=[run["error"]] if run.get("error") else [],
        )


def _safe_mode(value) -> ResearchMode:
    try:
        return ResearchMode(str(value or "url"))
    except ValueError:
        return ResearchMode.url