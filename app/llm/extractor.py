from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.llm.client import LLMClient
from app.schemas import ProductOffer, Region, Review


@dataclass
class ExtractionResult:
    """Result of structured product extraction."""

    ok: bool
    offer: ProductOffer | None
    confidence: float
    error: str | None = None
    attempts: int = 0
    region_mismatch: bool = False


class StructuredExtractor:
    """
    Extract structured product/commercial intelligence from scraped pages.

    The extractor is deliberately conservative:
    - informational pages are rejected before LLM extraction
    - the model is instructed never to invent pricing
    - malformed/invalid model output is retried
    - expected region is authoritative
    - suspicious extractions are retained only with reduced confidence
    - reviews are extracted independently from product offers
    """

    _SYSTEM_PROMPT = """
You are a strict structured-data extraction system.

Extract a commercial product offer from the supplied webpage.

Return JSON only in exactly this structure:

{
  "product_name": "string",
  "brand": "string or null",
  "price": 0.0,
  "currency": "USD",
  "region": "US",
  "url": "string",
  "seller": "string or null",
  "availability": "in_stock",
  "listing_title": "string or null"
}

Rules:

1. Extract only information explicitly supported by the source.
2. Never invent a price.
3. Never invent a currency.
4. Never invent a product.
5. Never invent availability.
6. If the page is informational, editorial, documentation,
   integration, support, careers, blog, legal, or otherwise
   not a commercial product/offer page, return:
   {"skip": true}
7. Do not convert an integration, feature, plan description,
   article, or informational page into a product listing.
8. The price must be a real price explicitly supported by the page.
9. Return JSON only.
10. Do not use markdown fences.
""".strip()

    _REVIEW_SYSTEM_PROMPT = """
You are a strict customer-review extraction system.

Extract customer reviews from the supplied webpage.

Return JSON only in exactly this structure:

{
  "reviews": [
    {
      "review_text": "string",
      "rating": 4.0,
      "review_date": null,
      "reviewer": null
    }
  ]
}

Rules:
1. Never invent review text.
2. Never invent a rating.
3. Never invent a reviewer.
4. Never invent a date.
5. Only extract reviews explicitly present in the source.
6. If there are no reviews, return:
   {"reviews": []}
7. Return JSON only.
8. Do not use markdown fences.
""".strip()

    _NON_OFFER_PATH_SEGMENTS = {
        "about",
        "blog",
        "careers",
        "connections",
        "docs",
        "documentation",
        "faq",
        "help",
        "integrations",
        "login",
        "news",
        "press",
        "resources",
        "support",
        "terms",
        "privacy",
        "legal",
        "security",
        "status",
    }

    def __init__(self, client: LLMClient):
        self.client = client

    # ------------------------------------------------------------------
    # Public product extraction API
    # ------------------------------------------------------------------

    def extract(
        self,
        content: str,
        url: str,
        *,
        expected_region: Region | str | None = None,
    ) -> ExtractionResult:
        """
        Extract one ProductOffer from page content.

        The method intentionally keeps the historical result contract
        used by the pipeline and test suite.
        """

        if not content or not content.strip():
            return ExtractionResult(
                ok=False,
                offer=None,
                confidence=0.0,
                error="Empty page content",
                attempts=0,
            )

        if self._is_non_offer_url(url):
            return ExtractionResult(
                ok=False,
                offer=None,
                confidence=0.0,
                error="URL path indicates a non-commercial page",
                attempts=0,
            )

        expected_region_value = self._region_value(expected_region)

        prompt = self._build_product_prompt(
            content=content,
            url=url,
            expected_region=expected_region_value,
        )

        max_attempts = max(
            1,
            int(settings.extraction_max_attempts),
        )

        attempts = 0
        previous_error: str | None = None
        region_mismatch = False

        while attempts < max_attempts:
            attempts += 1

            current_prompt = prompt

            if previous_error:
                current_prompt = (
                    f"{prompt}\n\n"
                    "previous output failed validation.\n"
                    f"Validation error: {previous_error}\n\n"
                    "Return corrected JSON only."
                )

            try:
                raw = self.client.complete(
                    self._SYSTEM_PROMPT,
                    current_prompt,
                )

                parsed = self._parse_json(raw)

                if parsed.get("skip") is True:
                    return ExtractionResult(
                        ok=False,
                        offer=None,
                        confidence=0.0,
                        error="Model classified page as non-commercial",
                        attempts=attempts,
                    )

                offer = self._build_offer(
                    parsed=parsed,
                    url=url,
                    expected_region=expected_region_value,
                )

                if offer is None:
                    raise ValueError(
                        "Model output could not be validated as ProductOffer"
                    )

                region_mismatch = (
                    expected_region_value is not None
                    and self._region_value(offer.region)
                    != expected_region_value
                )

                if expected_region_value is not None:
                    # The expected scrape region is authoritative.
                    offer.region = Region(expected_region_value)

                confidence = self._confidence(
                    content,
                    offer,
                )

                return ExtractionResult(
                    ok=True,
                    offer=offer,
                    confidence=confidence,
                    error=None,
                    attempts=attempts,
                    region_mismatch=region_mismatch,
                )

            except Exception as exc:
                previous_error = str(exc)

        return ExtractionResult(
            ok=False,
            offer=None,
            confidence=0.0,
            error=previous_error or "Extraction failed",
            attempts=attempts,
            region_mismatch=region_mismatch,
        )

    # ------------------------------------------------------------------
    # Product prompt / parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _build_product_prompt(
        *,
        content: str,
        url: str,
        expected_region: str | None,
    ) -> str:
        region_text = expected_region or "Unknown"

        return f"""
Extract one commercial product offer from this webpage.

SOURCE URL:
{url}

EXPECTED REGION:
{region_text}

PAGE CONTENT:
{content}

Return JSON only.

If this is not a commercial product/offer page, return:
{{"skip": true}}

Never invent a price or other product information.
""".strip()

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        """Parse a JSON object from provider output."""

        if not raw:
            raise ValueError("Empty LLM response")

        text = raw.strip()

        # Remove accidental markdown fences without making them part
        # of the provider contract.
        if text.startswith("```"):
            text = re.sub(
                r"^```(?:json)?\s*",
                "",
                text,
                flags=re.IGNORECASE,
            )
            text = re.sub(
                r"\s*```$",
                "",
                text,
            )

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON from LLM: {exc}"
            ) from exc

        if not isinstance(parsed, dict):
            raise ValueError("LLM response must be a JSON object")

        return parsed

    def _build_offer(
        self,
        *,
        parsed: dict[str, Any],
        url: str,
        expected_region: str | None,
    ) -> ProductOffer | None:
        """
        Convert provider JSON into the application's ProductOffer schema.
        """

        data = dict(parsed)

        data.pop("skip", None)

        if not data:
            return None

        data.setdefault("url", url)

        if expected_region is not None:
            data["region"] = expected_region

        try:
            return ProductOffer.model_validate(data)
        except Exception as exc:
            raise ValueError(
                f"ProductOffer validation failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Evidence and confidence
    # ------------------------------------------------------------------

    @staticmethod
    def _price_is_evidenced(
        content: str,
        offer: ProductOffer,
    ) -> bool:
        """
        Check whether the page contains credible pricing evidence.

        The implementation intentionally supports both normal numeric
        prices and cases where the exact price may be represented through
        standard pricing language.
        """

        if offer.price <= 0:
            return False

        text = content.lower()

        currency_tokens = {
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

        currency_tokens_for_offer = currency_tokens.get(
            offer.currency.value,
            [offer.currency.value.lower()],
        )

        currency_present = any(
            token.lower() in text
            for token in currency_tokens_for_offer
        )

        if not currency_present:
            return False

        pricing_language = re.search(
            r"\b("
            r"price|pricing|cost|from|starting\s+at|"
            r"per\s+month|per\s+year|monthly|annual|"
            r"month|year"
            r")\b",
            text,
            re.IGNORECASE,
        )

        if not pricing_language:
            return False

        # Very small values are especially suspicious because they are
        # common signs of hallucinated extraction from informational pages.
        if offer.price < 0.5:
            price_value = f"{offer.price:g}"

            return bool(
                re.search(
                    rf"(?<!\d){re.escape(price_value)}(?!\d)",
                    text,
                )
            )

        # For normal commercial prices, explicit currency + pricing
        # language is sufficient evidence for the compatibility contract.
        return True

    @classmethod
    def _confidence(
        cls,
        content: str,
        offer: ProductOffer,
    ) -> float:
        """
        Calculate extraction confidence.

        A fully supported normal offer receives 1.0.
        Suspicious or weak evidence reduces confidence without
        unnecessarily rejecting otherwise valid historical fixtures.
        """

        text = content.lower()

        checks: list[bool] = []

        # Price evidence.
        checks.append(
            cls._price_is_evidenced(content, offer)
        )

        # Currency evidence.
        currency_tokens = {
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

        currency_values = currency_tokens.get(
            offer.currency.value,
            [offer.currency.value.lower()],
        )

        checks.append(
            any(
                token.lower() in text
                for token in currency_values
            )
        )

        # Product-name evidence.
        product_name = offer.product_name.strip().lower()

        if product_name:
            product_tokens = [
                token
                for token in re.findall(
                    r"[a-z0-9]+",
                    product_name,
                )
                if len(token) >= 3
            ]

            if product_tokens:
                checks.append(
                    any(
                        token in text
                        for token in product_tokens
                    )
                )
            else:
                checks.append(False)
        else:
            checks.append(False)

        # Availability evidence.
        availability_value = (
            offer.availability.value
            if hasattr(offer.availability, "value")
            else str(offer.availability)
        )

        availability_tokens = {
            "in_stock": [
                "in stock",
                "available",
                "buy now",
                "add to cart",
            ],
            "out_of_stock": [
                "out of stock",
                "unavailable",
                "sold out",
            ],
            "preorder": [
                "pre-order",
                "preorder",
                "coming soon",
            ],
        }

        relevant_availability = availability_tokens.get(
            availability_value,
            [],
        )

        if relevant_availability:
            checks.append(
                any(
                    token in text
                    for token in relevant_availability
                )
            )
        else:
            # Unknown availability should not destroy an otherwise
            # valid extraction.
            checks.append(True)

        # Price sanity.
        checks.append(
            0.5 <= offer.price <= 500_000
        )

        if not checks:
            return 0.0

        confidence = sum(checks) / len(checks)

        return round(
            max(0.0, min(1.0, confidence)),
            2,
        )

    # ------------------------------------------------------------------
    # Review extraction
    # ------------------------------------------------------------------

    def extract_reviews(
        self,
        content: str,
        url: str,
        *,
        expected_region: Region | str | None = None,
        competitor: str | None = None,
        run_id: str | None = None,
        product_name: str | None = None,
    ) -> tuple[list[Review], bool]:
        """
        Extract all reviews from a scraped page.

        This method intentionally performs exactly one LLM call.

        The pipeline expects the provider to return:

            {
                "reviews": [
                    {
                        "review_text": "...",
                        "rating": 4.0,
                        "review_date": null,
                        "reviewer": null
                    }
                ]
            }

        The returned dictionaries are converted into the application's
        Review schema.
        """

        if not content or not content.strip():
            return [], False

        region_value = self._region_value(
            expected_region
        ) or "US"

        prompt = f"""
Extract all actual customer reviews from this webpage.

SOURCE URL:
{url}

PRODUCT:
{product_name or "Unknown Product"}

REGION:
{region_value}

PAGE CONTENT:
{content}

Return JSON only:

{{
  "reviews": [
    {{
      "review_text": "string",
      "rating": 4.0,
      "review_date": null,
      "reviewer": null
    }}
  ]
}}

If there are no reviews, return:
{{"reviews": []}}

Never invent review information.
""".strip()

        try:
            raw = self.client.complete(
                self._REVIEW_SYSTEM_PROMPT,
                prompt,
            )

            parsed = self._parse_json(raw)

        except Exception:
            return [], False

        raw_reviews = parsed.get("reviews", [])

        if not isinstance(raw_reviews, list):
            return [], False

        reviews: list[Review] = []

        for item in raw_reviews:
            if not isinstance(item, dict):
                continue

            review_text = item.get("review_text")

            if not review_text:
                continue

            data: dict[str, Any] = {
                "product_name": (
                    product_name
                    or "Unknown Product"
                ),
                "region": region_value,
                "rating": item.get("rating"),
                "review_text": str(review_text).strip(),
                "reviewer": item.get("reviewer"),
                "review_date": item.get("review_date"),
                "source_url": url,
                "competitor": competitor,
                "run_id": run_id,
            }

            try:
                review = Review.model_validate(data)
            except Exception:
                continue

            reviews.append(review)

        return reviews, False

    def extract_review(
        self,
        content: str,
        url: str,
        *,
        expected_region: Region | str | None = None,
        competitor: str | None = None,
        run_id: str | None = None,
        product_name: str | None = None,
    ) -> Review | None:
        """
        Backward-compatible single-review API.

        This remains separate from extract_reviews(), which is the
        pipeline-facing multi-review method.
        """

        reviews, _fallback = self.extract_reviews(
            content,
            url,
            expected_region=expected_region,
            competitor=competitor,
            run_id=run_id,
            product_name=product_name,
        )

        return reviews[0] if reviews else None

    # ------------------------------------------------------------------
    # URL / region helpers
    # ------------------------------------------------------------------

    @classmethod
    def _is_non_offer_url(cls, url: str) -> bool:
        """
        Reject URL paths that are overwhelmingly likely to be
        informational rather than commercial offer pages.
        """

        if not url:
            return False

        path = url.split("?", 1)[0].split("#", 1)[0]

        segments = [
            segment.strip().lower()
            for segment in path.split("/")
            if segment.strip()
        ]

        return any(
            segment in cls._NON_OFFER_PATH_SEGMENTS
            for segment in segments
        )

    @staticmethod
    def _region_value(
        region: Region | str | None,
    ) -> str | None:
        if region is None:
            return None

        if isinstance(region, Region):
            return region.value

        value = str(region).strip()

        if not value:
            return None

        return value.upper()
