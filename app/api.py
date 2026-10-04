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
from datetime import datetime, timezone
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


class DeepResearchRequest(BaseModel):
    mode: str = Field(default="url", pattern=r"^(url|topic|compare)$")
    url: Optional[str] = None
    topic: Optional[str] = None
    competitors: list[str] = Field(default_factory=list)
    question: str = Field(default="", max_length=1000)
    max_pages: int = Field(default=0, ge=0, le=60)
    max_depth: int = Field(default=0, ge=0, le=4)
    use_search: bool = True
    use_llm: bool = True


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    run_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    session_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    top_k: Optional[int] = Field(default=None, ge=1, le=25)


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
        _set_job(
            job_id,
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        def on_progress(event: dict) -> None:
            events.append(event)
            _set_job(job_id, events=list(events), current_event=event)
        pipeline = Pipeline(store=_store, review_store=_reviews, progress_callback=on_progress)
        result = pipeline.run(target, run_id=job_id)
        _set_job(job_id, status="completed", result=result.model_dump(mode="json"), events=events)
    except Exception as exc:
        logger.exception("research job %s failed", job_id)
        _set_job(job_id, status="failed", error=str(exc), events=events)


_deep_pipeline_instance = None


def _deep_pipeline():
    """Lazily built deep-research pipeline, shared across requests.

    Rebuilding it per request would re-initialise the document store schema and
    the LLM client chain on every call.
    """
    global _deep_pipeline_instance
    if _deep_pipeline_instance is None:
        from app.research.pipeline import DeepResearchPipeline

        _deep_pipeline_instance = DeepResearchPipeline(
            store=_store, llm=_pipeline.extractor.client
        )
    return _deep_pipeline_instance


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
            "status": "ok",
            "version": app.version,
            "mode": "demo" if settings.demo_mode else "live",
            "demo_mode": settings.demo_mode,
            "storage_backend": "postgresql" if _store._postgres else "sqlite",
            "offers": _store.count(), "review_backend": _reviews.backend,
            "scheduler": settings.scheduler_autostart,
            "auth_required": bool(settings.api_key_required),
            "active_llm_provider": client.provider_name,
            "active_llm_model": client.active_model,
            "llm_provider_chain": client.chain,
            "provider_tiers": settings.provider_tiers,
            "llm_models": {
                "apinex": settings.apinex_model if settings.apinex_api_key else None,
                "openai": settings.openai_model if settings.openai_api_key else None,
                "anthropic": settings.anthropic_model if settings.anthropic_api_key else None,
                "groq": settings.groq_model if settings.groq_api_key else None,
                "gemini": settings.gemini_model if settings.gemini_api_key else None,
            },
            "web_tools": bool(settings.apinex_api_key and settings.apinex_web_tools),
            "deep_research": {
                "enabled": settings.deep_llm_enabled,
                "max_pages": settings.deep_max_pages,
                "max_depth": settings.deep_max_depth,
                "search": settings.deep_search_enabled,
                "chat": {
                    "top_k": settings.chat_top_k,
                    "min_relevance": settings.chat_min_relevance,
                    "min_coverage": settings.chat_min_coverage,
                },
            },
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

    # ------------------------------------------------------------------
    # deep research (url / topic / compare)
    # ------------------------------------------------------------------
    @app.post("/research/deep", dependencies=[Depends(_require_api_key), Depends(_research_rate_limit)])
    def deep_research(payload: DeepResearchRequest):
        from app.research.models import ResearchMode, ResearchRequest

        mode = ResearchMode(payload.mode)
        if mode is ResearchMode.url and not payload.url:
            raise HTTPException(status_code=422, detail="url is required for mode=url")
        if mode is ResearchMode.topic and not (payload.topic or payload.question):
            raise HTTPException(status_code=422, detail="topic is required for mode=topic")

        request = ResearchRequest(
            mode=mode,
            url=payload.url,
            topic=payload.topic,
            competitors=[c for c in payload.competitors if c][:8],
            question=payload.question,
            max_pages=payload.max_pages or None,
            max_depth=payload.max_depth if payload.max_depth else None,
            use_search=payload.use_search,
            use_llm=payload.use_llm,
        )
        try:
            result = _deep_pipeline().run(request)
        except Exception as exc:
            logger.exception("deep research failed")
            raise HTTPException(status_code=500, detail=str(exc))
        return json.loads(result.model_dump_json())

    @app.get("/research/deep/runs")
    def deep_research_runs(limit: int = 25):
        limit = max(1, min(limit, 200))
        return _deep_pipeline().documents.list_runs(limit=limit)

    @app.get("/research/deep/runs/{run_id}")
    def deep_research_run(run_id: str):
        if not is_safe_run_id(run_id):
            raise HTTPException(status_code=422, detail="invalid run_id")
        result = _deep_pipeline().load(run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="research run not found")
        return json.loads(result.model_dump_json())

    # NOTE: literal /research/chat paths are declared before the parameterised
    # routes below so "chat" is never read as a session_id.
    @app.post("/research/chat", dependencies=[Depends(_require_api_key), Depends(_research_rate_limit)])
    def research_chat(payload: ChatRequest):
        from app.research.chat import ResearchChat

        chat = ResearchChat(_deep_pipeline().documents)
        answer = chat.ask(
            payload.question,
            run_id=payload.run_id,
            session_id=payload.session_id,
            top_k=payload.top_k,
        )
        return json.loads(answer.model_dump_json())

    @app.get("/research/chat/{session_id}")
    def research_chat_history(session_id: str, limit: int = 100):
        if not is_safe_run_id(session_id):
            raise HTTPException(status_code=422, detail="invalid session_id")
        documents = _deep_pipeline().documents
        session = documents.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="chat session not found")
        return {
            "session_id": session_id,
            "run_id": session.get("run_id"),
            "title": session.get("title"),
            "messages": documents.messages(session_id, limit=max(1, min(limit, 500))),
        }

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

    @app.get("/evidence/content")
    def evidence_content(source_url: HttpUrl, run_id: str | None = None):
        if run_id and not is_safe_run_id(run_id):
            raise HTTPException(status_code=422, detail="invalid run_id")
        content = _store.get_evidence(str(source_url), run_id=run_id)
        if content is None:
            raise HTTPException(status_code=404, detail="evidence not found")
        return {"source_url": str(source_url), "run_id": run_id, "content": content}

    # NOTE: declared after /evidence/content on purpose. A literal path
    # segment must win over the /evidence/{run_id} parameter, otherwise
    # "content" is read as a run_id and this endpoint becomes unreachable.
    @app.get("/evidence/{run_id}")
    def evidence(run_id: str, limit: int = 50):
        if not is_safe_run_id(run_id):
            raise HTTPException(status_code=422, detail="invalid run_id")
        return _store.evidence_for_run(run_id, limit=max(1, min(limit, 200)))

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

    @app.get("/insights/changes")
    def changes(target_company: str | None = None, limit: int = 50):
        return _insights.cross_run_changes(target_company=target_company, limit=max(1, min(limit, 200)))

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
