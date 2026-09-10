"""FastAPI service for research, intelligence, evidence, reports and schedules.

The API is the single application boundary for the production frontend. Write
and expensive endpoints require X-API-Key when API_KEY_REQUIRED=true. Demo mode
remains usable without credentials so the repository can be evaluated offline.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import uuid
from typing import Optional

from fastapi import BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
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
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_rate_lock = threading.Lock()
_rate_window: dict[str, list[float]] = {}


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


class JobCreateRequest(CompetitorTarget):
    pass


def _require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    """Constant-time API-key guard for expensive/mutating endpoints."""
    if settings.api_key_required and settings.api_key:
        if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
            raise HTTPException(status_code=401, detail="invalid or missing API key")
    elif settings.api_key_required and not settings.demo_mode:
        # Production deployments should fail closed if configured incorrectly.
        raise HTTPException(status_code=503, detail="API authentication is not configured")


def _research_rate_limit(request: Request) -> None:
    """Small single-process guard against accidental runaway scraping/LLM spend.
    Put a real distributed limiter in front of multi-instance deployments.
    """
    from fastapi import Request
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _rate_lock:
        values = [t for t in _rate_window.get(ip, []) if now - t < 60]
        if len(values) >= settings.research_rate_limit_per_minute:
            raise HTTPException(status_code=429, detail="research rate limit exceeded; try again later")
        values.append(now)
        _rate_window[ip] = values

def _set_job(job_id: str, **updates) -> None:
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).update(updates)


def _run_job(job_id: str, target: CompetitorTarget) -> None:
    events: list[dict] = []
    try:
        _set_job(job_id, status="running", started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
        def on_progress(event: dict) -> None:
            events.append(event)
            _set_job(job_id, events=list(events), current_event=event)
        pipeline = Pipeline(store=_store, review_store=_reviews, progress_callback=on_progress)
        result = pipeline.run(target, run_id=job_id)
        _set_job(job_id, status="completed", result=result.model_dump(mode="json"), events=events)
    except Exception as exc:
        logger.exception("research job %s failed", job_id)
        _set_job(job_id, status="failed", error=str(exc), events=events)


def create_app():
    from fastapi import FastAPI
    from fastapi.responses import FileResponse

    settings.ensure_dirs()
    if settings.api_key_required and not settings.demo_mode and not settings.api_key:
        logger.error("API_KEY_REQUIRED=true but API_KEY is missing; live service will reject protected endpoints")

    app = FastAPI(title=settings.app_name, version="5.0.0", docs_url="/docs", redoc_url="/redoc")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if settings.scheduler_autostart:
        from app.scheduler import get_scheduler
        get_scheduler(_pipeline).start()

    @app.get("/health")
    def health():
        client = _pipeline.extractor.client
        return {
            "status": "ok", "mode": "demo" if settings.demo_mode else "live",
            "storage_backend": "postgresql" if _store._postgres else "sqlite",
            "offers": _store.count(), "review_backend": _reviews.backend,
            "scheduler": settings.scheduler_autostart,
            "auth_required": bool(settings.api_key_required),
            "active_llm_provider": client.provider_name,
            "llm_provider_chain": [n for n, _ in client._providers],
        }

    @app.post("/research", response_model=PipelineResult, dependencies=[Depends(_require_api_key), Depends(_research_rate_limit)])
    def research(target: CompetitorTarget):
        try:
            return _pipeline.run(target)
        except Exception as e:
            logger.exception("research run failed")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/research/jobs", dependencies=[Depends(_require_api_key), Depends(_research_rate_limit)])
    def create_research_job(target: JobCreateRequest, background_tasks: BackgroundTasks):
        job_id = uuid.uuid4().hex[:12]
        _set_job(job_id, status="queued", target=target.model_dump(mode="json"), events=[])
        background_tasks.add_task(_run_job, job_id, target)
        return {"job_id": job_id, "status": "queued"}

    @app.get("/research/jobs/{job_id}", dependencies=[Depends(_require_api_key)])
    def research_job(job_id: str):
        if not is_safe_run_id(job_id):
            raise HTTPException(status_code=422, detail="invalid job_id")
        with _jobs_lock:
            job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return {"job_id": job_id, **job}

    @app.get("/offers")
    def offers(product: str | None = None, region: str | None = None, run_id: str | None = None, limit: int = 200):
        try:
            reg = parse_region(region)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid region: {region}")
        if run_id and not is_safe_run_id(run_id):
            raise HTTPException(status_code=422, detail="invalid run_id")
        limit = max(1, min(limit, 1000))
        return [o.model_dump(mode="json") for o in _store.recent_offers(product=product, region=reg, limit=limit, run_id=run_id)]

    @app.get("/evidence/{run_id}")
    def evidence(run_id: str, limit: int = 50):
        if not is_safe_run_id(run_id):
            raise HTTPException(status_code=422, detail="invalid run_id")
        return _store.evidence_for_run(run_id, limit=max(1, min(limit, 200)))

    @app.get("/evidence/content")
    def evidence_content(source_url: HttpUrl, run_id: str | None = None):
        if run_id and not is_safe_run_id(run_id):
            raise HTTPException(status_code=422, detail="invalid run_id")
        content = _store.get_evidence(str(source_url), run_id=run_id)
        if content is None:
            raise HTTPException(status_code=404, detail="evidence not found")
        return {"source_url": str(source_url), "run_id": run_id, "content": content}

    @app.get("/insights/undercut")
    def undercut(region: str | None = None, run_id: str | None = None):
        try:
            reg = parse_region(region)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid region: {region}")
        return [u.model_dump(mode="json") for u in _insights.undercut_analysis(reg, run_id)]

    @app.get("/insights/price-moves")
    def price_moves(days_back: int = 30, run_id: str | None = None):
        return [m.model_dump(mode="json") for m in _insights.price_moves(max(1, min(days_back, 365)), run_id)]

    @app.get("/insights/sentiment")
    def sentiment(product: str | None = None, region: str | None = None, run_id: str | None = None):
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

    @app.post("/insights/reviews/query", dependencies=[Depends(_require_api_key)])
    def reviews_query(body: ReviewQueryRequest):
        return _insights.answer_question(body.question, region=body.region, n=body.n, run_id=body.run_id)

    @app.get("/metrics")
    def metrics(limit: int = 20):
        return _store.recent_runs(limit=max(1, min(limit, 100)))

    @app.get("/metrics/aggregate")
    def metrics_aggregate():
        return _store.aggregate_metrics()

    @app.get("/proxies/status")
    def proxies_status():
        from app.scrapers.base import ProxyPool
        pool = ProxyPool(settings.proxy_urls, settings.proxy_regions)
        return {"generic": len(settings.proxy_urls), "regions": {k: len(v) for k, v in settings.proxy_regions.items()}, "verify_exit_ip": settings.proxy_verify}

    @app.post("/schedules", dependencies=[Depends(_require_api_key)])
    def create_schedule(body: ScheduleRequest):
        sid = _store.add_schedule(
            name=body.name or body.company, company=body.company, website=str(body.website),
            regions=[r.value for r in body.regions], focus_products=body.focus_products,
            competitors_json=json.dumps([c.model_dump() for c in body.competitors]), interval_hours=body.interval_hours,
        )
        return {"id": sid}

    @app.get("/schedules")
    def list_schedules():
        return _store.list_schedules()

    @app.delete("/schedules/{sid}", dependencies=[Depends(_require_api_key)])
    def delete_schedule(sid: int):
        if not _store.delete_schedule(sid):
            raise HTTPException(status_code=404, detail="schedule not found")
        return {"deleted": sid}

    @app.get("/reports")
    def list_reports():
        import os
        return sorted(f for f in os.listdir(settings.report_output_dir) if f.startswith("report_") and f.endswith(".pdf"))

    @app.get("/reports/{run_id}")
    def get_report(run_id: str):
        import os
        if not is_safe_run_id(run_id):
            raise HTTPException(status_code=422, detail="invalid run_id")
        path = os.path.join(settings.report_output_dir, f"report_{run_id}.pdf")
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="report not found")
        return FileResponse(path, media_type="application/pdf", filename=os.path.basename(path))

    return app


app = create_app()
