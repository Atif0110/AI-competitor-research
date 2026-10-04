"""Deep research engine.

Turns any URL, question or topic into an evidence-backed research dossier:

    plan -> fetch/crawl -> classify -> extract facts -> synthesise -> cite

The competitive-offer pipeline in ``app/orchestrator.py`` stays the product
core; this package is the research layer that runs on top of it.
"""
from app.research.models import (
    Citation,
    DeepResearchResult,
    Fact,
    PageDocument,
    ResearchPlan,
    ResearchRequest,
    ResearchSection,
    SearchHit,
)
from app.research.pipeline import DeepResearchPipeline

__all__ = [
    "Citation",
    "DeepResearchPipeline",
    "DeepResearchResult",
    "Fact",
    "PageDocument",
    "ResearchPlan",
    "ResearchRequest",
    "ResearchSection",
    "SearchHit",
]