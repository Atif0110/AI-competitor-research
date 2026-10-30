import threading
import time

from app import orchestrator
from app.focus import discovery_focus_terms
from app.orchestrator import Pipeline
from app.schemas import Competitor, CompetitorTarget


def _pipeline():
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.progress_callback = None
    return pipeline


def test_focus_filter_prefers_category_pages_and_bounds_rival_fallback(monkeypatch):
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
    monkeypatch.setattr(orchestrator, "discover_urls_for_entity", lambda website, focus_products=(): pages[website])
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
        "https://rival.example/products/speakers",
    ]
    assert len(notes) == 1
    assert "no URL path matched" in notes[0]


def test_focus_match_keeps_matching_and_pricing_candidates(monkeypatch):
    pages = {
        "https://acme.example": [
            "https://acme.example/products/headphones",
            "https://acme.example/pricing",
            "https://acme.example/about",
        ],
    }
    monkeypatch.setattr(orchestrator, "discover_urls_for_entity", lambda website, focus_products=(): pages[website])
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


def test_focus_hint_is_translated_to_category_for_competitors(monkeypatch):
    observed = {}

    def discover(website, focus_products=()):
        observed[website] = list(focus_products)
        return [f"{website}/products/item"]

    monkeypatch.setattr(orchestrator, "discover_urls_for_entity", discover)
    target = CompetitorTarget(
        company="Samsung",
        website="https://samsung.example",
        focus_products=["Galaxy S"],
        competitors=[Competitor(name="Apple", website="https://apple.example")],
    )

    _pipeline()._discover(target, demo=False)

    assert observed == {
        "https://samsung.example": ["Galaxy S"],
        "https://apple.example": list(discovery_focus_terms(["Galaxy S"], competitor=True)),
    }
    assert "iphone" in observed["https://apple.example"]


def test_empty_discovery_reports_website_problem_not_focus_mismatch(monkeypatch):
    monkeypatch.setattr(orchestrator, "discover_urls_for_entity", lambda website, focus_products=(): [])
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


def test_entity_discovery_runs_concurrently_but_keeps_input_order(monkeypatch):
    lock = threading.Lock()
    active = 0
    peak = 0
    pages = {
        "https://acme.example": ["https://acme.example/products/a"],
        "https://rival.example": ["https://rival.example/products/b"],
    }

    def discover(website, focus_products=()):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        return pages[website]

    monkeypatch.setattr(orchestrator, "discover_urls_for_entity", discover)
    target = CompetitorTarget(
        company="Acme",
        website="https://acme.example",
        competitors=[Competitor(name="Rival", website="https://rival.example")],
    )

    urls, errors = _pipeline()._discover(target, demo=False)

    assert peak == 2
    assert urls == [
        "https://acme.example/products/a",
        "https://rival.example/products/b",
    ]
    assert errors == []
