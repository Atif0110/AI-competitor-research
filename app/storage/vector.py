"""Review storage with Chroma/JSON offline and PostgreSQL + pgvector in production.

PostgreSQL mode stores the full review metadata in a relational table and, when
OPENAI_API_KEY is configured, stores text-embedding-3-small vectors for semantic
search. Without an embedding key it degrades to deterministic lexical search
rather than inventing retrieval results.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import List, Optional

from app.config import settings
from app.discovery import canonical_url
from app.schemas import Review

logger = logging.getLogger(__name__)


def _review_id(review: Review) -> str:
    raw = "|".join([canonical_url(review.source_url), review.product_name,
                    review.region.value, review.review_text])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class ReviewStore:
    def __init__(self) -> None:
        self._postgres = settings.database_url.startswith(("postgres://", "postgresql://"))
        self._dir = Path(settings.vector_store_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._json_path = self._dir / "reviews.json"
        if self._postgres:
            self._client = "postgresql+pgvector"
            self._init_postgres()
            return
        try:
            import chromadb  # noqa: F401
            self._client = "chroma"
        except ImportError:
            self._client = "json"

    @property
    def backend(self) -> str:
        return self._client

    def _pg_connect(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(settings.database_url, row_factory=dict_row)

    def _init_postgres(self) -> None:
        try:
            with self._pg_connect() as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                conn.execute(f"""CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY,
                    product_name TEXT NOT NULL,
                    region TEXT NOT NULL,
                    rating DOUBLE PRECISION,
                    review_text TEXT NOT NULL,
                    reviewer TEXT,
                    review_date TEXT,
                    source_url TEXT NOT NULL,
                    scraped_at TEXT,
                    competitor TEXT,
                    run_id TEXT,
                    embedding vector({settings.embedding_dimensions})
                )""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_run ON reviews(run_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_product_region ON reviews(product_name, region)")
                conn.commit()
        except Exception as exc:
            logger.exception("PostgreSQL/pgvector initialization failed")
            raise RuntimeError("PostgreSQL mode requires a reachable database with the pgvector extension") from exc

    def add(self, review: Review) -> None:
        if self._postgres:
            self._add_postgres(review)
        elif self._client == "chroma":
            self._add_chroma(review)
        else:
            self._add_json(review)

    def _embedding(self, text: str):
        if not settings.openai_api_key:
            return None
        try:
            from openai import OpenAI
            return OpenAI(api_key=settings.openai_api_key).embeddings.create(
                model=settings.embedding_model, input=text
            ).data[0].embedding
        except Exception:
            logger.warning("embedding generation failed; storing review without vector", exc_info=True)
            return None

    def _add_postgres(self, review: Review) -> None:
        rid = _review_id(review)
        emb = self._embedding(review.review_text)
        with self._pg_connect() as conn:
            conn.execute("""INSERT INTO reviews
                (id, product_name, region, rating, review_text, reviewer, review_date,
                 source_url, scraped_at, competitor, run_id, embedding)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET
                  rating=excluded.rating, reviewer=excluded.reviewer,
                  review_date=excluded.review_date, scraped_at=excluded.scraped_at,
                  competitor=excluded.competitor, run_id=excluded.run_id,
                  embedding=COALESCE(excluded.embedding, reviews.embedding)""",
                (rid, review.product_name, review.region.value, review.rating, review.review_text,
                 review.reviewer, review.review_date.isoformat() if review.review_date else None,
                 review.source_url, review.scraped_at.isoformat() if review.scraped_at else None,
                 review.competitor, review.run_id, emb))
            conn.commit()

    def _add_json(self, review: Review) -> None:
        rows = self._load_json(); rid = _review_id(review)
        rows = [r for r in rows if r.get("_id") != rid]
        payload = review.model_dump(mode="json"); payload["_id"] = rid; rows.append(payload)
        self._json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=1))

    def _load_json(self) -> list:
        if not self._json_path.exists(): return []
        try: return json.loads(self._json_path.read_text())
        except json.JSONDecodeError:
            logger.warning("reviews.json corrupt — starting fresh"); return []

    @staticmethod
    def _meta(review: Review) -> dict:
        meta = {"product_name": review.product_name, "region": review.region.value}
        if review.rating is not None: meta["rating"] = review.rating
        if review.competitor: meta["competitor"] = review.competitor
        if review.source_url: meta["source_url"] = review.source_url
        if review.reviewer: meta["reviewer"] = review.reviewer
        if review.review_date: meta["review_date"] = review.review_date.isoformat()
        if review.run_id: meta["run_id"] = review.run_id
        if review.scraped_at: meta["scraped_at"] = review.scraped_at.isoformat()
        return meta

    @staticmethod
    def _from_meta(doc: str, m: dict) -> Review:
        return Review(product_name=(m or {}).get("product_name", "unknown"), region=(m or {}).get("region", "US"),
                      rating=(m or {}).get("rating"), review_text=doc, source_url=(m or {}).get("source_url", "chroma"),
                      competitor=(m or {}).get("competitor"), reviewer=(m or {}).get("reviewer"),
                      review_date=(m or {}).get("review_date"), run_id=(m or {}).get("run_id"),
                      scraped_at=(m or {}).get("scraped_at"))

    def _add_chroma(self, review: Review) -> None:
        import chromadb
        client = chromadb.PersistentClient(path=str(self._dir)); col = client.get_or_create_collection("reviews")
        col.upsert(ids=[_review_id(review)], documents=[review.review_text], metadatas=[self._meta(review)])

    def all(self, run_id: Optional[str] = None) -> List[Review]:
        if self._postgres:
            with self._pg_connect() as conn:
                if run_id:
                    rows = conn.execute("SELECT * FROM reviews WHERE run_id=%s ORDER BY id", (run_id,)).fetchall()
                else:
                    rows = conn.execute("SELECT * FROM reviews ORDER BY id").fetchall()
            return [Review(product_name=r['product_name'], region=r['region'], rating=r['rating'], review_text=r['review_text'],
                           source_url=r['source_url'], competitor=r['competitor'], reviewer=r['reviewer'],
                           review_date=r['review_date'], run_id=r['run_id'], scraped_at=r['scraped_at']) for r in rows]
        if self._client == "chroma":
            import chromadb
            client = chromadb.PersistentClient(path=str(self._dir)); col = client.get_or_create_collection("reviews")
            where = {"run_id": run_id} if run_id else None; res = col.get(where=where)
            return [self._from_meta(d,m) for d,m in zip(res["documents"],res["metadatas"])]
        rows=self._load_json(); rows=[r for r in rows if not run_id or r.get("run_id")==run_id]
        return [Review.model_validate(r) for r in rows]

    def search(self, query: str, n: int = 5, product: Optional[str] = None,
               region: Optional[str] = None, run_id: Optional[str] = None) -> List[Review]:
        if self._postgres:
            emb=self._embedding(query)
            with self._pg_connect() as conn:
                filters=[]; params=[]
                if product: filters.append("product_name=%s"); params.append(product)
                if region: filters.append("region=%s"); params.append(region)
                if run_id: filters.append("run_id=%s"); params.append(run_id)
                where=(" WHERE "+" AND ".join(filters)) if filters else ""
                if emb is not None:
                    sql=f"SELECT *, 1 - (embedding <=> %s::vector) AS score FROM reviews {where} AND embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT %s" if where else "SELECT *, 1 - (embedding <=> %s::vector) AS score FROM reviews WHERE embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT %s"
                    qparams=[emb,*params,emb,n]
                    rows=conn.execute(sql,qparams).fetchall()
                else:
                    # Deterministic lexical fallback when embeddings are unavailable.
                    rows=conn.execute(f"SELECT * FROM reviews {where} ORDER BY id LIMIT %s", [*params,n]).fetchall()
            reviews=[Review(product_name=r['product_name'],region=r['region'],rating=r['rating'],review_text=r['review_text'],source_url=r['source_url'],competitor=r['competitor'],reviewer=r['reviewer'],review_date=r['review_date'],run_id=r['run_id'],scraped_at=r['scraped_at']) for r in rows]
            if emb is None:
                kw=set(query.lower().split()); reviews.sort(key=lambda r:-sum(w in r.review_text.lower() for w in kw))
            return reviews[:n]
        if self._client == "chroma":
            import chromadb
            client=chromadb.PersistentClient(path=str(self._dir)); col=client.get_or_create_collection("reviews")
            where={}
            if product: where["product_name"]=product
            if region: where["region"]=region
            if run_id: where["run_id"]=run_id
            res=col.query(query_texts=[query],n_results=n,where=where or None)
            docs=res["documents"][0] if res["documents"] else []; metas=res["metadatas"][0] if res["metadatas"] else []
            return [self._from_meta(d,m) for d,m in zip(docs,metas)]
        kw=set(query.lower().split()); scored=[]
        for r in self.all(run_id=run_id):
            if product and product.lower() not in r.product_name.lower(): continue
            if region and r.region.value != region: continue
            score=sum(1 for w in kw if w in r.review_text.lower())
            if score: scored.append((score,r))
        scored.sort(key=lambda t:-t[0]); return [r for _,r in scored[:n]]
