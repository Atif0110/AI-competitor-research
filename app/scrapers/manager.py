"""Build the scraper chain without allowing unpinned public-mode fetchers."""
from __future__ import annotations

import importlib.util
import logging

from app.config import settings
from app.url_safety import public_fetch_guard_enabled

from .base import BaseScraper, ScrapeError  # noqa: F401  (re-export)
from .orchestrator import ScraperOrchestrator

logger = logging.getLogger(__name__)


def build_scraper() -> ScraperOrchestrator:
    chain: list[BaseScraper] = []

    guarded = public_fetch_guard_enabled()
    if settings.firecrawl_api_key and not guarded:
        from .firecrawl import FirecrawlScraper
        chain.append(FirecrawlScraper())
        logger.info("scraper chain: firecrawl configured")
    elif settings.firecrawl_api_key:
        logger.warning("firecrawl disabled for anonymous URL research; URL resolution is performed outside this network boundary")

    if importlib.util.find_spec("playwright"):
        from .playwright_scraper import PlaywrightScraper
        chain.append(PlaywrightScraper())
        if guarded:
            logger.info("scraper chain: Playwright enabled through the DNS-pinning proxy")
        else:
            logger.info("scraper chain: playwright available")

    from .basic import BasicScraper
    chain.append(BasicScraper())  # always available as last resort

    return ScraperOrchestrator(chain)


def build_demo_scraper(targets) -> BaseScraper:
    from .demo import DemoScraper
    return DemoScraper(targets=targets)
