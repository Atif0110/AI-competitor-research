"""Base scraper primitives: ScrapeError, ScrapedPage, RateLimiter,
geo-keyed ProxyPool (Priority 3), exponential RetryPolicy, and the leaf
scraper interface used by the ScraperOrchestrator chain (Priority 2).
"""
from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class ScrapeError(Exception):
    """Raised when a page cannot be fetched by one scraper attempt."""


@dataclass
class ScrapedPage:
    url: str
    markdown: str
    proxy_used: Optional[str] = None
    attempt: int = 1
    region: Optional[str] = None


@dataclass
class ScrapeLog:
    """Per-URL telemetry for the chain attempt (Priority 2)."""
    url: str
    region: Optional[str] = None
    status: str = "success"  # "success" | "failed"
    scrapers_tried: List[str] = field(default_factory=list)
    attempts_per_scraper: Dict[str, int] = field(default_factory=dict)
    final_scraper: Optional[str] = None
    proxy_used: Optional[str] = None
    error: Optional[str] = None


class RateLimiter:
    """Per-domain minimum-interval limiter."""

    def __init__(self, min_interval: float = 1.0):
        self.min_interval = min_interval
        self._last: Dict[str, float] = {}

    def wait(self, host: str) -> None:
        now = time.monotonic()
        last = self._last.get(host, 0.0)
        wait = (last + self.min_interval) - now
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()


class ProxyPool:
    """Geo-targeted proxy pool: a generic round-robin list plus per-Region
    pools (PROXY_US=..., PROXY_IN=..., ...). `next(region)` picks the region
    pool first, generic as fallback. Exit-IP verification is pluggable via
    `verify` (ip-api.com style) and enabled with PROXY_VERIFY=true so the
    dashboard can claim "US -> US IP" only when actually verified."""

    def __init__(self, generic: List[str] | None = None,
                 by_region: Dict[str, List[str]] | None = None,
                 verify=None):
        self._generic = list(generic or [])
        self._regions = {k.upper(): list(v) for k, v in (by_region or {}).items()}
        self._rot: Dict[str, int] = defaultdict(int)
        self._healthy: Dict[str, bool] = {}
        self.verify = verify  # callable proxy:str -> country_code:str|None

    @property
    def enabled(self) -> bool:
        return bool(self._generic or self._regions)

    def has_region(self, region: str) -> bool:
        return region.upper() in self._regions

    def next(self, region: Optional[str] = None) -> Optional[str]:
        key = (region.upper() if region else None) or "__generic__"
        pool = self._regions.get(key) if key != "__generic__" else None
        if not pool:
            pool = self._generic
        if not pool:
            return None
        i = self._rot[key]
        self._rot[key] = i + 1
        return pool[i % len(pool)]

    def next_verified(self, region: Optional[str] = None) -> Optional[str]:
        """Strict geo policy (PROXY_VERIFY=true): return a proxy whose exit
        country matches the requested region, or None. NEVER silently falls
        back to the generic pool / a different geography — the caller treats
        None as 'scrape unavailable for this region'."""
        if not self.verify or not region:
            return self.next(region)
        pool = self._regions.get(region.upper())
        if not pool:
            return None
        for proxy in pool:
            if self._healthy.get(proxy) is True:
                return proxy
        for proxy in pool:
            try:
                country = self.verify(proxy)
                self._healthy[proxy] = country == region.upper()
                if country == region.upper():
                    self._rot[region.upper()] = 0
                    return proxy
            except Exception:
                self._healthy[proxy] = False
        return None


class RetryPolicy:
    """Exponential backoff with jitter."""

    def __init__(self, max_retries: int = 3, base_seconds: float = 1.5):
        self.max_retries = max_retries
        self.base = base_seconds

    def backoff(self, attempt: int) -> float:
        return self.base * (2 ** (attempt - 1)) + random.uniform(0, 0.4)


class BaseScraper(ABC):
    """A single-attempt leaf. The ScraperOrchestrator owns retries/rate-limit/
    proxy rotation and the cross-scraper fallback chain."""

    name = "base"
    region = None  # execution-region hint set by the orchestrator

    @abstractmethod
    def _fetch_once(self, url: str, proxy: Optional[str],
                    region: Optional[str] = None) -> ScrapedPage:
        """One attempt; raise ScrapeError on failure."""


def parse_proxy_url(proxy):
    """Parse a proxy URL into Playwright/requests-friendly kwargs (review #21).

    Handles http://user:pass@host:port, https://host:port, socks5://host:port,
    plain host:port and IPv6 brackets, via urllib.parse.
    Returns None when empty/invalid.

    >>> parse_proxy_url("http://user:secret@host:8080")
    {'server': 'http://host:8080', 'username': 'user', 'password': 'secret'}
    """
    import urllib.parse
    if not proxy or not str(proxy).strip():
        return None
    p = str(proxy).strip()
    if "://" not in p:
        return {"server": p}
    u = urllib.parse.urlparse(p)
    if not u.hostname:
        return None
    try:
        port = u.port
    except ValueError:
        return None
    host = "[%s]" % u.hostname if ":" in u.hostname else u.hostname
    scheme = u.scheme.lower()
    server = "%s://%s" % (scheme, host) + (":%d" % port if port else "")
    out = {"server": server}
    if u.username:
        out["username"] = urllib.parse.unquote(u.username)
        out["password"] = urllib.parse.unquote(u.password or "")
    return out
