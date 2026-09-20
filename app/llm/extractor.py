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
    attempts: int = 1
    region_mismatch: bool = False


class StructuredExtractor:
    """
    Structured extraction for product offers and reviews.

    Responsibilities:
    - reject obvious non-commercial pages
    - extract structured product offers
    - validate LLM output with Pydantic
    - retry malformed/invalid outputs
    - track extraction attempts
    - track region mismatches
    - require evidence for extracted prices
    - preserve compatibility with the existing pipeline
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
    # Product extraction
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

        # Reject obvious informational pages before spending an LLM call.
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

        max_attempts = max(
            1,
            int(settings.extraction_max_attempts),
        )

        last_raw: dict[str, Any] | None = None
        last_error = "Extraction failed."

        while attempts < max_attempts:
            attempts += 1

            if attempts == 1:
                current_prompt = prompt
            else:
                current_prompt = self._build_retry_prompt(
                    original_prompt=prompt,
                    previous_output=(
                        last_raw
                        if last_raw is not None
                        else {"raw": ""}
                    ),
                    validation_error=last_error,
                )

            try:
                raw_response = self.client.complete(
                    self._SYSTEM_PROMPT,
                    current_prompt,
                )
            except Exception as exc:
                last_error = f"LLM extraction failed: {exc}"

                if attempts >= max_attempts:
                    return ExtractionResult(
                        ok=False,
                        error=last_error,
                        attempts=attempts,
                    )

                continue

            try:
                parsed = self._parse_json(raw_response)
            except Exception as exc:
                last_error = str(exc)
                last_raw = None

                if attempts >= max_attempts:
                    return ExtractionResult(
                        ok=False,
                        error=(
                            "LLM returned invalid JSON after "
                            f"{attempts} attempts: {exc}"
                        ),
                        attempts=attempts,
                    )

                continue

            last_raw = parsed

            if parsed.get("skip") is True:
                return ExtractionResult(
                    ok=False,
                    error="LLM determined that the page is not a valid offer.",
                    raw=parsed,
                    attempts=attempts,
                )

            region_mismatch = self._region_mismatch(
                parsed,
                effective_region_value,
            )

            try:
                offer = self._validate_offer(
                    parsed,
                    url=url,
                    expected_region=effective_region_value,
                    run_id=run_id,
                )
            except Exception as exc:
                last_error = str(exc)

                if attempts >= max_attempts:
                    return ExtractionResult(
                        ok=False,
                        error=(
                            "Structured extraction failed validation "
                            f"after {attempts} attempts: {exc}"
                        ),
                        raw=parsed,
                        attempts=attempts,
                        region_mismatch=region_mismatch,
                    )

                continue

            if not self._price_is_evidenced(
                content,
                offer,
            ):
                last_error = (
                    "Extracted price is not sufficiently supported "
                    "by source content."
                )

                if attempts >= max_attempts:
                    return ExtractionResult(
                        ok=False,
                        error=last_error,
                        raw=parsed,
                        attempts=attempts,
                        region_mismatch=region_mismatch,
                    )

                continue

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

        return ExtractionResult(
            ok=False,
            error=last_error,
            raw=last_raw,
            attempts=attempts,
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
        Pipeline-compatible review extraction API.

        Returns:
            reviews, fallback_used
        """

        result = self.extract_review(
            content,
            url,
        )

        if not result.ok or result.review is None:
            return [], False

        review = result.review
        updates: dict[str, Any] = {}

        # Only add fields if they actually exist in the Review schema.
        fields = getattr(
            review,
            "model_fields",
            {},
        )

        if competitor is not None and "competitor" in fields:
            updates["competitor"] = competitor

        if run_id is not None and "run_id" in fields:
            updates["run_id"] = run_id

        if product_name is not None and "product_name" in fields:
            updates["product_name"] = product_name

        if expected_region is not None and "region" in fields:
            region_value = (
                expected_region.value
                if isinstance(expected_region, Region)
                else str(expected_region)
            )
            updates["region"] = region_value

        if updates:
            review = review.model_copy(
                update=updates,
            )

        return [review], False

    def extract_review(
        self,
        content: str,
        url: str,
    ) -> ExtractionResult:
        """
        Extract a single review.

        The demo provider is intentionally allowed to return the
        deterministic review fixture used by the existing pipeline.
        """

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

        max_attempts = max(
            1,
            int(settings.extraction_max_attempts),
        )

        last_error = "Review extraction failed."
        last_raw: dict[str, Any] | None = None

        while attempts < max_attempts:
            attempts += 1

            if attempts == 1:
                current_prompt = prompt
            else:
                current_prompt = self._build_review_retry_prompt(
                    original_prompt=prompt,
                    previous_output=(
                        last_raw
                        if last_raw is not None
                        else {"raw": ""}
                    ),
                    validation_error=last_error,
                )

            try:
                raw = self.client.complete(
                    self._REVIEW_SYSTEM_PROMPT,
                    current_prompt,
                )
            except Exception as exc:
                last_error = (
                    f"LLM review extraction failed: {exc}"
                )

                if attempts >= max_attempts:
                    return ExtractionResult(
                        ok=False,
                        error=last_error,
                        attempts=attempts,
                    )

                continue

            try:
                parsed = self._parse_json(raw)
            except Exception as exc:
                last_error = str(exc)
                last_raw = None

                if attempts >= max_attempts:
                    return ExtractionResult(
                        ok=False,
                        error=(
                            "LLM review response was not valid JSON "
                            f"after {attempts} attempts: {exc}"
                        ),
                        attempts=attempts,
                    )

                continue

            last_raw = parsed

            if parsed.get("skip") is True:
                return ExtractionResult(
                    ok=False,
                    error="No valid review found.",
                    raw=parsed,
                    attempts=attempts,
                )

            try:
                review = Review.model_validate(
                    parsed,
                )
            except Exception as exc:
                last_error = str(exc)

                if attempts >= max_attempts:
                    return ExtractionResult(
                        ok=False,
                        error=(
                            "Review extraction failed validation "
                            f"after {attempts} attempts: {exc}"
                        ),
                        raw=parsed,
                        attempts=attempts,
                    )

                continue

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

        return ExtractionResult(
            ok=False,
            error=last_error,
            raw=last_raw,
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
        try:
            path = (
                url.split("?", 1)[0]
                .split("#", 1)[0]
            )
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
                    match.group(0),
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
    # Region handling
    # ------------------------------------------------------------------

    @staticmethod
    def _region_mismatch(
        parsed: dict[str, Any],
        expected_region: str | None,
    ) -> bool:
        if expected_region is None:
            return False

        returned_region = parsed.get("region")

        if returned_region is None:
            return False

        returned_region_value = (
            returned_region.value
            if isinstance(returned_region, Region)
            else str(returned_region)
        )

        return (
            returned_region_value
            != expected_region
        )

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

        # The scraper URL is authoritative.
        cleaned["url"] = url

        # The execution region is authoritative.
        if expected_region:
            cleaned["region"] = expected_region

        if run_id:
            cleaned["run_id"] = run_id

        cleaned.pop(
            "skip",
            None,
        )

        return ProductOffer.model_validate(
            cleaned,
        )

    # ------------------------------------------------------------------
    # Price evidence
    # ------------------------------------------------------------------

    @staticmethod
    def _price_is_evidenced(
        content: str,
        offer: ProductOffer,
    ) -> bool:
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

        # Suspiciously tiny prices require exact evidence.
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
    # Offer confidence
    # ------------------------------------------------------------------

    @staticmethod
    def _confidence(
        content: str,
        offer: ProductOffer,
    ) -> float:
        text = content.lower()

        price_supported = (
            StructuredExtractor._price_is_evidenced(
                content,
                offer,
            )
        )

        currency_symbols = {
            "USD": ["$"],
            "EUR": ["€"],
            "GBP": ["£"],
            "INR": ["₹"],
            "JPY": ["¥"],
            "CAD": ["cad"],
            "AUD": ["aud"],
            "SGD": ["sgd"],
            "BRL": ["r$"],
        }

        currency_present = (
            offer.currency.value.lower() in text
            or any(
                symbol.lower() in text
                for symbol in currency_symbols.get(
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
