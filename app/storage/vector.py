"""Review store — ChromaDB when installed, JSON file fallback otherwise.

v4 (reviews #10/#11): ALL metadata is stored (product_name, region, rating,
competitor, source_url, reviewer, review_date, run_id, scraped_at), None
values are omitted (Chroma-safe), and reads reconstruct the full Review.
run_id filtering keeps insights per-run. Stable SHA-256 IDs prevent collisions.
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
        self._dir = Path(settings.vector_store_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._json_path = self._dir / "reviews.json"
        try:
            import chromadb  # noqa: F401
            self._client = "chroma"
        except ImportError:
            self._client = "json"

    @property
    def backend(self) -> str:
        return self._client

    def add(self, review: Review) -> None:
        if self._client == "chroma":
            self._add_chroma(review)
        else:
            self._add_json(review)

    def _add_json(self, review: Review) -> None:
        rows = self._load_json()
        rid = _review_id(review)
        rows = [r for r in rows if r.get("_id") != rid]
        payload = review.model_dump(mode="json")
        payload["_id"] = rid
        rows.append(payload)
        self._json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=1))

    def _load_json(self) -> list:
        if not self._json_path.exists():
            return []
        try:
            return json.loads(self._json_path.read_text())
        except json.JSONDecodeError:
            logger.warning("reviews.json corrupt — starting fresh")
            return []

    @staticmethod
    def _meta(review: Review) -> dict:
        """Chroma-safe metadata: omit None values (review #11)."""
        meta = {"product_name": review.product_name, "region": review.region.value}
        if review.rating is not None:
            meta["rating"] = review.rating
        if review.competitor:
            meta["competitor"] = review.competitor
        if review.source_url:
            meta["source_url"] = review.source_url
        if review.reviewer:
            meta["reviewer"] = review.reviewer
        if review.review_date:
            meta["review_date"] = review.review_date.isoformat()
        if review.run_id:
            meta["run_id"] = review.run_id
        if review.scraped_at:
            meta["scraped_at"] = review.scraped_at.isoformat()
        return meta

    @staticmethod
    def _from_meta(doc: str, m: dict) -> Review:
        return Review(
            product_name=(m or {}).get("product_name", "unknown"),
            region=(m or {}).get("region", "US"),
            rating=(m or {}).get("rating"),
            review_text=doc,
            source_url=(m or {}).get("source_url", "chroma"),
            competitor=(m or {}).get("competitor"),
            reviewer=(m or {}).get("reviewer"),
            review_date=(m or {}).get("review_date"),
            run_id=(m or {}).get("run_id"),
            scraped_at=(m or {}).get("scraped_at"),
        )

    def _add_chroma(self, review: Review) -> None:
        import chromadb

        client = chromadb.PersistentClient(path=str(self._dir))
        col = client.get_or_create_collection("reviews")
        col.upsert(ids=[_review_id(review)], documents=[review.review_text],
                   metadatas=[self._meta(review)])

    def all(self, run_id: Optional[str] = None) -> List[Review]:
        if self._client == "chroma":
            import chromadb

            client = chromadb.PersistentClient(path=str(self._dir))
            col = client.get_or_create_collection("reviews")
            where = {"run_id": run_id} if run_id else None
            res = col.get(where=where)
            return [self._from_meta(d, m)
                    for d, m in zip(res["documents"], res["metadatas"])]
        rows = self._load_json()
        if run_id:
            rows = [r for r in rows if r.get("run_id") == run_id]
        return [Review.model_validate(r) for r in rows]

    def search(self, query: str, n: int = 5, product: Optional[str] = None,
               region: Optional[str] = None, run_id: Optional[str] = None) -> List[Review]:
        if self._client == "chroma":
            import chromadb

            client = chromadb.PersistentClient(path=str(self._dir))
            col = client.get_or_create_collection("reviews")
            where = {}
            if product:
                where["product_name"] = product
            if region:
                where["region"] = region
            if run_id:
                where["run_id"] = run_id
            res = col.query(query_texts=[query], n_results=n, where=where or None)
            docs = res["documents"][0] if res["documents"] else []
            metas = res["metadatas"][0] if res["metadatas"] else []
            return [self._from_meta(d, m) for d, m in zip(docs, metas)]
        kw = set(query.lower().split())
        scored = []
        for r in self.all(run_id=run_id):
            if product and product.lower() not in r.product_name.lower():
                continue
            if region and r.region.value != region:
                continue
            score = sum(1 for w in kw if w in r.review_text.lower())
            if score:
                scored.append((score, r))
        scored.sort(key=lambda t: -t[0])
        return [r for _, r in scored[:n]]
