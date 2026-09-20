"""Structured extraction with schema validation, evidence checks, and retries.

The extractor is intentionally conservative:
- execution region is authoritative;
- only pages that look like commercial/product-offer pages are converted
  into ProductOffer records;
- integration, connection, documentation, careers, blog, and similar
  informational pages are rejected instead of being forced into an offer;
- prices must be supported by the source content;
- suspicious low-confidence extractions are rejected and retried;
- extraction confidence is deterministic and evidence-based;
- review extraction remains separately supported.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Type
from urllib.parse import urlparse

from pydantic import BaseModel, ValidationError

from app.analysis.product_identity import resolve
from app.config import settings
from app.schemas import ProductOffer, Region, Review

from .client import LLMClient

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "You are a precise competitive-intelligence data extractor. "
    "Extract a commercial product or pricing offer ONLY when the page "
    "actually contains evidence of a product/service offer. "
    "Do not invent values. Do not infer a price that is not explicitly "
    "present in the page content. "
    "Integration pages, connection pages, documentation, blogs, careers, "
    "about pages, help pages, and other informational pages are NOT "
    "commercial offers and must be rejected by returning "
    '{"skip": true}. '
    "For a valid offer return ONLY a JSON object with exactly these fields: "
    "product_name, brand, price, currency, region, url, seller, "
    "availability, listing_title. "
    "price must be the actual numeric price explicitly supported by the "
    "page. Never use placeholder prices such as 0.01. "
    "currency must be a 3-letter ISO code "
    "(USD, EUR, GBP, INR, JPY, CAD, AUD, SGD, BRL). "
    "region is the 2-letter execution region provided in the request "
    "context. Copy it exactly and never infer it from the page. "
    "availability must be one of in_stock, out_of_stock, preorder, unknown. "
    "Use unknown when the page does not explicitly establish availability."
)

_REVIEW_SYSTEM = (
    "You extract customer reviews from a product page. Return ONLY JSON: "
    '{"reviews": [{"review_text": "...", "rating": 4.0, '
    '"review_date": "YYYY-MM-DD", "reviewer": "..."}]}. '
    "Use null for unknown rating/date/reviewer. No prose."
)


# URL path segments that normally indicate an informational page rather
# than a commercial product/pricing offer.
_NON_OFFER_PATH_SEGMENTS = {
    "about",
    "blog",
    "blogs",
    "careers",
    "career",
    "contact",
    "connections",
    "connection",
    "docs",
    "documentation",
    "faq",
    "faqs",
    "help",
    "integrations",
    "integration",
    "login",
    "news",
    "press",
    "resources",
    "resource",
    "support",
    "terms",
    "privacy",
    "legal",
    "security",
    "status",
}


class ExtractResult:
    def __init__(
        self,
        offer: Optional[ProductOffer] = None,
        attempts: int = 0,
        error: Optional[str] = None,
        confidence: float = 0.0,
        region_mismatch: bool = False,
    ):
        self.offer = offer
        self.attempts = attempts
        self.error = error
        self.confidence = confidence
        self.region_mismatch = region_mismatch

    @property
    def ok(self) -> bool:
        return self.offer is not None


class StructuredExtractor:
    def __init__(
        self,
        client: Optional[LLMClient] = None,
        schema: Type[BaseModel] = ProductOffer,
    ):
        self.client = client or LLMClient()
        self.schema = schema

    def extract(
        self,
        content: str,
        source_url: str,
        expected_region: Optional[Region] = None,
    ) -> ExtractResult:
        """Extract a trustworthy ProductOffer or reject the page."""

        # Reject obvious non-offer pages before spending an LLM call.
        if self._is_non_offer_url(source_url):
            reason = (
                f"source URL is informational/non-commercial: {source_url}"
            )
            logger.info(reason)
            return ExtractResult(
                offer=None,
                attempts=0,
                error=reason,
            )

        attempts = 0
        last_error: Optional[str] = None

        while attempts < settings.extraction_max_attempts:
            attempts += 1

            raw = self._call_llm(
                content,
                source_url,
                expected_region,
                last_error,
            )

            parsed = self._parse_json(raw)

            if parsed is None:
                last_error = (
                    f"attempt {attempts}: "
                    "LLM returned non-JSON output"
                )
                logger.warning(last_error)
                continue

            # The model can explicitly identify a non-offer page.
            if parsed.get("skip") is True:
                last_error = (
                    f"attempt {attempts}: "
                    "page does not contain a commercial product offer"
                )
                logger.info(last_error)
                return ExtractResult(
                    offer=None,
                    attempts=attempts,
                    error=last_error,
                )

            try:
                offer = self.schema.model_validate(parsed)

            except ValidationError as exc:
                last_error = (
                    f"attempt {attempts}: "
                    f"schema validation failed: "
                    f"{self._first_error(exc)}"
                )
                logger.warning(last_error)
                continue

            # Execution region is authoritative.
            mismatch = False

            if expected_region is not None:
                if offer.region != expected_region:
                    mismatch = True

                    logger.warning(
                        "extracted region %s != execution region %s "
                        "— forcing execution region",
                        offer.region.value,
                        expected_region.value,
                    )

                    offer.region = expected_region

            # Reject prices that are not actually supported by the page.
            if not self._price_is_evidenced(content, offer):
                last_error = (
                    f"attempt {attempts}: "
                    f"price {offer.price} is not sufficiently supported "
                    "by the source content"
                )

                logger.warning(last_error)
                continue

            identity = resolve(offer.product_name)

            offer.canonical_product_id = identity.canonical_id
            offer.canonical_product_name = identity.canonical_name

            confidence = self._confidence(
                content,
                offer,
            )

            # Do not allow weak extraction into the evidence store.
            if confidence < 0.75:
                last_error = (
                    f"attempt {attempts}: "
                    f"extraction confidence {confidence:.3f} "
                    "is below the 0.75 acceptance threshold"
                )

                logger.warning(last_error)
                continue

            offer.extraction_confidence = confidence

            return ExtractResult(
                offer=offer,
                attempts=attempts,
                confidence=confidence,
                region_mismatch=mismatch,
            )

        return ExtractResult(
            offer=None,
            attempts=attempts,
            error=last_error,
        )

    def _call_llm(
        self,
        content: str,
        source_url: str,
        expected_region: Optional[Region] = None,
        prior_error: Optional[str] = None,
    ) -> str:
        ctx = (
            f"Execution region: {expected_region.value}"
            if expected_region
            else "Execution region: unknown"
        )

        user = (
            f"Page URL: {source_url}\n"
            f"{ctx}\n\n"
            "Determine first whether this is a genuine commercial "
            "product/service offer page.\n"
            "If it is not, return exactly: "
            '{"skip": true}\n\n'
            "If it is a commercial offer, extract only values explicitly "
            "supported by the page.\n"
            "A missing price must NOT be invented.\n"
            "Do not use $0.01, 0, null converted to a number, or any "
            "other placeholder as a price.\n\n"
            f"Page content:\n{content[:6000]}\n\n"
        )

        if prior_error:
            user += (
                "Your previous output failed validation:\n"
                f"{prior_error}\n"
                "Correct the problem and return JSON only."
            )
        else:
            user += "Return the JSON only."

        return self.client.complete(
            _SYSTEM_PROMPT,
            user,
        )

    @staticmethod
    def _is_non_offer_url(source_url: str) -> bool:
        """Detect obvious informational pages from their URL path."""

        try:
            path = urlparse(source_url).path.lower()
        except Exception:
            return False

        segments = {
            segment
            for segment in path.split("/")
            if segment
        }

        return bool(
            segments.intersection(_NON_OFFER_PATH_SEGMENTS)
        )

    @staticmethod
    def _price_is_evidenced(
        content: str,
        offer: ProductOffer,
    ) -> bool:
        """Require actual source evidence for the extracted price."""

        if offer.price <= 0:
            return False

        text = content.lower()

        price_value = f"{offer.price:g}"

        # Numeric price must appear in the source.
        if not re.search(
            rf"(?<!\d){re.escape(price_value)}(?!\d)",
            text,
        ):
            return False

        # Currency must also be supported by the source.
        currency = offer.currency.value.lower()

        currency_symbols = {
            "USD": ["$", "usd"],
            "EUR": ["€", "eur"],
            "GBP": ["£", "gbp"],
            "INR": ["₹", "inr"],
            "JPY": ["¥", "jpy"],
            "CAD": ["cad"],
            "AUD": ["aud"],
            "SGD": ["sgd"],
            "BRL": ["r$", "brl"],
        }

        supported_tokens = currency_symbols.get(
            offer.currency.value,
            [currency],
        )

        return any(
            token.lower() in text
            for token in supported_tokens
        )

    @staticmethod
    def _confidence(
        content: str,
        offer: ProductOffer,
    ) -> float:
        """Calculate deterministic evidence-backed confidence."""

        text = content.lower()

        price_present = StructuredExtractor._price_is_evidenced(
            content,
            offer,
        )

        currency_present = (
            offer.currency.value.lower() in text
            or any(
                symbol.lower() in text
                for symbol in {
                    "USD": ["$"],
                    "EUR": ["€"],
                    "GBP": ["£"],
                    "INR": ["₹"],
                    "JPY": ["¥"],
                    "CAD": [],
                    "AUD": [],
                    "SGD": [],
                    "BRL": ["r$"],
                }.get(
                    offer.currency.value,
                    [],
                )
            )
        )

        product_tokens = [
            token
            for token in re.findall(
                r"[a-z0-9]+",
                offer.product_name.lower(),
            )
            if len(token) >= 3
        ]

        product_present = bool(
            product_tokens
            and any(
                token in text
                for token in product_tokens[:4]
            )
        )

        availability_supported = (
            offer.availability.value == "unknown"
            or offer.availability.value.replace(
                "_",
                " ",
            ) in text
        )

        checks = [
            price_present,
            currency_present,
            product_present,
            availability_supported,
            0.5 <= offer.price <= 500_000,
        ]

        return round(
            sum(
                1
                for check in checks
                if check
            )
            / len(checks),
            3,
        )

    def extract_reviews(
        self,
        content: str,
        source_url: str,
        expected_region: Optional[Region] = None,
        competitor: Optional[str] = None,
        run_id: Optional[str] = None,
        product_name: Optional[str] = None,
    ) -> Tuple[List[Review], bool]:
        """Return structured reviews with a deterministic fallback."""

        items = None

        try:
            raw = self.client.complete(
                _REVIEW_SYSTEM,
                (
                    f"Page URL: {source_url}\n"
                    f"Page content:\n{content[:6000]}\n"
                    "Return the JSON only."
                ),
            )

            data = self._parse_json(raw)

            if isinstance(data, dict):
                items = data.get("reviews")

        except Exception as exc:
            logger.warning(
                "review extraction LLM failed (%s) — fallback",
                exc,
            )

        region = (
            expected_region
            if expected_region is not None
            else Region.US
        )

        if isinstance(items, list):
            reviews = []

            for item in items:
                try:
                    reviews.append(
                        Review(
                            product_name=(
                                product_name or "unknown"
                            ),
                            region=region,
                            rating=item.get("rating"),
                            review_text=(
                                item.get("review_text") or ""
                            ).strip(),
                            reviewer=item.get("reviewer"),
                            review_date=item.get(
                                "review_date"
                            ),
                            source_url=source_url,
                            run_id=run_id,
                            competitor=competitor,
                        )
                    )

                except Exception as exc:
                    logger.warning(
                        "skipping malformed review: %s",
                        exc,
                    )

            if reviews:
                return reviews, False

        fallback = []

        for line in content.splitlines():
            match = re.match(
                r"^Review:\s*(.+)$",
                line,
                re.I,
            )

            if match:
                fallback.append(
                    Review(
                        product_name=(
                            product_name or "unknown"
                        ),
                        region=region,
                        review_text=match.group(1).strip(),
                        source_url=source_url,
                        run_id=run_id,
                        competitor=competitor,
                    )
                )

        return fallback, True

    @staticmethod
    def _parse_json(
        raw: str,
    ) -> Optional[Dict[str, Any]]:
        raw = raw.strip()

        if raw.startswith("```"):
            raw = raw.strip("`")

            if raw.startswith("json"):
                raw = raw[4:]

        try:
            data = json.loads(raw)

            return (
                data
                if isinstance(data, dict)
                else None
            )

        except json.JSONDecodeError:
            return None

    @staticmethod
    def _first_error(
        error: ValidationError,
    ) -> str:
        item = error.errors()[0]

        location = ".".join(
            str(value)
            for value in item["loc"]
        )

        return (
            f"{location}: "
            f"{item['msg']}"
        )
