"""Relational storage for SQLite (offline/demo) and PostgreSQL (production).

The public OfferStore API is intentionally backend-agnostic so the pipeline,
analysis layer, API, and tests use exactly the same storage contract.
Observation identity remains:
  canonical_url | canonical_product_id | region | competitor | seller | snapshot_hour
run_id is metadata, not part of the observation key.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Any

from app.config import settings
from app.discovery import canonical_url
from app.schemas import ProductOffer, Region

logger = logging.getLogger(__name__)

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_name TEXT NOT NULL,
    brand TEXT, price REAL NOT NULL, currency TEXT NOT NULL, region TEXT NOT NULL,
    url TEXT NOT NULL, seller TEXT, availability TEXT, listing_title TEXT,
    scraped_at TEXT NOT NULL, competitor TEXT, canonical_url TEXT,
    normalized_price_usd REAL, exchange_rate REAL, exchange_rate_timestamp TEXT,
    offer_key TEXT, run_id TEXT, canonical_product_id TEXT,
    canonical_product_name TEXT, extraction_confidence REAL
);
CREATE INDEX IF NOT EXISTS idx_offers_product ON offers(product_name, region);
CREATE INDEX IF NOT EXISTS idx_offers_time ON offers(scraped_at);
CREATE INDEX IF NOT EXISTS idx_offers_run ON offers(run_id);
CREATE TABLE IF NOT EXISTS run_metrics (
    run_id TEXT PRIMARY KEY, started_at TEXT, mode TEXT, target_company TEXT,
    competitors INTEGER, regions INTEGER, products INTEGER, urls_discovered INTEGER,
    urls_attempted INTEGER, urls_succeeded INTEGER, urls_failed INTEGER,
    scraping_success_rate REAL, retries_used INTEGER, extraction_attempts INTEGER,
    extraction_successes INTEGER, extraction_failures INTEGER, extraction_accuracy REAL,
    runtime_s REAL, llm_calls INTEGER, provider_attempts INTEGER, fallbacks INTEGER,
    providers_used TEXT, scrapers_used TEXT, errors TEXT
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, company TEXT, website TEXT,
    regions TEXT, focus_products TEXT, competitors_json TEXT, interval_hours INTEGER,
    last_run_at TEXT, enabled INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS raw_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT, source_url TEXT, content_hash TEXT, scraped_at TEXT, content TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_dedup ON raw_evidence(source_url, content_hash);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON raw_evidence(run_id, source_url);
"""

_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id BIGSERIAL PRIMARY KEY,
    product_name TEXT NOT NULL,
    brand TEXT, price DOUBLE PRECISION NOT NULL, currency TEXT NOT NULL, region TEXT NOT NULL,
    url TEXT NOT NULL, seller TEXT, availability TEXT, listing_title TEXT,
    scraped_at TEXT NOT NULL, competitor TEXT, canonical_url TEXT,
    normalized_price_usd DOUBLE PRECISION, exchange_rate DOUBLE PRECISION,
    exchange_rate_timestamp TEXT, offer_key TEXT, run_id TEXT, canonical_product_id TEXT,
    canonical_product_name TEXT, extraction_confidence DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_offers_product ON offers(product_name, region);
CREATE INDEX IF NOT EXISTS idx_offers_time ON offers(scraped_at);
CREATE INDEX IF NOT EXISTS idx_offers_run ON offers(run_id);
CREATE TABLE IF NOT EXISTS run_metrics (
    run_id TEXT PRIMARY KEY, started_at TEXT, mode TEXT, target_company TEXT,
    competitors INTEGER, regions INTEGER, products INTEGER, urls_discovered INTEGER,
    urls_attempted INTEGER, urls_succeeded INTEGER, urls_failed INTEGER,
    scraping_success_rate DOUBLE PRECISION, retries_used INTEGER, extraction_attempts INTEGER,
    extraction_successes INTEGER, extraction_failures INTEGER, extraction_accuracy DOUBLE PRECISION,
    runtime_s DOUBLE PRECISION, llm_calls INTEGER, provider_attempts INTEGER, fallbacks INTEGER,
    providers_used TEXT, scrapers_used TEXT, errors TEXT
);
CREATE TABLE IF NOT EXISTS schedules (
    id BIGSERIAL PRIMARY KEY, name TEXT, company TEXT, website TEXT,
    regions TEXT, focus_products TEXT, competitors_json TEXT, interval_hours INTEGER,
    last_run_at TEXT, enabled INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS raw_evidence (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT, source_url TEXT, content_hash TEXT, scraped_at TEXT, content TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_dedup ON raw_evidence(source_url, content_hash);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON raw_evidence(run_id, source_url);
CREATE UNIQUE INDEX IF NOT EXISTS idx_offers_key ON offers(offer_key);
CREATE INDEX IF NOT EXISTS idx_offers_canon ON offers(canonical_product_id, region);
CREATE INDEX IF NOT EXISTS idx_offers_series ON offers(product_name, region, competitor, scraped_at);
"""

_NEW_COLUMNS = [
    ("offers", "competitor", "TEXT"), ("offers", "canonical_url", "TEXT"),
    ("offers", "normalized_price_usd", "REAL"), ("offers", "exchange_rate", "REAL"),
    ("offers", "exchange_rate_timestamp", "TEXT"), ("offers", "offer_key", "TEXT"),
    ("offers", "run_id", "TEXT"), ("offers", "canonical_product_id", "TEXT"),
    ("offers", "canonical_product_name", "TEXT"), ("offers", "extraction_confidence", "REAL"),
]


def _snapshot_bucket(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H")


def _offer_key(offer: ProductOffer) -> str:
    raw = "|".join([
        canonical_url(offer.canonical_url or offer.url),
        offer.canonical_product_id or "?", offer.region.value,
        offer.competitor or "", offer.seller or "", _snapshot_bucket(offer.scraped_at),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


class OfferStore:
    def __init__(self, database_url: Optional[str] = None):
        self._url = database_url or settings.database_url
        self._postgres = self._url.startswith(("postgres://", "postgresql://"))
        if self._postgres:
            self._init_db()
            return
        if not self._url.startswith("sqlite://"):
            raise ValueError("DATABASE_URL must be sqlite:///... or postgresql://...")
        self._path = self._url.replace("sqlite://", "", 1)
        if self._path.startswith("/") and not self._url.startswith("sqlite:////"):
            self._path = self._path.lstrip("/")
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            if self._postgres:
                conn.execute(_PG_SCHEMA)
            else:
                conn.executescript(_SQLITE_SCHEMA)
                self._migrate(conn)
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_offers_key ON offers(offer_key)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_canon ON offers(canonical_product_id, region)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_series ON offers(product_name, region, competitor, scraped_at)")

    def _migrate(self, conn) -> None:
        for table, col, typ in _NEW_COLUMNS:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass

    @contextmanager
    def _connect(self):
        if self._postgres:
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(self._url, row_factory=dict_row)
        else:
            conn = sqlite3.connect(self._path)
            conn.row_factory = sqlite3.Row
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

    # ---------------- offers ----------------
    def upsert_offer(self, offer: ProductOffer) -> int:
        key = _offer_key(offer)
        params = (
            offer.product_name, offer.brand, offer.price, offer.currency.value,
            offer.region.value, offer.url, offer.seller, offer.availability.value,
            offer.listing_title, offer.scraped_at.isoformat(), offer.competitor,
            canonical_url(offer.canonical_url or offer.url), offer.normalized_price_usd,
            offer.exchange_rate, offer.exchange_rate_timestamp, key, offer.run_id,
            offer.canonical_product_id, offer.canonical_product_name, offer.extraction_confidence,
        )
        sql = """INSERT INTO offers
            (product_name, brand, price, currency, region, url, seller, availability,
             listing_title, scraped_at, competitor, canonical_url, normalized_price_usd,
             exchange_rate, exchange_rate_timestamp, offer_key, run_id, canonical_product_id,
             canonical_product_name, extraction_confidence)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(offer_key) DO UPDATE SET
              price=excluded.price, availability=excluded.availability,
              scraped_at=excluded.scraped_at, listing_title=excluded.listing_title,
              normalized_price_usd=excluded.normalized_price_usd,
              exchange_rate=excluded.exchange_rate, run_id=excluded.run_id,
              extraction_confidence=excluded.extraction_confidence"""
        with self._connect() as conn:
            self._execute(conn, sql, params)
        return 1

    def insert_offer(self, offer: ProductOffer) -> int:
        return self.upsert_offer(offer)

    def insert_many(self, offers: List[ProductOffer]) -> int:
        return sum(self.upsert_offer(o) for o in offers)

    def recent_offers(self, product: Optional[str] = None, region: Optional[Region] = None,
                      limit: int = 200, run_id: Optional[str] = None) -> List[ProductOffer]:
        sql = "SELECT * FROM offers WHERE 1=1"
        params: list[Any] = []
        if product:
            sql += " AND product_name LIKE ?"
            params.append(f"%{product}%")
        if region:
            sql += " AND region = ?"
            params.append(region.value)
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " ORDER BY scraped_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = self._execute(conn, sql, tuple(params)).fetchall()
        offers = []
        for r in rows:
            try:
                offers.append(_row_to_offer(r))
            except Exception:
                logger.warning("skipping malformed offer row")
        return offers

    def count(self, run_id: Optional[str] = None) -> int:
        with self._connect() as conn:
            if run_id:
                return self._execute(conn, "SELECT COUNT(*) FROM offers WHERE run_id=?", (run_id,)).fetchone()[0]
            return self._execute(conn, "SELECT COUNT(*) FROM offers").fetchone()[0]

    def distinct_products(self, run_id: Optional[str] = None) -> List[str]:
        sql = "SELECT DISTINCT COALESCE(canonical_product_id, product_name) AS p FROM offers"
        params = ()
        if run_id:
            sql += " WHERE run_id=?"
            params = (run_id,)
        with self._connect() as conn:
            rows = self._execute(conn, sql, params).fetchall()
        return [r["p"] for r in rows]

    # ---------------- raw evidence ----------------
    def save_evidence(self, run_id: str, source_url: str, content: str) -> None:
        h = hashlib.sha256(content.encode()).hexdigest()
        with self._connect() as conn:
            self._execute(conn,
                "INSERT INTO raw_evidence (run_id, source_url, content_hash, scraped_at, content) "
                "VALUES (?,?,?,?,?) ON CONFLICT(source_url, content_hash) DO NOTHING",
                (run_id, source_url, h, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), content[:60000]))

    def evidence_for_run(self, run_id: str, limit: int = 20) -> List[dict]:
        with self._connect() as conn:
            rows = self._execute(conn,
                "SELECT run_id, source_url, content_hash, scraped_at, length(content) AS size "
                "FROM raw_evidence WHERE run_id=? ORDER BY scraped_at DESC LIMIT ?", (run_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_evidence(self, source_url: str, run_id: Optional[str] = None) -> Optional[str]:
        with self._connect() as conn:
            if run_id:
                row = self._execute(conn,
                    "SELECT content FROM raw_evidence WHERE source_url=? AND run_id=? ORDER BY id DESC LIMIT 1",
                    (source_url, run_id)).fetchone()
            else:
                row = self._execute(conn,
                    "SELECT content FROM raw_evidence WHERE source_url=? ORDER BY id DESC LIMIT 1",
                    (source_url,)).fetchone()
        return row["content"] if row else None

    # ---------------- run metrics ----------------
    def record_run(self, m: dict) -> None:
        values = (
            m["run_id"], m.get("started_at", ""), m.get("mode", ""), m.get("target_company", ""),
            m.get("competitors", 0), m.get("regions", 0), m.get("products", 0), m.get("urls_discovered", 0),
            m.get("urls_attempted", 0), m.get("urls_succeeded", 0), m.get("urls_failed", 0),
            m.get("scraping_success_rate", 0.0), m.get("retries_used", 0), m.get("extraction_attempts", 0),
            m.get("extraction_successes", 0), m.get("extraction_failures", 0), m.get("extraction_accuracy", 0.0),
            m.get("runtime_s", 0.0), m.get("llm_calls", 0), m.get("provider_attempts", 0), m.get("fallbacks", 0),
            json.dumps(m.get("providers_used", [])), json.dumps(m.get("scrapers_used", [])),
            json.dumps(m.get("errors", [])),
        )
        sql = """INSERT INTO run_metrics
            (run_id, started_at, mode, target_company, competitors, regions, products,
             urls_discovered, urls_attempted, urls_succeeded, urls_failed,
             scraping_success_rate, retries_used, extraction_attempts, extraction_successes,
             extraction_failures, extraction_accuracy, runtime_s, llm_calls, provider_attempts,
             fallbacks, providers_used, scrapers_used, errors)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id) DO UPDATE SET
              started_at=excluded.started_at, mode=excluded.mode, target_company=excluded.target_company,
              competitors=excluded.competitors, regions=excluded.regions, products=excluded.products,
              urls_discovered=excluded.urls_discovered, urls_attempted=excluded.urls_attempted,
              urls_succeeded=excluded.urls_succeeded, urls_failed=excluded.urls_failed,
              scraping_success_rate=excluded.scraping_success_rate, retries_used=excluded.retries_used,
              extraction_attempts=excluded.extraction_attempts, extraction_successes=excluded.extraction_successes,
              extraction_failures=excluded.extraction_failures, extraction_accuracy=excluded.extraction_accuracy,
              runtime_s=excluded.runtime_s, llm_calls=excluded.llm_calls,
              provider_attempts=excluded.provider_attempts, fallbacks=excluded.fallbacks,
              providers_used=excluded.providers_used, scrapers_used=excluded.scrapers_used, errors=excluded.errors"""
        with self._connect() as conn:
            self._execute(conn, sql, values)

    def recent_runs(self, limit: int = 20) -> List[dict]:
        with self._connect() as conn:
            rows = self._execute(conn, "SELECT * FROM run_metrics ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("providers_used", "scrapers_used", "errors"):
                try:
                    d[k] = json.loads(d[k] or "[]")
                except (json.JSONDecodeError, TypeError):
                    d[k] = []
            out.append(d)
        return out

    def aggregate_metrics(self) -> dict:
        sql = """SELECT COUNT(*) AS runs, AVG(scraping_success_rate) AS avg_scrape,
                 AVG(extraction_accuracy) AS avg_extract, AVG(runtime_s) AS avg_runtime,
                 MAX(urls_succeeded) AS max_pages, SUM(competitors) AS total_competitors,
                 SUM(regions) AS total_regions, SUM(products) AS total_products,
                 SUM(llm_calls) AS total_llm_calls FROM run_metrics"""
        with self._connect() as conn:
            row = self._execute(conn, sql).fetchone()
        latest = self.recent_runs(limit=1)
        return {k: row[k] for k in row.keys()} | {"latest_run": latest[0] if latest else None}

    # ---------------- schedules ----------------
    def add_schedule(self, name, company, website, regions, focus_products,
                     competitors_json="[]", interval_hours=24) -> int:
        with self._connect() as conn:
            if self._postgres:
                row = self._execute(conn,
                    "INSERT INTO schedules (name, company, website, regions, focus_products, competitors_json, interval_hours, last_run_at, enabled) "
                    "VALUES (?,?,?,?,?,?,?,NULL,1) RETURNING id",
                    (name, company, website, json.dumps(regions), json.dumps(focus_products), competitors_json, interval_hours)).fetchone()
                return int(row["id"])
            cur = self._execute(conn,
                "INSERT INTO schedules (name, company, website, regions, focus_products, competitors_json, interval_hours, last_run_at, enabled) "
                "VALUES (?,?,?,?,?,?,?,NULL,1)",
                (name, company, website, json.dumps(regions), json.dumps(focus_products), competitors_json, interval_hours))
            return int(cur.lastrowid or 0)

    def list_schedules(self) -> List[dict]:
        with self._connect() as conn:
            rows = self._execute(conn, "SELECT * FROM schedules ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("regions", "focus_products", "competitors_json"):
                try:
                    d[k] = json.loads(d[k] or "[]")
                except (json.JSONDecodeError, TypeError):
                    d[k] = []
            d["competitors"] = d.pop("competitors_json", []) or []
            out.append(d)
        return out

    def get_schedule(self, sid: int) -> Optional[dict]:
        return next((s for s in self.list_schedules() if s["id"] == sid), None)

    def delete_schedule(self, sid: int) -> bool:
        with self._connect() as conn:
            cur = self._execute(conn, "DELETE FROM schedules WHERE id=?", (sid,))
            return cur.rowcount > 0

    def set_schedule_last_run(self, sid: int, ts: str) -> None:
        with self._connect() as conn:
            self._execute(conn, "UPDATE schedules SET last_run_at=? WHERE id=?", (ts, sid))


def _row_to_offer(r) -> ProductOffer:
    payload = {k: r[k] for k in r.keys() if k != "id"}
    return ProductOffer.model_validate(payload)
