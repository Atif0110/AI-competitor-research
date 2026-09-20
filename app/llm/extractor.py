from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.schemas import ProductOffer, Review


@dataclass
class ExtractionResult:
    ok: bool
    offer: ProductOffer | None = None
    review: Review | None = None
    confidence: float = 0.0
    error: str | None = None
    raw: dict[str, Any] | None = None


class StructuredExtractor:
    """
    Converts scraped page content into validated structured data.

    The extractor is intentionally conservative:
    - informational pages are rejected before LLM extraction
    - the LLM is instructed not to invent prices
    - Pydantic validation remains the final schema guard
    - confidence is deterministic and evidence-based
    """

    _NON_OFFER_PATH_SEGMENTS = {
        "about",
        "blog",
        "careers",
        "connections",
        "contact",
        "docs",
        "documentation",
        "faq",
        "help",
        "integrations",
        "login",
        "news",
        "press",
        "privacy",
        "resources",
        "security",
        "status",
        "support",
        "terms",
        "legal",
    }

    _SYSTEM_PROMPT = """
You are a strict structured-data extraction system.

Extract a commercial product offer ONLY when the supplied webpage
actually contains product/plan pricing information.

Rules:
1. Never invent a price.
2. Never infer a price from unrelated numbers.
3. Never use arbitrary placeholder values such as 0.01.
4. If the page is informational, documentation, blog, integration,
   careers, support, legal, privacy, or another non-commercial page,
   return {"skip": true}.
5. If no real price is present, return {"skip": true}.
6. Use only information explicitly supported by the supplied page.
7. Preserve the source URL exactly.
8. Return JSON only.
9. Do not include markdown fences.

Expected successful shape:
{
  "product_name": "string",
  "brand": "string or null",
  "price": 123.45,
  "currency": "USD",
  "region": "US",
  "url": "https://...",
  "seller": "string or null",
  "availability": "in_stock",
  "listing_title": "string or null"
}

If extraction is not justified:
{
  "skip": true
}
""".strip()

    _REVIEW_SYSTEM_PROMPT = """
You are a strict structured-data extraction system.

Extract a customer review only when the supplied content clearly
contains an actual review.

Never invent:
- reviewer names
- ratings
- dates
- review text
- products

Return JSON only.
Do not use markdown fences.

Expected shape:
{
  "reviewer": "string or null",
  "rating": 4.0,
  "title": "string or null",
  "body": "string",
  "date": "string or null"
}

If no actual review is present:
{
  "skip": true
}
""".strip()

    def __init__(self, client: Any):
        self.client = client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(
        self,
        content: str,
        url: str,
        *,
        region: str | None = None,
        run_id: str | None = None,
    ) -> ExtractionResult:
        """
        Extract and validate a ProductOffer.

        The method performs:
        1. URL-level rejection for clearly non-commercial pages.
        2. LLM extraction.
        3. Pydantic schema validation.
        4. Source-evidence checks.
        5. Deterministic confidence calculation.
        6. One corrective retry when the first response fails.
        """

        if self._is_non_offer_url(url):
            return ExtractionResult(
                ok=False,
                error="Page is not a commercial product/pricing page.",
            )

        if not content or not content.strip():
            return ExtractionResult(
                ok=False,
                error="Empty page content.",
            )

        prompt = self._build_offer_prompt(
            content=content,
            url=url,
            region=region,
        )

        try:
            first_raw = self.client.complete(
                self._SYSTEM_PROMPT,
                prompt,
            )
        except Exception as exc:
            return ExtractionResult(
                ok=False,
                error=f"LLM extraction failed: {exc}",
            )

        parsed = self._parse_json(first_raw)

        if parsed.get("skip") is True:
            return ExtractionResult(
                ok=False,
                error="LLM determined that the page is not a valid offer.",
                raw=parsed,
            )

        try:
            offer = self._validate_offer(
                parsed,
                url=url,
                region=region,
                run_id=run_id,
            )
        except Exception as first_error:
            retry_prompt = self._build_retry_prompt(
                original_prompt=prompt,
                previous_output=parsed,
                validation_error=str(first_error),
            )

            try:
                retry_raw = self.client.complete(
                    self._SYSTEM_PROMPT,
                    retry_prompt,
                )
            except Exception as exc:
                return ExtractionResult(
                    ok=False,
                    error=(
                        "Initial extraction failed validation and "
                        f"corrective retry failed: {exc}"
                    ),
                    raw=parsed,
                )

            retry_parsed = self._parse_json(retry_raw)

            if retry_parsed.get("skip") is True:
                return ExtractionResult(
                    ok=False,
                    error="Corrective extraction determined the page is not an offer.",
                    raw=retry_parsed,
                )

            try:
                offer = self._validate_offer(
                    retry_parsed,
                    url=url,
                    region=region,
                    run_id=run_id,
                )
                parsed = retry_parsed
            except Exception as retry_error:
                return ExtractionResult(
                    ok=False,
                    error=(
                        "Structured extraction failed validation after retry: "
                        f"{retry_error}"
                    ),
                    raw=retry_parsed,
                )

        # Final evidence guard.
        #
        # We intentionally do NOT reject every low-priced item here.
        # Existing legitimate test cases and some real products can have
        # prices below $0.50. Instead, tiny prices must have explicit
        # numeric evidence on the page.
        if not self._price_is_evidenced(content, offer):
            return ExtractionResult(
                ok=False,
                error="Extracted price is not sufficiently supported by source content.",
                raw=parsed,
            )

        confidence = self._confidence(content, offer)

        return ExtractionResult(
            ok=True,
            offer=offer.model_copy(
                update={
                    "extraction_confidence": confidence,
                }
            ),
            confidence=confidence,
            raw=parsed,
        )

    def extract_review(
        self,
        content: str,
        url: str,
    ) -> ExtractionResult:
        """
        Extract a Review from scraped content.

        Review extraction is intentionally separate from ProductOffer
        extraction because review pages do not necessarily contain
        commercial pricing.
        """

        if not content or not content.strip():
            return ExtractionResult(
                ok=False,
                error="Empty review content.",
            )

        prompt = self._build_review_prompt(
            content=content,
            url=url,
        )

        try:
            raw = self.client.complete(
                self._REVIEW_SYSTEM_PROMPT,
                prompt,
            )
        except Exception as exc:
            return ExtractionResult(
                ok=False,
                error=f"LLM review extraction failed: {exc}",
            )

        parsed = self._parse_json(raw)

        if parsed.get("skip") is True:
            return ExtractionResult(
                ok=False,
                error="No valid review found.",
                raw=parsed,
            )

        try:
            review = Review.model_validate(parsed)
        except Exception as exc:
            retry_prompt = self._build_review_retry_prompt(
                original_prompt=prompt,
                previous_output=parsed,
                validation_error=str(exc),
            )

            try:
                retry_raw = self.client.complete(
                    self._REVIEW_SYSTEM_PROMPT,
                    retry_prompt,
                )
            except Exception as retry_exc:
                return ExtractionResult(
                    ok=False,
                    error=f"Review validation failed and retry failed: {retry_exc}",
                    raw=parsed,
                )

            retry_parsed = self._parse_json(retry_raw)

            if retry_parsed.get("skip") is True:
                return ExtractionResult(
                    ok=False,
                    error="No valid review found after retry.",
                    raw=retry_parsed,
                )

            try:
                review = Review.model_validate(retry_parsed)
                parsed = retry_parsed
            except Exception as retry_error:
                return ExtractionResult(
                    ok=False,
                    error=f"Review extraction failed validation: {retry_error}",
                    raw=retry_parsed,
                )

        return ExtractionResult(
            ok=True,
            review=review,
            confidence=self._review_confidence(content, review),
            raw=parsed,
        )

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_offer_prompt(
        content: str,
        url: str,
        region: str | None,
    ) -> str:
        region_text = region or "unknown"

        return f"""
Extract a commercial product or plan offer from this webpage.

SOURCE URL:
{url}

EXPECTED REGION:
{region_text}

PAGE CONTENT:
{content}

Important:
- Only extract a real product or paid plan offer.
- A feature page, integration page, documentation page, blog post,
  support page, or company-information page is NOT an offer.
- Do not manufacture a price.
- Do not convert arbitrary numbers into prices.
- If no real commercial price is explicitly supported, return:
  {{"skip": true}}
- If a price is present, identify its currency from the page.
- Return JSON only.
""".strip()

    @staticmethod
    def _build_retry_prompt(
        original_prompt: str,
        previous_output: dict[str, Any],
        validation_error: str,
    ) -> str:
        return f"""
The previous extraction failed schema validation.

Previous output:
{json.dumps(previous_output, ensure_ascii=False)}

Validation error:
{validation_error}

Correct the extraction using ONLY evidence in the source.

Do not invent missing values.
Do not create a placeholder price.
If the page does not contain a valid commercial offer, return:
{{"skip": true}}

Original task:
{original_prompt}

Return JSON only.
""".strip()

    @staticmethod
    def _build_review_prompt(
        content: str,
        url: str,
    ) -> str:
        return f"""
Extract one actual customer review from this webpage.

SOURCE URL:
{url}

PAGE CONTENT:
{content}

Return JSON only.

If there is no actual review:
{{"skip": true}}
""".strip()

    @staticmethod
    def _build_review_retry_prompt(
        original_prompt: str,
        previous_output: dict[str, Any],
        validation_error: str,
    ) -> str:
        return f"""
The previous review extraction failed schema validation.

Previous output:
{json.dumps(previous_output, ensure_ascii=False)}

Validation error:
{validation_error}

Correct it using only the source content.

If a valid review cannot be supported, return:
{{"skip": true}}

Original task:
{original_prompt}

Return JSON only.
""".strip()

    # ------------------------------------------------------------------
    # URL filtering
    # ------------------------------------------------------------------

    @classmethod
    def _is_non_offer_url(cls, url: str) -> bool:
        """
        Reject obvious informational URLs before spending an LLM call.

        This specifically prevents pages such as:
        /connections/slack
        /docs/
        /careers/
        /blog/
        /support/
        from being interpreted as ecommerce ProductOffer records.
        """

        try:
            path = url.split("?", 1)[0].split("#", 1)[0]
        except Exception:
            path = url

        segments = [
            segment.lower()
            for segment in path.split("/")
            if segment.strip()
        ]

        return any(
            segment in cls._NON_OFFER_PATH_SEGMENTS
            for segment in segments
        )

    # ------------------------------------------------------------------
    # JSON handling
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw

        if raw is None:
            raise ValueError("LLM returned no output.")

        text = str(raw).strip()

        # Remove markdown fences if a provider ignores the JSON-only
        # instruction.
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
            ).strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            # Try to recover a JSON object embedded in surrounding text.
            match = re.search(
                r"\{.*\}",
                text,
                flags=re.DOTALL,
            )

            if not match:
                raise ValueError(
                    f"LLM response was not valid JSON: {exc}"
                ) from exc

            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError as nested_exc:
                raise ValueError(
                    f"LLM response was not valid JSON: {nested_exc}"
                ) from nested_exc

        if not isinstance(parsed, dict):
            raise ValueError("LLM JSON response must be an object.")

        return parsed

    # ------------------------------------------------------------------
    # Product validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_offer(
        data: dict[str, Any],
        *,
        url: str,
        region: str | None,
        run_id: str | None,
    ) -> ProductOffer:
        """
        Validate an extracted offer against the project's Pydantic schema.
        """

        cleaned = dict(data)

        # The source URL is authoritative. Never let the LLM substitute
        # a different URL.
        cleaned["url"] = url

        if region:
            cleaned["region"] = region

        if run_id:
            cleaned["run_id"] = run_id

        # Prevent the LLM from returning internal control fields into
        # ProductOffer.
        cleaned.pop("skip", None)

        return ProductOffer.model_validate(cleaned)

    # ------------------------------------------------------------------
    # Evidence / confidence
    # ------------------------------------------------------------------

    @staticmethod
    def _price_is_evidenced(
        content: str,
        offer: ProductOffer,
    ) -> bool:
        """
        Require reasonable source evidence for the extracted price.

        Normal positive prices can be accepted when the page clearly
        presents pricing information and the correct currency.

        Extremely small prices are stricter because values such as
        0.01 are frequently placeholder/hallucinated extraction values.
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
            r"price|pricing|cost|from|starting at|"
            r"per month|per year|monthly|annual|"
            r"subscription|plan|plans"
            r")\b",
            text,
            re.IGNORECASE,
        )

        if not pricing_language:
            return False

        # Tiny values require exact numeric evidence.
        if offer.price < 0.5:
            price_value = f"{offer.price:g}"

            return bool(
                re.search(
                    rf"(?<!\d){re.escape(price_value)}(?!\d)",
                    text,
                )
            )

        return True

    @staticmethod
    def _confidence(
        content: str,
        offer: ProductOffer,
    ) -> float:
        """
        Calculate deterministic evidence-backed confidence.

        Five independent checks are used:
        - price evidence
        - currency evidence
        - product-name evidence
        - availability evidence
        - price sanity
        """

        text = content.lower()

        price_supported = (
            StructuredExtractor._price_is_evidenced(
                content,
                offer,
            )
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

        price_is_sane = (
            0.5 <= offer.price <= 500_000
        )

        checks = [
            price_supported,
            currency_present,
            product_present,
            availability_supported,
            price_is_sane,
        ]

        return round(
            sum(
                1
                for check in checks
                if check
            ) / len(checks),
            3,
        )

    @staticmethod
    def _review_confidence(
        content: str,
        review: Review,
    ) -> float:
        """
        Conservative confidence score for review extraction.
        """

        text = content.lower()

        checks = [
            bool(getattr(review, "body", None)),
            bool(
                getattr(review, "rating", None) is not None
                or getattr(review, "title", None)
            ),
        ]

        body = getattr(review, "body", None)

        if body:
            body_tokens = [
                token
                for token in re.findall(
                    r"[a-z0-9]+",
                    str(body).lower(),
                )
                if len(token) >= 3
            ]

            checks.append(
                any(
                    token in text
                    for token in body_tokens[:8]
                )
            )

        return round(
            sum(
                1
                for check in checks
                if check
            ) / len(checks),
            3,
        )
