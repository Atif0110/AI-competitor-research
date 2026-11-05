"""ScraperOrchestrator — a real strategy chain (Priority 2).

Private runs use Firecrawl -> Playwright -> Basic HTTP. Anonymous runs try
DNS-pinned Basic HTTP first and render with Playwright when the response is a
challenge, JavaScript shell, or too thin to be useful; the HTTP response stays
available if browser rendering fails.

Each scraper gets up to `max_retries` attempts (backoff + rate limit +
geo proxy rotation); on persistent failure the next scraper in the chain is
tried. A ScrapeLog is produced per URL: scrapers tried, attempts each,
final scraper, proxy, status — surfaced in the report/dashboard.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional, Sequence
from urllib.parse import urlparse

from app.config import settings
from app.url_safety import public_fetch_guard_enabled

from .base import BaseScraper, ProxyPool, RetryPolicy, ScrapeError, ScrapeLog, ScrapedPage

logger = logging.getLogger(__name__)


class ScraperOrchestrator:
    def __init__(self, scrapers: Sequence[BaseScraper], proxy_pool: Optional[ProxyPool] = None):
        self.scrapers = list(scrapers)
        self.rate_limiter = None  # lazy import of RateLimiter to keep deps light
        from .base import RateLimiter

        self.rate_limiter = RateLimiter(settings.rate_limit_min_interval)
        self.proxy_pool = proxy_pool or ProxyPool(settings.proxy_urls, settings.proxy_regions)
        self.retry_policy = RetryPolicy(settings.max_retries, settings.backoff_base_seconds)

    def fetch(self, url: str, region: Optional[str] = None) -> tuple[ScrapedPage, ScrapeLog]:
        host = urlparse(url).netloc
        log = ScrapeLog(url=url, region=region)
        last_error: Optional[Exception] = None
        basic_fallback: Optional[ScrapedPage] = None
        basic_fallback_log: Optional[ScrapeLog] = None

        for scraper_index, scraper in enumerate(self.scrapers):
            max_attempts = self.retry_policy.max_retries
            public_max_attempts = getattr(scraper, "public_max_attempts", None)
            if public_fetch_guard_enabled() and public_max_attempts is not None:
                max_attempts = max(1, min(max_attempts, int(public_max_attempts)))
            attempts = 0
            while attempts < max_attempts:
                attempts += 1
                self.rate_limiter.wait(host)
                proxy = self._pick_proxy(region, log, url)
                try:
                    page = scraper._fetch_once(url, proxy, region=region)
                    page.attempt = attempts
                    log.final_scraper = scraper.name
                    log.proxy_used = proxy
                    log.attempts_per_scraper[scraper.name] = attempts
                    if scraper.name not in log.scrapers_tried:
                        log.scrapers_tried.append(scraper.name)

                    browser_fallback_available = any(
                        candidate.name == "playwright"
                        for candidate in self.scrapers[scraper_index + 1:]
                    )
                    if (
                        public_fetch_guard_enabled()
                        and scraper.name == "basic"
                        and browser_fallback_available
                        and self._needs_browser_render(page.markdown)
                    ):
                        # Keep the safe HTTP result in case browser rendering
                        # is blocked or times out. A thin response can still
                        # provide useful evidence to the extractor.
                        basic_fallback = page
                        basic_fallback_log = log
                        continue

                    return page, log
                except ScrapeError as e:
                    last_error = e
                    log.attempts_per_scraper[scraper.name] = attempts
                    if scraper.name not in log.scrapers_tried:
                        log.scrapers_tried.append(scraper.name)
                    logger.warning("[%s] attempt %d/%d failed for %s: %s",
                                   scraper.name, attempts, max_attempts, url, e)
                    if attempts < max_attempts:
                        time.sleep(self.retry_policy.backoff(attempts))

        if basic_fallback is not None and basic_fallback_log is not None:
            basic_fallback_log.final_scraper = "basic"
            basic_fallback_log.status = "success"
            return basic_fallback, basic_fallback_log

        log.status = "failed"
        log.error = str(last_error)
        exc = ScrapeError(f"all scrapers failed for {url}: tried={log.scrapers_tried}: {last_error}")
        exc.log = log
        raise exc

    @staticmethod
    def _needs_browser_render(markdown: str) -> bool:
        """Identify responses where rendering may recover useful evidence."""
        text = " ".join(markdown.lower().split())
        if len(text) < 350 or len(text.split()) < 40:
            return True

        challenge_markers = (
            "enable javascript",
            "javascript is required",
            "checking your browser",
            "verify you are human",
            "verify that you are human",
            "complete the security check",
            "unusual traffic",
            "automated requests",
            "robot check",
            "access denied",
            "captcha",
        )
        return any(marker in text for marker in challenge_markers)

    def _pick_proxy(self, region: Optional[str], log: ScrapeLog, url: str) -> Optional[str]:
        """Strict geo policy: with PROXY_VERIFY=true a region that has a pool
        MUST resolve to a verified proxy for that region, else the scrape is
        refused (never silently use a different geography)."""
        if public_fetch_guard_enabled():
            # A remote proxy resolves the destination outside our pinned
            # socket guard. Public mode deliberately fetches from this host.
            return None
        if settings.proxy_verify and region:
            proxy = self.proxy_pool.next_verified(region)
            if proxy is None:
                if self.proxy_pool.has_region(region) and self.proxy_pool.enabled:
                    log.status = "failed"
                    log.error = f"no verified proxy for region {region}"
                    exc = ScrapeError(f"scraping unavailable for {url}: no verified proxy for region {region} (PROXY_VERIFY=true)")
                    exc.log = log
                    raise exc
                return None  # no region pool configured -> run without proxy, explicitly
            return proxy
        return self.proxy_pool.next(region)

    @property
    def names(self) -> List[str]:
        return [s.name for s in self.scrapers]
