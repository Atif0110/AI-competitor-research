"""Structured extraction with schema validation, reject-and-retry, and flagging.

Engineering-review changes:
- `expected_region` is enforced: the pipeline knows the execution region; the
  LLM may NOT override it. A mismatch is recorded and the execution region wins.
- retries are corrective: each retry is told exactly which validation failed.
- `extraction_confidence`: deterministic believability checks on top of schema
  validity (screens "Headphones @ $0.01").
- `extract_reviews()`: real structured review extraction (text/rating/date/
  reviewer) with the old regex line-parse kept only as an explicit fallback.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Type

from pydantic import BaseModel, ValidationError

from app.analysis.product_identity import resolve
from app.config import settings
from app.schemas import ProductOffer, Region, Review

from .client import LLMClient

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a precise data extractor. Given raw page content, extract the product "
    "offer and return ONLY a JSON object matching this schema. No prose, no markdown "
    "code fences, no commentary. Use exactly these field names: "
    "product_name, brand, price, currency, region, url, seller, availability, "
    "listing_title. price is a number; currency is a 3-letter ISO code "
    "(USD, EUR, GBP, INR, JPY, CAD, AUD, SGD, BRL); region is the 2-letter code "
    "given in the request context — copy it EXACTLY, never guess it from the page; "
    "availability is one of in_stock, out_of_stock, preorder, unknown."
)

_REVIEW_SYSTEM = (
    "You extract customer reviews from a product page. Return ONLY JSON: "
    '{"reviews": [{"review_text": "...", "rating": 4.0, '
    '"review_date": "YYYY-MM-DD", "reviewer": "..."}]}. '
    "Use null for unknown rating/date/reviewer. No prose."
)


class ExtractResult:
    def __init__(self, offer: Optional[ProductOffer] = None, attempts: int = 0,
                 error: Optional[str] = None, confidence: float = 0.0,
                 region_mismatch: bool = False):
        self.offer = offer
        self.attempts = attempts
        self.error = error
        self.confidence = confidence
        self.region_mismatch = region_mismatch

    @property
    def ok(self) -> bool:
        return self.offer is not None


class StructuredExtractor:
    def __init__(self, client: Optional[LLMClient] = None, schema: Type[BaseModel] = ProductOffer):
        self.client = client or LLMClient()
        self.schema = schema

    def extract(self, content: str, source_url: str,
                expected_region: Optional[Region] = None) -> ExtractResult:
        attempts = 0
        last_error: Optional[str] = None
        while attempts < settings.extraction_max_attempts:
            attempts += 1
            raw = self._call_llm(content, source_url, expected_region, last_error)
            parsed = self._parse_json(raw)
            if parsed is None:
                last_error = f"attempt {attempts}: LLM returned non-JSON output"
                logger.warning(last_error)
                continue
            try:
                offer = self.schema.model_validate(parsed)
            except ValidationError as e:
                last_error = f"attempt {attempts}: schema validation failed: {self._first_error(e)}"
                logger.warning(last_error)
                continue

            # The execution context defines the region — the LLM cannot override it.
            mismatch = False
            if expected_region is not None:
                if offer.region != expected_region:
                    mismatch = True
                    logger.warning("extracted region %s != execution region %s — forcing execution region",
                                   offer.region.value, expected_region.value)
                    offer.region = expected_region

            identity = resolve(offer.product_name)
            offer.canonical_product_id = identity.canonical_id
            offer.canonical_product_name = identity.canonical_name
            offer.extraction_confidence = self._confidence(content, offer)
            return ExtractResult(offer=offer, attempts=attempts,
                                 confidence=offer.extraction_confidence,
                                 region_mismatch=mismatch)
        return ExtractResult(offer=None, attempts=attempts, error=last_error)

    def _call_llm(self, content: str, source_url: str,
                  expected_region: Optional[Region] = None,
                  prior_error: Optional[str] = None) -> str:
        ctx = f"Execution region: {expected_region.value}" if expected_region else "Execution region: unknown (extract from page)"
        user = f"Page URL: {source_url}\n{ctx}\n\nPage content:\n{content[:6000]}\n\n"
        if prior_error:
            user += f"Your previous output failed validation: {prior_error}\nReturn a CORRECTED JSON object only.\n"
        else:
            user += "Return the JSON only."
        return self.client.complete(_SYSTEM_PROMPT, user)

    @staticmethod
    def _confidence(content: str, offer: ProductOffer) -> float:
        """Believability checks: price sane, values actually appear in the page."""
        checks = [
            0.5 <= offer.price <= 500_000,  # believable price band (catches "$0.01" offers)
            bool(re.search(re.escape(str(offer.price)), content, re.I)),
            offer.currency.value in content.upper(),
            any(t in content.lower() for t in offer.product_name.lower().split()[:4]),
        ]
        return round(sum(1 for c in checks if c) / len(checks), 3)

    def extract_reviews(self, content: str, source_url: str,
                        expected_region: Optional[Region] = None,
                        competitor: Optional[str] = None,
                        run_id: Optional[str] = None,
                        product_name: Optional[str] = None) -> Tuple[List[Review], bool]:
        """-> (reviews, fallback_used). LLM structured extraction; on LLM failure
        falls back to demo-format `Review: ...` line parsing."""
        items = None
        try:
            raw = self.client.complete(
                _REVIEW_SYSTEM,
                f"Page URL: {source_url}\nPage content:\n{content[:6000]}\nReturn the JSON only.",
            )
            data = self._parse_json(raw)
            if isinstance(data, dict):
                items = data.get("reviews")
        except Exception as e:
            logger.warning("review extraction LLM failed (%s) — fallback", e)

        region = expected_region if expected_region is not None else Region.US
        if isinstance(items, list):
            reviews = []
            for it in items:
                try:
                    reviews.append(Review(
                        product_name=product_name or "unknown",
                        region=region,
                        rating=it.get("rating"),
                        review_text=(it.get("review_text") or "").strip(),
                        reviewer=it.get("reviewer"),
                        review_date=it.get("review_date"),
                        source_url=source_url,
                        run_id=run_id,
                        competitor=competitor,
                    ))
                except Exception as e:
                    logger.warning("skipping malformed review: %s", e)
            if reviews:
                return reviews, False

        fallback = []
        for line in content.splitlines():
            m = re.match(r"^Review:\s*(.+)$", line, re.I)
            if m:
                fallback.append(Review(
                    product_name=product_name or "unknown", region=region,
                    review_text=m.group(1).strip(), source_url=source_url,
                    run_id=run_id, competitor=competitor,
                ))
        return fallback, True

    @staticmethod
    def _parse_json(raw: str) -> Optional[Dict[str, Any]]:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _first_error(e: ValidationError) -> str:
        err = e.errors()[0]
        loc = ".".join(str(x) for x in err["loc"])
        return f"{loc}: {err['msg']}"
