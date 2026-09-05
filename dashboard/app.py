"""Streamlit dashboard v2 — KPIs from real run metrics, geo-proxy status,
competitor/region/product filters, price trend charts, sentiment, reports.

Usage:  streamlit run dashboard/app.py   (pip install streamlit)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

from app.analysis.insights import InsightsEngine  # noqa: E402
from app.config import settings  # noqa: E402
from app.orchestrator import Pipeline  # noqa: E402
from app.schemas import Competitor, CompetitorTarget, Region  # noqa: E402
from app.storage.db import OfferStore  # noqa: E402
from app.storage.vector import ReviewStore  # noqa: E402

settings.ensure_dirs()
store = OfferStore()
reviews = ReviewStore()
pipeline = Pipeline(store=store, review_store=reviews)
insights = InsightsEngine(store, reviews, pipeline.extractor.client)

st.set_page_config(page_title="Competitive Intelligence", layout="wide")

if "target_state" not in st.session_state:
    st.session_state.target_state = {
        "company": "Acme Audio", "website": "https://acme.example.com",
        "regions": ["US", "EU"], "products": "Pro X Headphones, Lite Earbuds",
        "competitors": "SoundWorks, GloboTech",
    }

agg = store.aggregate_metrics()
latest = agg.get("latest_run") or {}
st.title("Competitive Intelligence Dashboard")
st.caption(f"Mode: {'demo (no API keys)' if settings.demo_mode else 'live'} · "
           f"Offers: {store.count()} · Review backend: {reviews.backend}")

k1, k2, k3, k4 = st.columns(4)
k1.metric("Products tracked", latest.get("products", 0) or 0)
k2.metric("Regions", latest.get("regions", 0) or 0)
k3.metric("Scraping success", f"{latest.get('scraping_success_rate', 0) or 0:.0%}" if latest else "—",
          help="Most recent run's scraping success rate, from run_metrics")
k4.metric("Alerts", len(pipeline.insights.detect_events()) if store.count() else 0)

tab_run, tab_offers, tab_insights, tab_proxy, tab_metrics, tab_report = st.tabs(
    ["Run research", "Offers", "Insights", "Geo proxies", "Metrics", "Reports"])

with tab_run:
    st.subheader("New research run (target + competitor graph)")
    with st.form("run"):
        c1, c2 = st.columns(2)
        company = c1.text_input("Company", st.session_state.target_state["company"])
        website = c2.text_input("Website", st.session_state.target_state["website"])
        regions = st.multiselect("Regions", [r.value for r in Region],
                                 default=st.session_state.target_state["regions"])
        products = st.text_input("Focus products (comma separated)",
                                 st.session_state.target_state["products"])
        competitors = st.text_input("Competitors (comma separated names, websites "
                                    "will be {name}.example.com in demo)",
                                    st.session_state.target_state["competitors"])
        submit = st.form_submit_button("Run")
    if submit:
        target = CompetitorTarget(
            company=company, website=website,
            regions=[Region(r) for r in regions],
            focus_products=[p.strip() for p in products.split(",") if p.strip()],
            competitors=[Competitor(name=n.strip(), website=f"https://{n.strip().lower().replace(' ', '-')}.example.com")
                         for n in competitors.split(",") if n.strip()],
        )
        with st.spinner("Running pipeline…"):
            result = pipeline.run(target)
        st.success(f"Run {result.run_id} · {result.duration_s}s · "
                   f"{result.pages_scraped} pages · {result.extractions_ok} extractions · "
                   f"scrape {result.metrics.get('scraping_success_rate', 0) * 100:.1f}% · "
                   f"extraction {result.metrics.get('extraction_accuracy', 0) * 100:.1f}%")
        for e in result.events or []:
            st.warning(e["message"])

with tab_offers:
    col_p, col_r = st.columns(2)
    product = col_p.text_input("Filter product", "")
    region_f = col_r.selectbox("Filter region", ["ALL"] + [r.value for r in Region])
    offers = store.recent_offers(product=product or None,
                                 region=Region(region_f) if region_f != "ALL" else None, limit=200)
    st.dataframe([o.model_dump(mode="json") for o in offers], use_container_width=True)
    st.caption("Every row: native price + USD-normalized price + source URL + scrape time "
               "(provenance).")

with tab_insights:
    left, right = st.columns(2)
    with left:
        st.subheader("Price undercut")
        st.dataframe([u.model_dump(mode="json") for u in insights.undercut_analysis()],
                     use_container_width=True)
    with right:
        st.subheader("Price moves")
        st.dataframe([m.model_dump(mode="json") for m in insights.price_moves()],
                     use_container_width=True)
    st.subheader("Sentiment (LLM classification when keys configured, keyword fallback otherwise)")
    senti = insights.sentiment()
    st.json({k: senti.model_dump(mode="json")[k] for k in
             ("review_count", "avg_rating", "topic_breakdown", "fallback_used")})

    st.subheader("Ask about customer feedback (RAG with citations)")
    q = st.text_input("Question", "What are customers complaining about in India?")
    if st.button("Ask") and q.strip():
        ans = insights.answer_question(q)
        st.markdown(ans["answer"])
        st.caption("Sources:")
        for s in ans["sources"][:5]:
            st.markdown(f"- **{s['product']}** [{s['region']}] {s['url']}")

with tab_proxy:
    from app.scrapers.base import ProxyPool

    pool = ProxyPool(settings.proxy_urls, settings.proxy_regions)
    st.write("**Configured pools:**", {"generic": len(settings.proxy_urls),
                                       **{k: len(v) for k, v in settings.proxy_regions.items()}})
    status = "verification ON — exit IP country checked before use" if settings.proxy_verify \
        else "verification OFF (PROXY_VERIFY=false) — round-robin selection"
    st.caption(status)
    rows = [{"Region": reg, "Proxies": len(v),
             "Status": " unverified" if not settings.proxy_verify else "✓ pool ready"}
            for reg, v in settings.proxy_regions.items()]
    if not settings.proxy_regions:
        st.info("No geo proxies configured — add PROXY_IN=..., PROXY_UK=... etc. to .env.")
    st.dataframe(rows or [{"Region": "—", "Proxies": 0, "Status": "none"}],
                 use_container_width=True)

with tab_metrics:
    st.subheader("Run metrics (the resume numbers)")
    st.dataframe(store.recent_runs(limit=10), use_container_width=True)
    st.json(store.aggregate_metrics())

with tab_report:
    import glob

    files = sorted(glob.glob(f"{settings.report_output_dir}/report_*.pdf"))
    if files:
        latest = max(files, key=Path.stat)
        st.write(f"{Path(latest).name}")
        st.download_button("Download report PDF", data=Path(latest).read_bytes(),
                           file_name=Path(latest).name, mime="application/pdf")
    else:
        st.info("No reports yet — run research, then `python scripts/generate_report.py`.")
