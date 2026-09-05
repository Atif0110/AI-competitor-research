"""Layer 3 scraper: plain HTTP GET with headers (works without any API keys)."""
from __future__ import annotations

import logging
from typing import Optional

from .base import BaseScraper, ScrapeError, ScrapedPage

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


class BasicScraper(BaseScraper):
    name = "basic"

    def _fetch_once(self, url: str, proxy: Optional[str],
                    region: Optional[str] = None) -> ScrapedPage:
        import requests

        proxies = {"http": proxy, "https": proxy} if proxy else None
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=self._timeout(), proxies=proxies)
        except requests.RequestException as e:
            raise ScrapeError(f"request failed: {e}") from e
        if resp.status_code >= 400:
            raise ScrapeError(f"http {resp.status_code}")
        text = resp.text
        if not text:
            raise ScrapeError("empty response body")
        return ScrapedPage(url=url, markdown=_html_to_text(text), proxy_used=proxy, region=region)

    def _timeout(self) -> int:
        from app.config import settings

        return settings.request_timeout_seconds


def _html_to_text(html: str) -> str:
    """Rough HTML->text (Firecrawl/Playwright recommended for production;
    this is a zero-dependency fallback that strips tags and unescapes entities."""
    import html as html_lib
    import re

    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return html_lib.unescape(text).strip()
