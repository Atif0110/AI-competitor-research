"""End-to-end deep-research test against a local fixture website.

Runs the real pipeline (planner -> crawler -> extraction -> synthesis ->
persistence) with no external network and no LLM key, then asserts the run
produced cited, retrievable evidence.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app.config import settings
from app.research.models import ResearchMode, ResearchRequest, ResearchStatus
from app.research.pipeline import DeepResearchPipeline
from app.storage.documents import DocumentStore

PAGES = {
    "/": b"""<html><head><title>Acme Audio</title>
    <meta name="description" content="Acme builds wireless headphones"></head><body>
    <nav><a href="/pricing">Pricing</a><a href="/about">About</a></nav>
    <h1>Acme Audio</h1>
    <p>Acme builds wireless headphones for commuters and teams.</p>
    <h2>Products</h2>
    <ul><li>Pro X Headphones</li><li>Air Buds Lite</li></ul>
    <a href="/pricing">See pricing</a></body></html>""",
    "/pricing": b"""<html><head><title>Pricing - Acme Audio</title></head><body>
    <h1>Pricing</h1>
    <h2>Standard</h2><p>The Standard plan costs $19 per month and includes 3 seats.</p>
    <h2>Enterprise</h2><p>The Enterprise plan costs $49 per month and includes 25 seats.
    Annual billing is $470 per year.</p>
    <h2>Free trial</h2><p>Every plan includes a 30 day free trial.</p>
    <a href="/reviews">Customer reviews</a></body></html>""",
    "/about": b"""<html><head><title>About Acme Audio</title></head><body>
    <h1>About us</h1>
    <p>Acme Audio was founded in 2019 in Berlin and employs 240 people.</p>
    <p>We ship to 32 countries.</p></body></html>""",
    "/reviews": b"""<html><head><title>Customer reviews - Acme Audio</title></head><body>
    <h1>Customer reviews</h1>
    <h2>Verified reviews</h2>
    <p>"Battery life is superb, lasts three days." Rating 5 stars.</p>
    <p>"Support took four days to reply and the case is bulky." Rating 2 stars.</p>
    </body></html>""",
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        body = PAGES.get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"<html><body>not found</body></html>")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence test output
        return


@pytest.fixture()
def site():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "demo")
    monkeypatch.setattr(settings, "demo_mode_enabled", False)
    monkeypatch.setattr(settings, "vector_store_dir", str(tmp_path / "vec"))
    monkeypatch.setattr(settings, "checkpoint_dir", str(tmp_path / "ckpt"))
    monkeypatch.setattr(settings, "report_output_dir", str(tmp_path / "reports"))
    monkeypatch.setattr(settings, "database_url", "sqlite:///" + str(tmp_path / "research.db"))
    monkeypatch.setattr(settings, "deep_per_host_delay", 0.0)
    monkeypatch.setattr(settings, "deep_max_pages", 6)
    monkeypatch.setattr(settings, "deep_search_enabled", False)
    monkeypatch.setattr(settings, "deep_respect_robots", False)
    return tmp_path


def test_deep_research_url_run_is_cited_and_retrievable(site, env):
    events = []
    pipeline = DeepResearchPipeline(progress_callback=events.append)

    result = pipeline.run(
        ResearchRequest(
            mode=ResearchMode.url,
            url=site,
            question="What does Acme charge for its plans and how many seats are included?",
            max_pages=5,
            max_depth=1,
            use_search=False,
            use_llm=False,
        )
    )

    assert result.status in (ResearchStatus.completed, ResearchStatus.partial)
    assert result.coverage.pages_fetched >= 2
    assert result.sections, "no retrievable sections were stored"
    assert result.citations, "no citation registry was built"
    assert all(c.ref.startswith("S") for c in result.citations)
    assert "[S" in result.report_markdown, "report must cite its sources"
    assert "Sources" in result.report_markdown

    # the pricing page must actually have been captured
    assert any("pricing" in page["url"] for page in result.pages)

    # facts were extracted from the captured text without any model
    assert result.facts
    assert any(
        "$49" in fact.statement or "19" in fact.statement
        for fact in result.facts
    )

    # persistence: the run can be reloaded from storage with the same shape
    stored = pipeline.load(result.run_id)
    assert stored is not None
    assert stored.run_id == result.run_id
    assert len(stored.sections) == len(result.sections)
    assert stored.report_markdown == result.report_markdown

    # retrieval finds the pricing answer for a targeted question
    hits = pipeline.documents.search_sections(
        result.run_id, "how much does the enterprise plan cost per month", k=3
    )
    assert hits
    assert any("$49" in section.content or "49" in section.content for section, _ in hits)

    # progress events tell the UI what is happening
    kinds = {e["event"] for e in events}
    assert {"run_started", "crawl_started", "page_processed", "run_completed"} <= kinds

    # run history is queryable for the frontend
    runs = pipeline.documents.list_runs()
    assert any(r["run_id"] == result.run_id for r in runs)


def test_deep_research_reports_gaps_when_evidence_is_missing(site, env):
    pipeline = DeepResearchPipeline()
    result = pipeline.run(
        ResearchRequest(
            mode=ResearchMode.url,
            url=site,
            question="What is the enterprise SLA uptime guarantee?",
            max_pages=2,
            max_depth=0,
            use_search=False,
            use_llm=False,
        )
    )
    assert result.gaps, "unanswered questions must be reported as gaps"
    assert all(gap.reason for gap in result.gaps)


def test_deep_research_missing_site_fails_without_crashing(env):
    pipeline = DeepResearchPipeline()
    result = pipeline.run(
        ResearchRequest(
            mode=ResearchMode.url,
            url="http://127.0.0.1:9/does-not-exist",
            question="anything",
            max_pages=1,
            max_depth=0,
            use_search=False,
            use_llm=False,
        )
    )
    assert result.status == ResearchStatus.failed
    assert result.errors
    assert pipeline.load(result.run_id) is not None


def test_plan_is_deterministic_without_llm():
    from app.research.planner import build_plan

    plan = build_plan(
        ResearchRequest(
            mode=ResearchMode.url,
            url="https://acme.example.com",
            question="How does Acme price its enterprise plan?",
        ),
        llm=None,
    )
    assert plan.planner == "deterministic"
    assert plan.sub_questions
    assert plan.seed_urls == ["https://acme.example.com/"]
    assert plan.max_pages >= 1


# ----------------------------------------------------------------------
# extraction regressions
# ----------------------------------------------------------------------
def test_parser_keeps_structure_and_nav_links():
    from app.research.fetching import parse_page

    parsed = parse_page(PAGES["/pricing"].decode("utf-8"), "http://acme.example.com/pricing")

    # headings survive as Markdown, so sections can be anchored to them
    assert "## Standard" in parsed.markdown
    assert "## Enterprise" in parsed.markdown
    # list items stay separate lines instead of running together
    home = parse_page(PAGES["/"].decode("utf-8"), "http://acme.example.com/")
    assert "- Pro X Headphones" in home.markdown
    assert "- Air Buds Lite" in home.markdown
    # nav/footer links are the main discovery path and must survive
    assert any("/pricing" in link for link in home.links)
    assert any("/about" in link for link in home.links)


def test_sections_keep_heading_trail_of_merged_units():
    from app.research.extract import split_sections

    sections = split_sections(
        "# Pricing\n\n## Standard\n\n"
        + "The Standard plan costs $19 per month and includes 3 seats.\n\n"
        + "## Enterprise\n\n"
        + "The Enterprise plan costs $49 per month and includes 25 seats.\n\n"
        + "## Free trial\n\nEvery plan includes a 30 day free trial.\n",
        chunk_chars=100000,
        min_chars=120,
    )
    assert sections
    headings = " ".join(heading for heading, _ in sections)
    assert "Standard" in headings
    assert "Enterprise" in headings


def test_extracted_statements_are_prose_not_markdown():
    from app.research.extract import _iter_sentences

    sentences = _iter_sentences(
        "# Pricing\n\n## Standard\n\nThe Standard plan costs $19 per month.\n\n"
        "- Pro X Headphones\n- Air Buds Lite\n"
    )
    joined = " ".join(sentences)
    assert "#" not in joined
    assert "The Standard plan costs $19 per month." in joined
    assert "Pro X Headphones" in joined
    assert "Air Buds Lite" in joined
    assert "- Pro" not in joined


def test_price_extraction_requires_a_real_unit():
    from app.research.extract import _PRICE_RE

    assert _PRICE_RE.findall("The plan costs $19 per month.") == ["$19 per month"]
    assert _PRICE_RE.findall("Annual billing is $470 per year.") == ["$470 per year"]
    # a dangling connector must not be captured as part of the value
    assert not any(match.strip() in ("$19 per", "$19 a", "$19 /") for match in _PRICE_RE.findall("Costs $19 per month."))


def test_incomplete_status_ignores_a_single_failure():
    from app.research.pipeline import DeepResearchPipeline as Pipeline

    assert not Pipeline._is_incomplete(pages_fetched=2, pages_failed=1, budget_exhausted=False)
    assert Pipeline._is_incomplete(pages_fetched=3, pages_failed=3, budget_exhausted=False)
    assert Pipeline._is_incomplete(pages_fetched=1, pages_failed=0, budget_exhausted=True)
    assert not Pipeline._is_incomplete(pages_fetched=6, pages_failed=1, budget_exhausted=True)


# ----------------------------------------------------------------------
# research chat
# ----------------------------------------------------------------------
def _run_fixture(pipeline, site, question="What does Acme charge for its plans?"):
    return pipeline.run(
        ResearchRequest(
            mode=ResearchMode.url,
            url=site,
            question=question,
            max_pages=4,
            max_depth=1,
            use_search=False,
            use_llm=False,
        )
    )


def test_chat_answers_from_evidence_with_citations(site, env):
    from app.research.chat import ResearchChat

    pipeline = DeepResearchPipeline()
    run = _run_fixture(pipeline, site)
    chat = ResearchChat(pipeline.documents, client=None)

    answer = chat.ask(
        "How much is the enterprise plan per month?",
        run_id=run.run_id,
        session_id="sess-1",
    )
    assert answer.answerable is True
    assert "$49" in answer.message
    assert answer.sources_used >= 1
    assert [c["ref"] for c in answer.citations]
    assert any("pricing" in c["url"] for c in answer.citations)

    # the turn is persisted against the caller's session id
    messages = pipeline.documents.messages("sess-1")
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_chat_refuses_to_answer_beyond_the_evidence(site, env):
    from app.research.chat import ResearchChat

    pipeline = DeepResearchPipeline()
    run = _run_fixture(pipeline, site)
    chat = ResearchChat(pipeline.documents, client=None)

    answer = chat.ask(
        "What is their enterprise SLA uptime guarantee?",
        run_id=run.run_id,
        session_id="sess-2",
    )
    assert answer.answerable is False
    assert answer.sources_used == 0
    assert "does not cover" in answer.message


def test_chat_without_a_run_explains_itself(site, env):
    from app.research.chat import ResearchChat

    pipeline = DeepResearchPipeline()
    chat = ResearchChat(pipeline.documents, client=None)
    answer = chat.ask("anything?", run_id="does-not-exist", session_id="sess-3")
    assert answer.answerable is False
    assert "No research run" in answer.message


def test_chat_survives_a_model_that_fails(site, env):
    from app.research.chat import ResearchChat

    class _Broken:
        provider = "broken"

        def complete(self, system, prompt):
            raise RuntimeError("provider down")

    pipeline = DeepResearchPipeline()
    run = _run_fixture(pipeline, site)
    chat = ResearchChat(pipeline.documents, client=_Broken())
    answer = chat.ask(
        "What does the enterprise plan cost per month?",
        run_id=run.run_id,
        session_id="sess-4",
    )
    assert answer.answerable is True
    assert "$49" in answer.message


def test_research_chat_endpoints(site, env, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import app

    monkeypatch.setattr(settings, "api_key_required", False)

    pipeline = DeepResearchPipeline()
    run = _run_fixture(pipeline, site)
    client = TestClient(app)

    asked = client.post(
        "/research/chat",
        json={
            "question": "How much is the standard plan per month?",
            "run_id": run.run_id,
            "session_id": "api-sess",
        },
    )
    assert asked.status_code == 200, asked.text
    payload = asked.json()
    assert payload["answerable"] is True
    assert payload["citations"]
    assert payload["message"]

    history = client.get("/research/chat/api-sess")
    assert history.status_code == 200
    assert len(history.json()["messages"]) == 2

    runs = client.get("/research/deep/runs")
    assert runs.status_code == 200
    assert any(r["run_id"] == run.run_id for r in runs.json())

    reloaded = client.get(f"/research/deep/runs/{run.run_id}")
    assert reloaded.status_code == 200
    assert reloaded.json()["report_markdown"] == run.report_markdown