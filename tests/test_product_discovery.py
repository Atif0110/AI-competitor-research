from app import discovery


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
