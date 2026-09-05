"""ScraperOrchestrator — a real strategy chain (Priority 2).

    Firecrawl -> 403
              -> Playwright -> JS challenge
                            -> Basic HTTP -> success   (telemetry recorded)

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

        for scraper in self.scrapers:
            attempts = 0
            while attempts < self.retry_policy.max_retries:
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
                    return page, log
                except ScrapeError as e:
                    last_error = e
                    log.attempts_per_scraper[scraper.name] = attempts
                    if scraper.name not in log.scrapers_tried:
                        log.scrapers_tried.append(scraper.name)
                    logger.warning("[%s] attempt %d/%d failed for %s: %s",
                                   scraper.name, attempts, self.retry_policy.max_retries, url, e)
                    if attempts < self.retry_policy.max_retries:
                        time.sleep(self.retry_policy.backoff(attempts))

        log.status = "failed"
        log.error = str(last_error)
        exc = ScrapeError(f"all scrapers failed for {url}: tried={log.scrapers_tried}: {last_error}")
        exc.log = log
        raise exc

    def _pick_proxy(self, region: Optional[str], log: ScrapeLog, url: str) -> Optional[str]:
        """Strict geo policy: with PROXY_VERIFY=true a region that has a pool
        MUST resolve to a verified proxy for that region, else the scrape is
        refused (never silently use a different geography)."""
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
