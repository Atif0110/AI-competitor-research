"""Layer 2 scraper: Playwright headless browser (lazy import — not installed by default)."""
from __future__ import annotations

import logging
from typing import Optional

from .base import BaseScraper, ScrapeError, ScrapedPage, parse_proxy_url
from app.url_safety import is_public_http_url

logger = logging.getLogger(__name__)


class PlaywrightScraper(BaseScraper):
    name = "playwright"

    def _fetch_once(
        self,
        url: str,
        proxy: Optional[str],
        region: Optional[str] = None,
    ) -> ScrapedPage:
        if not is_public_http_url(url):
            raise ScrapeError("blocked non-public or invalid URL")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            raise ScrapeError(
                "playwright not installed (pip install playwright)"
            ) from e

        with sync_playwright() as p:
            kwargs = {}

            if proxy:
                parsed = parse_proxy_url(proxy)
                if parsed:
                    kwargs["proxy"] = parsed

            # Use the installed Chromium browser channel instead of
            # Playwright's separate chromium-headless-shell executable.
            browser = p.chromium.launch(
                headless=False,
                **kwargs,
            )

            try:
                page = browser.new_page()
                page.route(
                    "**/*",
                    lambda route: route.continue_()
                    if is_public_http_url(route.request.url)
                    else route.abort(),
                )

                page.goto(
                    url,
                    timeout=self._timeout() * 1000,
                    wait_until="domcontentloaded",
                )

                if not is_public_http_url(page.url):
                    raise ScrapeError("blocked redirect to non-public or invalid URL")

                # Allow client-side JavaScript to finish rendering.
                page.wait_for_timeout(1200)

                markdown = page.inner_text("body")

                if not markdown:
                    raise ScrapeError(
                        "playwright returned empty body text"
                    )

                return ScrapedPage(
                    url=url,
                    markdown=markdown,
                    proxy_used=proxy,
                    region=region,
                )

            finally:
                browser.close()

    def _timeout(self) -> int:
        from app.config import settings

        return settings.request_timeout_seconds
