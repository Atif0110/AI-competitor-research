from app import orchestrator
from app.orchestrator import Pipeline
from app.schemas import Competitor, CompetitorTarget


def _pipeline():
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.progress_callback = None
    return pipeline


def test_focus_miss_falls_back_to_one_ranked_url_per_company(monkeypatch):
    pages = {
        "https://acme.example": [
            "https://acme.example/products/earbuds",
            "https://acme.example/collections/audio",
            "https://acme.example/products/overear",
        ],
        "https://rival.example": [
            "https://rival.example/catalog/audio",
            "https://rival.example/products/speakers",
        ],
    }
    monkeypatch.setattr(orchestrator, "discover_urls_for_entity", lambda website: pages[website])
    target = CompetitorTarget(
        company="Acme",
        website="https://acme.example",
        focus_products=["wireless headphones"],
        competitors=[Competitor(name="Rival", website="https://rival.example")],
    )

    urls, notes = _pipeline()._discover(target, demo=False)

    assert urls == [
        "https://acme.example/products/earbuds",
        "https://rival.example/catalog/audio",
    ]
    assert len(notes) == 2
    assert all("trying the highest-ranked page" in note for note in notes)


def test_focus_match_keeps_matching_and_pricing_candidates(monkeypatch):
    pages = {
        "https://acme.example": [
            "https://acme.example/products/headphones",
            "https://acme.example/pricing",
            "https://acme.example/about",
        ],
    }
    monkeypatch.setattr(orchestrator, "discover_urls_for_entity", lambda website: pages[website])
    target = CompetitorTarget(
        company="Acme",
        website="https://acme.example",
        focus_products=["headphones"],
    )

    urls, notes = _pipeline()._discover(target, demo=False)

    assert urls == [
        "https://acme.example/pricing",
        "https://acme.example/products/headphones",
    ]
    assert notes == []


def test_empty_discovery_reports_website_problem_not_focus_mismatch(monkeypatch):
    monkeypatch.setattr(orchestrator, "discover_urls_for_entity", lambda website: [])
    target = CompetitorTarget(
        company="Acme",
        website="https://acme.example",
        focus_products=["wireless headphones"],
    )

    urls, notes = _pipeline()._discover(target, demo=False)

    assert urls == []
    assert len(notes) == 1
    assert "no usable pages found" in notes[0]
    assert "focus filter" not in notes[0]
