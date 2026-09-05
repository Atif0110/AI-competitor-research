"""PDF report builder (reportlab) — a consulting-grade business deliverable.

v4 (reviews #1/#25): the ENTIRE report is scoped to one run_id — undercut,
price moves, regional snapshot, sentiment and positioning briefs all pass
run_id to the InsightsEngine so observations from other runs never leak in.
Every analytical table carries its provenance inline: source URLs + scrape
times under each row, and the appendix traces all observations of the run
with their observation IDs.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from app.analysis.insights import InsightsEngine
from app.config import settings
from app.schemas import PipelineResult, ProductOffer, Region

logger = logging.getLogger(__name__)

_BLUE = colors.HexColor("#1a3a6b")
_LIGHT = colors.HexColor("#eef2f7")


class ReportBuilder:
    def __init__(self, insights: InsightsEngine):
        self.insights = insights
        self.out_dir = Path(settings.report_output_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.run_id: Optional[str] = None

    def build(self, result: PipelineResult) -> Path:
        self.run_id = result.run_id        # scope everything to this run (#1)
        path = self.out_dir / f"report_{result.run_id}.pdf"
        doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=18 * mm,
                                rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm)
        styles = getSampleStyleSheet()
        h1 = ParagraphStyle("H1X", parent=styles["Title"], fontSize=20, spaceAfter=6)
        h2 = ParagraphStyle("H2X", parent=styles["Heading2"], fontSize=13, textColor=_BLUE)
        body = ParagraphStyle("BodyX", parent=styles["BodyText"], fontSize=9.5, leading=13)
        small = ParagraphStyle("SmallX", parent=styles["BodyText"], fontSize=7.5,
                               leading=9.5, textColor=colors.HexColor("#444444"))

        story = []
        story.append(Paragraph("Competitive Intelligence Report", h1))
        story.append(Paragraph(
            f"Target: <b>{result.target.company}</b> · Run: {result.run_id} · Date: {result.run_date} "
            f"· Mode: {result.mode} · Analysis scoped to run {result.run_id} only.",
            body))
        story.append(Paragraph(
            f"Competitors tracked: {result.competitors} · Regions: {len(result.target.regions)} · "
            f"Pages scraped: {result.pages_scraped} (failed {result.extractions_failed} extractions) · "
            f"Retries: {result.retries_used} · Duration: {result.duration_s}s",
            body))
        story.append(Spacer(1, 10))

        # 0. Executive summary — from real run metrics + detected events
        story.append(Paragraph("Executive Summary", h2))
        m = result.metrics or {}
        story.append(Paragraph(
            f"Scraping success <b>{m.get('scraping_success_rate', 0) * 100:.1f}%</b> · "
            f"Extraction accuracy <b>{m.get('extraction_accuracy', 0) * 100:.1f}%</b> · "
            f"Products observed (this run) <b>{m.get('products', 0)}</b> · "
            f"Data sources <b>{result.discovered_urls}</b> · "
            f"LLM providers: <b>{', '.join(m.get('providers_used', [])) or 'demo'}</b>",
            body))
        events = result.events or []
        if events:
            story.append(Paragraph("Top movements this run:", body))
            for e in events[:5]:
                icon = {"price_drop": "▼", "price_increase": "▲", "undercut_change": "⇄",
                        "new_product": "★", "low_scrape_success": "!"}.get(e.get("kind", ""), "•")
                story.append(Paragraph(f"{icon} {e['message']}", body))
        else:
            story.append(Paragraph("No significant price events detected in this window "
                                   "(events appear once two snapshots exist).", body))
        story.append(Spacer(1, 10))

        story.append(Paragraph("1. Price Undercut Analysis", h2))
        self._undercut_table(story, small)
        story.append(Spacer(1, 10))

        story.append(Paragraph("2. Price Moves & Trends", h2))
        self._moves_table(story)
        self._regional_table(story)
        story.append(Spacer(1, 10))

        story.append(Paragraph("3. Review Sentiment", h2))
        self._sentiment_block(story)
        story.append(Spacer(1, 10))

        story.append(Paragraph("4. Positioning Briefs", h2))
        self._briefs(story)

        story.append(Spacer(1, 12))
        story.append(Paragraph("Appendix A — Data Provenance (traceability)", h2))
        story.append(Paragraph(
            "Every observation below links an insight back to its source URL and scrape time. "
            "All rows are filtered to run "
            + (self.run_id or "?") + " — observations from later runs are excluded from this report.",
            body))
        self._provenance_table(story)

        if result.errors:
            story.append(Spacer(1, 8))
            story.append(Paragraph(f"Warnings ({len(result.errors)}):", body))
            for err in result.errors[:10]:
                story.append(Paragraph("· " + err[:150], body))

        doc.build(story)
        return path

    def _undercut_table(self, story, small) -> None:
        # provenance inline: source URL + scrape time under each row (#25)
        rows = [["Product", "Region", "Lowest seller", "Price", "Runner-up", "Gap %", "Source URL / time"]]
        for u in self.insights.undercut_analysis(run_id=self.run_id):
            src = (u.source_urls[0][:38] if u.source_urls else "—")
            t = (u.observation_times[0] if u.observation_times else "—")
            rows.append([u.product_name, u.region.value, u.leader,
                         f"{u.leader_price:.2f} ({u.currency.value})",
                         u.runner_up or "—",
                         f"{u.gap_pct:.1f}%" if u.gap_pct is not None else "—",
                         src + " @" + t])
        if len(rows) == 1:
            story.append(Paragraph("No structured offers in this run yet.", self._body()))
            return
        story.append(self._table(rows))
        story.append(Paragraph("Prices shown in native currency; the gap % is computed on "
                               "USD-normalized values (review #8/#9).", small))

    def _moves_table(self, story) -> None:
        rows = [["Product", "Region", "Event", "From (native)", "To (native)", "Δ% (USD)",
                 "From USD", "To USD", "Currency"]]
        for mv in self.insights.price_moves(run_id=self.run_id):
            rows.append([mv.product_name, mv.region.value, mv.event,
                         f"{mv.from_price:.2f}", f"{mv.to_price:.2f}", f"{mv.change_pct:+.1f}%",
                         f"{mv.from_price_usd:.2f}" if mv.from_price_usd is not None else "—",
                         f"{mv.to_price_usd:.2f}" if mv.to_price_usd is not None else "—",
                         mv.currency.value])
        if len(rows) > 1:
            story.append(self._table(rows))
        else:
            story.append(Paragraph("No price moves ≥5% between snapshots.", self._body()))

    def _regional_table(self, story) -> None:
        rows = [["Product", "Region", "Competitor", "Current (native)", "7d Δ%",
                 "Min USD", "Max USD", "Samples"]]
        for r in self.insights.regional_snapshot(run_id=self.run_id):
            rows.append([r["product"], r["region"], r["competitor"],
                         f"{r['current_native']:.2f} ({r['currency']})",
                         f"{r['change_7d']:+.1f}" if r["change_7d"] is not None else "—",
                         f"{r['min_usd']:.2f}", f"{r['max_usd']:.2f}", str(r["samples"])])
        if len(rows) > 1:
            story.append(Paragraph("Regional price snapshot (native currency for display; "
                                   "cross-currency statistics in USD):", self._body()))
            story.append(self._table(rows))

    def _sentiment_block(self, story) -> None:
        senti = self.insights.sentiment(run_id=self.run_id)
        story.append(Paragraph(
            f"Reviews (run {self.run_id}): {senti.review_count} · Avg rating: {senti.avg_rating or 'n/a'} · "
            f"Method: {'LLM classification' if not senti.fallback_used else 'keyword fallback (no LLM keys)'}",
            self._body()))
        if senti.topic_breakdown:
            top = sorted(senti.topic_breakdown.items(), key=lambda kv: -kv[1])[:5]
            story.append(Paragraph("Top topics: " + " · ".join(f"{k} {v}%" for k, v in top), self._body()))
        if senti.top_complaints:
            story.append(Paragraph("Complaints: " + "; ".join(senti.top_complaints[:3]), self._body()))
        if senti.top_praise:
            story.append(Paragraph("Praise: " + "; ".join(senti.top_praise[:3]), self._body()))

    def _briefs(self, story) -> None:
        products = sorted({u.product_name for u in self.insights.undercut_analysis(run_id=self.run_id)})
        if not products:
            story.append(Paragraph("Run the pipeline first to collect offers.", self._body()))
            return
        for p in products:
            brief = self.insights.positioning_brief(p, run_id=self.run_id)
            story.append(Paragraph(f"<b>{brief['product']}</b>"
                                   f"{' · LLM synthesis' if brief.get('llm_generated') else ' · deterministic'}", self._body()))
            story.append(Paragraph(brief.get("brief") or brief["recommendation"], self._body()))
            story.append(Paragraph(
                "Price range: " + "; ".join(brief["price_range_in_regions"] or ["n/a"]) +
                f" · Avg rating: {brief['avg_rating'] or 'n/a'} ({brief['review_count']} reviews)",
                self._body()))
            story.append(Spacer(1, 4))

    def _provenance_table(self, story) -> None:
        offers: List[ProductOffer] = self.insights.store.recent_offers(
            run_id=self.run_id, limit=40)
        if not offers:
            story.append(Paragraph("No observations in this run to trace.", self._body()))
            return
        story.append(Paragraph(f"Showing up to 40 of {len(offers)} observations from run "
                               f"{self.run_id} — each row can be re-fetched from its source URL "
                               "and reconciled against the stored raw evidence.", self._body()))
        rows = [["#", "Product", "Region", "Competitor", "Price", "Currency", "Source URL", "Scraped at (UTC)"]]
        for i, o in enumerate(offers, 1):
            rows.append([str(i), o.product_name, o.region.value,
                         o.competitor or o.seller or "—",
                         f"{o.price:.2f}", o.currency.value,
                         o.url[:40], o.scraped_at.strftime("%Y-%m-%d %H:%M")])
        story.append(self._table(rows))

    def _table(self, rows) -> Table:
        t = Table(rows, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), _BLUE),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _LIGHT]),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        return t

    def _body(self):
        return getSampleStyleSheet()["BodyText"]
