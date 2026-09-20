from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.schemas import ProductOffer, Review, Region


@dataclass
class ExtractionResult:
    ok: bool
    offer: ProductOffer | None = None
    review: Review | None = None
    confidence: float = 0.0
    error: str | None = None
    raw: dict[str, Any] | None = None

    # Extraction telemetry.
    attempts: int = 1

    # True when the LLM returned a different region from the
    # region requested by the pipeline.
    region_mismatch: bool = False


class StructuredExtractor:
    """
    Extract structured product offers and reviews from scraped content.

    Design goals:
    - Reject obvious non-commercial pages before LLM extraction.
    - Never silently invent prices.
    - Preserve existing pipeline/test compatibility.
    - Validate structured output through Pydantic.
    - Track extraction attempts.
    - Track region mismatches.
    - Produce deterministic evidence-backed confidence.
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
actually contains product or plan pricing information.

Rules:
1. Never invent a price.
2. Never infer a price from unrelated numbers.
3. Never use arbitrary placeholder values such as 0.01.
4. Informational pages, documentation, blogs, integrations,
   careers, support, legal, privacy, or company-information pages
   are NOT product offers.
5. If no real commercial price is present, return {"skip": true}.
6. Use only information explicitly supported by the supplied page.
7. Preserve the supplied source URL.
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
        expected_region: Region | str | None = None,
        run_id: str | None = None,
    ) -> ExtractionResult:
        """
        Extract a ProductOffer.

        `expected_region` is retained for compatibility with the
        existing orchestrator and test suite.

        If expected_region is supplied, it is authoritative over
        whatever region the LLM returns.
        """

        attempts = 0

        effective_region = (
            expected_region
            if expected_region is not None
            else region
        )

        if isinstance(effective_region, Region):
            effective_region_value = effective_region.value
        elif effective_region is not None:
            effective_region_value = str(effective_region)
        else:
            effective_region_value = None

        # --------------------------------------------------------------
        # URL-level filtering
        # --------------------------------------------------------------

        if self._is_non_offer_url(url):
            return ExtractionResult(
                ok=False,
                error="Page is not a commercial product/pricing page.",
                attempts=attempts,
            )

        if not content or not content.strip():
            return ExtractionResult(
                ok=False,
                error="Empty page content.",
                attempts=attempts,
            )

        prompt = self._build_offer_prompt(
            content=content,
            url=url,
            region=effective_region_value,
        )

        # --------------------------------------------------------------
        # First LLM attempt
        # --------------------------------------------------------------

        attempts += 1

        try:
            first_raw = self.client.complete(
                self._SYSTEM_PROMPT,
                prompt,
            )
        except Exception as exc:
            return ExtractionResult(
                ok=False,
                error=f"LLM extraction failed: {exc}",
                attempts=attempts,
            )

        # --------------------------------------------------------------
        # Parse first response
        # --------------------------------------------------------------

        try:
            parsed = self._parse_json(first_raw)
        except Exception as first_parse_error:
            retry_prompt = self._build_retry_prompt(
                original_prompt=prompt,
                previous_output={"raw": str(first_raw)},
                validation_error=str(first_parse_error),
            )

            attempts += 1

            try:
                retry_raw = self.client.complete(
                    self._SYSTEM_PROMPT,
                    retry_prompt,
                )
            except Exception as retry_exc:
                return ExtractionResult(
                    ok=False,
                    error=(
                        "Initial extraction returned invalid JSON and "
                        f"corrective retry failed: {retry_exc}"
                    ),
                    attempts=attempts,
                )

            try:
                parsed = self._parse_json(retry_raw)
            except Exception as retry_parse_error:
                return ExtractionResult(
                    ok=False,
                    error=(
                        "LLM returned invalid JSON after corrective retry: "
                        f"{retry_parse_error}"
                    ),
                    attempts=attempts,
                )

        # --------------------------------------------------------------
        # Explicit skip
        # --------------------------------------------------------------

        if parsed.get("skip") is True:
            return ExtractionResult(
                ok=False,
                error="LLM determined that the page is not a valid offer.",
                raw=parsed,
                attempts=attempts,
            )

        # --------------------------------------------------------------
        # Region mismatch detection
        # --------------------------------------------------------------

        region_mismatch = False

        if effective_region_value is not None:
            returned_region = parsed.get("region")

            if returned_region is not None:
                returned_region_value = (
                    returned_region.value
                    if isinstance(returned_region, Region)
                    else str(returned_region)
                )

                region_mismatch = (
                    returned_region_value
                    != effective_region_value
                )

        # --------------------------------------------------------------
        # Pydantic validation
        # --------------------------------------------------------------

        try:
            offer = self._validate_offer(
                parsed,
                url=url,
                expected_region=effective_region_value,
                run_id=run_id,
            )

        except Exception as first_error:
            retry_prompt = self._build_retry_prompt(
                original_prompt=prompt,
                previous_output=parsed,
                validation_error=str(first_error),
            )

            attempts += 1

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
                    attempts=attempts,
                    region_mismatch=region_mismatch,
                )

            try:
                retry_parsed = self._parse_json(retry_raw)
            except Exception as retry_parse_error:
                return ExtractionResult(
                    ok=False,
                    error=(
                        "Corrective retry returned invalid JSON: "
                        f"{retry_parse_error}"
                    ),
                    raw=parsed,
                    attempts=attempts,
                    region_mismatch=region_mismatch,
                )

            if retry_parsed.get("skip") is True:
                return ExtractionResult(
                    ok=False,
                    error=(
                        "Corrective extraction determined the page "
                        "is not an offer."
                    ),
                    raw=retry_parsed,
                    attempts=attempts,
                    region_mismatch=region_mismatch,
                )

            # Recalculate region mismatch using the corrected output.
            if effective_region_value is not None:
                returned_region = retry_parsed.get("region")

                if returned_region is not None:
                    returned_region_value = (
                        returned_region.value
                        if isinstance(returned_region, Region)
                        else str(returned_region)
                    )

                    region_mismatch = (
                        returned_region_value
                        != effective_region_value
                    )

            try:
                offer = self._validate_offer(
                    retry_parsed,
                    url=url,
                    expected_region=effective_region_value,
                    run_id=run_id,
                )

                parsed = retry_parsed

            except Exception as retry_error:
                return ExtractionResult(
                    ok=False,
                    error=(
                        "Structured extraction failed validation "
                        f"after retry: {retry_error}"
                    ),
                    raw=retry_parsed,
                    attempts=attempts,
                    region_mismatch=region_mismatch,
                )

        # --------------------------------------------------------------
        # Price evidence guard
        # --------------------------------------------------------------

        if not self._price_is_evidenced(content, offer):
            return ExtractionResult(
                ok=False,
                error=(
                    "Extracted price is not sufficiently supported "
                    "by source content."
                ),
                raw=parsed,
                attempts=attempts,
                region_mismatch=region_mismatch,
            )

        # --------------------------------------------------------------
        # Confidence
        # --------------------------------------------------------------

        confidence = self._confidence(
            content,
            offer,
        )

        final_offer = offer.model_copy(
            update={
                "extraction_confidence": confidence,
            }
        )

        return ExtractionResult(
            ok=True,
            offer=final_offer,
            confidence=confidence,
            raw=parsed,
            attempts=attempts,
            region_mismatch=region_mismatch,
        )

    # ------------------------------------------------------------------
    # Review extraction
    # ------------------------------------------------------------------

    def extract_review(
        self,
        content: str,
        url: str,
    ) -> ExtractionResult:
        """Extract and validate a customer review."""

        attempts = 0

        if not content or not content.strip():
            return ExtractionResult(
                ok=False,
                error="Empty review content.",
                attempts=attempts,
            )

        prompt = self._build_review_prompt(
            content=content,
            url=url,
        )

        attempts += 1

        try:
            raw = self.client.complete(
                self._REVIEW_SYSTEM_PROMPT,
                prompt,
            )
        except Exception as exc:
            return ExtractionResult(
                ok=False,
                error=f"LLM review extraction failed: {exc}",
                attempts=attempts,
            )

        try:
            parsed = self._parse_json(raw)
        except Exception as exc:
            return ExtractionResult(
                ok=False,
                error=(
                    f"LLM review response was not valid JSON: {exc}"
                ),
                attempts=attempts,
            )

        if parsed.get("skip") is True:
            return ExtractionResult(
                ok=False,
                error="No valid review found.",
                raw=parsed,
                attempts=attempts,
            )

        try:
            review = Review.model_validate(parsed)

        except Exception as first_error:
            retry_prompt = self._build_review_retry_prompt(
                original_prompt=prompt,
                previous_output=parsed,
                validation_error=str(first_error),
            )

            attempts += 1

            try:
                retry_raw = self.client.complete(
                    self._REVIEW_SYSTEM_PROMPT,
                    retry_prompt,
                )
            except Exception as retry_exc:
                return ExtractionResult(
                    ok=False,
                    error=(
                        "Review validation failed and retry failed: "
                        f"{retry_exc}"
                    ),
                    raw=parsed,
                    attempts=attempts,
                )

            try:
                retry_parsed = self._parse_json(retry_raw)
            except Exception as retry_parse_error:
                return ExtractionResult(
                    ok=False,
                    error=(
                        "Review corrective retry returned invalid JSON: "
                        f"{retry_parse_error}"
                    ),
                    raw=parsed,
                    attempts=attempts,
                )

            if retry_parsed.get("skip") is True:
                return ExtractionResult(
                    ok=False,
                    error="No valid review found after retry.",
                    raw=retry_parsed,
                    attempts=attempts,
                )

            try:
                review = Review.model_validate(
                    retry_parsed
                )
                parsed = retry_parsed

            except Exception as retry_error:
                return ExtractionResult(
                    ok=False,
                    error=(
                        "Review extraction failed validation: "
                        f"{retry_error}"
                    ),
                    raw=retry_parsed,
                    attempts=attempts,
                )

        return ExtractionResult(
            ok=True,
            review=review,
            confidence=self._review_confidence(
                content,
                review,
            ),
            raw=parsed,
            attempts=attempts,
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
- A feature page, integration page, documentation page,
  blog post, support page, or company-information page
  is NOT an offer.
- Do not manufacture a price.
- Do not convert arbitrary numbers into prices.
- If no real commercial price is explicitly supported,
  return {{"skip": true}}.
- If a price is present, identify its currency from the page.
- Return JSON only.
""".strip()

    @staticmethod
    def _build_retry_prompt(
        original_prompt: str,
        previous_output: dict[str, Any],
        validation_error: str,
    ) -> str:
        """
        Corrective extraction prompt.

        The phrase "previous output failed" is intentionally preserved
        for compatibility with the existing test suite.
        """

        return f"""
The previous output failed validation.

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
The previous output failed review validation.

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
    def _is_non_offer_url(
        cls,
        url: str,
    ) -> bool:
        """
        Reject obvious informational URLs before spending an LLM call.
        """

        try:
            path = url.split(
                "?",
                1,
            )[0].split(
                "#",
                1,
            )[0]
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
    # JSON parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(
        raw: Any,
    ) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw

        if raw is None:
            raise ValueError(
                "LLM returned no output."
            )

        text = str(raw).strip()

        if not text:
            raise ValueError(
                "LLM returned empty output."
            )

        # Remove markdown code fences.
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
            # Recover a JSON object embedded in provider commentary.
            match = re.search(
                r"\{.*\}",
                text,
                flags=re.DOTALL,
            )

            if not match:
                raise ValueError(
                    "LLM response was not valid JSON: "
                    f"{exc}"
                ) from exc

            try:
                parsed = json.loads(
                    match.group(0)
                )

            except json.JSONDecodeError as nested_exc:
                raise ValueError(
                    "LLM response was not valid JSON: "
                    f"{nested_exc}"
                ) from nested_exc

        if not isinstance(parsed, dict):
            raise ValueError(
                "LLM JSON response must be an object."
            )

        return parsed

    # ------------------------------------------------------------------
    # Product validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_offer(
        data: dict[str, Any],
        *,
        url: str,
        expected_region: str | None,
        run_id: str | None,
    ) -> ProductOffer:
        cleaned = dict(data)

        # Source URL is authoritative.
        cleaned["url"] = url

        # Pipeline execution region is authoritative.
        if expected_region:
            cleaned["region"] = expected_region

        if run_id:
            cleaned["run_id"] = run_id

        cleaned.pop(
            "skip",
            None,
        )

        return ProductOffer.model_validate(
            cleaned
        )

    # ------------------------------------------------------------------
    # Price evidence
    # ------------------------------------------------------------------

    @staticmethod
    def _price_is_evidenced(
        content: str,
        offer: ProductOffer,
    ) -> bool:
        """
        Require reasonable evidence for the extracted price.

        Normal positive prices can pass when the page clearly contains
        pricing language and the relevant currency.

        Very small values require exact numeric evidence.
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
            [
                offer.currency.value.lower()
            ],
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

        # Tiny values require exact source evidence.
        if offer.price < 0.5:
            price_value = f"{offer.price:g}"

            return bool(
                re.search(
                    rf"(?<!\d)"
                    rf"{re.escape(price_value)}"
                    rf"(?!\d)",
                    text,
                )
            )

        return True

    # ------------------------------------------------------------------
    # Confidence
    # ------------------------------------------------------------------

    @staticmethod
    def _confidence(
        content: str,
        offer: ProductOffer,
    ) -> float:
        """
        Deterministic evidence-backed confidence.

        A complete, internally consistent offer should score 1.0.
        """

        text = content.lower()

        price_supported = (
            StructuredExtractor._price_is_evidenced(
                content,
                offer,
            )
        )

        currency_present = (
            offer.currency.value.lower()
            in text
            or any(
                symbol.lower() in text
                for symbol in {
                    "USD": ["$"],
                    "EUR": ["€"],
                    "GBP": ["£"],
                    "INR": ["₹"],
                    "JPY": ["¥"],
                    "CAD": ["cad"],
                    "AUD": ["aud"],
                    "SGD": ["sgd"],
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
            or offer.availability.value in text
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
            )
            / len(checks),
            3,
        )

    # ------------------------------------------------------------------
    # Review confidence
    # ------------------------------------------------------------------

    @staticmethod
    def _review_confidence(
        content: str,
        review: Review,
    ) -> float:
        text = content.lower()

        body = getattr(
            review,
            "body",
            None,
        )

        title = getattr(
            review,
            "title",
            None,
        )

        rating = getattr(
            review,
            "rating",
            None,
        )

        checks = [
            bool(body),
            rating is not None or bool(title),
        ]

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
            )
            / len(checks),
            3,
        )
