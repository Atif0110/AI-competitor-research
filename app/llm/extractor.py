from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.focus import product_identity_matches_source
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
    skipped: bool = False


class StructuredExtractor:
    """
    Extract structured commercial intelligence from scraped pages.

    Product extraction:
    - rejects obvious non-commercial URLs
    - asks the LLM for structured product data
    - validates the result against ProductOffer
    - retries malformed/invalid responses
    - stops immediately on provider quota/availability failures
    - preserves expected-region mismatch telemetry
    - makes expected_region authoritative
    - calculates extraction confidence

    Review extraction:
    - performs one LLM call per page
    - extracts all explicitly present reviews
    - converts them into the application's Review schema
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
8. The price must be explicitly supported by the page. Use 0 only for an explicitly free plan/tier, never for a free trial.
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
    # PRODUCT EXTRACTION
    # ------------------------------------------------------------------

    def extract(
        self,
        content: str,
        url: str,
        *,
        expected_region: Region | str | None = None,
        focus_products: tuple[str, ...] | list[str] = (),
    ) -> ExtractionResult:
        """
        Extract one ProductOffer from page content.
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
                error="Page type is not a product or pricing offer",
                attempts=0,
                skipped=True,
            )

        expected_region_value = self._region_value(
            expected_region
        )

        prompt = self._build_product_prompt(
            content=content,
            url=url,
            expected_region=expected_region_value,
            focus_products=focus_products,
        )

        # Keep extraction conservative because provider quotas are often
        # request-based. A single page should normally consume one LLM call.
        max_attempts = max(
            1,
            int(settings.extraction_max_attempts),
        )

        attempts = 0
        previous_error: str | None = None

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
                        error="No priced product offer was found on this page",
                        attempts=attempts,
                        skipped=True,
                    )

                offer = self._build_offer(
                    parsed=parsed,
                    url=url,
                )

                if offer is None:
                    raise ValueError(
                        "Model output could not be validated as ProductOffer"
                    )

                if not product_identity_matches_source(offer.product_name, url, content):
                    return ExtractionResult(
                        ok=False,
                        offer=None,
                        confidence=0.0,
                        error="Extracted product name is not supported by this page's URL or primary content",
                        attempts=attempts,
                        skipped=True,
                    )

                # IMPORTANT:
                # Compare the model's original region BEFORE changing it.
                model_region = self._region_value(
                    offer.region
                )

                region_mismatch = (
                    expected_region_value is not None
                    and model_region != expected_region_value
                )

                # The requested scrape region is authoritative.
                if expected_region_value is not None:
                    offer.region = Region(
                        expected_region_value
                    )

                if not self._price_is_evidenced(content, offer):
                    raise ValueError(
                        "Zero price was returned without explicit free-plan evidence"
                        if offer.price == 0
                        else "Extracted price is not supported by exact amount and currency evidence"
                    )

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

                error_text = previous_error.lower()

                # Provider quota/rate-limit failures must never be retried.
                # Retrying here would consume the same exhausted quota again.
                quota_error = (
                    "429" in error_text
                    or "resource_exhausted" in error_text
                    or "quota exceeded" in error_text
                    or "rate limit" in error_text
                    or "ratelimit" in error_text
                )

                # Provider availability failures should also stop retries.
                # The LLM client already handles configured provider fallback.
                provider_unavailable = (
                    "503" in error_text
                    or "service unavailable" in error_text
                    or "high demand" in error_text
                )

                if quota_error or provider_unavailable:
                    break

                # Validation/parsing failures can still use the configured
                # retry budget to correct malformed LLM output.
                if attempts < max_attempts:
                    continue

                break

        return ExtractionResult(
            ok=False,
            offer=None,
            confidence=0.0,
            error=previous_error or "Extraction failed",
            attempts=attempts,
            region_mismatch=False,
        )

    # ------------------------------------------------------------------
    # PRODUCT PROMPT
    # ------------------------------------------------------------------

    @staticmethod
    def _build_product_prompt(
        *,
        content: str,
        url: str,
        expected_region: str | None,
        focus_products: tuple[str, ...] | list[str] = (),
    ) -> str:
        region_text = expected_region or "Unknown"
        focus_text = ", ".join(str(value) for value in focus_products) or "None"

        content_budget = max(
            2000,
            min(int(settings.extraction_max_content_chars), 60000),
        )
        compact_content = StructuredExtractor._compact_content(
            content,
            content_budget,
        )

        return f"""
Extract one commercial product offer from this webpage.

SOURCE URL:
{url}

EXPECTED REGION:
{region_text}

REQUESTED PRODUCT OR CATEGORY:
{focus_text}

PAGE CONTENT (relevant excerpt):
{compact_content}

Return JSON only.

Extract the page's primary product offer, not a related/recommended product elsewhere in the page.
The exact numeric price and product identity must both be supported by this source.
When a requested product/category is provided, only return an offer in that category.

If this is not a commercial product/offer page, return:
{{"skip": true}}

Never invent a price or other product information.
""".strip()

    @staticmethod
    def _compact_content(content: str, max_chars: int) -> str:
        """Keep headings and commercial evidence while bounding prompt size."""
        if len(content) <= max_chars:
            return content

        lines = content.splitlines()
        price_evidence = re.compile(
            r"\$|£|€|₹|¥|\b(?:USD|EUR|GBP|INR|JPY|CAD|AUD|SGD|BRL)\b|"
            r"\b\d+(?:\.\d+)?\s*(?:/\s*(?:month|year)|per\s+(?:month|year))",
            re.IGNORECASE,
        )
        pricing_context = re.compile(
            r"\b(?:pricing|price|plans?|monthly|annual|free trial|starting at)\b",
            re.IGNORECASE,
        )
        primary: set[int] = set()
        for index, line in enumerate(lines):
            if price_evidence.search(line):
                primary.update(range(max(0, index - 1), min(len(lines), index + 2)))
        for index, line in enumerate(lines):
            if pricing_context.search(line):
                primary.update(range(index, min(len(lines), index + 2)))

        excerpt: list[str] = []
        size = 0
        ordered_indices = list(sorted(primary))
        ordered_indices.extend(index for index in range(min(16, len(lines))) if index not in primary)
        # Add a small amount of structure after the highest-value evidence.
        ordered_indices.extend(
            index for index, line in enumerate(lines)
            if index not in primary and line.lstrip().startswith("#")
        )
        selected: set[int] = set()
        for index in ordered_indices:
            line = lines[index].strip()
            if not line:
                continue
            if size + len(line) + 1 > max_chars:
                continue
            selected.add(index)
            excerpt.append(line)
            size += len(line) + 1

        if not excerpt:
            return content[:max_chars]
        return "\n".join(lines[index].strip() for index in sorted(selected))

    # ------------------------------------------------------------------
    # JSON PARSING
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        """
        Parse a JSON object from provider output.
        """

        if not raw:
            raise ValueError("Empty LLM response")

        text = raw.strip()

        # Gracefully handle accidental markdown fences.
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
            raise ValueError(
                "LLM response must be a JSON object"
            )

        return parsed

    # ------------------------------------------------------------------
    # PRODUCT VALIDATION
    # ------------------------------------------------------------------

    @staticmethod
    def _build_offer(
        *,
        parsed: dict[str, Any],
        url: str,
    ) -> ProductOffer | None:
        """
        Convert provider JSON into ProductOffer.
        """

        data = dict(parsed)

        data.pop("skip", None)

        if not data:
            return None

        data.setdefault("url", url)

        try:
            return ProductOffer.model_validate(data)
        except Exception as exc:
            raise ValueError(
                f"ProductOffer validation failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # PRICE EVIDENCE
    # ------------------------------------------------------------------

    @staticmethod
    def _price_is_evidenced(
        content: str,
        offer: ProductOffer,
    ) -> bool:
        """
        Check whether pricing is reasonably supported by the source.
        """

        if offer.price < 0:
            return False

        text = content.lower()

        if offer.price == 0:
            # A zero price is valid only for a clearly free plan/tier. A free
            # trial is a time-limited promotion, not a zero-priced plan.
            return bool(
                re.search(
                    r"\b(?:free\s+(?:plan|tier|version|forever)|forever\s+free|"
                    r"no\s+cost|at\s+no\s+charge)\b|"
                    r"(?:\$|€|£|₹|¥)\s*0(?:\.00)?\b",
                    text,
                    re.IGNORECASE,
                )
            )

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

        supported_tokens = currency_tokens.get(
            offer.currency.value,
            [offer.currency.value.lower()],
        )

        currency_present = any(
            token.lower() in text
            for token in supported_tokens
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

        # Require the model's exact amount to appear in the source. Currency
        # and pricing words elsewhere on a template are not sufficient evidence.
        try:
            from decimal import Decimal, InvalidOperation

            expected_amount = Decimal(str(offer.price)).quantize(Decimal("0.01"))
            amount_found = False
            for match in re.finditer(
                r"(?<![a-z0-9])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?![a-z0-9])",
                text,
                re.IGNORECASE,
            ):
                try:
                    observed = Decimal(match.group(0).replace(",", "")).quantize(Decimal("0.01"))
                except InvalidOperation:
                    continue
                if observed == expected_amount:
                    amount_found = True
                    break
            if not amount_found:
                return False
        except Exception:
            return False

        # Extremely small prices need exact numeric evidence.
        if offer.price < 0.5:
            price_value = f"{offer.price:g}"

            return bool(
                re.search(
                    rf"(?<!\d){re.escape(price_value)}(?!\d)",
                    text,
                )
            )

        # Normal commercial prices are accepted when the page
        # contains currency + pricing language.
        return True

    # ------------------------------------------------------------------
    # CONFIDENCE
    # ------------------------------------------------------------------

    @classmethod
    def _confidence(
        cls,
        content: str,
        offer: ProductOffer,
    ) -> float:
        """
        Calculate extraction confidence.

        Fully supported normal offers receive 1.0.
        Suspicious/weak evidence reduces confidence.
        """

        text = content.lower()

        checks: list[bool] = []

        # --------------------------------------------------------------
        # 1. Price evidence
        # --------------------------------------------------------------

        checks.append(
            cls._price_is_evidenced(
                content,
                offer,
            )
        )

        # --------------------------------------------------------------
        # 2. Currency evidence
        # --------------------------------------------------------------

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

        supported_currency_tokens = currency_tokens.get(
            offer.currency.value,
            [offer.currency.value.lower()],
        )

        checks.append(
            offer.price == 0
            or any(
                token.lower() in text
                for token in supported_currency_tokens
            )
        )

        # --------------------------------------------------------------
        # 3. Product name evidence
        # --------------------------------------------------------------

        product_name = offer.product_name.strip().lower()

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

        # --------------------------------------------------------------
        # 4. Availability evidence
        # --------------------------------------------------------------

        availability_value = (
            offer.availability.value
            if hasattr(
                offer.availability,
                "value",
            )
            else str(offer.availability)
        )

        availability_tokens = {
            "in_stock": [
                "in stock",
                "in_stock",
                "available",
                "buy now",
                "add to cart",
            ],
            "out_of_stock": [
                "out of stock",
                "out_of_stock",
                "unavailable",
                "sold out",
            ],
            "preorder": [
                "pre-order",
                "preorder",
                "coming soon",
            ],
            "unknown": [],
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
            # Unknown availability is not negative evidence.
            checks.append(True)

        # --------------------------------------------------------------
        # 5. Price sanity
        # --------------------------------------------------------------

        checks.append(
            offer.price == 0 or 0.5 <= offer.price <= 500_000
        )

        confidence = (
            sum(checks) / len(checks)
            if checks
            else 0.0
        )

        return round(
            max(
                0.0,
                min(
                    1.0,
                    confidence,
                ),
            ),
            2,
        )

    # ------------------------------------------------------------------
    # REVIEW EXTRACTION
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

        Exactly one LLM call is made for this operation.
        """

        if not content or not content.strip():
            return [], False

        # A review-model call on every pricing/product page is usually wasted.
        # Only run it where the page contains review evidence; keep demo mode's
        # deterministic fixture behavior intact.
        if getattr(self.client, "provider_name", "demo") != "demo" and not self._has_review_evidence(content, url):
            return [], False

        region_value = (
            self._region_value(
                expected_region
            )
            or "US"
        )

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

        raw_reviews = parsed.get(
            "reviews",
            [],
        )

        if not isinstance(
            raw_reviews,
            list,
        ):
            return [], False

        reviews: list[Review] = []

        for item in raw_reviews:
            if not isinstance(
                item,
                dict,
            ):
                continue

            review_text = item.get(
                "review_text"
            )

            if not review_text:
                continue

            data: dict[str, Any] = {
                "product_name": (
                    product_name
                    or "Unknown Product"
                ),
                "region": region_value,
                "rating": item.get(
                    "rating"
                ),
                "review_text": str(
                    review_text
                ).strip(),
                "reviewer": item.get(
                    "reviewer"
                ),
                "review_date": item.get(
                    "review_date"
                ),
                "source_url": url,
                "competitor": competitor,
                "run_id": run_id,
            }

            try:
                review = Review.model_validate(
                    data
                )
            except Exception:
                continue

            reviews.append(review)

        return reviews, False

    @staticmethod
    def _has_review_evidence(content: str, url: str) -> bool:
        if re.search(r"/(?:reviews?|testimonials?)(?:/|$)", url, re.IGNORECASE):
            return True
        return bool(
            re.search(
                r"(?:\b\d(?:\.\d)?\s*(?:/\s*5|out of 5|stars?)\b|"
                r"verified (?:customer|buyer)|what customers say|"
                r"customer testimonials|\u2605{2,}|\u2b50{2,})",
                content,
                re.IGNORECASE,
            )
        )

    # ------------------------------------------------------------------
    # BACKWARD-COMPATIBLE SINGLE REVIEW API
    # ------------------------------------------------------------------

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

        The pipeline-facing API is extract_reviews().
        """

        reviews, _fallback = self.extract_reviews(
            content,
            url,
            expected_region=expected_region,
            competitor=competitor,
            run_id=run_id,
            product_name=product_name,
        )

        return (
            reviews[0]
            if reviews
            else None
        )

    # ------------------------------------------------------------------
    # URL HELPERS
    # ------------------------------------------------------------------

    @classmethod
    def _is_non_offer_url(
        cls,
        url: str,
    ) -> bool:
        """
        Reject paths that clearly indicate informational pages.
        """

        if not url:
            return False

        path = (
            url
            .split("?", 1)[0]
            .split("#", 1)[0]
        )

        segments = [
            segment.strip().lower()
            for segment in path.split("/")
            if segment.strip()
        ]

        return any(
            segment in cls._NON_OFFER_PATH_SEGMENTS
            for segment in segments
        )

    # ------------------------------------------------------------------
    # REGION HELPERS
    # ------------------------------------------------------------------

    @staticmethod
    def _region_value(
        region: Region | str | None,
    ) -> str | None:
        if region is None:
            return None

        if isinstance(
            region,
            Region,
        ):
            return region.value

        value = str(
            region
        ).strip()

        if not value:
            return None

        return value.upper()
