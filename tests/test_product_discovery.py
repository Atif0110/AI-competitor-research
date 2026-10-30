from app import discovery
from app.focus import discovery_focus_terms, focus_matches_offer
from app.orchestrator import Pipeline
from app.schemas import CompetitorTarget, Currency, ProductOffer, Region


def test_focus_match_ranks_product_pages_above_seed_homepage():
    website = "https://www.boat-lifestyle.com/"
    urls = [
        website,
        "https://www.boat-lifestyle.com/products/airdopes-141-pro",
        "https://www.boat-lifestyle.com/collections/airdopes",
        "https://www.boat-lifestyle.com/pages/price-list",
    ]

    ranked = discovery._rank_and_limit_candidates(
        urls, website, focus_products=["Airdopes"]
    )

    assert ranked[0] == "https://www.boat-lifestyle.com/products/airdopes-141-pro"
    assert website in ranked


def test_discovery_combines_sitemap_results_and_ranks_focus_before_limit(monkeypatch):
    website = "https://www.boat-lifestyle.com/"
    product = "https://www.boat-lifestyle.com/products/airdopes-141-pro"
    monkeypatch.setattr(discovery.settings, "firecrawl_api_key", None)
    monkeypatch.setattr(discovery, "_discover_search_urls", lambda _website, _focus: [])
    monkeypatch.setattr(
        discovery,
        "discover_sitemaps",
        lambda _website, focus_products=(): [website, product],
    )

    results = discovery.discover_urls_for_entity(website, focus_products=["Airdopes"])

    assert results[:2] == [product, website]


def test_site_search_keeps_only_public_pages_from_requested_company(monkeypatch):
    from app.research.models import SearchHit

    class FakeSearch:
        def __init__(self, timeout):
            assert timeout == 4

        def search(self, query, limit):
            assert "site:shop.example.com" in query
            assert "Airdopes" in query
            assert limit == 8
            return [
                SearchHit(url="https://shop.example.com/products/airdopes"),
                SearchHit(url="https://other.example.com/products/airdopes"),
                SearchHit(url="http://127.0.0.1/admin"),
            ]

    monkeypatch.setattr("app.research.sources.DuckDuckGoSearch", FakeSearch)

    found = discovery._discover_search_urls(
        "https://shop.example.com/", ["Airdopes"]
    )

    assert found == ["https://shop.example.com/products/airdopes"]


def test_a_brand_specific_focus_maps_to_category_for_rival_and_filters_accessories():
    rival_focus = discovery_focus_terms(["Airdopes"], competitor=True)
    assert "earbuds" in rival_focus
    urls = [
        "https://www.gonoise.com/products/noise-air-buds-pro-se-earbuds",
        "https://www.boat-lifestyle.com/products/aux-500",
        "https://www.boat-lifestyle.com/products/pb-200",
    ]

    selected = Pipeline._focus_filter(urls, rival_focus)

    assert selected == [urls[0]]
    assert focus_matches_offer("Noise Air Buds Pro SE Earbuds", None, urls[0], rival_focus)
    assert not focus_matches_offer("EnergyShroom PB200 Power Bank", None, urls[1], ["Airdopes"], allow_category_fallback=False)


def test_offer_validation_rejects_off_focus_primary_and_accepts_rival_equivalent():
    target = CompetitorTarget(
        company="boAt",
        website="https://www.boat-lifestyle.com",
        focus_products=["Airdopes"],
    )
    offer = ProductOffer(
        product_name="EnergyShroom PB200 Power Bank",
        price=1199,
        currency=Currency.INR,
        region=Region.IN,
        url="https://www.boat-lifestyle.com/products/aux-500",
    )
    assert not Pipeline._offer_matches_target(
        offer, offer.url, "boAt", target
    )

    rival_offer = ProductOffer(
        product_name="Noise Air Buds Pro SE Earbuds",
        price=1299,
        currency=Currency.INR,
        region=Region.IN,
        url="https://www.gonoise.com/products/noise-air-buds-pro-se-earbuds",
    )
    assert Pipeline._offer_matches_target(
        rival_offer, rival_offer.url, "Noise", target
    )


def test_company_attribution_matches_host_boundaries_not_url_prefixes():
    entities = CompetitorTarget(
        company="Acme",
        website="https://www.acme.example",
        competitors=[
            {"name": "Other", "website": "https://other.example"},
        ],
    ).all_entities()

    assert Pipeline._entity_for_url("https://acme.example/products/one", entities) == "Acme"
    assert Pipeline._entity_for_url("https://shop.acme.example/products/one", entities) == "Acme"
    assert Pipeline._entity_for_url("https://acme.example.attacker.test/products/one", entities) is None


def test_phone_series_focus_matches_model_variants_and_maps_to_rival_phones():
    from app.focus import focus_matches

    focus = ["Galaxy S series"]
    assert focus_matches("/in/smartphones/galaxy-s25-ultra", focus, allow_category_fallback=False)
    assert "iphone" in discovery_focus_terms(focus, competitor=True)
