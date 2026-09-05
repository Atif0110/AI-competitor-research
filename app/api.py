"""FastAPI service — research runs, offers, insights, reports, metrics,
geo-proxy status and schedules.

v4: Pydantic request models (ReviewQueryRequest, ScheduleRequest) give proper
422 validation (#18); enum query params parse via util.parse_region with 422
instead of 500 (#19); /reports/{run_id} sanitizes the run ID before touching
the filesystem (#20); every insights endpoint accepts run_id so analysis is
scoped to one run (#1).
Run:  uvicorn app.api:app --reload     Docs: http://localhost:8000/docs
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl

from app.analysis.insights import InsightsEngine
from app.config import settings
from app.orchestrator import Pipeline
from app.schemas import Competitor, CompetitorTarget, PipelineResult, Region
from app.storage.db import OfferStore
from app.storage.vector import ReviewStore
from app.util import is_safe_run_id, parse_region

logger = logging.getLogger(__name__)

_store = OfferStore()
_reviews = ReviewStore()
_pipeline = Pipeline(store=_store, review_store=_reviews)
_insights = InsightsEngine(_store, _reviews, _pipeline.extractor.client)


class ReviewQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    region: Optional[Region] = None
    run_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    n: int = Field(default=8, ge=1, le=50)


class ScheduleRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=150)
    company: str = Field(min_length=1, max_length=150)
    website: HttpUrl
    regions: list[Region] = Field(default_factory=lambda: [Region.US])
    focus_products: list[str] = Field(default_factory=list)
    competitors: list[Competitor] = Field(default_factory=list)
    interval_hours: int = Field(default=24, ge=1, le=720)


def create_app():
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse

    settings.ensure_dirs()
    app = FastAPI(title=settings.app_name, version="4.0.0")

    if settings.scheduler_autostart:
        from app.scheduler import get_scheduler
        get_scheduler(_pipeline).start()

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "demo" if settings.demo_mode else "live",
                "offers": _store.count(), "review_backend": _reviews.backend,
                "scheduler": settings.scheduler_autostart}

    @app.post("/research", response_model=PipelineResult)
    def research(target: CompetitorTarget):
        try:
            return _pipeline.run(target)
        except Exception as e:
            logger.exception("research run failed")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/offers")
    def offers(product: str | None = None, region: str | None = None,
               run_id: str | None = None, limit: int = 200):
        try:
            reg = parse_region(region)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid region: {region}")
        if run_id and not is_safe_run_id(run_id):
            raise HTTPException(status_code=422, detail="invalid run_id")
        return [o.model_dump(mode="json")
                for o in _store.recent_offers(product=product, region=reg, limit=limit,
                                              run_id=run_id)]

    # ---- insights (all run-scoped; #1) ----
    @app.get("/insights/undercut")
    def undercut(region: str | None = None, run_id: str | None = None):
        try:
            reg = parse_region(region)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid region: {region}")
        return [u.model_dump(mode="json") for u in _insights.undercut_analysis(reg, run_id)]

    @app.get("/insights/price-moves")
    def price_moves(days_back: int = 30, run_id: str | None = None):
        return [m.model_dump(mode="json") for m in _insights.price_moves(days_back, run_id)]

    @app.get("/insights/sentiment")
    def sentiment(product: str | None = None, region: str | None = None,
                  run_id: str | None = None):
        try:
            reg = parse_region(region)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid region: {region}")
        return _insights.sentiment(product=product, region=reg, run_id=run_id).model_dump(mode="json")

    @app.get("/insights/regional")
    def regional(run_id: str | None = None):
        return _insights.regional_snapshot(run_id)

    @app.get("/insights/positioning")
    def positioning(product: str, region: str | None = None, run_id: str | None = None):
        try:
            reg = parse_region(region)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid region: {region}")
        return _insights.positioning_brief(product, reg, run_id)

    @app.get("/insights/events")
    def events(run_id: str | None = None):
        return [e.model_dump(mode="json") for e in _insights.detect_events(run_id or "")]

    # ---- RAG Q&A with citations (#12: honest no-retrieval answer) ----
    @app.post("/insights/reviews/query")
    def reviews_query(body: ReviewQueryRequest):
        return _insights.answer_question(body.question, region=body.region,
                                         n=body.n, run_id=body.run_id)

    # ---- metrics ----
    @app.get("/metrics")
    def metrics(limit: int = 20):
        return _store.recent_runs(limit=limit)

    @app.get("/metrics/aggregate")
    def metrics_aggregate():
        return _store.aggregate_metrics()

    # ---- geo proxy status ----
    @app.get("/proxies/status")
    def proxies_status():
        from app.scrapers.base import ProxyPool

        pool = ProxyPool(settings.proxy_urls, settings.proxy_regions)
        return {
            "generic": len(settings.proxy_urls),
            "regions": {k: len(v) for k, v in settings.proxy_regions.items()},
            "verify_exit_ip": settings.proxy_verify,
        }

    # ---- schedules (Pydantic body; #18) ----
    @app.post("/schedules")
    def create_schedule(body: ScheduleRequest):
        sid = _store.add_schedule(
            name=body.name or body.company,
            company=body.company, website=str(body.website),
            regions=[r.value for r in body.regions],
            focus_products=body.focus_products,
            competitors_json=json.dumps([c.model_dump() for c in body.competitors]),
            interval_hours=body.interval_hours,
        )
        return {"id": sid}

    @app.get("/schedules")
    def list_schedules():
        return _store.list_schedules()

    @app.delete("/schedules/{sid}")
    def delete_schedule(sid: int):
        if not _store.delete_schedule(sid):
            raise HTTPException(status_code=404, detail="schedule not found")
        return {"deleted": sid}

    @app.get("/reports")
    def list_reports():
        import os

        out = settings.report_output_dir
        return sorted(f for f in os.listdir(out) if f.startswith("report_") and f.endswith(".pdf"))

    @app.get("/reports/{run_id}")
    def get_report(run_id: str):
        import os

        # sanitize before touching the filesystem (#20)
        if not is_safe_run_id(run_id):
            raise HTTPException(status_code=422,
                                detail="run_id may only contain A-Za-z0-9 _ -")
        path = os.path.join(settings.report_output_dir, f"report_{run_id}.pdf")
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="report not found")
        return FileResponse(path, media_type="application/pdf", filename=os.path.basename(path))

    return app


app = create_app()
