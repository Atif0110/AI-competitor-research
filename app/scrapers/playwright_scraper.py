"""Layer 2 scraper: Playwright headless browser (lazy import — not installed by default)."""
from __future__ import annotations

import logging
import threading
from contextlib import nullcontext
from typing import Optional

from .base import BaseScraper, ScrapeError, ScrapedPage, parse_proxy_url
from app.url_safety import DNSPinningHTTPProxy, is_public_http_url, public_fetch_guard_enabled

logger = logging.getLogger(__name__)
_PUBLIC_BROWSER_SEMAPHORE = threading.BoundedSemaphore(1)


def _browser_slot(guarded: bool):
    """Bound public Chromium memory use while keeping private concurrency."""
    return _PUBLIC_BROWSER_SEMAPHORE if guarded else nullcontext()


class PlaywrightScraper(BaseScraper):
    name = "playwright"
    public_max_attempts = 1

    def _fetch_once(
        self,
        url: str,
        proxy: Optional[str],
        region: Optional[str] = None,
    ) -> ScrapedPage:
        if not is_public_http_url(url):
            raise ScrapeError("blocked non-public or invalid URL")
        guarded = public_fetch_guard_enabled()
        navigation_timeout = min(self._timeout(), 20) if guarded else self._timeout()
        navigation_timeout_ms = max(1_000, navigation_timeout * 1000)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            raise ScrapeError(
                "playwright not installed (pip install playwright)"
            ) from e

        with _browser_slot(guarded), sync_playwright() as p:
            proxy_context = (
                DNSPinningHTTPProxy(
                    connect_timeout=min(10, self._timeout()),
                    read_timeout=max(15, self._timeout()),
                )
                if guarded
                else nullcontext(None)
            )
            with proxy_context as pinned_proxy:
                kwargs = {}
                if pinned_proxy is not None:
                    # Chromium uses this loopback proxy for HTTP and HTTPS.
                    # QUIC and non-proxied WebRTC UDP are disabled so browser
                    # traffic cannot skip the DNS-pinning TCP boundary.
                    # Chromium normally bypasses proxies for localhost and
                    # loopback destinations. Remove that implicit bypass so
                    # the local guard also rejects those requests.
                    kwargs["proxy"] = {
                        "server": pinned_proxy.url,
                        "bypass": "<-loopback>",
                    }
                    kwargs["args"] = [
                        "--disable-quic",
                        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    ]
                elif proxy:
                    parsed = parse_proxy_url(proxy)
                    if parsed:
                        kwargs["proxy"] = parsed

                launch_options = {"headless": pinned_proxy is not None, **kwargs}
                if pinned_proxy is not None:
                    # The Chromium channel uses full Chrome's newer headless
                    # mode, avoiding an X server while retaining the real
                    # browser implementation for public scraping.
                    launch_options["channel"] = "chromium"
                    launch_options["timeout"] = navigation_timeout_ms
                try:
                    browser = p.chromium.launch(**launch_options)
                except Exception as exc:
                    raise ScrapeError("Playwright browser launch failed") from exc

                try:
                    page = browser.new_page()

                    def route_public_content(route):
                        request = route.request
                        if (
                            not is_public_http_url(request.url)
                            or getattr(request, "resource_type", "other")
                            in {"image", "media", "font"}
                        ):
                            route.abort()
                        else:
                            route.continue_()

                    page.route(
                        "**/*",
                        route_public_content,
                    )

                    page.goto(
                        url,
                        timeout=navigation_timeout_ms,
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
                        proxy_used=proxy if not guarded else None,
                        region=region,
                    )
                except ScrapeError:
                    raise
                except Exception as exc:
                    raise ScrapeError("Playwright page fetch failed") from exc

                finally:
                    try:
                        browser.close()
                    except Exception:
                        logger.debug("could not close Playwright after scrape", exc_info=True)

    def _timeout(self) -> int:
        from app.config import settings

        return settings.request_timeout_seconds
