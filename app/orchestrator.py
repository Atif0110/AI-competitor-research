# Corrected duplicate progress-event emissions
"""Pipeline orchestration — ties discovery, region fan-out, geo-aware scraping,
extraction, storage, analysis, metrics and event detection together.

v4 changes:
- run-scoped analysis: every insight method accepts run_id and the store layer
  filters by `WHERE run_id = ?` (SQL, not Python) — a report for run A never
  mixes observations from runs B/C (review #1/#2).
- observation identity: run_id is METADATA on the offer, not part of the key
  (review #3) — see app/storage/db.py `_offer_key`.
- _provider_chain builds the chain from BOTH sides of each fallback and appends
  the final provider — ["groq", "openai"], never ["openai", "openai"] (#4).
- metrics separate llm_calls / provider_attempts / fallbacks (#5) and count
  distinct canonical product IDs per run (#2).
- focus_products matches the URL PATH only (#23) and returns 0 URLs with a
  warning when nothing matches — never silently scrapes everything (#24).
- raw evidence (markdown + content hash) is stored per URL for debug/re-extract
  (review #26).
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlparse

from app.analysis.currency import normalize_price
from app.analysis.insights import InsightsEngine
from app.config import settings
from app.discovery import canonical_url, dedupe_urls, discover_urls_for_entity
from app.llm.client import LLMClient
from app.llm.extractor import StructuredExtractor
from app.schemas import (
    Competitor,
    CompetitorTarget,
    EventAlert,
    PipelineResult,
    ProductOffer,
    Region,
    Review,
    RunMetrics,
)
from app.scrapers.base import ScrapeError
from app.scrapers.demo import DemoScraper, product_urls_for
from app.scrapers.manager import build_scraper
from app.scrapers.orchestrator import ScraperOrchestrator
from app.storage.db import OfferStore
from app.storage.vector import ReviewStore

logger = logging.getLogger(__name__)


@dataclass
class RunStats:
    pages_scraped: int = 0
    urls_failed: int = 0
    extractions_ok: int = 0
    extractions_failed: int = 0
    extraction_attempts: int = 0
    retries_used: int = 0
    errors: List[str] = field(default_factory=list)
    scrapers_used: List[str] = field(default_factory=list)
    discovered: int = 0


def _log_retries(log) -> int:
    """Retries = attempts beyond the first across every scraper in the chain."""
    return sum(
        max(0, attempts - 1)
        for attempts in log.attempts_per_scraper.values()
    )


class Pipeline:
    def __init__(
        self,
        store: Optional[OfferStore] = None,
        review_store: Optional[ReviewStore] = None,
        extractor: Optional[StructuredExtractor] = None,
        scraper=None,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ):
        self.store = store or OfferStore()
        self.review_store = review_store or ReviewStore()
        self.extractor = extractor or StructuredExtractor(LLMClient())
        self.scraper = scraper
        self.progress_callback = progress_callback
        self.insights = InsightsEngine(
            self.store,
            self.review_store,
            self.extractor.client,
        )

    def _progress(self, event: str, **data) -> None:
        if not self.progress_callback:
            return

        payload = {
            "event": event,
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(),
            ),
            **data,
        }

        try:
            self.progress_callback(payload)
        except Exception:
            logger.debug(
                "progress callback failed",
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # CHECKPOINTS
    # ------------------------------------------------------------------

    def _checkpoint(self, run_id: str) -> set:
        p = (
            Path(settings.vector_store_dir)
            / f"checkpoint_{run_id}.json"
        )

        if p.exists():
            try:
                return set(
                    json.loads(
                        p.read_text()
                    )
                )
            except (
                json.JSONDecodeError,
                OSError,
            ):
                logger.warning(
                    "corrupt checkpoint %s — starting fresh",
                    run_id,
                )
                return set()

        return set()

    def _save_checkpoint(
        self,
        run_id: str,
        done: set,
    ) -> None:
        p = (
            Path(settings.vector_store_dir)
            / f"checkpoint_{run_id}.json"
        )

        p.write_text(
            json.dumps(
                sorted(done)
            )
        )

    # ------------------------------------------------------------------
    # DISCOVERY + FOCUS FILTERING
    # ------------------------------------------------------------------

    def _discover(
        self,
        target: CompetitorTarget,
        demo: bool,
    ) -> Tuple[List[str], list]:
        urls: List[str] = []
        errors: list = []

        for ent in target.all_entities():
            if demo:
                urls += product_urls_for(
                    ent.name,
                    target.regions,
                )
            else:
                try:
                    found = discover_urls_for_entity(
                        ent.website
                    )

                    logger.info(
                        "discovered %d URLs for %s",
                        len(found),
                        ent.name,
                    )

                    urls += found

                except Exception as exc:
                    errors.append(
                        f"discovery {ent.name}: {exc}"
                    )

        urls = dedupe_urls(urls)

        filtered = self._focus_filter(
            urls,
            target,
        )

        if filtered is None:
            errors.append(
                "focus filter: no discovered URLs matched "
                "focus_products — run will scrape 0 URLs "
                "(broaden focus_products to proceed)"
            )
            urls = []
        else:
            urls = filtered

        return urls, errors

    @staticmethod
    def _focus_filter(
        urls: List[str],
        target: CompetitorTarget,
    ) -> Optional[List[str]]:
        """Keep URLs whose PATH mentions a focus product.

        Matches hyphenated slug AND whole tokens with word boundaries.
        Never matches raw substrings against the domain.

        Returns None when focus_products match nothing.
        """

        if not target.focus_products:
            return urls

        patterns: List[str] = []

        for product in target.focus_products:
            patterns.append(
                re.escape(
                    product.lower().replace(
                        " ",
                        "-",
                    )
                )
            )

            for token in product.lower().split():
                if len(token) > 2:
                    patterns.append(
                        r"(?<![a-z0-9])"
                        + re.escape(token)
                        + r"(?![a-z0-9])"
                    )

        keep = []

        for url in urls:
            path = urlparse(url).path.lower()

            if any(
                re.search(pattern, path)
                for pattern in patterns
            ):
                keep.append(url)

        if not keep:
            logger.warning(
                "focus_products matched no discovered URLs "
                "— returning 0 URLs"
            )
            return None

        return keep

    # ------------------------------------------------------------------
    # ENTITY + REGION ATTRIBUTION
    # ------------------------------------------------------------------

    @staticmethod
    def _entity_for_url(
        url: str,
        entities: List[Competitor],
    ) -> Optional[str]:
        canon = canonical_url(url)

        for entity in entities:
            base = canonical_url(
                entity.website
            )

            if canon.startswith(base):
                return entity.name

        return None

    def _finalize_offer(
        self,
        offer: ProductOffer,
        competitor: Optional[str],
        run_id: str,
    ) -> ProductOffer:
        offer.competitor = competitor
        offer.run_id = run_id

        if offer.normalized_price_usd is None:
            usd, rate, timestamp = normalize_price(
                offer.price,
                offer.currency.value,
            )

            offer.normalized_price_usd = usd
            offer.exchange_rate = rate
            offer.exchange_rate_timestamp = timestamp

        if not offer.canonical_product_id:
            from app.analysis.product_identity import (
                resolve as _resolve,
            )

            identity = _resolve(
                offer.product_name
            )

            offer.canonical_product_id = (
                identity.canonical_id
            )
            offer.canonical_product_name = (
                identity.canonical_name
            )

        return offer

    # ------------------------------------------------------------------
    # MAIN RUN
    # ------------------------------------------------------------------

    def run(
        self,
        target: CompetitorTarget,
        run_id: Optional[str] = None,
    ) -> PipelineResult:
        start = time.monotonic()

        run_id = (
            run_id
            or uuid.uuid4().hex[:12]
        )

        started_at = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        )

        demo = (
            settings.demo_mode
            or self.extractor.client.provider_name == "demo"
        )

        stats = RunStats()
        entities = target.all_entities()

        # Per-run metric isolation.
        self.extractor.client.reset_telemetry()

        self._progress(
            "run_started",
            run_id=run_id,
            mode="demo" if demo else "live",
            target_company=target.company,
        )

        urls, discovery_errors = self._discover(
            target,
            demo,
        )

        stats.discovered = len(urls)

        self._progress(
            "discovery_complete",
            run_id=run_id,
            urls_discovered=len(urls),
        )

        stats.errors.extend(
            discovery_errors
        )

        done = self._checkpoint(
            run_id
        )

        # --------------------------------------------------------------
        # SCRAPER SELECTION
        # --------------------------------------------------------------

        if self.scraper is not None:
            scraper = self.scraper
        elif demo:
            scraper = ScraperOrchestrator(
                [
                    DemoScraper(
                        [target]
                    )
                ]
            )
        else:
            scraper = build_scraper()

        # --------------------------------------------------------------
        # REGION FAN-OUT
        # --------------------------------------------------------------

        items: List[
            Tuple[str, str]
        ] = []

        seen = set()

        for url in urls:
            for region in target.regions:
                key = (
                    f"{url}||{region.value}"
                )

                if key not in seen:
                    seen.add(key)
                    items.append(
                        (
                            url,
                            region.value,
                        )
                    )

        self._progress(
            "region_fanout_ready",
            run_id=run_id,
            work_items=len(items),
        )

        # --------------------------------------------------------------
        # PROCESS EACH URL / REGION
        # --------------------------------------------------------------

        for index, (
            url,
            region,
        ) in enumerate(
            items,
            start=1,
        ):
            checkpoint_key = (
                f"{url}||{region}"
            )

            if checkpoint_key in done:
                continue

            self._progress(
                "scrape_started",
                run_id=run_id,
                url=url,
                region=region,
                index=index,
                total=len(items),
            )

            # ----------------------------------------------------------
            # SCRAPING
            # ----------------------------------------------------------

            try:
                page, log = scraper.fetch(
                    url,
                    region=region,
                )

            except ScrapeError as exc:
                stats.urls_failed += 1

                stats.errors.append(
                    f"{url} [{region}]: {exc}"
                )

                if getattr(
                    exc,
                    "log",
                    None,
                ):
                    stats.retries_used += (
                        _log_retries(
                            exc.log
                        )
                    )

                    stats.scrapers_used.extend(
                        exc.log.scrapers_tried
                    )

                continue

            stats.pages_scraped += 1

            self._progress(
                "scrape_succeeded",
                run_id=run_id,
                url=url,
                region=region,
                pages_scraped=stats.pages_scraped,
            )

            stats.retries_used += (
                _log_retries(log)
            )

            stats.scrapers_used.extend(
                log.scrapers_tried
            )

            # ----------------------------------------------------------
            # RAW EVIDENCE
            # ----------------------------------------------------------

            try:
                self.store.save_evidence(
                    run_id,
                    url,
                    page.markdown,
                )
            except Exception as exc:
                stats.errors.append(
                    f"evidence {url}: {exc}"
                )

            # ----------------------------------------------------------
            # PRODUCT EXTRACTION
            # ----------------------------------------------------------

            competitor = self._entity_for_url(
                url,
                entities,
            )

            result = self.extractor.extract(
                page.markdown,
                url,
                expected_region=Region(
                    region
                ),
            )

            stats.extraction_attempts += (
                result.attempts
            )

            if result.ok:
                offer = self._finalize_offer(
                    result.offer,
                    competitor,
                    run_id,
                )

                self.store.upsert_offer(
                    offer
                )

                stats.extractions_ok += 1

                self._progress(
                    "extraction_succeeded",
                    run_id=run_id,
                    url=url,
                    region=region,
                    product=offer.product_name,
                    extraction_confidence=(
                        offer.extraction_confidence
                    ),
                )

                # ------------------------------------------------------
                # REVIEW EXTRACTION
                # ------------------------------------------------------

                reviews, _fallback = (
                    self.extractor.extract_reviews(
                        page.markdown,
                        url,
                        expected_region=Region(
                            region
                        ),
                        competitor=competitor,
                        run_id=run_id,
                        product_name=offer.product_name,
                    )
                )

                for review in reviews:
                    self.review_store.add(
                        review
                    )

            else:
                # ------------------------------------------------------
                # EXTRACTION FAILURE
                # ------------------------------------------------------

                stats.extractions_failed += 1

                # IMPORTANT:
                # The previous implementation only sent the error through
                # the progress callback. That meant Render application logs
                # contained the HTTP polling requests but not the actual
                # extraction exception.
                #
                # This log deliberately records only metadata + the error.
                # It does NOT log page contents or secrets.
                logger.error(
                    "extraction failed: "
                    "run_id=%s url=%s region=%s error=%s",
                    run_id,
                    url,
                    region,
                    result.error,
                )

                self._progress(
                    "extraction_failed",
                    run_id=run_id,
                    url=url,
                    region=region,
                    error=result.error,
                )

                stats.errors.append(
                    f"{url} [{region}]: "
                    f"extraction failed — {result.error}"
                )

            # ----------------------------------------------------------
            # CHECKPOINT
            # ----------------------------------------------------------

            done.add(
                checkpoint_key
            )

            if (
                stats.pages_scraped % 10 == 0
            ):
                self._save_checkpoint(
                    run_id,
                    done,
                )

        self._save_checkpoint(
            run_id,
            done,
        )

        # --------------------------------------------------------------
        # METRICS
        # --------------------------------------------------------------

        duration = round(
            time.monotonic() - start,
            2,
        )

        metrics = self._build_metrics(
            target,
            run_id,
            started_at,
            demo,
            stats,
            duration,
            entities,
        )

        try:
            self.store.record_run(
                metrics.model_dump(
                    mode="json"
                )
            )
        except Exception as exc:
            logger.warning(
                "could not record run metrics: %s",
                exc,
            )

        # --------------------------------------------------------------
        # EVENT DETECTION
        # --------------------------------------------------------------

        events = self.insights.detect_events(
            run_id
        )

        attempted = (
            stats.pages_scraped
            + stats.urls_failed
        )

        if (
            attempted > 0
            and (
                stats.pages_scraped
                / attempted
            )
            < settings.min_scrape_success_rate
        ):
            events.append(
                EventAlert(
                    run_id=run_id,
                    kind="low_scrape_success",
                    severity="critical",
                    message=(
                        "Scraping success dropped below "
                        f"{settings.min_scrape_success_rate * 100:.0f}% "
                        f"({attempted} urls, "
                        f"{stats.urls_failed} failed)."
                    ),
                )
            )

        # --------------------------------------------------------------
        # COMPLETE
        # --------------------------------------------------------------

        self._progress(
            "run_completed",
            run_id=run_id,
            pages_scraped=stats.pages_scraped,
            extractions_ok=stats.extractions_ok,
            extractions_failed=stats.extractions_failed,
            duration_s=duration,
        )

        logger.info(
            "run %s done: %d pages, %d ok, "
            "%d failed in %.1fs (%s mode)",
            run_id,
            stats.pages_scraped,
            stats.extractions_ok,
            stats.extractions_failed,
            duration,
            "demo" if demo else "live",
        )

        return PipelineResult(
            target=target,
            run_id=run_id,
            run_date=time.strftime(
                "%Y-%m-%d"
            ),
            mode=(
                "demo"
                if demo
                else "live"
            ),
            pages_scraped=stats.pages_scraped,
            extractions_ok=stats.extractions_ok,
            extractions_failed=(
                stats.extractions_failed
            ),
            retries_used=stats.retries_used,
            duration_s=duration,
            errors=stats.errors,
            competitors=len(entities),
            discovered_urls=stats.discovered,
            events=[
                event.model_dump(
                    mode="json"
                )
                for event in events
            ],
            metrics=metrics.model_dump(
                mode="json"
            ),
        )

    # ------------------------------------------------------------------
    # METRICS
    # ------------------------------------------------------------------

    def _build_metrics(
        self,
        target: CompetitorTarget,
        run_id: str,
        started_at: str,
        demo: bool,
        stats: RunStats,
        duration: float,
        entities: List[Competitor],
    ) -> RunMetrics:
        attempts = max(
            1,
            stats.extraction_attempts,
        )

        # Per-run product count.
        products = len(
            self.store.distinct_products(
                run_id=run_id
            )
        )

        telemetry = (
            self.extractor.client.telemetry()
        )

        return RunMetrics(
            run_id=run_id,
            mode=(
                "demo"
                if demo
                else "live"
            ),
            target_company=target.company,
            competitors=len(entities),
            regions=len(target.regions),
            products=products,
            urls_discovered=stats.discovered,
            urls_attempted=(
                stats.pages_scraped
                + stats.urls_failed
            ),
            urls_succeeded=(
                stats.pages_scraped
            ),
            urls_failed=stats.urls_failed,
            scraping_success_rate=round(
                stats.pages_scraped
                / max(
                    1,
                    stats.pages_scraped
                    + stats.urls_failed,
                ),
                4,
            ),
            retries_used=stats.retries_used,
            extraction_attempts=(
                stats.extraction_attempts
            ),
            extraction_successes=(
                stats.extractions_ok
            ),
            extraction_failures=(
                stats.extractions_failed
            ),
            extraction_accuracy=round(
                stats.extractions_ok
                / attempts,
                4,
            ),
            runtime_s=duration,
            llm_calls=telemetry["calls"],
            provider_attempts=(
                telemetry[
                    "provider_attempts"
                ]
            ),
            fallbacks=(
                telemetry[
                    "fallbacks_count"
                ]
            ),
            providers_used=_provider_chain(
                self.extractor.client
            ),
            scrapers_used=(
                _unique(
                    stats.scrapers_used
                )
                or (
                    ["demo"]
                    if demo
                    else []
                )
            ),
            errors=stats.errors,
        )


def _provider_chain(
    client,
) -> list:
    """Build the provider chain from both sides of each fallback."""

    telemetry = client.telemetry()

    chain: List[str] = []

    for (
        from_provider,
        to_provider,
    ) in telemetry.get(
        "fallbacks",
        [],
    ):
        chain.extend(
            [
                from_provider,
                to_provider,
            ]
        )

    chain.append(
        telemetry["provider"]
    )

    return _unique(
        chain
    )


def _unique(
    items: list,
) -> list:
    seen = set()
    output = []

    for item in items:
        if item not in seen:
            seen.add(item)
            output.append(item)

    return output
