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
from typing import Annotated, Optional

from fastapi import BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, HttpUrl, field_validator
from fastapi.responses import JSONResponse

from app.analysis.insights import InsightsEngine
from app.config import settings
from app.orchestrator import Pipeline
from app.schemas import Competitor, CompetitorTarget, PipelineResult, Region
from app.storage.db import OfferStore
from app.storage.vector import ReviewStore
from app.util import is_safe_run_id, parse_region
from app.url_safety import is_public_http_url

logger = logging.getLogger(__name__)
_store = OfferStore()
_reviews = ReviewStore()
_pipeline = Pipeline(store=_store, review_store=_reviews)
_insights = InsightsEngine(_store, _reviews, _pipeline.extractor.client)
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_rate_lock = threading.Lock()
_rate_window: dict[str, list[float]] = {}
_public_hour_window: dict[str, list[float]] = {}


class ReviewQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    region: Optional[Region] = None
    run_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    n: int = Field(default=8, ge=1, le=50)


class DeepResearchRequest(BaseModel):
    mode: str = Field(default="url", pattern=r"^(url|topic|compare)$")
    url: Optional[str] = Field(default=None, max_length=2048)
    source_urls: list[Annotated[str, Field(min_length=4, max_length=2048)]] = Field(default_factory=list, max_length=10)
    topic: Optional[str] = Field(default=None, max_length=200)
    company: Optional[str] = Field(default=None, max_length=200)
    competitors: list[Annotated[str, Field(min_length=1, max_length=150)]] = Field(default_factory=list, max_length=10)
    question: str = Field(default="", max_length=1000)
    max_pages: int = Field(default=0, ge=0, le=60)
    max_depth: int = Field(default=0, ge=0, le=4)
    use_search: bool = True
    use_llm: bool = True

    @field_validator("url")
    @classmethod
    def validate_public_url(cls, value):
        if value is not None:
            candidate = value.strip()
            if "://" not in candidate:
                candidate = "https://" + candidate
            if not is_public_http_url(candidate):
                raise ValueError("url must be a public HTTP(S) URL without credentials or a custom port")
            return candidate
        return value

    @field_validator("source_urls")
    @classmethod
    def validate_public_source_urls(cls, values):
        normalized = []
        for value in values:
            candidate = value.strip()
            if "://" not in candidate:
                candidate = "https://" + candidate
            if not is_public_http_url(candidate):
                raise ValueError("each source URL must be a public HTTP(S) URL without credentials or a custom port")
            if candidate not in normalized:
                normalized.append(candidate)
        return normalized


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
    """Constant-time API-key guard for private/mutating endpoints."""
    if settings.api_key_required and settings.api_key:
        if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
            raise HTTPException(status_code=401, detail="invalid or missing API key")
    elif settings.api_key_required and not settings.demo_mode:
        # Production deployments should fail closed if configured incorrectly.
        raise HTTPException(status_code=503, detail="API authentication is not configured")


def _require_public_research_or_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
) -> None:
    """Allow only the visitor-facing research API through in public mode."""
    path = request.url.path
    public_write = request.method == "POST" and path in {
        "/research", "/research/jobs", "/research/deep", "/research/chat",
    }
    public_job_read = request.method == "GET" and path.startswith("/research/jobs/")
    if settings.public_workspace_mode and (public_write or public_job_read):
        return
    _require_api_key(x_api_key)


def _remove_finished_job(job_id: str) -> None:
    """Keep only active workers in RAM; durable job snapshots live in storage."""
    with _jobs_lock:
        _jobs.pop(job_id, None)


def _research_rate_limit(request: Request) -> None:
    """Small single-process guard against accidental runaway scraping/LLM spend.
    Put a real distributed limiter in front of multi-instance deployments.
    """
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    path = request.url.path
    if settings.public_workspace_mode:
        # Bound anonymous hosted usage even when the shared deployment key is
        # disabled. New crawls are costly; chat requests receive a separate cap.
        group = "chat" if path == "/research/chat" else "research"
        minute_cap = 12 if group == "chat" else 2
        hour_cap = 40 if group == "chat" else 12
        key = f"{group}:{ip}"
        with _rate_lock:
            minute_values = [t for t in _rate_window.get(key, []) if now - t < 60]
            if len(minute_values) >= minute_cap:
                raise HTTPException(status_code=429, detail="public workspace limit reached; try again shortly")
            hour_values = [t for t in _public_hour_window.get(key, []) if now - t < 3600]
            if len(hour_values) >= hour_cap:
                raise HTTPException(status_code=429, detail="public workspace hourly research limit reached; try again later")
            minute_values.append(now)
            hour_values.append(now)
            _rate_window[key] = minute_values
            _public_hour_window[key] = hour_values
        return
    with _rate_lock:
        values = [t for t in _rate_window.get(ip, []) if now - t < 60]
        if len(values) >= settings.research_rate_limit_per_minute:
            raise HTTPException(status_code=429, detail="research rate limit exceeded; try again later")
        values.append(now)
        _rate_window[ip] = values


def _validate_public_target(target: CompetitorTarget) -> None:
    if settings.public_workspace_mode and (len(target.competitors) > 4 or len(target.regions) > 3):
        raise HTTPException(status_code=422, detail="public research is limited to four competitors and three regions per run")

def _set_job(job_id: str, **updates) -> None:
    with _jobs_lock:
        job = _jobs.setdefault(job_id, {})
        job.update(updates)
        job.setdefault("job_id", job_id)
        snapshot = dict(job)
    try:
        _store.save_job(job_id, snapshot)
    except Exception:
        logger.exception("could not persist research job %s", job_id)


def _progress_for_job(job_id: str, event: dict) -> None:
    """Persist a bounded event history and the most recent worker activity."""
    with _jobs_lock:
        current = dict(_jobs.get(job_id) or {})
    events = list(current.get("events") or [])
    events.append(event)
    _set_job(job_id, events=events[-200:], current_event=event)


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
            _progress_for_job(job_id, event)
        pipeline = Pipeline(store=_store, review_store=_reviews, progress_callback=on_progress)
        result = pipeline.run(target, run_id=job_id)
        result_payload = result.model_dump(mode="json")
        result_payload["offers_captured"] = _store.count(run_id=job_id)
        metrics = result_payload.get("metrics") or {}
        if not metrics.get("urls_attempted"):
            _set_job(
                job_id,
                status="failed",
                error=(
                    "No source pages could be checked. Confirm the company and competitor "
                    "websites, then clear or broaden Focus products and try again."
                ),
                result=result_payload,
                events=events,
                chat_ready=False,
                chat_error=None,
            )
            _remove_finished_job(job_id)
            return
        chat_ready = False
        chat_error = None
        try:
            _progress_for_job(job_id, {
                "event": "chat_indexing_started",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": job_id,
                "product_name": "preparing evidence for questions",
            })
            _index_competitive_run_for_chat(_deep_pipeline().documents, job_id, target)
            chat_ready = True
        except Exception:
            chat_error = "Evidence indexing failed; open the captured evidence and retry chat later."
            logger.exception("could not index competitive run %s for grounded chat", job_id)
        _set_job(job_id, status="completed", result=result_payload, events=events,
                 chat_ready=chat_ready, chat_error=chat_error)
        _remove_finished_job(job_id)
    except Exception:
        logger.exception("research job %s failed", job_id)
        _set_job(
            job_id,
            status="failed",
            error="Research failed unexpectedly. Retry the run; contact the operator if it continues.",
            events=events,
        )
        _remove_finished_job(job_id)


def _index_competitive_run_for_chat(documents, run_id: str, target: CompetitorTarget) -> None:
    """Make competitive page text and structured offers retrievable by grounded chat."""
    from urllib.parse import urlparse

    from app.research.models import Fact, PageDocument, PageType, ResearchSection
    from app.discovery import canonical_url

    existing = documents.get_run(run_id)
    if existing and (existing.get("coverage") or {}).get("competitive_indexed"):
        return
    if existing:
        documents.delete_run(run_id)

    title = f"{target.company} competitive research"
    objective = f"Competitive research for {target.company}; answer using only pages and offers captured in this run."
    documents.create_run(run_id, "compare", objective, str(target.website), title)

    source_refs: dict[str, tuple[str, Optional[int]]] = {}
    sections = []
    facts = []
    fetched_pages = 0
    source_rows = _store.evidence_for_run(run_id, limit=200)
    for source in source_rows:
        url = source.get("source_url") or ""
        content = _store.get_evidence(url, run_id=run_id) or ""
        if not url or not content.strip():
            continue
        ref = f"S{len(source_refs) + 1}"
        page_title = urlparse(url).netloc or url
        document = PageDocument(
            url=url, title=page_title, text=content, markdown=content,
            status="fetched", page_type=PageType.product, source_ref=ref,
            content_hash=source.get("content_hash") or "",
        )
        page_id = documents.save_page(run_id, document)
        fetched_pages += 1
        source_refs[canonical_url(url)] = (ref, page_id)
        sections.append(ResearchSection(
            run_id=run_id, page_id=page_id, source_ref=ref, url=url,
            title=page_title, heading="Captured source page", content=content[:30000],
            page_type=PageType.product,
        ))

    offers = _store.recent_offers(limit=1000, run_id=run_id)
    for offer in offers:
        url = offer.url
        source_key = canonical_url(offer.canonical_url or offer.url)
        if source_key not in source_refs:
            ref, page_id = f"S{len(source_refs) + 1}", None
            source_refs[source_key] = (ref, page_id)
        ref, page_id = source_refs[source_key]
        price = f"{offer.currency.value} {offer.price:,.2f}"
        statement = (
            f"{offer.product_name} is listed at {price} in {offer.region.value}; "
            f"availability: {offer.availability.value}."
        )
        details = [statement]
        if offer.competitor:
            details.append(f"Competitor: {offer.competitor}.")
        if offer.seller:
            details.append(f"Seller: {offer.seller}.")
        if offer.listing_title:
            details.append(f"Listing: {offer.listing_title}.")
        sections.append(ResearchSection(
            run_id=run_id, page_id=page_id, source_ref=ref, url=url,
            title=offer.product_name, heading="Captured structured offer",
            content=" ".join(details), page_type=PageType.pricing,
        ))
        facts.append(Fact(
            page_id=page_id, source_ref=ref, url=url, subject=offer.product_name,
            question=f"What is the {offer.region.value} price and availability?",
            statement=statement, value=price, confidence=offer.extraction_confidence or 0.65,
            kind="price", quote=offer.listing_title or statement,
        ))

    documents.save_sections(run_id, sections)
    documents.save_facts(run_id, facts)
    documents.update_run(
        run_id, status="completed",
        coverage={"competitive_indexed": True, "pages_fetched": fetched_pages, "pages_failed": 0,
                  "sections": len(sections), "facts": len(facts),
                  "characters": sum(len(section.content) for section in sections)},
    )


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
    try:
        _store.reconcile_jobs_after_restart()
    except Exception:
        logger.exception("could not reconcile persisted research jobs on startup")
    if settings.api_key_required and not settings.public_workspace_mode and not settings.demo_mode and not settings.api_key:
        logger.error("API_KEY_REQUIRED=true but API_KEY is missing; live service will reject protected endpoints")

    app = FastAPI(title=settings.app_name, version="5.0.0", docs_url="/docs", redoc_url="/redoc")

    @app.middleware("http")
    async def require_auth_for_private_reads(request: Request, call_next):
        # Public mode intentionally exposes the visitor workspace. When an
        # operator switches to private mode, protect every read route too,
        # including history, reports, metrics, and OpenAPI documentation.
        if (
            not settings.public_workspace_mode
            and request.method in {"GET", "HEAD"}
            and request.url.path != "/health"
        ):
            try:
                _require_api_key(request.headers.get("x-api-key"))
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                )
        return await call_next(request)

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
            "auth_required": bool(settings.api_key_required and not settings.public_workspace_mode),
            "public_workspace": bool(settings.public_workspace_mode),
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

    @app.post("/research", response_model=PipelineResult, dependencies=[Depends(_require_public_research_or_api_key), Depends(_research_rate_limit)])
    def research(target: CompetitorTarget):
        _validate_public_target(target)
        try:
            return _pipeline.run(target)
        except Exception:
            error_id = uuid.uuid4().hex[:12]
            logger.exception("research run failed (reference=%s)", error_id)
            raise HTTPException(status_code=500, detail=f"Research failed unexpectedly. Reference: {error_id}")

    @app.post("/research/jobs", dependencies=[Depends(_require_public_research_or_api_key), Depends(_research_rate_limit)])
    def create_research_job(target: JobCreateRequest, background_tasks: BackgroundTasks):
        _validate_public_target(target)
        job_id = uuid.uuid4().hex[:12]
        target_data = target.model_dump(mode="json")
        if settings.public_workspace_mode:
            with _jobs_lock:
                active = sum(job.get("status") in {"queued", "running"} for job in _jobs.values())
                if active >= 2:
                    raise HTTPException(status_code=429, detail="the public research queue is busy; try again when a run finishes")
                # Reserve the slot while holding the same lock used by other
                # public submissions, so two concurrent requests cannot both
                # observe the final available slot.
                _jobs[job_id] = {"job_id": job_id, "status": "queued", "target": target_data, "events": []}
        _set_job(job_id, status="queued", target=target_data, events=[])
        background_tasks.add_task(_run_job, job_id, target)
        return {"job_id": job_id, "status": "queued"}

    @app.get("/research/jobs/{job_id}", dependencies=[Depends(_require_public_research_or_api_key)])
    def research_job(job_id: str):
        if not is_safe_run_id(job_id):
            raise HTTPException(status_code=422, detail="invalid job_id")
        with _jobs_lock:
            job = dict(_jobs[job_id]) if job_id in _jobs else None
        if job is None:
            job = _store.get_job(job_id)
        if job is None:
            metrics = next((item for item in _store.recent_runs(limit=100) if item.get("run_id") == job_id), None)
            if metrics:
                return {
                    "job_id": job_id, "status": "completed", "events": [],
                    "result": {"duration_s": metrics.get("runtime_s", 0), "metrics": metrics},
                }
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return {"job_id": job_id, **job}

    # ------------------------------------------------------------------
    # deep research (url / topic / compare)
    # ------------------------------------------------------------------
    @app.post("/research/deep", dependencies=[Depends(_require_public_research_or_api_key), Depends(_research_rate_limit)])
    def deep_research(payload: DeepResearchRequest):
        from app.research.models import ResearchMode, ResearchRequest

        if settings.public_workspace_mode:
            if len(payload.competitors) > 4:
                raise HTTPException(status_code=422, detail="public deep research is limited to four competitors per run")
            if payload.max_pages > settings.deep_max_pages or payload.max_depth > settings.deep_max_depth:
                raise HTTPException(status_code=422, detail="requested crawl exceeds the public workspace page/depth limits")

        mode = ResearchMode(payload.mode)
        if mode is ResearchMode.url and not payload.url:
            raise HTTPException(status_code=422, detail="url is required for mode=url")
        if mode is ResearchMode.topic and not (payload.topic or payload.question):
            raise HTTPException(status_code=422, detail="topic is required for mode=topic")
        if mode is ResearchMode.compare and not (payload.topic or payload.company):
            raise HTTPException(status_code=422, detail="subject is required for mode=compare")
        if mode is ResearchMode.compare and not payload.competitors:
            raise HTTPException(status_code=422, detail="at least one competitor is required for mode=compare")

        request = ResearchRequest(
            mode=mode,
            url=payload.url,
            topic=payload.topic,
            company=payload.company or payload.topic,
            competitors=[c for c in payload.competitors if c][:8],
            question=payload.question,
            max_pages=payload.max_pages or None,
            max_depth=payload.max_depth if payload.max_depth else None,
            use_search=payload.use_search,
            use_llm=payload.use_llm,
            extra_seeds=payload.source_urls,
        )
        try:
            result = _deep_pipeline().run(request)
        except Exception:
            error_id = uuid.uuid4().hex[:12]
            logger.exception("deep research failed (reference=%s)", error_id)
            raise HTTPException(status_code=500, detail=f"Research failed unexpectedly. Reference: {error_id}")
        return json.loads(result.model_dump_json())

    @app.get("/research/deep/runs")
    def deep_research_runs(limit: int = 25):
        limit = max(1, min(limit, 500))
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
    @app.post("/research/chat", dependencies=[Depends(_require_public_research_or_api_key), Depends(_research_rate_limit)])
    def research_chat(payload: ChatRequest):
        from app.research.chat import ResearchChat

        documents = _deep_pipeline().documents
        stored_run = documents.get_run(payload.run_id) if payload.run_id else None
        competitive_index_incomplete = bool(
            stored_run
            and stored_run.get("mode") == "compare"
            and not (stored_run.get("coverage") or {}).get("competitive_indexed")
        )
        if payload.run_id and (not stored_run or competitive_index_incomplete):
            job = _store.get_job(payload.run_id) or {}
            metrics = next((item for item in _store.recent_runs(limit=100) if item.get("run_id") == payload.run_id), None)
            if job or metrics:
                target_data = job.get("target") or {
                    "company": metrics.get("target_company", "Competitive research") if metrics else "Competitive research",
                    "website": "https://source-unavailable.invalid",
                }
                try:
                    target = CompetitorTarget.model_validate(target_data)
                    _index_competitive_run_for_chat(documents, payload.run_id, target)
                except Exception:
                    logger.exception("could not prepare competitive run %s for chat", payload.run_id)
        chat = ResearchChat(documents)
        answer = chat.ask(
            payload.question,
            run_id=payload.run_id,
            session_id=payload.session_id,
            top_k=payload.top_k,
        )
        return json.loads(answer.model_dump_json())

    @app.get("/research/chats")
    def research_chats(limit: int = 100):
        """List saved conversations so a user can reopen and continue them."""
        documents = _deep_pipeline().documents
        sessions = documents.list_sessions(limit=max(1, min(limit, 500)))
        return [{
            "session_id": session["id"],
            "run_id": session.get("run_id"),
            "title": session.get("title") or "Research chat",
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
            "last_message": session.get("last_message") or "",
            "message_count": session.get("message_count") or 0,
        } for session in sessions]

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

    @app.get("/research/runs")
    def research_runs(limit: int = 25):
        """Compact, durable history across the two existing research paths."""
        limit = max(1, min(limit, 500))
        unique = {}
        for job in _store.recent_jobs(limit=limit):
            if job.get("status") in {"queued", "running", "failed", "interrupted", "completed"}:
                target = job.get("target") or {}
                result = job.get("result") or {}
                metrics = result.get("metrics") or {}
                job_run = {
                    "run_id": job.get("job_id"), "kind": "competitive",
                    "status": job.get("status"), "title": target.get("company") or "Competitive research",
                    "created_at": job.get("started_at") or job.get("created_at"),
                    "duration_s": result.get("duration_s") or metrics.get("runtime_s"),
                    "metrics": metrics, "error": job.get("error"),
                    "offers_captured": result.get("offers_captured", _store.count(run_id=job.get("job_id"))),
                }
                unique[("competitive", job.get("job_id"))] = job_run
        for metrics in _store.recent_runs(limit=limit):
            run = {
                "run_id": metrics["run_id"], "kind": "competitive", "status": "completed",
                "title": metrics.get("target_company") or "Competitive research",
                "created_at": metrics.get("started_at"), "duration_s": metrics.get("runtime_s"),
                "metrics": metrics, "offers_captured": _store.count(run_id=metrics["run_id"]),
            }
            key = (run["kind"], run["run_id"])
            if key in unique:
                # Preserve the durable job state, but add final metrics if
                # the pipeline completed before a later job-finalization error.
                unique[key].update({"metrics": metrics, "duration_s": metrics.get("runtime_s")})
            else:
                unique[key] = run
        try:
            for run in _deep_pipeline().documents.list_runs(limit=limit):
                coverage = run.get("coverage") or {}
                if coverage.get("competitive_indexed"):
                    continue
                deep_run = {
                    "run_id": run.get("run_id"), "kind": "deep", "status": run.get("status"),
                    "title": run.get("title") or run.get("question") or "Deep research",
                    "created_at": run.get("created_at"), "duration_s": coverage.get("duration_s"),
                    "coverage": coverage,
                }
                unique[("deep", run.get("run_id"))] = deep_run
        except Exception:
            logger.exception("could not load deep-research history")
        return sorted(unique.values(), key=lambda item: item.get("created_at") or "", reverse=True)[:limit]

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
