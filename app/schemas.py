"""Pydantic schemas — the contract between discovery, extraction, storage and analysis.

Data model:
    ResearchTarget (Acme) -> competitors [SoundWorks, GloboTech] x regions x products
Every object the LLM extracts must validate against one of these before it is
allowed near the database. Malformed output is rejected and retried (or flagged).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Currency(str, Enum):
    USD = "USD"; EUR = "EUR"; GBP = "GBP"; INR = "INR"; JPY = "JPY"
    CAD = "CAD"; AUD = "AUD"; SGD = "SGD"; BRL = "BRL"


class Region(str, Enum):
    US = "US"; EU = "EU"; UK = "UK"; IN = "IN"; JP = "JP"
    CA = "CA"; AU = "AU"; SG = "SG"; BR = "BR"


class Availability(str, Enum):
    in_stock = "in_stock"; out_of_stock = "out_of_stock"
    preorder = "preorder"; unknown = "unknown"


class Competitor(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    website: str = Field(min_length=4)

    @field_validator("website")
    @classmethod
    def normalize_website(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            v = "https://" + v
        return v.rstrip("/")


class CompetitorTarget(BaseModel):
    """Input: who to research, against whom, where, on which products.

    (`ResearchTarget` is an alias; both names work in configs and API payloads.)"""
    company: str = Field(min_length=1, max_length=150)
    website: str = Field(min_length=4)
    regions: List[Region] = Field(default_factory=lambda: [Region.US])
    focus_products: List[str] = Field(default_factory=list)
    competitors: List[Competitor] = Field(default_factory=list)

    @field_validator("website")
    @classmethod
    def normalize_website(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            v = "https://" + v
        return v.rstrip("/")

    def all_entities(self) -> List[Competitor]:
        return [Competitor(name=self.company, website=self.website)] + list(self.competitors)


ResearchTarget = CompetitorTarget


class ProductOffer(BaseModel):
    """One structured observation for a product in a region."""
    product_name: str = Field(min_length=1, max_length=200)
    brand: Optional[str] = None
    price: float
    currency: Currency
    region: Region
    url: str = Field(min_length=1)
    seller: Optional[str] = None
    availability: Availability = Availability.unknown
    listing_title: Optional[str] = None
    run_id: Optional[str] = None
    canonical_product_id: Optional[str] = None
    canonical_product_name: Optional[str] = None
    extraction_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    scraped_at: datetime = Field(default_factory=_utcnow)
    competitor: Optional[str] = None
    canonical_url: Optional[str] = None
    normalized_price_usd: Optional[float] = None
    exchange_rate: Optional[float] = None
    exchange_rate_timestamp: Optional[str] = None

    @field_validator("price")
    @classmethod
    def price_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"price must be > 0, got {v}")
        return round(v, 2)

    @field_validator("product_name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        return v.strip()


class Review(BaseModel):
    product_name: str = Field(min_length=1)
    region: Region
    rating: Optional[float] = Field(default=None, ge=0, le=5)
    review_text: str = Field(min_length=1)
    reviewer: Optional[str] = None
    review_date: Optional[date] = None
    source_url: str = Field(min_length=1)
    scraped_at: Optional[datetime] = None
    competitor: Optional[str] = None
    run_id: Optional[str] = None


class PricePoint(BaseModel):
    date: date
    price: float
    currency: Currency
    seller: Optional[str] = None
    normalized_price_usd: Optional[float] = None


class PriceMove(BaseModel):
    """v4: native AND normalized values are both reported — zero ambiguity."""
    product_name: str
    region: Region
    from_price: float          # native
    to_price: float            # native
    currency: Currency
    from_price_usd: Optional[float] = None
    to_price_usd: Optional[float] = None
    change_pct: float          # computed on normalized USD
    from_date: date
    to_date: date
    event: str                 # "drop" | "increase"


class PriceStats(BaseModel):
    """v4: native values kept for display; usd_* used for comparisons."""
    product_name: str
    region: Region
    currency: Currency
    current_price: float            # native
    previous_price: Optional[float] = None  # native
    current_usd: Optional[float] = None
    min_price: float                # native
    max_price: float                # native
    median_price: float             # native
    min_usd: Optional[float] = None
    max_usd: Optional[float] = None
    median_usd: Optional[float] = None
    change_7d: Optional[float] = None   # % on USD
    change_14d: Optional[float] = None  # % on USD
    change_30d: Optional[float] = None  # % on USD
    volatility: float
    samples: int


class Undercut(BaseModel):
    product_name: str
    region: Region
    leader: str
    leader_price: float       # native, display
    currency: Currency = Currency.USD
    runner_up: str | None = None
    runner_up_price: float | None = None
    gap_pct: float | None = None   # on normalized USD
    source_urls: List[str] = Field(default_factory=list)
    observation_times: List[str] = Field(default_factory=list)


class SentimentSummary(BaseModel):
    product_name: Optional[str] = None
    region: Optional[Region] = None
    review_count: int = 0
    avg_rating: Optional[float] = None
    topic_mentions: dict = Field(default_factory=dict)
    topic_breakdown: Dict[str, float] = Field(default_factory=dict)
    top_complaints: List[str] = Field(default_factory=list)
    top_praise: List[str] = Field(default_factory=list)
    feature_severities: List[dict] = Field(default_factory=list)
    fallback_used: bool = False
    provenance: List[str] = Field(default_factory=list)


class EventAlert(BaseModel):
    run_id: str
    kind: str  # price_drop | price_increase | new_product | undercut_change | low_scrape_success
    message: str
    severity: str = "info"
    product_name: Optional[str] = None
    region: Optional[Region] = None
    competitor: Optional[str] = None
    change_pct: Optional[float] = None


class RunMetrics(BaseModel):
    """Per-run validation metrics (resume numbers)."""
    run_id: str
    mode: str
    target_company: str
    competitors: int
    regions: int
    products: int
    urls_discovered: int = 0
    urls_attempted: int = 0
    urls_succeeded: int = 0
    urls_failed: int = 0
    scraping_success_rate: float = 0.0
    retries_used: int = 0
    extraction_attempts: int = 0
    extraction_successes: int = 0
    extraction_failures: int = 0
    extraction_accuracy: float = 0.0
    runtime_s: float = 0.0
    llm_calls: int = 0
    provider_attempts: int = 0
    fallbacks: int = 0
    providers_used: List[str] = Field(default_factory=list)
    scrapers_used: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


class PipelineResult(BaseModel):
    target: CompetitorTarget
    run_id: str
    run_date: str
    mode: str
    pages_scraped: int = 0
    extractions_ok: int = 0
    extractions_failed: int = 0
    retries_used: int = 0
    duration_s: float = 0.0
    cached: bool = False
    errors: List[str] = Field(default_factory=list)
    competitors: int = 1
    discovered_urls: int = 0
    events: List[dict] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)
