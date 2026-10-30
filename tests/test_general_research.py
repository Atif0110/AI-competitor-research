from io import BytesIO

from reportlab.pdfgen import canvas

from app.research.crawler import CrawlBudget, Crawler
from app.research.fetching import FetchResult, looks_like_document
from app.research.models import ResearchMode, ResearchPlan, ResearchRequest
from app.research.planner import build_plan


def _sample_pdf() -> bytes:
    output = BytesIO()
    document = canvas.Canvas(output)
    document.setTitle("Climate research paper")
    document.drawString(72, 720, "Observed urban tree cover reduced neighborhood heat exposure.")
    document.save()
    return output.getvalue()


def test_general_topic_planner_does_not_force_competitor_or_pricing_questions():
    request = ResearchRequest(
        mode=ResearchMode.topic,
        topic="Urban tree cover and neighborhood heat",
        question="How does urban tree cover affect heat exposure and public health?",
    )

    plan = build_plan(request, llm=None)

    assert any("evidence" in question.lower() for question in plan.sub_questions)
    assert all("price" not in question.lower() for question in plan.sub_questions)
    assert all("competitor" not in question.lower() for question in plan.sub_questions)
    assert not any("pricing" in query.lower() for query in plan.search_queries)
    assert any("urban tree cover" in query.lower() for query in plan.search_queries)


def test_arbitrary_source_urls_are_researchable_and_added_to_the_seed_plan():
    request = ResearchRequest(
        mode=ResearchMode.url,
        url="https://example.org/research",
        extra_seeds=["https://arxiv.org/pdf/1234.5678.pdf"],
        question="What does this paper conclude?",
    )

    plan = build_plan(request, llm=None)

    assert any("paper" in question.lower() for question in plan.sub_questions) or any(
        "conclude" in question.lower() for question in plan.sub_questions
    )
    assert request.extra_seeds[0] in plan.seed_urls


def test_pdf_links_are_fetchable_and_text_is_extracted_with_citations_metadata():
    url = "https://example.org/papers/climate-study.pdf"
    pdf_bytes = _sample_pdf()

    class FakeFetcher:
        def fetch(self, requested_url):
            assert requested_url == url
            return FetchResult(
                url=url,
                ok=True,
                status=200,
                final_url=url,
                content_type="application/pdf",
                body=pdf_bytes,
            )

    assert looks_like_document(url)
    assert not looks_like_document("https://example.org/image.png")

    crawler = Crawler(
        fetcher=FakeFetcher(),
        budget=CrawlBudget(max_pages=1, max_depth=0),
        max_chars=5000,
    )
    document = crawler._fetch_one(url, 0, ResearchPlan(subject="urban climate"))

    assert document.status == "fetched"
    assert document.title == "Climate research paper"
    assert "urban tree cover" in document.text.lower()
    assert document.scraper == "pypdf"


def test_opaque_pdf_with_no_extractable_text_is_skipped_honestly():
    from app.research.crawler import _extract_pdf_text

    text, _title, error = _extract_pdf_text(b"%PDF-not-a-real-document", 1000)

    assert not text
    assert error == "PDF could not be parsed"


def test_deep_research_api_passes_validated_source_urls_to_pipeline(monkeypatch):
    from fastapi.testclient import TestClient

    import app.api as api_module
    from app.research.models import DeepResearchResult

    class FakePipeline:
        def run(self, request):
            self.request = request
            return DeepResearchResult(run_id="source-url-test")

    fake = FakePipeline()
    monkeypatch.setattr(api_module.settings, "api_key_required", False)
    monkeypatch.setattr(api_module.settings, "public_workspace_mode", False)
    monkeypatch.setattr(api_module, "_deep_pipeline", lambda: fake)
    with api_module._rate_lock:
        api_module._rate_window.clear()

    response = TestClient(api_module.app).post(
        "/research/deep",
        json={
            "mode": "url",
            "url": "https://example.org/article",
            "source_urls": ["https://arxiv.org/pdf/1234.5678.pdf"],
            "use_search": False,
        },
    )

    assert response.status_code == 200, response.text
    assert fake.request.extra_seeds == ["https://arxiv.org/pdf/1234.5678.pdf"]
