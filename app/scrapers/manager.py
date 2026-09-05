"""Scraper manager — builds the ScraperOrchestrator chain from what is
configured/installed: Firecrawl -> Playwright -> Basic HTTP (always present
as the final fallback)."""
from __future__ import annotations

import importlib.util
import logging

from app.config import settings

from .base import BaseScraper, ScrapeError  # noqa: F401  (re-export)
from .orchestrator import ScraperOrchestrator

logger = logging.getLogger(__name__)


def build_scraper() -> ScraperOrchestrator:
    chain: list[BaseScraper] = []

    if settings.firecrawl_api_key:
        from .firecrawl import FirecrawlScraper
        chain.append(FirecrawlScraper())
        logger.info("scraper chain: firecrawl configured")

    if importlib.util.find_spec("playwright"):
        from .playwright_scraper import PlaywrightScraper
        chain.append(PlaywrightScraper())
        logger.info("scraper chain: playwright available")

    from .basic import BasicScraper
    chain.append(BasicScraper())  # always available as last resort

    return ScraperOrchestrator(chain)


def build_demo_scraper(targets) -> BaseScraper:
    from .demo import DemoScraper
    return DemoScraper(targets=targets)
