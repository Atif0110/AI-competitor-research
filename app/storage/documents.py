"""Research document storage: runs, pages, sections, facts and chat history.

One backend-agnostic store for the research layer, mirroring the
``OfferStore`` contract so SQLite (offline/demo) and PostgreSQL (production)
behave identically.

Retrieval strategy
------------------
SQLite gets an FTS5 index over section content with BM25 ranking. PostgreSQL
gets a GIN ``tsvector`` index. Both fall back to a deterministic lexical scan
when the index is unavailable, so chat never silently returns "no results"
just because an extension is missing.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.config import settings
from app.discovery import canonical_url
from app.research.models import (
    Citation,
    Fact,
    PageType,
    ResearchPlan,
    ResearchSection,
    ResearchStatus,
)
from app.schemas import Region

logger = logging.getLogger(__name__)

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    question TEXT,
    target_url TEXT,
    title TEXT,
    plan TEXT,
    coverage TEXT,
    report_markdown TEXT,
    error TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS research_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source_ref TEXT,
    url TEXT NOT NULL,
    canonical_url TEXT,
    title TEXT,
    description TEXT,
    page_type TEXT,
    status TEXT,
    http_status INTEGER,
    depth INTEGER,
    origin TEXT,
    scraper TEXT,
    relevance REAL,
    content_hash TEXT,
    chars INTEGER,
    text TEXT,
    error TEXT,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pages_run ON research_pages(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pages_run_url ON research_pages(run_id, url);
CREATE TABLE IF NOT EXISTS research_sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    page_id INTEGER,
    source_ref TEXT,
    url TEXT,
    title TEXT,
    heading TEXT,
    content TEXT NOT NULL,
    section_index INTEGER,
    word_count INTEGER,
    page_type TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sections_run ON research_sections(run_id);
CREATE TABLE IF NOT EXISTS research_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    page_id INTEGER,
    source_ref TEXT,
    url TEXT,
    subject TEXT,
    question TEXT,
    statement TEXT NOT NULL,
    value TEXT,
    confidence REAL,
    kind TEXT,
    quote TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_run ON research_facts(run_id);
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    title TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    citations TEXT,
    meta TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON chat_messages(session_id, id);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS research_fts USING fts5(
    section_id UNINDEXED,
    title,
    heading,
    url,
    content,
    tokenize='unicode61'
);
"""

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-\.]*")

_STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "you", "your", "are", "was",
    "were", "have", "has", "had", "not", "but", "all", "any", "can", "could",
    "from", "how", "its", "into", "our", "out", "she", "his", "her", "his",
    "they", "them", "their", "there", "these", "those", "who", "what", "when",
    "where", "which", "why", "will", "would", "should", "about", "does", "did",
    "doing", "have", "has", "been", "being", "than", "then", "also", "just",
    "more", "most", "some", "such", "only", "other", "into", "over", "under",
    "use", "using", "used", "get", "got", "make", "made", "does", "each",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def tokenize(text: str) -> List[str]:
    """Lowercase word tokens used by both indexing and lexical scoring."""
    return [
        token
        for token in _WORD_RE.findall((text or "").lower())
        if len(token) > 2 and token not in _STOPWORDS
    ]


def lexical_score(query: str, text: str) -> float:
    """Deterministic 0..1 lexical relevance, used as fallback ranking."""
    terms = set(tokenize(query))
    if not terms:
        return 0.0
    doc_tokens = tokenize(text)
    if not doc_tokens:
        return 0.0
    counts: Dict[str, int] = {}
    for token in doc_tokens:
        counts[token] = counts.get(token, 0) + 1
    hits = sum(min(counts.get(term, 0), 5) for term in terms)
    coverage = hits / len(terms)
    density = hits / max(1, len(doc_tokens))
    return round(min(1.0, 0.75 * coverage + 2.5 * density), 4)


class DocumentStore:
    """Persistent store for research runs and chat."""

    def __init__(self, database_url: Optional[str] = None):
        self._url = database_url or settings.database_url
        self._postgres = self._url.startswith(("postgres://", "postgresql://"))
        if self._postgres:
            self._init_postgres()
            return
        if not self._url.startswith("sqlite://"):
            raise ValueError("DATABASE_URL must be sqlite:///... or postgresql://...")
        self._path = self._url.replace("sqlite://", "", 1)
        if self._path.startswith("/") and not self._url.startswith("sqlite:////"):
            self._path = self._path.lstrip("/")
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_sqlite()

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def _init_sqlite(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SQLITE_SCHEMA)
            self._fts = False
            try:
                conn.executescript(_FTS_SCHEMA)
                self._fts = True
            except sqlite3.DatabaseError:
                # FTS5 is optional; lexical fallback keeps search working.
                logger.info("sqlite FTS5 unavailable — using lexical ranking")

    def _init_postgres(self) -> None:
        import psycopg

        with psycopg.connect(self._url) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS research_runs (
                    run_id TEXT PRIMARY KEY, status TEXT NOT NULL, mode TEXT NOT NULL,
                    question TEXT, target_url TEXT, title TEXT, plan JSONB, coverage JSONB,
                    report_markdown TEXT, error TEXT, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS research_pages (
                    id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL, source_ref TEXT,
                    url TEXT NOT NULL, canonical_url TEXT, title TEXT, description TEXT,
                    page_type TEXT, status TEXT, http_status INTEGER, depth INTEGER,
                    origin TEXT, scraper TEXT, relevance REAL, content_hash TEXT,
                    chars INTEGER, text TEXT, error TEXT, fetched_at TEXT,
                    UNIQUE (run_id, url)
                );
                CREATE INDEX IF NOT EXISTS idx_pages_run_pg ON research_pages(run_id);
                CREATE TABLE IF NOT EXISTS research_sections (
                    id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL, page_id BIGINT,
                    source_ref TEXT, url TEXT, title TEXT, heading TEXT, content TEXT NOT NULL,
                    section_index INTEGER, word_count INTEGER, page_type TEXT, created_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sections_run_pg ON research_sections(run_id);
                CREATE TABLE IF NOT EXISTS research_facts (
                    id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL, page_id BIGINT,
                    source_ref TEXT, url TEXT, subject TEXT, question TEXT, statement TEXT NOT NULL,
                    value TEXT, confidence REAL, kind TEXT, quote TEXT, created_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_facts_run_pg ON research_facts(run_id);
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id TEXT PRIMARY KEY, run_id TEXT, title TEXT, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id BIGSERIAL PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, citations TEXT, meta TEXT, created_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session_pg ON chat_messages(session_id, id);
                """
            )
            conn.commit()
        self._fts = False

    @contextmanager
    def _connect(self):
        if self._postgres:
            import psycopg
            from psycopg.rows import dict_row

            conn = psycopg.connect(self._url, row_factory=dict_row)
        else:
            conn = sqlite3.connect(self._path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=30000")
            except sqlite3.DatabaseError:
                logger.debug("could not apply sqlite pragmas", exc_info=True)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _execute(self, conn, sql: str, params: tuple = ()):
        if self._postgres:
            return conn.execute(sql.replace("?", "%s"), params)
        return conn.execute(sql, params)

    def _json_load(self, value, default):
        if value in (None, ""):
            return default
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default

    # ------------------------------------------------------------------
    # runs
    # ------------------------------------------------------------------
    def create_run(
        self,
        run_id: str,
        mode: str,
        question: str,
        target_url: Optional[str],
        title: str = "",
    ) -> None:
        with self._connect() as conn:
            self._execute(
                conn,
                "INSERT INTO research_runs (run_id, status, mode, question, target_url, "
                "title, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
                (run_id, ResearchStatus.running.value, mode, question, target_url, title, _now(), _now()),
            )

    def update_run(
        self,
        run_id: str,
        *,
        status: Optional[str] = None,
        plan: Optional[ResearchPlan] = None,
        coverage: Optional[dict] = None,
        report_markdown: Optional[str] = None,
        error: Optional[str] = None,
        title: Optional[str] = None,
    ) -> None:
        fields, params = [], []
        if status is not None:
            fields.append("status = ?"); params.append(status)
        if plan is not None:
            fields.append("plan = ?"); params.append(plan.model_dump_json())
        if coverage is not None:
            fields.append("coverage = ?"); params.append(json.dumps(coverage))
        if report_markdown is not None:
            fields.append("report_markdown = ?"); params.append(report_markdown)
        if error is not None:
            fields.append("error = ?"); params.append(error)
        if title is not None:
            fields.append("title = ?"); params.append(title)
        if not fields:
            return
        fields.append("updated_at = ?"); params.append(_now())
        params.append(run_id)
        with self._connect() as conn:
            self._execute(
                conn,
                f"UPDATE research_runs SET {', '.join(fields)} WHERE run_id = ?",
                tuple(params),
            )

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = self._execute(
                conn, "SELECT * FROM research_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["plan"] = self._json_load(data.get("plan"), None)
        data["coverage"] = self._json_load(data.get("coverage"), {})
        return data

    def list_runs(self, limit: int = 25) -> List[dict]:
        # ``rowid`` only exists in SQLite; PostgreSQL orders by the stored
        # timestamp and run_id instead.
        order = (
            "created_at DESC, run_id DESC"
            if self._postgres
            else "created_at DESC, rowid DESC"
        )
        with self._connect() as conn:
            rows = self._execute(
                conn,
                f"SELECT * FROM research_runs ORDER BY {order} LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data.pop("report_markdown", None)
            data["plan"] = self._json_load(data.get("plan"), None)
            data["coverage"] = self._json_load(data.get("coverage"), {})
            out.append(data)
        return out

    def delete_run(self, run_id: str) -> bool:
        with self._connect() as conn:
            if self._fts:
                ids = [
                    str(r["section_id"])
                    for r in self._execute(
                        conn, "SELECT id AS section_id FROM research_sections WHERE run_id = ?", (run_id,)
                    ).fetchall()
                ]
                for section_id in ids:
                    self._execute(conn, "DELETE FROM research_fts WHERE section_id = ?", (section_id,))
            for table in ("research_facts", "research_sections", "research_pages"):
                self._execute(conn, f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
            cur = self._execute(conn, "DELETE FROM research_runs WHERE run_id = ?", (run_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # pages
    # ------------------------------------------------------------------
    def save_page(self, run_id: str, doc) -> int:
        """Insert or update a crawled page and return its row id."""
        url = getattr(doc, "url", None) or ""
        source_ref = getattr(doc, "source_ref", "") or ""
        title = getattr(doc, "title", "") or ""
        description = getattr(doc, "description", "") or ""
        page_type = getattr(getattr(doc, "page_type", PageType.other), "value", "other")
        status = getattr(doc, "status", "") or ""
        http_status = getattr(doc, "http_status", None)
        depth = int(getattr(doc, "depth", 0) or 0)
        origin = getattr(doc, "origin", "") or ""
        scraper = getattr(doc, "scraper", "") or ""
        relevance = float(getattr(doc, "relevance", 0.0) or 0.0)
        content_hash = getattr(doc, "content_hash", "") or ""
        text = getattr(doc, "text", "") or ""
        error = getattr(doc, "error", None)
        with self._connect() as conn:
            existing = self._execute(
                conn, "SELECT id FROM research_pages WHERE run_id = ? AND url = ?", (run_id, url)
            ).fetchone()
            if existing:
                page_id = existing["id"]
                self._execute(
                    conn,
                    "UPDATE research_pages SET source_ref=?, title=?, description=?, page_type=?, "
                    "status=?, http_status=?, depth=?, origin=?, scraper=?, relevance=?, "
                    "content_hash=?, chars=?, text=?, error=?, fetched_at=? WHERE id=?",
                    (
                        source_ref, title, description, page_type, status, http_status,
                        depth, origin, scraper, relevance, content_hash, len(text),
                        text, error, _now(), page_id,
                    ),
                )
                return page_id
            cur = self._execute(
                conn,
                "INSERT INTO research_pages (run_id, source_ref, url, canonical_url, title, description, "
                "page_type, status, http_status, depth, origin, scraper, relevance, content_hash, chars, "
                "text, error, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, source_ref, url, canonical_url(url) if url else "", title,
                    description, page_type, status, http_status, depth, origin, scraper,
                    relevance, content_hash, len(text), text, error, _now(),
                ),
            )
            return int(cur.lastrowid or 0)

    def pages_for_run(self, run_id: str, limit: int = 200) -> List[dict]:
        with self._connect() as conn:
            rows = self._execute(
                conn,
                "SELECT id, run_id, source_ref, url, title, description, page_type, status, http_status, "
                "depth, origin, scraper, relevance, content_hash, chars, error, fetched_at "
                "FROM research_pages WHERE run_id = ? ORDER BY id LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # sections
    # ------------------------------------------------------------------
    def save_sections(self, run_id: str, sections: List[ResearchSection]) -> List[int]:
        ids: List[int] = []
        with self._connect() as conn:
            for section in sections:
                cur = self._execute(
                    conn,
                    "INSERT INTO research_sections (run_id, page_id, source_ref, url, title, heading, "
                    "content, section_index, word_count, page_type, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        section.page_id,
                        section.source_ref,
                        section.url,
                        section.title,
                        section.heading,
                        section.content,
                        section.index,
                        section.word_count or len(section.content.split()),
                        getattr(section.page_type, "value", "other"),
                        _now(),
                    ),
                )
                section_id = int(cur.lastrowid or 0)
                section.section_id = section_id
                ids.append(section_id)
                if self._fts:
                    try:
                        self._execute(
                            conn,
                            "INSERT INTO research_fts (section_id, title, heading, url, content) "
                            "VALUES (?,?,?,?,?)",
                            (str(section_id), section.title, section.heading, section.url, section.content),
                        )
                    except sqlite3.DatabaseError:
                        logger.debug("fts insert failed", exc_info=True)
        return ids

    def sections_for_run(self, run_id: str, limit: int = 2000) -> List[ResearchSection]:
        with self._connect() as conn:
            rows = self._execute(
                conn,
                "SELECT * FROM research_sections WHERE run_id = ? ORDER BY id LIMIT ?",
                (run_id, limit),
            ).fetchall()
        sections = []
        for row in rows:
            sections.append(
                ResearchSection(
                    section_id=row["id"],
                    page_id=row["page_id"],
                    run_id=row["run_id"],
                    source_ref=row["source_ref"] or "",
                    url=row["url"] or "",
                    title=row["title"] or "",
                    heading=row["heading"] or "",
                    content=row["content"] or "",
                    index=row["section_index"] or 0,
                    word_count=row["word_count"] or 0,
                    page_type=_page_type(row["page_type"]),
                )
            )
        return sections

    # ------------------------------------------------------------------
    # facts
    # ------------------------------------------------------------------
    def save_facts(self, run_id: str, facts: List[Fact]) -> int:
        if not facts:
            return 0
        with self._connect() as conn:
            for fact in facts:
                self._execute(
                    conn,
                    "INSERT INTO research_facts (run_id, page_id, source_ref, url, subject, question, "
                    "statement, value, confidence, kind, quote, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        fact.page_id,
                        fact.source_ref,
                        fact.url,
                        fact.subject,
                        fact.question,
                        fact.statement,
                        fact.value,
                        fact.confidence,
                        fact.kind,
                        fact.quote,
                        _now(),
                    ),
                )
        return len(facts)

    def search_facts_query(
        self,
        run_id: str,
        query: str,
        k: int = 5,
    ) -> List[Tuple[ResearchSection, float]]:
        """Rank extracted facts against a question as pseudo-sections.

        Chat falls back to this when section search finds nothing, so a value
        that was captured as a fact is still reachable.
        """
        ranked: List[Tuple[ResearchSection, float]] = []
        for fact in self.facts_for_run(run_id):
            haystack = f"{fact.subject} {fact.statement} {fact.value or ''}"
            score = lexical_score(query, haystack)
            if score <= 0:
                continue
            ranked.append(
                (
                    ResearchSection(
                        run_id=run_id,
                        page_id=fact.page_id,
                        source_ref=fact.source_ref,
                        url=fact.url,
                        title=fact.subject,
                        heading=fact.question or fact.subject,
                        content=f"{fact.statement} ({fact.value})" if fact.value else fact.statement,
                    ),
                    score,
                )
            )
        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked[:k]

    def facts_for_run(self, run_id: str, limit: int = 500) -> List[Fact]:
        with self._connect() as conn:
            rows = self._execute(
                conn,
                "SELECT * FROM research_facts WHERE run_id = ? ORDER BY id LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [
            Fact(
                page_id=r["page_id"],
                source_ref=r["source_ref"] or "",
                url=r["url"] or "",
                subject=r["subject"] or "",
                question=r["question"] or "",
                statement=r["statement"],
                value=r["value"],
                confidence=r["confidence"] if r["confidence"] is not None else 0.5,
                kind=r["kind"] or "fact",
                quote=r["quote"] or "",
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # retrieval
    # ------------------------------------------------------------------
    def search_sections(
        self,
        run_id: str,
        query: str,
        k: int = 6,
        page_types: Optional[Iterable[str]] = None,
    ) -> List[Tuple[ResearchSection, float]]:
        """Ranked retrieval over one run's sections.

        Tries the native index first and falls back to deterministic lexical
        scoring so search still works without FTS5 or PostgreSQL.
        """
        if not query.strip():
            return []

        rows = self._search_fts(run_id, query, k * 3)
        if rows:
            candidates = [
                (self._section_from_row(row), score)
                for row, score in self._normalize_ranks(rows)
            ]
        else:
            candidates = self._search_lexical(run_id, query)

        wanted = {str(pt) for pt in page_types} if page_types else None
        results = [
            (section, score)
            for section, score in candidates
            if wanted is None or getattr(section.page_type, "value", section.page_type) in wanted
        ]
        results.sort(key=lambda item: item[1], reverse=True)
        return results[:k]

    @staticmethod
    def _normalize_ranks(rows: List[dict]) -> List[Tuple[dict, float]]:
        """Turn engine ranks into comparable 0..1 relevance scores.

        SQLite ``bm25()`` returns negative numbers where more negative is a
        better match, while PostgreSQL ``ts_rank`` returns small positive
        relevance values. Normalising inside the result set keeps one
        threshold meaningful on both backends.
        """
        if not rows:
            return []
        relevance: List[float] = []
        for row in rows:
            raw = float(row.get("rank", 0.0) or 0.0)
            relevance.append(-raw if raw < 0 else raw)
        top = max(relevance) or 1.0
        return [
            (row, max(0.0, min(1.0, value / top)))
            for row, value in zip(rows, relevance)
        ]

    def _search_fts(self, run_id: str, query: str, limit: int) -> List[dict]:
        match = _fts_query(query)
        if not match:
            return []
        try:
            with self._connect() as conn:
                if self._postgres:
                    sql = (
                        "SELECT s.*, ts_rank(to_tsvector('english', s.content), "
                        "plainto_tsquery('english', %s)) AS rank FROM research_sections s "
                        "WHERE s.run_id = %s ORDER BY rank DESC LIMIT %s"
                    )
                    rows = self._execute(conn, sql, (query, run_id, limit)).fetchall()
                    return [dict(r) for r in rows]
                if not self._fts:
                    return []
                sql = (
                    "SELECT s.*, bm25(research_fts) AS rank FROM research_fts "
                    "JOIN research_sections s ON CAST(s.id AS TEXT) = research_fts.section_id "
                    "WHERE research_fts MATCH ? AND s.run_id = ? ORDER BY rank LIMIT ?"
                )
                rows = self._execute(conn, sql, (match, run_id, limit)).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            logger.debug("index search failed; using lexical fallback", exc_info=True)
            return []

    def _search_lexical(self, run_id: str, query: str) -> List[Tuple[ResearchSection, float]]:
        scored: List[Tuple[ResearchSection, float]] = []
        for section in self.sections_for_run(run_id):
            haystack = f"{section.title} {section.heading} {section.content}"
            score = lexical_score(query, haystack)
            if score > 0:
                scored.append((section, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored

    @staticmethod
    def _section_from_row(row: dict) -> ResearchSection:
        return ResearchSection(
            section_id=row.get("id"),
            page_id=row.get("page_id"),
            run_id=row.get("run_id") or "",
            source_ref=row.get("source_ref") or "",
            url=row.get("url") or "",
            title=row.get("title") or "",
            heading=row.get("heading") or "",
            content=row.get("content") or "",
            index=row.get("section_index") or 0,
            word_count=row.get("word_count") or 0,
            page_type=_page_type(row.get("page_type")),
        )

    # ------------------------------------------------------------------
    # chat sessions
    # ------------------------------------------------------------------
    def create_session(self, session_id: str, run_id: Optional[str], title: str = "") -> None:
        with self._connect() as conn:
            self._execute(
                conn,
                "INSERT INTO chat_sessions (id, run_id, title, created_at, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (session_id, run_id, title or "Research chat", _now(), _now()),
            )

    def get_session(self, session_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = self._execute(
                conn, "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, run_id: Optional[str] = None, limit: int = 25) -> List[dict]:
        with self._connect() as conn:
            if run_id:
                rows = self._execute(
                    conn,
                    "SELECT * FROM chat_sessions WHERE run_id = ? ORDER BY updated_at DESC LIMIT ?",
                    (run_id, limit),
                ).fetchall()
            else:
                rows = self._execute(
                    conn,
                    "SELECT * FROM chat_sessions ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def delete_session(self, session_id: str) -> bool:
        with self._connect() as conn:
            self._execute(conn, "DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            cur = self._execute(conn, "DELETE FROM chat_sessions WHERE id = ?", (session_id,))
            return cur.rowcount > 0

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        citations: Optional[List[dict]] = None,
        meta: Optional[dict] = None,
    ) -> None:
        with self._connect() as conn:
            self._execute(
                conn,
                "INSERT INTO chat_messages (session_id, role, content, citations, meta, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    session_id,
                    role,
                    content,
                    json.dumps(citations or []),
                    json.dumps(meta or {}),
                    _now(),
                ),
            )
            self._execute(
                conn,
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                (_now(), session_id),
            )

    def messages(self, session_id: str, limit: int = 100) -> List[dict]:
        with self._connect() as conn:
            rows = self._execute(
                conn,
                "SELECT id, session_id, role, content, citations, meta, created_at FROM chat_messages "
                "WHERE session_id = ? ORDER BY id LIMIT ?",
                (session_id, limit),
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["citations"] = self._json_load(data.get("citations"), [])
            data["meta"] = self._json_load(data.get("meta"), {})
            out.append(data)
        return out

    def citations_for_run(self, run_id: str) -> List[Citation]:
        """Distinct page-level sources for a run, in ``S1..Sn`` order."""
        seen: Dict[str, Citation] = {}
        for section in self.sections_for_run(run_id):
            ref = section.source_ref
            if ref and ref not in seen:
                seen[ref] = Citation(
                    ref=ref,
                    url=section.url,
                    title=section.title,
                    heading=section.heading,
                )
        return [seen[ref] for ref in sorted(seen, key=_ref_sort_key)]


def _ref_sort_key(ref: str):
    digits = "".join(ch for ch in ref if ch.isdigit())
    return (int(digits) if digits else 0, ref)


def _page_type(value: Any) -> PageType:
    try:
        return PageType(str(value))
    except ValueError:
        return PageType.other


def _fts_query(query: str) -> str:
    """Build a safe FTS5 MATCH expression from free text."""
    terms = [t.replace('"', "") for t in tokenize(query)]
    return " OR ".join(f'"{t}"' for t in terms[:24])


def region_of(value: Optional[str]) -> Optional[Region]:
    if not value:
        return None
    try:
        return Region(str(value).upper())
    except ValueError:
        return None