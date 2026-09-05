"""Currency normalization (Priority 12).

Every offer keeps its native price + currency for display, and gains a
normalized USD value + the exchange rate + when the rate was observed, so
cross-currency comparison never compares raw currency numbers.
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

from app.config import settings


def get_rates() -> dict:
    return settings.exchange_rates


def normalize_price(price: float, currency: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """-> (normalized_price_usd, exchange_rate, timestamp)"""
    rates = get_rates()
    rate = rates.get(str(currency).upper())
    if rate is None:
        return None, None, None
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return round(price * rate, 2), rate, ts
