"""v4 engineering-review regression tests.

Covers the bug-class items:
- #4  provider chain order builds ["groq","openai"], not ["openai","openai"]
- #1  run-scoped insights (SQL WHERE run_id) — foreign-run data never leaks
- #2  distinct_products(run_id) counts per-run canonical identities
- #3  observation key excludes run_id (idempotent across runs; run = metadata)
- #12 RAG returns an honest no-retrieval answer, never arbitrary reviews
- #13 sentiment average ignores None ratings
- #15 scheduler UTC-safe conversion (calendar.timegm)
- #19 invalid region enum -> 422 (API)
- #20 run_id sanitisation before filesystem access
- #24 focus filter returns 0 URLs instead of scraping everything
- #26 raw evidence stored with content hash
"""
from __future__ import annotations

import calendar
import sqlite3
import time

import pytest

from app.analysis.insights import InsightsEngine
from app.config import settings
from app.orchestrator import Pipeline, _provider_chain
from app.schemas import (Availability, Competitor, CompetitorTarget, Currency,
                         ProductOffer, Region, Review)
from app.scheduler import Scheduler, utc_to_epoch
from app.storage.db import OfferStore
from app.storage.vector import ReviewStore
from app.util import is_safe_run_id, parse_region


# ---------------------------------------------------------------- helpers
def _mk_offer(name: str, region: Region, price: float, run_id: str, competitor: str,
              url: str, hour: str = "2026-09-05T10") -> ProductOffer:
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(hour + ":00:00+00:00")
    return ProductOffer(
        product_name=name, brand="Acme", price=price, currency=Currency.USD,
        region=region, url=url, seller=competitor, availability=Availability.in_stock,
        run_id=run_id, competitor=competitor,
        canonical_product_id=f"pid-{name.lower().replace(' ', '-')}",
        canonical_product_name=name, normalized_price_usd=price,
        scraped_at=dt,
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "demo")
    monkeypatch.setattr(settings, "vector_store_dir", str(tmp_path / "vec"))
    return tmp_path


def _store(tmp_path) -> OfferStore:
    return OfferStore(database_url=f"sqlite:///{tmp_path}/t.db")


def _review_store(tmp_path) -> ReviewStore:
    return ReviewStore()


# ---------------------------------------------------------------- #4
class _FakeClient:
    def __init__(self, provider, fallbacks):
        self._p, self._f = provider, fallbacks

    def telemetry(self):
        return {"provider": self._p, "fallbacks": list(self._f), "calls": 1,
                "provider_attempts": len(self._f) + 1, "fallbacks_count": len(self._f)}


def test_provider_chain_order():
    assert _provider_chain(_FakeClient("openai", [("groq", "openai")])) == ["groq", "openai"]


def test_provider_chain_full_chain_order():
    f = _FakeClient("anthropic", [("groq", "openai"), ("openai", "anthropic")])
    assert _provider_chain(f) == ["groq", "openai", "anthropic"]


# ---------------------------------------------------------------- #1, #2, #3
def test_run_scoped_insights(env):
    tmp = env
    store = _store(tmp)
    # run A: 2 products (US)
    store.upsert_offer(_mk_offer("Pro X", Region.US, 500, "runA", "Acme", "https://a.com/product/pro-x"))
    store.upsert_offer(_mk_offer("Pro Y", Region.US, 700, "runA", "SoundWorks", "https://b.com/product/pro-y"))
    # run B: 1 different product (IN)
    store.upsert_offer(_mk_offer("Pro Z", Region.IN, 100, "runB", "GloboTech", "https://c.com/product/pro-z"))

    engine = InsightsEngine(store, _review_store(tmp))

    ua = engine.undercut_analysis(run_id="runA")
    assert {u.product_name for u in ua} == {"Pro X", "Pro Y"}
    ub = engine.undercut_analysis(run_id="runB")
    assert {u.product_name for u in ub} == {"Pro Z"}
    assert {u.product_name for u in engine.undercut_analysis()} == {"Pro X", "Pro Y", "Pro Z"}

    assert sorted(store.distinct_products(run_id="runA")) == ["pid-pro-x", "pid-pro-y"]
    assert store.distinct_products(run_id="runB") == ["pid-pro-z"]
    assert len(store.distinct_products()) == 3

    assert {o.run_id for o in store.recent_offers(run_id="runA")} == {"runA"}
    assert all(o.run_id == "runB" for o in store.recent_offers(run_id="runB"))


def test_observation_key_excludes_run_id(env):
    tmp = env
    store = _store(tmp)
    # same observation (same URL/product/region/competitor/seller/hour),
    # different run -> SAME row (run_id is metadata, #3)
    store.upsert_offer(_mk_offer("Pro X", Region.US, 480, "runA", "Acme",
                                 "https://a.com/product/pro-x"))
    store.upsert_offer(_mk_offer("Pro X", Region.US, 450, "runB", "Acme",
                                 "https://a.com/product/pro-x"))
    assert store.count() == 1
    row = store.recent_offers(run_id="runB")
    assert len(row) == 1 and row[0].run_id == "runB" and row[0].price == 450
    # different snapshot hour -> NEW row (historical snapshot)
    store.upsert_offer(_mk_offer("Pro X", Region.US, 450, "runC", "Acme",
                                 "https://a.com/product/pro-x", hour="2026-09-05T11"))
    assert store.count() == 2


# ---------------------------------------------------------------- #12
def test_rag_no_retrieval_is_honest(env):
    tmp = env
    rs = _review_store(tmp)
    rs.add(Review(product_name="Pro X", region=Region.IN, rating=5,
                  review_text="Sound is superb, comfort excellent",
                  source_url="https://x.com/r/1", run_id="runA", competitor="Acme"))
    engine = InsightsEngine(_store(tmp), rs)
    res = engine.answer_question("what do customers complain about battery life?", run_id="runA")
    assert res["sources"] == []
    assert "No relevant reviews" in res["answer"]
    # arbitrary reviews are NOT substituted


# ---------------------------------------------------------------- #13
def test_sentiment_avg_ignores_none_ratings(env):
    tmp = env
    rs = _review_store(tmp)
    for txt, r in [("Great", 5.0), ("Good", 4.0), ("Meh", None), ("OK", None)]:
        rs.add(Review(product_name="Pro X", region=Region.US, rating=r, review_text=txt,
                      source_url="https://x.com/r/1", run_id="runA"))
    engine = InsightsEngine(_store(tmp), rs)
    s = engine.sentiment(product="Pro X", region=Region.US, run_id="runA")
    assert s.review_count == 4
    assert s.avg_rating == 4.5  # 9/2, not 9/4


# ---------------------------------------------------------------- #15
def test_scheduler_utc_to_epoch():
    assert utc_to_epoch("1970-01-01T00:00:00Z") == 0
    assert utc_to_epoch("2026-09-05T00:00:00Z") == calendar.timegm(
        time.strptime("2026-09-05T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))


def test_scheduler_not_due_when_future_last_run(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "demo")
    s = Scheduler(Pipeline())
    sched = {"id": 1, "last_run_at": "2099-01-01T00:00:00Z", "interval_hours": 24, "enabled": 1}
    assert s._due(sched) is False
    sched2 = {"id": 2, "last_run_at": None, "interval_hours": 24, "enabled": 1}
    assert s._due(sched2) is True


# ---------------------------------------------------------------- #19, #20
def test_parse_region_invalid_raises_valueerror():
    with pytest.raises(ValueError):
        parse_region("XYZ")
    assert parse_region("in") == Region.IN
    assert parse_region(None) is None


def test_run_id_sanitisation():
    assert is_safe_run_id("abc_123-456") is True
    assert is_safe_run_id("../../etc/passwd") is False
    assert is_safe_run_id("a b") is False
    assert is_safe_run_id("") is False


def test_undercut_invalid_region_422():
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from app.api import app

    client = TestClient(app)
    r = client.get("/insights/undercut?region=XYZ")
    assert r.status_code == 422
    r2 = client.get("/insights/undercut?region=US")
    assert r2.status_code == 200


def test_report_run_id_sanitised_before_fs():
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from app.api import app

    client = TestClient(app)
    r = client.get("/reports/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code in (404, 422)  # framework rejects the traversal
    r2 = client.get("/reports/not..valid..run")
    assert r2.status_code == 422  # fails the ^[A-Za-z0-9_-]+$ regex
    safe_404 = client.get("/reports/does-not-exist-run")
    assert safe_404.status_code == 404


# ---------------------------------------------------------------- #24
def test_focus_filter_empty_on_no_match():
    urls = ["https://acme.example.com/product/pro-x", "https://acme.example.com/products/lite"]
    target = CompetitorTarget(company="Acme", website="https://acme.example.com",
                              focus_products=["iPhone 17"])
    assert Pipeline._focus_filter(urls, target) is None  # no fallback to everything


def test_focus_filter_matches_path_only():
    # "pro" token must NOT match in the domain nor in "/product/" (#23)
    urls = ["https://pro-x-company.example.com/electronics/audio",
            "https://acme.example.com/product/pro-x-headphones",
            "https://acme.example.com/product/lite-earbuds"]
    target = CompetitorTarget(company="Acme", website="https://acme.example.com",
                              focus_products=["Pro X Headphones"])
    kept = Pipeline._focus_filter(urls, target)
    assert kept == ["https://acme.example.com/product/pro-x-headphones"]


# ---------------------------------------------------------------- #26
def test_raw_evidence_stored(env):
    tmp = env
    store = _store(tmp)
    store.save_evidence("runA", "https://x.com/product/1", "<html>Pro X $500</html>")
    ev = store.evidence_for_run("runA")
    assert len(ev) == 1
    assert ev[0]["source_url"] == "https://x.com/product/1"
    assert len(ev[0]["content_hash"]) == 64
    assert ev[0]["size"] > 0
    assert store.get_evidence("https://x.com/product/1", "runA").startswith("<html>")
    # evidence is isolated by run
    assert store.evidence_for_run("runB") == []


# ---------------------------------------------------------------- #21
def test_parse_proxy_url():
    from app.scrapers.base import parse_proxy_url

    p1 = parse_proxy_url("http://user:secret@host:8080")
    assert p1["server"] == "http://host:8080"
    assert p1["username"] == "user" and p1["password"] == "secret"
    p2 = parse_proxy_url("https://proxy.example:443")
    assert p2["server"] == "https://proxy.example:443" and "username" not in p2
    p3 = parse_proxy_url("socks5://proxy.example:1080")
    assert p3["server"] == "socks5://proxy.example:1080"
    p4 = parse_proxy_url("http://[::1]:3128")
    assert p4["server"] == "http://[::1]:3128"
    assert parse_proxy_url("") is None


# ---------------------------------------------------------------- #7
def test_product_identity_preserves_variants():
    from app.analysis.product_identity import resolve

    assert resolve("Pro X Headphones ANC").canonical_id != resolve("Pro X Headphones").canonical_id
    assert resolve("Pro X Headphones Gen 2").canonical_id != resolve("Pro X Headphones Gen 3").canonical_id
    assert resolve("Pro X Headphones") == resolve("Pro-X-Headphones")  # hyphens normalize


# ---------------------------------------------------------------- #11
def test_chroma_meta_omits_none_and_keeps_all_fields():
    from app.storage.vector import ReviewStore as RS

    r = Review(product_name="Pro X", region=Region.US, rating=None, review_text="t",
               source_url="https://u/1", run_id="runA", competitor=None, reviewer="Bob")
    meta = RS._meta(r)
    assert "rating" not in meta and "competitor" not in meta   # None omitted
    assert meta["product_name"] == "Pro X" and meta["region"] == "US"
    assert meta["reviewer"] == "Bob" and meta["run_id"] == "runA" and meta["source_url"] == "https://u/1"


# ---------------------------------------------------------------- #22
def test_discovery_sitemap_index_followed(monkeypatch):
    import app.discovery as D

    def fake_fetch(url):
        if url.endswith("/robots.txt"):
            return "User-agent: *\nSitemap: https://x.com/sitemap_index.xml\n"
        if "sitemap_index" in url:
            return ("<urlset><url><loc>https://x.com/products-1.xml</loc></url>"
                    "<url><loc>https://x.com/products-2.xml</loc></url></urlset>")
        if "products-1" in url:
            return ("<urlset><url><loc>https://x.com/product/pro-x</loc></url>"
                    "<url><loc>https://x.com/about</loc></url></urlset>")
        if "products-2" in url:
            return "<urlset><url><loc>https://x.com/product/pro-y</loc></url></urlset>"
        return ""

    monkeypatch.setattr(D, "_fetch", fake_fetch)
    urls = D.discover_sitemaps("https://x.com")
    assert "https://x.com/product/pro-x" in urls
    assert "https://x.com/product/pro-y" in urls
    assert "https://x.com/about" not in urls   # non-product filtered
