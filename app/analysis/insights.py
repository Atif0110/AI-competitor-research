"""Insights engine — run-scoped intelligence layer (v4).

- EVERY analysis method accepts run_id and is scoped to that run's observations
  (reviews #1/#2): the report for run A never mixes runs B/C data.
- Price statistics report BOTH native and USD-normalized values (reviews #8/#9);
  comparisons always use normalized USD, display always uses native currency.
- Sentiment average ignores None ratings (review #13 — real bug fixed).
- new_product events: product identity absent before the latest snapshot
  (review #14).
- RAG Q&A: zero relevant retrieval returns an honest "no relevant reviews"
  answer — never substitutes arbitrary reviews (review #12).
"""
from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from datetime import timedelta
from typing import Dict, List, Optional

from app.analysis.product_identity import resolve
from app.config import settings
from app.llm.client import LLMClient
from app.schemas import (EventAlert, PriceMove, PriceStats, ProductOffer, Region,
                         Review, SentimentSummary, Undercut)
from app.storage.db import OfferStore
from app.storage.vector import ReviewStore

logger = logging.getLogger(__name__)

_MIN_PRICE_CHANGE_PCT = 5.0
_COMPLAINT_TERMS = ["bulky", "frustrating", "slow", "expensive", "short", "breaks", "loud", "delay"]
_PRAISE_TERMS = ["amazing", "superb", "great", "comfortable", "excellent", "love", "perfect", "fast"]

_SENTIMENT_SYSTEM = (
    "You classify customer reviews for competitive intelligence. Return ONLY JSON with "
    'keys: "sentiment" ("positive"|"neutral"|"negative"), "topics" (topic->count), '
    '"complaints" (list of short phrases), "praise" (list), '
    '"feature_severities" ([{"feature": str, "severity": 1-5}]). No prose.'
)

_POSITIONING_SYSTEM = (
    "You are a competitive intelligence analyst. Write a 3-4 sentence competitive "
    "positioning brief for the product, given the structured facts below. Contrast the "
    "target against each competitor, reference rating/complaint evidence, and end with "
    "one clear recommendation. Plain prose, no markdown, no JSON."
)

_QA_SYSTEM = (
    "You are a competitive-intelligence analyst. Answer the question using ONLY the "
    "provided customer reviews. Quote specific review text. Finish by listing the "
    "source URLs you used. If the reviews cannot answer, say so plainly."
)


def _price_key(o: ProductOffer) -> float:
    """Comparison value: normalized USD when available, native otherwise."""
    return o.normalized_price_usd if o.normalized_price_usd is not None else o.price


def _gid(o: ProductOffer) -> str:
    return o.canonical_product_id or resolve(o.product_name).canonical_id


def _gname(o: ProductOffer) -> str:
    return o.canonical_product_name or o.product_name


class InsightsEngine:
    def __init__(self, store: OfferStore, review_store: ReviewStore,
                 llm: Optional[LLMClient] = None, run_id: Optional[str] = None):
        self.store = store
        self.review_store = review_store
        self.llm = llm or LLMClient()
        self.run_id = run_id  # optional default scope

    def _llm_usable(self) -> bool:
        return self.llm is not None and self.llm.provider_name != "demo"

    def _scope(self, run_id: Optional[str]) -> Optional[str]:
        return run_id or self.run_id

    def _offers(self, run_id: Optional[str] = None, limit: int = 20000) -> List[ProductOffer]:
        return self.store.recent_offers(limit=limit, run_id=self._scope(run_id))

    def _reviews(self, run_id: Optional[str] = None) -> List[Review]:
        return self.review_store.all(run_id=self._scope(run_id))

    def _match_offers(self, product: str, region: Optional[Region] = None,
                      competitor: Optional[str] = None,
                      run_id: Optional[str] = None) -> List[ProductOffer]:
        gid = resolve(product).canonical_id
        offers = []
        for o in self._offers(run_id, limit=10000):
            if not (_gid(o) == gid or product.lower() in o.product_name.lower()):
                continue
            if region is not None and o.region != region:
                continue
            if competitor is not None and (o.competitor or o.seller or "unknown") != competitor:
                continue
            offers.append(o)
        return offers

    def _series_groups(self, offers: List[ProductOffer]):
        groups = defaultdict(list)
        for o in offers:
            if o.availability.value == "out_of_stock":
                continue
            ent = o.competitor or o.seller or "unknown"
            groups[(_gid(o), _gname(o), o.region, ent)].append(o)
        return groups

    # ================= price series / stats =================
    def price_series(self, product: str, region: Region,
                     competitor: Optional[str] = None,
                     run_id: Optional[str] = None) -> list:
        offers = sorted(self._match_offers(product, region, competitor, run_id),
                        key=lambda x: x.scraped_at)
        return [(o.scraped_at.date().isoformat(), o.price,
                 o.competitor or o.seller or "unknown", o.normalized_price_usd) for o in offers]

    def price_stats(self, product: str, region: Region,
                    competitor: Optional[str] = None,
                    run_id: Optional[str] = None) -> Optional[PriceStats]:
        offers = self._match_offers(product, region, competitor, run_id)
        if not offers:
            return None
        series = sorted(offers, key=lambda o: o.scraped_at)
        today = series[-1].scraped_at.date()
        usd_vals = [(_price_key(o)) for o in series]
        native = [o.price for o in series]

        def change(days: int) -> Optional[float]:
            cutoff = today - timedelta(days=days)
            pivot = [o for o in series if o.scraped_at.date() >= cutoff]
            if len(pivot) >= 2 and _price_key(pivot[0]) > 0:
                return round((_price_key(pivot[-1]) - _price_key(pivot[0])) / _price_key(pivot[0]) * 100, 2)
            return None

        vol = 0.0
        if len(usd_vals) >= 3:
            pcts = [(usd_vals[i] - usd_vals[i - 1]) / usd_vals[i - 1] * 100
                    for i in range(1, len(usd_vals)) if usd_vals[i - 1] > 0]
            vol = statistics.pstdev(pcts) if pcts else 0.0
        return PriceStats(
            product_name=_gname(series[-1]), region=region, currency=series[-1].currency,
            current_price=series[-1].price,
            previous_price=series[-2].price if len(series) > 1 else None,
            current_usd=_price_key(series[-1]),
            min_price=min(native), max_price=max(native),
            median_price=round(statistics.median(native), 2),
            min_usd=min(usd_vals), max_usd=max(usd_vals),
            median_usd=round(statistics.median(usd_vals), 2),
            change_7d=change(7), change_14d=change(14), change_30d=change(30),
            volatility=round(vol, 4), samples=len(series),
        )

    def regional_snapshot(self, run_id: Optional[str] = None) -> List[dict]:
        rows = []
        for (gid, name, region, ent), group in self._series_groups(self._offers(run_id, 10000)).items():
            series = sorted(group, key=lambda o: o.scraped_at)
            cur = series[-1]
            p7 = None
            cutoff = cur.scraped_at.date() - timedelta(days=7)
            pivot = [o for o in series if o.scraped_at.date() >= cutoff]
            if len(pivot) >= 2 and _price_key(pivot[0]) > 0:
                p7 = round((_price_key(pivot[-1]) - _price_key(pivot[0])) / _price_key(pivot[0]) * 100, 2)
            rows.append({
                "product": name, "region": region.value, "competitor": ent,
                "current_native": cur.price, "currency": cur.currency.value,
                "current_usd": _price_key(cur), "change_7d": p7,
                "min_usd": min(_price_key(o) for o in series),
                "max_usd": max(_price_key(o) for o in series),
                "samples": len(series), "run_id": cur.run_id,
            })
        rows.sort(key=lambda r: (r["product"], r["region"], r["competitor"]))
        return rows

    # ================= moves / undercuts =================
    def price_moves(self, days_back: int = 30, run_id: Optional[str] = None) -> List[PriceMove]:
        moves: List[PriceMove] = []
        for (gid, name, region, ent), group in self._series_groups(self._offers(run_id, 30000)).items():
            group.sort(key=lambda o: o.scraped_at)
            dates = sorted({o.scraped_at.date() for o in group})
            if len(dates) < 2:
                continue
            prev = [o for o in group if o.scraped_at.date() == dates[-2]]
            last = [o for o in group if o.scraped_at.date() == dates[-1]]
            if not prev or not last:
                continue
            from_u = min(_price_key(o) for o in prev)
            to_u = min(_price_key(o) for o in last)
            if not from_u:
                continue
            from_native = min(o.price for o in prev)
            to_native = min(o.price for o in last)
            change = (to_u - from_u) / from_u * 100
            if abs(change) >= _MIN_PRICE_CHANGE_PCT:
                moves.append(PriceMove(
                    product_name=name, region=region,
                    from_price=from_native, to_price=to_native,
                    currency=last[0].currency,
                    from_price_usd=round(from_u, 2), to_price_usd=round(to_u, 2),
                    change_pct=round(change, 2), from_date=dates[-2], to_date=dates[-1],
                    event="drop" if change < 0 else "increase",
                ))
        moves.sort(key=lambda m: m.change_pct)
        return moves

    def undercut_analysis(self, region: Optional[Region] = None,
                          run_id: Optional[str] = None) -> List[Undercut]:
        offers = [o for o in self._offers(run_id, 30000)
                  if o.availability.value != "out_of_stock"]
        if region:
            offers = [o for o in offers if o.region == region]
        by_product: Dict[tuple, list] = defaultdict(list)
        for o in offers:
            by_product[(_gid(o), _gname(o), o.region)].append(o)
        results = []
        for (gid, name, reg), group in by_product.items():
            best = min(group, key=_price_key)
            runners = [o for o in group if o is not best]
            runner = min(runners, key=_price_key) if runners else None
            src_urls = [o.url for o in ([best] + ([runner] if runner else []))][:4]
            times = [o.scraped_at.strftime("%Y-%m-%d %H:%M") for o in ([best] + ([runner] if runner else []))][:4]
            results.append(Undercut(
                product_name=name, region=reg,
                leader=best.competitor or best.seller or "unknown", leader_price=best.price,
                currency=best.currency,
                runner_up=(runner.competitor or runner.seller) if runner else None,
                runner_up_price=runner.price if runner else None,
                gap_pct=round((_price_key(runner) - _price_key(best)) / _price_key(best) * 100, 2)
                if runner else None,
                source_urls=src_urls, observation_times=times,
            ))
        results.sort(key=lambda u: u.product_name)
        return results

    # ================= sentiment =================
    def sentiment(self, product: Optional[str] = None,
                  region: Optional[Region] = None,
                  run_id: Optional[str] = None) -> SentimentSummary:
        reviews = self._reviews(run_id)
        if product:
            reviews = [r for r in reviews if product.lower() in r.product_name.lower()]
        if region:
            reviews = [r for r in reviews if r.region == region]
        if self._llm_usable() and reviews:
            try:
                return self._llm_sentiment(reviews)
            except Exception as e:
                logger.warning("LLM sentiment failed (%s) — keyword fallback", e)
        return self._keyword_sentiment(reviews)

    def _keyword_sentiment(self, reviews: List[Review]) -> SentimentSummary:
        ratings = [r.rating for r in reviews if r.rating is not None]  # #13: ignore None
        topics: Dict[str, int] = defaultdict(int)
        complaints, praise = [], []
        for r in reviews:
            text = r.review_text.lower()
            for t in _COMPLAINT_TERMS:
                if t in text:
                    topics["complaint:" + t] += 1
            for p in _PRAISE_TERMS:
                if p in text:
                    topics["praise:" + p] += 1
            complaints.extend(r.review_text for t in _COMPLAINT_TERMS if t in text)
            praise.extend(r.review_text for p in _PRAISE_TERMS if p in text)
        total = sum(topics.values()) or 1
        return SentimentSummary(
            review_count=len(reviews),
            avg_rating=round(sum(ratings) / len(ratings), 2) if ratings else None,
            topic_mentions=dict(topics),
            topic_breakdown={k: round(v / total * 100, 1) for k, v in topics.items()},
            top_complaints=complaints[:5], top_praise=praise[:5],
            fallback_used=True,
            provenance=list(dict.fromkeys(r.source_url for r in reviews))[:10],
        )

    def _llm_sentiment(self, reviews: List[Review]) -> SentimentSummary:
        ratings = [r.rating for r in reviews if r.rating is not None]
        user = "\n".join(f"- [{r.rating or '?'}/5 {r.region.value}] {r.review_text}"
                         for r in reviews[:40])
        raw = self.llm.complete(_SENTIMENT_SYSTEM, user)
        data = json.loads(_strip_json(raw))
        topics = data.get("topics") or {}
        total = sum(topics.values()) or 1
        return SentimentSummary(
            review_count=len(reviews),
            avg_rating=round(sum(ratings) / len(ratings), 2) if ratings else None,
            topic_mentions=dict(topics),
            topic_breakdown={k: round(v / total * 100, 1) for k, v in topics.items()},
            top_complaints=data.get("complaints", [])[:5],
            top_praise=data.get("praise", [])[:5],
            feature_severities=data.get("feature_severities", []),
            fallback_used=False,
            provenance=list(dict.fromkeys(r.source_url for r in reviews))[:10],
        )

    # ================= positioning brief =================
    def positioning_brief(self, product: str, region: Optional[Region] = None,
                          run_id: Optional[str] = None) -> dict:
        base = self._brief_base(product, region, run_id)
        if self._llm_usable():
            try:
                payload = (
                    f"Product: {product}\n"
                    f"Price landscape: {'; '.join(base['price_range_in_regions']) or 'n/a'}\n"
                    f"Average rating: {base['avg_rating'] or 'n/a'} ({base['review_count']} reviews)\n"
                    f"Complaints: {'; '.join(base['top_complaints']) or 'none captured'}\n"
                    f"Praise: {'; '.join(base['top_praise']) or 'none captured'}")
                base["brief"] = self.llm.complete(_POSITIONING_SYSTEM, payload).strip()
                base["llm_generated"] = True
                return base
            except Exception as e:
                logger.warning("LLM positioning failed (%s) — deterministic fallback", e)
        base["brief"] = base["recommendation"]
        base["llm_generated"] = False
        return base

    def _brief_base(self, product: str, region: Optional[Region],
                    run_id: Optional[str]) -> dict:
        undercuts = [u for u in self.undercut_analysis(region, run_id)
                     if u.product_name == product
                     or resolve(u.product_name).canonical_id == resolve(product).canonical_id]
        senti = self.sentiment(product=product, region=region, run_id=run_id)
        offers = self._match_offers(product, region, run_id=run_id)
        urls = [o.url for o in offers[:50]]
        span = None
        if undercuts:
            span = (min(u.leader_price for u in undercuts), max(u.leader_price for u in undercuts))
        return {
            "product": product,
            "price_range_in_regions": [f"{u.region.value}: {u.leader_price} ({u.leader})"
                                       for u in undercuts],
            "avg_rating": senti.avg_rating,
            "review_count": senti.review_count,
            "top_complaints": senti.top_complaints[:3],
            "top_praise": senti.top_praise[:3],
            "topic_breakdown": senti.topic_breakdown,
            "provenance": list(dict.fromkeys(urls))[:10],
            "recommendation": _recommendation(span),
        }

    # ================= events (incl. new_product, #14) =================
    def detect_events(self, run_id: str = "",
                      drop_threshold_pct: Optional[float] = None) -> List[EventAlert]:
        threshold = drop_threshold_pct or settings.price_drop_alert_pct
        events: List[EventAlert] = []
        offers = self._offers(run_id, 30000)
        groups = self._series_groups(offers)
        all_dates = sorted({o.scraped_at.date() for o in offers})
        latest_date = all_dates[-1] if all_dates else None

        for (gid, name, region, ent), group in groups.items():
            group.sort(key=lambda o: o.scraped_at)
            dates = sorted({o.scraped_at.date() for o in group})
            if latest_date and min(dates) == latest_date and len(all_dates) >= 2:
                events.append(EventAlert(
                    run_id=run_id, kind="new_product", severity="info",
                    message=f"{ent} listed {name} in {region.value} for the first time in this window.",
                    product_name=name, region=region, competitor=ent,
                ))
            if len(dates) < 2:
                continue
            prev = [o for o in group if o.scraped_at.date() == dates[-2]]
            last = [o for o in group if o.scraped_at.date() == dates[-1]]
            if not prev or not last:
                continue
            from_u = min(_price_key(o) for o in prev)
            to_u = min(_price_key(o) for o in last)
            if from_u <= 0:
                continue
            change = (to_u - from_u) / from_u * 100
            if change <= -threshold:
                events.append(EventAlert(
                    run_id=run_id, kind="price_drop",
                    severity="critical" if change <= -2 * threshold else "warn",
                    message=f"{ent} reduced {name} by {abs(change):.1f}% in {region.value} "
                            f"({min(o.price for o in prev):.2f} -> {min(o.price for o in last):.2f}).",
                    product_name=name, region=region, competitor=ent,
                    change_pct=round(change, 2),
                ))
            elif change >= threshold:
                events.append(EventAlert(
                    run_id=run_id, kind="price_increase", severity="info",
                    message=f"{ent} increased {name} by {change:.1f}% in {region.value}.",
                    product_name=name, region=region, competitor=ent,
                    change_pct=round(change, 2),
                ))

        by_product: Dict[tuple, list] = defaultdict(list)
        for o in offers:
            if o.availability.value != "out_of_stock":
                by_product[(_gid(o), _gname(o), o.region)].append(o)
        for (gid, name, region), sub in by_product.items():
            dates = sorted({o.scraped_at.date() for o in sub})
            if len(dates) < 2:
                continue
            prev = [o for o in sub if o.scraped_at.date() == dates[-2]]
            last = [o for o in sub if o.scraped_at.date() == dates[-1]]
            if not prev or not last:
                continue
            p_lead = min(prev, key=_price_key)
            l_lead = min(last, key=_price_key)
            if (p_lead.competitor or p_lead.seller) != (l_lead.competitor or l_lead.seller):
                events.append(EventAlert(
                    run_id=run_id, kind="undercut_change", severity="warn",
                    message=f"{(l_lead.competitor or l_lead.seller)} is now the lowest for {name} "
                            f"in {region.value}, displacing {p_lead.competitor or p_lead.seller}.",
                    product_name=name, region=region, competitor=l_lead.competitor or l_lead.seller,
                ))
        return events

    # ================= RAG Q&A (honest retrieval, #12) =================
    def answer_question(self, question: str, region: Optional[Region] = None,
                        n: int = 8, run_id: Optional[str] = None) -> dict:
        scope = self._scope(run_id)
        hits = self.review_store.search(question, n=n,
                                        region=region.value if region else None,
                                        run_id=scope)
        if not hits:
            if not self._reviews(scope):
                return {"answer": "No reviews stored yet — run research first.", "sources": []}
            return {"answer": "No relevant reviews were retrieved for this question. "
                              "Try a broader question or check that reviews exist for this filter.",
                    "sources": []}

        if self._llm_usable():
            try:
                user = (f"Question: {question}\n\nReviews:\n" + "\n".join(
                    f"- [{r.region.value}|{r.rating or '?'}/5] {r.review_text} (source: {r.source_url})"
                    for r in hits))
                answer = self.llm.complete(_QA_SYSTEM, user)
            except Exception as e:
                logger.warning("RAG LLM synthesis failed (%s) — fallback", e)
                answer = self._rag_fallback(question, hits)
        else:
            answer = self._rag_fallback(question, hits)

        sources = [{"product": r.product_name, "region": r.region.value,
                    "url": r.source_url, "rating": r.rating, "run_id": r.run_id} for r in hits]
        return {"answer": answer, "sources": sources}

    def _rag_fallback(self, question: str, hits: List[Review]) -> str:
        lines = [f"- ({r.region.value}) {r.review_text}" for r in hits[:5]]
        return ("Top reviews related to your question (keyword retrieval; configure LLM keys "
                "for synthesized answers):\n" + "\n".join(lines))


def _strip_json(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()


def _recommendation(span) -> str:
    if not span:
        return "Collect more data before recommending a price."
    low, high = span
    if high / low > 1.15:
        return (f"Price gap is large ({low:.2f}–{high:.2f}): investigate whether the low seller "
                "is bundle-only, refurb, or a stock-out bait.")
    return "Prices are within a healthy band; differentiate on positioning and reviews."
