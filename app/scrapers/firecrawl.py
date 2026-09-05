"""Layer 1 scraper: Firecrawl — HTML -> clean markdown via API."""
from __future__ import annotations

import logging
from typing import Optional

from .base import BaseScraper, ScrapeError, ScrapedPage

logger = logging.getLogger(__name__)


class FirecrawlScraper(BaseScraper):
    name = "firecrawl"

    def __init__(self) -> None:
        super().__init__()
        from app.config import settings

        self.api_key = settings.firecrawl_api_key
        self.base_url = settings.firecrawl_base_url

    def _fetch_once(self, url: str, proxy: Optional[str],
                    region: Optional[str] = None) -> ScrapedPage:
        import requests

        if not self.api_key:
            raise ScrapeError("FIRECRAWL_API_KEY not set")
        payload = {"url": url, "formats": ["markdown"]}
        # Firecrawl runs server-side: a client-side proxy cannot influence it,
        # so geo is handled through Firecrawl's own location control. EU is a
        # MARKET, not a country (review #6) — never sent as `country`.
        if region and region.upper() != "EU":
            country = {"UK": "GB"}.get(region.upper(), region.upper())
            payload["location"] = {"country": country}
        elif region:
            logger.info("firecrawl: region=%s is a market — no country location sent", region)
        if proxy:
            logger.info("firecrawl ignores client proxy (API-side); region handled via location=%s", region)
        resp = requests.post(
            f"{self.base_url}/scrape",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self._timeout(),
        )
        if resp.status_code != 200:
            raise ScrapeError(f"firecrawl http {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        markdown = (data.get("data") or {}).get("markdown") or ""
        if not markdown:
            raise ScrapeError("firecrawl returned empty markdown")
        return ScrapedPage(url=url, markdown=markdown, proxy_used=None)  # honest: proxy was not used

    def _timeout(self) -> int:
        from app.config import settings

        return settings.request_timeout_seconds
