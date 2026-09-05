"""End-to-end pipeline test in demo mode: scraping -> extraction -> storage -> insights."""
from pathlib import Path

from app.analysis.insights import InsightsEngine
from app.config import settings
from app.orchestrator import Pipeline
from app.schemas import CompetitorTarget
from app.storage.db import OfferStore
from app.storage.vector import ReviewStore


def test_full_pipeline_demo_mode(tmp_path, monkeypatch):
    settings.vector_store_dir = str(tmp_path / "vectors")
    settings.report_output_dir = str(tmp_path / "reports")
    settings.database_url = "sqlite:///" + str(tmp_path / "test.db")
    settings.groq_api_key = settings.openai_api_key = settings.anthropic_api_key = None
    settings.llm_provider = "demo"

    store = OfferStore()
    reviews = ReviewStore()
    pipeline = Pipeline(store=store, review_store=reviews)

    target = CompetitorTarget(
        company="Acme Audio",
        website="https://acme.example.com",
        regions=["US", "EU"],
        focus_products=["Pro X Headphones"],
    )
    result = pipeline.run(target)

    assert result.pages_scraped > 0
    assert result.extractions_ok > 0
    assert result.mode == "demo"
    assert store.count() == result.extractions_ok

    insights = InsightsEngine(store, reviews)
    undercuts = insights.undercut_analysis()
    assert len(undercuts) > 0
    assert all(u.leader_price > 0 for u in undercuts)

    sentiment = insights.sentiment()
    assert sentiment.review_count > 0

    # RAG-style keyword search on stored reviews
    hits = reviews.search("comfortable")
    assert isinstance(hits, list)

    # Report generation must produce a real PDF
    from app.output.report import ReportBuilder
    from app.schemas import PipelineResult

    res = PipelineResult(
        target=target, run_id="test", run_date="2026-01-01", mode="demo",
        pages_scraped=result.pages_scraped, extractions_ok=result.extractions_ok,
        extractions_failed=result.extractions_failed, retries_used=0,
        duration_s=0.1, errors=[],
    )
    pdf = ReportBuilder(insights).build(res)
    assert pdf.exists()
    assert pdf.stat().st_size > 2000, "report PDF too small to be real"
    assert pdf.read_bytes().startswith(b"%PDF"), "not a PDF file"


def test_checkpoint_idempotency(tmp_path, monkeypatch):
    settings.vector_store_dir = str(tmp_path / "vectors")
    settings.database_url = "sqlite:///" + str(tmp_path / "test2.db")
    settings.groq_api_key = settings.openai_api_key = settings.anthropic_api_key = None
    settings.llm_provider = "demo"

    store = OfferStore()
    pipeline = Pipeline(store=store, review_store=ReviewStore())
    target = CompetitorTarget(company="Acme Audio", website="https://acme.example.com",
                              regions=["US"], focus_products=["Pro X Headphones"])
    r1 = pipeline.run(target, run_id="ckpt_test")
    assert r1.pages_scraped > 0  # 1 regionless URL x N regions (real fan-out)

    # Second run on a brand-new DB, same run_id -> checkpoint must skip every page
    settings.database_url = "sqlite:///" + str(tmp_path / "run2.db")
    store2 = OfferStore()
    pipeline2 = Pipeline(store=store2, review_store=ReviewStore())
    r2 = pipeline2.run(target, run_id="ckpt_test")
    assert r2.pages_scraped == 0, "checkpoint failed: pages re-scraped"
    assert r2.extractions_ok == 0, "checkpoint failed: extractions repeated"
    assert store2.count() == 0, "checkpoint failed: rows re-inserted"
