"""Relational storage: offers (upsert by observation identity), run metrics,
schedules, and raw evidence.

Observation identity (review #3 — explicit decision):
    canonical_url + canonical_product_id + region + competitor + seller + snapshot_hour
run_id is METADATA on the observation, not part of the key: the same
observation (same URL/product/region/seller within the hour) upserts to one
row regardless of run; a new hourly snapshot creates a new row. This is
idempotent observation storage, and run boundaries are reproduced via run_id.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from app.config import settings
from app.discovery import canonical_url
from app.schemas import Currency, ProductOffer, Region

logger = logging.getLogger(__name__)

_BASE_SCHEMA = """
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_dedup
    ON raw_evidence(source_url, content_hash);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON raw_evidence(run_id, source_url);
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
    """Observation identity: NO run_id — run is metadata (review #3)."""
    canon = canonical_url(offer.canonical_url or offer.url)
    pid = offer.canonical_product_id or "?"
    raw = "|".join([canon, pid, offer.region.value, offer.competitor or "",
                    offer.seller or "", _snapshot_bucket(offer.scraped_at)])
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


class OfferStore:
    def __init__(self, database_url: Optional[str] = None):
        self._url = database_url or settings.database_url
        if not self._url.startswith("sqlite://"):
            raise ValueError(
                "DATABASE_URL must be sqlite:///... in this build. PostgreSQL is a "
                "planned adapter — SQLite is fully supported.")
        self._path = self._url.replace("sqlite://", "", 1)
        if self._path.startswith("/") and not self._url.startswith("sqlite:////"):
            self._path = self._path.lstrip("/")
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_BASE_SCHEMA)
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
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------------- offers ----------------
    def upsert_offer(self, offer: ProductOffer) -> int:
        key = _offer_key(offer)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO offers (product_name, brand, price, currency, region, url,"
                " seller, availability, listing_title, scraped_at, competitor, canonical_url,"
                " normalized_price_usd, exchange_rate, exchange_rate_timestamp, offer_key,"
                " run_id, canonical_product_id, canonical_product_name, extraction_confidence)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(offer_key) DO UPDATE SET"
                " price=excluded.price, availability=excluded.availability,"
                " scraped_at=excluded.scraped_at, listing_title=excluded.listing_title,"
                " normalized_price_usd=excluded.normalized_price_usd,"
                " exchange_rate=excluded.exchange_rate, run_id=excluded.run_id",
                (offer.product_name, offer.brand, offer.price, offer.currency.value,
                 offer.region.value, offer.url, offer.seller, offer.availability.value,
                 offer.listing_title, offer.scraped_at.isoformat(), offer.competitor,
                 canonical_url(offer.canonical_url or offer.url),
                 offer.normalized_price_usd, offer.exchange_rate,
                 offer.exchange_rate_timestamp, key,
                 offer.run_id, offer.canonical_product_id, offer.canonical_product_name,
                 offer.extraction_confidence),
            )
        return 1

    def insert_offer(self, offer: ProductOffer) -> int:  # backward-compatible alias
        return self.upsert_offer(offer)

    def insert_many(self, offers: List[ProductOffer]) -> int:
        return sum(self.upsert_offer(o) for o in offers)

    def recent_offers(self, product: Optional[str] = None, region: Optional[Region] = None,
                      limit: int = 200, run_id: Optional[str] = None) -> List[ProductOffer]:
        sql = "SELECT * FROM offers WHERE 1=1"
        params: list = []
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
            rows = conn.execute(sql, params).fetchall()
        offers = []
        for r in rows:
            try:
                offers.append(_row_to_offer(r))
            except Exception:
                logger.warning("skipping malformed offer row %s", r["id"])
        return offers

    def count(self, run_id: Optional[str] = None) -> int:
        with self._connect() as conn:
            if run_id:
                return conn.execute("SELECT COUNT(*) FROM offers WHERE run_id=?", (run_id,)).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]

    def distinct_products(self, run_id: Optional[str] = None) -> List[str]:
        """Per-run product identity count (review #2): canonical IDs or raw names."""
        with self._connect() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT DISTINCT COALESCE(canonical_product_id, product_name) AS p "
                    "FROM offers WHERE run_id=?", (run_id,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT DISTINCT COALESCE(canonical_product_id, product_name) AS p "
                    "FROM offers").fetchall()
        return [r["p"] for r in rows]

    # ---------------- raw evidence (review #26) ----------------
    def save_evidence(self, run_id: str, source_url: str, content: str) -> None:
        h = hashlib.sha256(content.encode()).hexdigest()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO raw_evidence (run_id, source_url, content_hash,"
                " scraped_at, content) VALUES (?,?,?,?,?)"
                " ON CONFLICT(source_url, content_hash) DO NOTHING",
                (run_id, source_url, h, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 content[:60000]),
            )

    def evidence_for_run(self, run_id: str, limit: int = 20) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_id, source_url, content_hash, scraped_at,"
                " length(content) AS size FROM raw_evidence WHERE run_id=? "
                "ORDER BY scraped_at DESC LIMIT ?", (run_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_evidence(self, source_url: str, run_id: Optional[str] = None) -> Optional[str]:
        with self._connect() as conn:
            if run_id:
                row = conn.execute(
                    "SELECT content FROM raw_evidence WHERE source_url=? AND run_id=? "
                    "ORDER BY id DESC LIMIT 1", (source_url, run_id)).fetchone()
            else:
                row = conn.execute(
                    "SELECT content FROM raw_evidence WHERE source_url=? "
                    "ORDER BY id DESC LIMIT 1", (source_url,)).fetchone()
        return row["content"] if row else None

    # ---------------- run metrics ----------------
    def record_run(self, m: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO run_metrics (run_id, started_at, mode, target_company,"
                " competitors, regions, products, urls_discovered, urls_attempted, urls_succeeded,"
                " urls_failed, scraping_success_rate, retries_used, extraction_attempts,"
                " extraction_successes, extraction_failures, extraction_accuracy, runtime_s,"
                " llm_calls, provider_attempts, fallbacks, providers_used, scrapers_used, errors)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (m["run_id"], m.get("started_at", ""), m.get("mode", ""),
                 m.get("target_company", ""), m.get("competitors", 0), m.get("regions", 0),
                 m.get("products", 0), m.get("urls_discovered", 0), m.get("urls_attempted", 0),
                 m.get("urls_succeeded", 0), m.get("urls_failed", 0),
                 m.get("scraping_success_rate", 0.0), m.get("retries_used", 0),
                 m.get("extraction_attempts", 0), m.get("extraction_successes", 0),
                 m.get("extraction_failures", 0), m.get("extraction_accuracy", 0.0),
                 m.get("runtime_s", 0.0), m.get("llm_calls", 0),
                 m.get("provider_attempts", 0), m.get("fallbacks", 0),
                 json.dumps(m.get("providers_used", [])),
                 json.dumps(m.get("scrapers_used", [])),
                 json.dumps(m.get("errors", []))),
            )

    def recent_runs(self, limit: int = 20) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM run_metrics ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("providers_used", "scrapers_used", "errors"):
                try:
                    d[k] = json.loads(d[k] or "[]")
                except json.JSONDecodeError:
                    d[k] = []
            out.append(d)
        return out

    def aggregate_metrics(self) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS runs, AVG(scraping_success_rate) AS avg_scrape,"
                " AVG(extraction_accuracy) AS avg_extract, AVG(runtime_s) AS avg_runtime,"
                " MAX(urls_succeeded) AS max_pages, SUM(competitors) AS total_competitors,"
                " SUM(regions) AS total_regions, SUM(products) AS total_products,"
                " SUM(llm_calls) AS total_llm_calls FROM run_metrics").fetchone()
        latest = self.recent_runs(limit=1)
        return {k: row[k] for k in row.keys()} | {"latest_run": latest[0] if latest else None}

    # ---------------- schedules ----------------
    def add_schedule(self, name, company, website, regions, focus_products,
                     competitors_json="[]", interval_hours=24) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO schedules (name, company, website, regions, focus_products,"
                " competitors_json, interval_hours, last_run_at, enabled)"
                " VALUES (?,?,?,?,?,?,?,NULL,1)",
                (name, company, website, json.dumps(regions), json.dumps(focus_products),
                 competitors_json, interval_hours))
            return cur.lastrowid or 0

    def list_schedules(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("regions", "focus_products", "competitors_json"):
                try:
                    d[k] = json.loads(d[k] or "[]")
                except json.JSONDecodeError:
                    pass
            d["competitors"] = d.pop("competitors_json", "[]") or []
            out.append(d)
        return out

    def get_schedule(self, sid: int) -> Optional[dict]:
        for s in self.list_schedules():
            if s["id"] == sid:
                return s
        return None

    def delete_schedule(self, sid: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM schedules WHERE id=?", (sid,))
            return cur.rowcount > 0

    def set_schedule_last_run(self, sid: int, ts: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE schedules SET last_run_at=? WHERE id=?", (ts, sid))


def _row_to_offer(r: sqlite3.Row) -> ProductOffer:
    payload = {k: r[k] for k in r.keys() if k != "id"}
    return ProductOffer.model_validate(payload)
