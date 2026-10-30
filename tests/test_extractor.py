"""Tests for the structured extractor — expected_region enforcement,
corrective retries, confidence, and demo-provider extraction."""
import json

import pytest

from app.config import settings
from app.llm.client import DemoClient, LLMClient
from app.llm.extractor import StructuredExtractor
from app.schemas import Region


def _demo_markdown():
    return """# Pro X Headphones
Brand: Acme Audio
Price: 499.00
Currency: USD
Region: US
Availability: in_stock
Seller: Acme Store
"""


def test_demo_provider_extracts_valid_offer():
    settings.llm_provider = "demo"
    client = LLMClient()
    extractor = StructuredExtractor(client)
    result = extractor.extract(_demo_markdown(), "https://acme.example.com/us/product/pro-x-headphones")
    assert result.ok
    assert result.offer.product_name == "Pro X Headphones"
    assert result.offer.price == 499.0
    assert result.offer.currency.value == "USD"
    assert result.offer.region.value == "US"


def test_provider_fallback_order_contains_demo_when_no_keys():
    old_keys = (settings.groq_api_key, settings.openai_api_key, settings.anthropic_api_key)
    settings.groq_api_key = settings.openai_api_key = settings.anthropic_api_key = None
    try:
        client = LLMClient()
        assert client.provider_name == "demo"
    finally:
        settings.groq_api_key, settings.openai_api_key, settings.anthropic_api_key = old_keys


def test_extractor_retries_and_flags_bad_output():
    class BadClient:
        def complete(self, system, user):
            return "not json at all"

    settings.llm_provider = "demo"
    extractor = StructuredExtractor(BadClient())
    result = extractor.extract(_demo_markdown(), "u")
    assert not result.ok
    assert result.offer is None
    assert result.attempts == settings.extraction_max_attempts
    assert "JSON" in (result.error or "") or "validation" in (result.error or "")


def test_extractor_rejects_missing_required_field():
    class PartialClient:
        def complete(self, system, user):
            return json.dumps({"price": 10.0})

    extractor = StructuredExtractor(PartialClient())
    result = extractor.extract(_demo_markdown(), "u")
    assert not result.ok


def test_expected_region_enforced():
    """LLM says US; pipeline requested IN -> IN wins, mismatch recorded."""
    settings.llm_provider = "demo"
    extractor = StructuredExtractor(LLMClient())
    result = extractor.extract(_demo_markdown(), "u", expected_region=Region.IN)
    assert result.ok
    assert result.offer.region == Region.IN
    assert result.region_mismatch is True


def test_expected_region_match_no_mismatch():
    settings.llm_provider = "demo"
    result = StructuredExtractor(LLMClient()).extract(_demo_markdown(), "u", expected_region=Region.US)
    assert result.ok
    assert result.region_mismatch is False


def test_corrective_retry_receives_validation_error():
    class QueueClient:
        def __init__(self):
            self.users = []

        def complete(self, system, user):
            self.users.append(user)
            if len(self.users) == 1:
                return json.dumps({"price": 10.0, "currency": "RUB"})  # invalid currency
            return json.dumps({"product_name": "Pro X Headphones", "price": 499.0,
                               "currency": "USD", "region": "US", "url": "u"})

    settings.llm_provider = "demo"
    extractor = StructuredExtractor(QueueClient())
    result = extractor.extract(_demo_markdown(), "u")
    assert result.ok
    assert "previous output failed" in extractor.client.users[1]


def test_confidence_full_marks_for_sane_offer():
    settings.llm_provider = "demo"
    result = StructuredExtractor(LLMClient()).extract(_demo_markdown(), "u")
    assert result.confidence == 1.0


def test_confidence_drops_for_suspicious_price():
    settings.llm_provider = "demo"
    result = StructuredExtractor(LLMClient()).extract(
        "# Pro X Headphones\nPrice: 0.01\nCurrency: USD\nRegion: US\n", "u")
    assert result.ok
    assert result.confidence < 1.0


def test_non_offer_page_is_reported_as_skip_without_calling_model():
    class NeverCalled:
        provider_name = "test"

        def complete(self, system, user):
            raise AssertionError("non-offer URL should be skipped before an LLM call")

    result = StructuredExtractor(NeverCalled()).extract(
        "# Product documentation", "https://example.com/docs/api"
    )
    assert result.skipped
    assert not result.ok
    assert result.attempts == 0


def test_model_classified_no_price_is_skip_not_extraction_error():
    class SkipClient:
        provider_name = "test"

        def complete(self, system, user):
            return '{"skip": true}'

    result = StructuredExtractor(SkipClient()).extract(
        "# Coda docs and team hubs\nA collaborative workspace.",
        "https://coda.io/product/docs-and-team-hubs",
    )
    assert result.skipped
    assert not result.ok
    assert result.attempts == 1
    assert "priced product offer" in result.error


def test_prompt_input_is_bounded_and_retains_pricing_evidence():
    content = "# Product\n\n" + ("A long feature description.\n" * 900)
    content += "\n## Pricing\nThe Pro plan costs $19 per month.\n"
    prompt = StructuredExtractor._build_product_prompt(
        content=content,
        url="https://example.com/pricing",
        expected_region="US",
    )
    excerpt = prompt.split("PAGE CONTENT (relevant excerpt):\n", 1)[1]
    assert len(excerpt) < settings.extraction_max_content_chars + 400
    assert "$19 per month" in excerpt


def test_explicit_free_plan_is_accepted_but_free_trial_is_not_zero_price():
    class FreeClient:
        provider_name = "test"

        def __init__(self, price):
            self.price = price

        def complete(self, system, user):
            return json.dumps({
                "product_name": "Free plan", "price": 0, "currency": "USD",
                "region": "US", "url": "https://example.com/pricing",
                "availability": "in_stock",
            })

    extractor = StructuredExtractor(FreeClient(0))
    explicit_free = extractor.extract(
        "# Plans\nThe Free plan is free forever.",
        "https://example.com/pricing",
    )
    assert explicit_free.ok
    assert explicit_free.offer.price == 0

    free_trial = extractor.extract(
        "# Plans\nThe Pro plan includes a free trial for 30 days.",
        "https://example.com/pricing",
    )
    assert not free_trial.ok
    assert "Zero price" in free_trial.error


def test_rejects_model_price_not_present_as_an_exact_amount():
    class WrongPriceClient:
        provider_name = "test"

        def complete(self, system, user):
            return json.dumps({
                "product_name": "Pro X Headphones", "price": 999, "currency": "INR",
                "region": "IN", "url": "https://shop.example/products/pro-x-headphones",
            })

    result = StructuredExtractor(WrongPriceClient()).extract(
        "# Pro X Headphones\nPrice: ₹899\nCurrency: INR\n",
        "https://shop.example/products/pro-x-headphones",
    )
    assert not result.ok
    assert "price" in (result.error or "").lower()


def test_rejects_recommended_product_misattributed_to_page_url():
    class RelatedProductClient:
        provider_name = "test"

        def complete(self, system, user):
            return json.dumps({
                "product_name": "EnergyShroom PB200 Power Bank", "price": 1199,
                "currency": "INR", "region": "IN",
                "url": "https://www.boat-lifestyle.com/products/aux-500",
            })

    result = StructuredExtractor(RelatedProductClient()).extract(
        "# boAt AUX 500\nRecommended: EnergyShroom PB200 Power Bank\n"
        "Price: ₹1,199\nCurrency: INR\nAdd to cart",
        "https://www.boat-lifestyle.com/products/aux-500",
        focus_products=["Airdopes"],
    )
    assert result.skipped
    assert not result.ok
    assert "product name is not supported" in (result.error or "")


def test_opaque_product_url_can_use_primary_page_heading_as_identity_evidence():
    from app.focus import product_identity_matches_source

    assert product_identity_matches_source(
        "Pro X Headphones",
        "https://shop.example/products/123456",
        "# Pro X Headphones\nPrice: $499.00",
    )
