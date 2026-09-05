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
            return json.dumps({"product_name": "Pro X Headphones", "price": 10.0,
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
