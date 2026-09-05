"""Demo scraper — deterministic, offline, zero-credential.

URLs carry the canonical product slug (so focus_products filtering and product
identity work), and every fetch accepts the execution `region` the pipeline
passes — mirroring the live fan-out contract.
"""
from __future__ import annotations

import datetime
import hashlib
import math
from typing import List, Optional

from app.schemas import Competitor, Region

from .base import BaseScraper, ScrapedPage

_PRODUCTS = [
    ("Pro X Headphones", "Acme Audio", 499.00, 4.2),
    ("Lite Earbuds", "Acme Audio", 129.00, 3.9),
    ("Studio Mic 2", "SoundWorks", 249.00, 4.6),
]
_SLUG_TO_IDX = {p[0].lower().replace(" ", "-"): i for i, p in enumerate(_PRODUCTS)}

_REVIEWS = [
    "Battery life is amazing, lasts two days of heavy use.",
    "Very comfortable for long sessions, but a bit heavy.",
    "Charging case is bulky and the cable is too short.",
    "Pricey compared to rivals, but the build quality justifies it.",
    "Sound quality is superb, noise cancellation works great.",
    "Customer support took three days to reply — frustrating.",
]

_COMPETITOR_POSITION = {
    "soundworks": 0.85, "globo": 1.12, "audio": 1.0, "tech": 1.06, "store": 1.03,
}

_REGION_MULT = {"US": 1.0, "EU": 0.92, "UK": 0.82, "IN": 0.15, "JP": 145.0,
                "CA": 1.0, "AU": 0.72, "SG": 0.80, "BR": 0.20}
_REGION_CURRENCY = {"US": "USD", "EU": "EUR", "UK": "GBP", "IN": "INR", "JP": "JPY",
                    "CA": "CAD", "AU": "AUD", "SG": "SGD", "BR": "BRL"}


def _entity_slug(name: str) -> str:
    return name.lower().replace(" ", "-")


def _entity_factor(name: str) -> float:
    low = name.lower()
    for key, mult in _COMPETITOR_POSITION.items():
        if key in low:
            return mult
    return 1.0


def _wobble(product_idx: int) -> float:
    day = datetime.date.today().day
    return 1 + 0.03 * math.sin(day * (product_idx + 1))


def _page_url_for(product_idx: int, region: str, competitor: str) -> str:
    slug = _PRODUCTS[product_idx][0].lower().replace(" ", "-")
    return f"https://{_entity_slug(competitor)}.example.com/{region.lower()}/product/{slug}"


def product_urls_for(entity_name: str, regions: List[Region]) -> List[str]:
    """Discovery-stage output for one entity (demo mode).

    URLs are REGIONLESS: the pipeline's region fan-out attaches one execution
    region per fetch, and region= is always passed to the scraper chain."""
    return [f"https://{_entity_slug(entity_name)}.example.com/product/"
            f"{_PRODUCTS[idx][0].lower().replace(' ', '-')}" for idx in range(len(_PRODUCTS))]


def _render_markdown(product_idx: int, region: str, competitor: str, variant: int) -> str:
    name, brand, base_price, rating = _PRODUCTS[product_idx]
    mult = _REGION_MULT.get(region, 1.0)
    local_price = round(base_price * mult * _entity_factor(competitor) * _wobble(product_idx)
                        * (1 + 0.02 * variant), 2)
    sold_out = variant % 5 == 0 and _entity_factor(competitor) < 1.0
    seller = f"{competitor.title()} Store" if competitor != "target" else "Target Store"
    reviews = _REVIEWS[variant % len(_REVIEWS):] + _REVIEWS[:variant % len(_REVIEWS)]
    lines = [
        f"# {name}",
        f"Brand: {brand}",
        f"Price: {local_price:.2f}",
        f"Currency: {_REGION_CURRENCY.get(region, 'USD')}",
        f"Region: {region}",
        "Availability: " + ("out_of_stock" if sold_out else "in_stock"),
        f"Seller: {seller}",
        f"Rating: {rating}",
        "\nCustomer Reviews:",
        *[f"Review: {t}" for t in reviews[:2]],
    ]
    return "\n".join(lines)


class DemoScraper(BaseScraper):
    name = "demo"

    def __init__(self, targets=None):
        super().__init__()
        self._targets = targets or []

    def _fetch_once(self, url: str, proxy: Optional[str] = None,
                    region: Optional[str] = None) -> ScrapedPage:
        competitor = url.split("//")[1].split(".")[0]
        region = (region or "US").upper()
        slug = url.split("/")[-1]
        product_idx = _SLUG_TO_IDX.get(slug, 0)
        variant = int(hashlib.md5((url + "|" + region).encode()).hexdigest(), 16) % 4
        return ScrapedPage(url=url,
                           markdown=_render_markdown(product_idx, region, competitor, variant),
                           proxy_used=None, region=region)

    def all_target_urls(self):
        urls = []
        for t in self._targets:
            for ent in t.all_entities():
                urls.extend(product_urls_for(ent.name, t.regions))
        return urls
