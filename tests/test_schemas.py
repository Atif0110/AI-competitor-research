"""Unit tests for schemas — the validation contract."""
import pytest
from pydantic import ValidationError

from app.schemas import Availability, CompetitorTarget, Currency, ProductOffer, Region


def test_valid_offer():
    offer = ProductOffer(product_name="Pro X Headphones", price=499.0, currency="USD",
                         region="US", url="https://x.example/p")
    assert offer.currency is Currency.USD
    assert offer.availability is Availability.unknown
    assert offer.price == 499.0


def test_zero_price_rejected():
    with pytest.raises(ValidationError):
        ProductOffer(product_name="X", price=0, currency="USD", region="US", url="u")


def test_negative_price_rejected():
    with pytest.raises(ValidationError):
        ProductOffer(product_name="X", price=-1, currency="USD", region="US", url="u")


def test_bad_currency_rejected():
    with pytest.raises(ValidationError):
        ProductOffer(product_name="X", price=1, currency="RUB", region="US", url="u")


def test_bad_region_rejected():
    with pytest.raises(ValidationError):
        ProductOffer(product_name="X", price=1, currency="USD", region="XX", url="u")


def test_schema_field_names_match_extraction_contract():
    """The extractor's JSON keys must map 1:1 onto ProductOffer fields."""
    expected = {"product_name", "brand", "price", "currency", "region", "url",
                "seller", "availability", "listing_title"}
    actual = set(ProductOffer.model_fields.keys()) & expected
    assert expected <= set(ProductOffer.model_fields.keys()), f"missing {expected - actual}"


def test_target_normalizes_website():
    t = CompetitorTarget(company="Acme", website="acme.example.com")
    assert t.website == "https://acme.example.com"


def test_target_accepts_single_region_string():
    t = CompetitorTarget(company="Acme", website="https://acme.example.com", regions=["US"])
    assert t.regions == [Region.US]
