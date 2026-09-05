"""Failure-mode + engineering-review tests (all offline, fakes/monkeypatch):
scraper chain fallback, region fan-out, expected_region enforcement, verified-
or-fail proxies, retry counting from ScrapeLog, per-run metric isolation,
focus_products filtering, and report-generation error handling."""
from pathlib import Path

import pytest

from app.config import settings
from app.schemas import CompetitorTarget, ProductOffer, Region
from app.scrapers.base import BaseScraper, ProxyPool, ScrapeError, ScrapedPage
from app.scrapers.orchestrator import ScraperOrchestrator
from app.orchestrator import Pipeline
from app.storage.db import OfferStore
from app.storage.vector import ReviewStore


# ---------------- scraper chain fallback ----------------
class _Fail403(BaseScraper):
    name = "firecrawl"

    def _fetch_once(self, url, proxy, region=None):
        raise ScrapeError("403 Forbidden")


class _FailJS(BaseScraper):
    name = "playwright"

    def _fetch_once(self, url, proxy, region=None):
        raise ScrapeError("JS challenge blocked")


class _Works(BaseScraper):
    name = "basic"

    def _fetch_once(self, url, proxy, region=None):
        return ScrapedPage(url=url, markdown="<h1>Product</h1>", proxy_used=proxy, region=region)


class _RegionAware(BaseScraper):
    name = "regional"

    def __init__(self):
        super().__init__()
        self.calls = []

    def _fetch_once(self, url, proxy, region=None):
        self.calls.append((url, region))
        r = region or "US"
        return ScrapedPage(url=url, region=r, markdown=(
            "# Pro X Headphones\nBrand: Acme Audio\nPrice: 499.00\nCurrency: USD\n"
            f"Region: {r}\nAvailability: in_stock\nSeller: Acme Store\n"))


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(settings, "max_retries", 2)
    monkeypatch.setattr(settings, "backoff_base_seconds", 0.01)
    monkeypatch.setattr(settings, "rate_limit_min_interval", 0.0)


def test_scraper_chain_falls_back_on_403(fast_retries):
    orch = ScraperOrchestrator([_Fail403(), _Works()])
    page, log = orch.fetch("https://x.example/p")
    assert page.markdown == "<h1>Product</h1>"
    assert log.scrapers_tried == ["firecrawl", "basic"]
    assert log.final_scraper == "basic"
    assert log.status == "success"


def test_all_scrapers_fail_raises_with_telemetry(fast_retries):
    orch = ScraperOrchestrator([_Fail403(), _FailJS()])
    with pytest.raises(ScrapeError) as ei:
        orch.fetch("https://x.example/p")
    assert "firecrawl" in str(ei.value) and "playwright" in str(ei.value)
    assert getattr(ei.value, "log", None) is not None


# ---------------- region fan-out (live path) ----------------
def test_live_region_fanout(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "sqlite:///" + str(tmp_path / "f.db"))
    monkeypatch.setattr(settings, "vector_store_dir", str(tmp_path / "v"))
    settings.groq_api_key = settings.openai_api_key = settings.anthropic_api_key = None
    settings.llm_provider = "demo"

    rscraper = _RegionAware()
    store = OfferStore()
    pipe = Pipeline(store=store, review_store=ReviewStore(),
                    scraper=ScraperOrchestrator([rscraper]))
    target = CompetitorTarget(company="Acme Audio", website="https://acme.example.com",
                              regions=["US", "IN"], focus_products=["Pro X Headphones"])
    result = pipe.run(target)

    seen = {r for _, r in rscraper.calls}
    assert seen == {"US", "IN"}
    stored_regions = {o.region.value for o in store.recent_offers(limit=100)}
    assert stored_regions == {"US", "IN"}
    # every extraction ran with an enforced execution region
    assert all(o.region.value in {"US", "IN"} for o in store.recent_offers(limit=100))


# ---------------- focus_products filtering ----------------
def test_focus_products_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "sqlite:///" + str(tmp_path / "fo.db"))
    monkeypatch.setattr(settings, "vector_store_dir", str(tmp_path / "v"))
    settings.groq_api_key = settings.openai_api_key = settings.anthropic_api_key = None
    settings.llm_provider = "demo"

    pipe = Pipeline(store=OfferStore(), review_store=ReviewStore())
    target = CompetitorTarget(company="Acme Audio", website="https://acme.example.com",
                              regions=["US", "EU", "UK"], focus_products=["Lite Earbuds"])
    result = pipe.run(target)
    assert result.discovered_urls == 1  # 1 focus product, regionless URL
    assert result.pages_scraped == 3   # 1 URL x 3 regions (real fan-out)


# ---------------- verified-or-fail proxies ----------------
def _verify_sg(proxy):
    return "SG"


def test_verified_proxy_no_wrong_geo_fallback():
    pool = ProxyPool(generic=["us1"], by_region={"IN": ["sg1"]}, verify=_verify_sg)
    assert pool.next_verified("IN") is None  # only SG exit -> no IN proxy -> None, NOT us1


def test_orchestrator_refuses_unverified_region(fast_retries, monkeypatch):
    monkeypatch.setattr(settings, "proxy_verify", True)
    pool = ProxyPool(generic=["us1"], by_region={"IN": ["sg1"]}, verify=_verify_sg)
    orch = ScraperOrchestrator([_Works()], proxy_pool=pool)
    with pytest.raises(ScrapeError) as ei:
        orch.fetch("https://x.example/p", region="IN")
    assert "no verified proxy" in str(ei.value)


def test_proxy_pool_exit_ip_verification():
    def fake_verify(proxy):
        return "IN" if "in1" in proxy else "SG"

    pool = ProxyPool(generic=[], by_region={"IN": ["in1", "sg1"]}, verify=fake_verify)
    assert pool.next_verified("IN") == "in1"
    assert pool.next_verified("IN") == "in1"  # healthy cache


def test_proxy_verify_disabled_plain_round_robin():
    pool = ProxyPool(generic=[], by_region={"JP": ["jp1", "jp2"]}, verify=None)
    assert pool.next_verified("JP") == "jp1"
    assert pool.next_verified("JP") == "jp2"


# ---------------- retries counted from ScrapeLog ----------------
def test_retries_counted_from_scrapelog(tmp_path, monkeypatch, fast_retries):
    monkeypatch.setattr(settings, "database_url", "sqlite:///" + str(tmp_path / "r.db"))
    monkeypatch.setattr(settings, "vector_store_dir", str(tmp_path / "v"))
    settings.groq_api_key = settings.openai_api_key = settings.anthropic_api_key = None
    settings.llm_provider = "demo"

    store = OfferStore()
    pipe = Pipeline(store=store, review_store=ReviewStore(),
                    scraper=ScraperOrchestrator([_Fail403(), _Works()]))
    target = CompetitorTarget(company="Acme Audio", website="https://acme.example.com",
                              regions=["US"], focus_products=["Pro X Headphones"])
    result = pipe.run(target)
    # per page: firecrawl 2 attempts (1 retry) + basic 1 (0) = 1 retry; 1 focus page
    assert result.retries_used == 1


# ---------------- per-run metric isolation ----------------
def test_metrics_isolated_per_run(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "sqlite:///" + str(tmp_path / "m.db"))
    monkeypatch.setattr(settings, "vector_store_dir", str(tmp_path / "v"))
    settings.groq_api_key = settings.openai_api_key = settings.anthropic_api_key = None
    settings.llm_provider = "demo"

    pipe = Pipeline(store=OfferStore(), review_store=ReviewStore())
    target = CompetitorTarget(company="Acme Audio", website="https://acme.example.com",
                              regions=["US"], focus_products=["Pro X Headphones"])
    r1 = pipe.run(target)
    r2 = pipe.run(target)
    # demo: exactly 2 LLM calls per page (extract + reviews), per run — NOT cumulative
    assert r2.metrics["llm_calls"] == r2.pages_scraped * 2
    assert r2.metrics["llm_calls"] == r1.metrics["llm_calls"]


# ---------------- report script: real run_metrics, error on missing ----------------
def test_report_script_errors_on_unknown_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "database_url", "sqlite:///" + str(tmp_path / "g.db"))
    monkeypatch.setattr(settings, "vector_store_dir", str(tmp_path / "v"))
    monkeypatch.setattr(settings, "report_output_dir", str(tmp_path / "rep"))

    from scripts.generate_report import main

    with pytest.raises(SystemExit) as ei:
        main(["--run-id", "does-not-exist"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "not found" in err


# ---------------- db upsert idempotency stays green ----------------
def test_upsert_same_offer_once(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "sqlite:///" + str(tmp_path / "u.db"))
    store = OfferStore()
    offer = ProductOffer(product_name="Pro X Headphones", price=100, currency="USD",
                         region="US", url="https://s.example/p", competitor="SoundWorks")
    store.upsert_offer(offer)
    store.upsert_offer(offer)
    assert store.count() == 1


def test_review_extraction_and_rag_qa(tmp_path, monkeypatch):
    """Real review extraction path + RAG Q&A with sources (priorities 9 & 10)."""
    monkeypatch.setattr(settings, "database_url", "sqlite:///" + str(tmp_path / "rv.db"))
    monkeypatch.setattr(settings, "vector_store_dir", str(tmp_path / "v"))
    settings.groq_api_key = settings.openai_api_key = settings.anthropic_api_key = None
    settings.llm_provider = "demo"

    pipe = Pipeline(store=OfferStore(), review_store=ReviewStore())
    target = CompetitorTarget(company="Acme Audio", website="https://acme.example.com",
                              regions=["US"], focus_products=["Pro X Headphones"])
    pipe.run(target)

    assert len(pipe.review_store.all()) > 0  # reviews extracted and stored
    # query a keyword that actually exists in a stored review (demo rotation varies)
    kw = pipe.review_store.all()[0].review_text.split()[0].strip(".,!?")
    ans = pipe.insights.answer_question(kw)
    assert ans["answer"]
    assert len(ans["sources"]) > 0  # citations present
    assert all(s.get("url") for s in ans["sources"])
