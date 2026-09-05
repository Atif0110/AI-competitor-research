# AI Competitor Research Tool — v3

An agent-driven competitive intelligence system: target + competitor graph ×
regions × discovered product pages, fetched **once per execution region**,
extracted with an **enforced execution region** and schema+semantic validation,
stored with upsert idempotency and run-level provenance, analyzed on
**product identity × region × competitor** with **USD-normalized** comparisons,
and delivered as PDF report, dashboard, RAG Q&A, event alerts and a scheduler.
Every number on the dashboard/report comes from the `run_metrics` table.

Built for **Mohd Atif — Freelance AI/ML Engineer**.
Stack: **Python · FastAPI · LLM orchestration (multi-provider fallback) · RAG · agent workflows**.

---

## 1. What changed in v3 (engineering review fixes)

| # | Fix | Where |
|---|---|---|
| 1 | **Real region fan-out** — every URL is fetched once per execution region; `region=` is passed to the scraper chain in live mode too | `app/orchestrator.py` |
| 2 | **expected_region enforced** — the LLM cannot pick the region; a mismatch is logged and the execution region wins | `app/llm/extractor.py` |
| 3 | **Firecrawl geo honesty** — Firecrawl is API-side: it uses its own `location` control and ignores client proxies (documented; proxy only applies to Playwright/Basic) | `app/scrapers/firecrawl.py` |
| 4 | **Verified-region-or-fail** — `PROXY_VERIFY=true` never silently falls back to the wrong geography | `app/scrapers/base.py` |
| 5 | **Per-run metric isolation** — LLM counters reset at run start | `app/orchestrator.py` |
| 6 | **Retries from ScrapeLog** — `sum(attempts-1)` across the chain, not `page.attempt-1` | `app/orchestrator.py` |
| 7 | **Report from real run_metrics** — `--run-id` lookup; errors if the run doesn't exist | `scripts/generate_report.py` |
| 8 | **Price comparison** — canonical product identity (SHA-256 of noise-stripped name) + region + competitor; comparisons use `normalized_price_usd`, native price shown for display | `app/analysis/insights.py`, `product_identity.py` |
| 9 | **Real review extraction** — LLM structured reviews (text/rating/date/reviewer); regex kept only as labelled fallback | `app/llm/extractor.py` |
| 10 | **RAG Q&A with citations** — `POST /insights/reviews/query` + Streamlit ask-box | `app/api.py`, `dashboard/app.py` |
| — | `pytest` runs without `PYTHONPATH` (`pythonpath = .` in `pytest.ini`) | `pytest.ini` |

## 2. Architecture

```
Research Request (company + competitors + regions + products)
        │
        ▼
URL Discovery ── Firecrawl /map ──▶ sitemap.xml ──▶ canonical dedupe ──▶ focus_products filter
        │
        ▼
REGION FAN-OUT ── URL × (US | UK | IN | JP | …)   ← execution region decided by the pipeline
        │
        ▼
Geo-aware scraping (region-aware)          Firecrawl(location=region)
   retry + backoff + rate limit            Playwright(client proxy)
   proxy rotation                          Basic HTTP (client proxy)
        │                       PROXY_VERIFY=true ⇒ verified region-or-fail
        ▼
LLM Extraction ── expected_region enforced ── schema + confidence validation
        │            corrective retries (previous error fed back)
        ▼
Storage ── offers (run_id, canonical identity, native + USD-normalized price)
        │       reviews (structured: text/rating/date/reviewer)
        ▼
Intelligence ── product identity × region × competitor series
        │       undercut/moves/7-14-30d trends on normalized USD
        │       LLM sentiment · LLM positioning briefs · RAG Q&A
        ▼
Output + Ops ── PDF (exec summary + provenance) · dashboard · event alerts
               · scheduler · run_metrics (per-run, resume-grade numbers)
```

## 3. Quick start (zero API keys — demo mode)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python research.py                          # full demo: 1 target + 2 competitors × 5 regions
python scripts/generate_report.py           # most recent run; --run-id <id> for a specific one
pytest                                      # no PYTHONPATH needed — passes bare
uvicorn app.api:app --reload                # Swagger on :8000/docs  (try POST /insights/reviews/query)
pip install streamlit && streamlit run dashboard/app.py
```

> Example demo-mode output (NOT a production benchmark — synthetic data, no keys):
> ```
> Acme Audio  3 competitors · 5 markets · 3 products · 45 pages · scrape 100.0% · extraction 100.0% · 42.0s
> ```

## 4. Config (copy .env.example → .env)

| Variable | Purpose |
|---|---|
| `FIRECRAWL_API_KEY` | layer-1 scraping + site map for discovery |
| `PROXY_US=` `PROXY_IN=` … | geo-targeted proxy pools per region |
| `PROXY_VERIFY=true` | strict geo: exit-IP must match region, otherwise the scrape is refused (no wrong-geo fallback) |
| `GROQ_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | extraction + analysis models — fallback chain `groq → openai → anthropic` |
| `EXCHANGE_RATES_JSON` | FX rates for USD normalization (rate + timestamp stored) |
| `SCHEDULER_AUTOSTART=true` | start the in-process scheduler with the API |
| `SLACK_WEBHOOK_URL` / `SMTP_*` | event alerts |

## 5. Honest status of the live path

Wired and unit-tested with fakes, but **not exercised against the real
internet here** (the sandbox has no credentials): Firecrawl `/map`+scrape,
Playwright rendering, residential proxies, and the three real LLM providers.
Demo mode runs the whole system end-to-end deterministically. PostgreSQL /
pgvector remains the documented next adapter (SQLite is the tested engine).

## 6. Roadmap

- Day 1–2: schemas + scraper chain + discovery ✓
- Day 3–4: storage + upserts + FX + run provenance ✓
- Day 5–6: intelligence (identity grouping, normalized comparisons) + RAG Q&A ✓
- Day 7–8: run against 3 real competitors × 2+ regions with real proxies
- Day 9–10: PostgreSQL + pgvector, scheduled runs, API auth
- Day 11–14: resume bullets, demo video

## v4 engineering decisions (run-scoping + identity/geo modelling)

- **#3 Observation identity**: `offer_key = SHA256(canonical_url | canonical_product_id | region | competitor | seller | snapshot_hour)`. `run_id` is METADATA on the observation, NOT part of the key — the same observation within the same hour upserts to one row across runs (idempotent observation storage); a new snapshot hour creates a new row. Every run's rows are still recoverable via `WHERE run_id = ?`.
- **#6 Market vs GeoCountry**: `Region.EU` is treated as a MARKET in this build (demo scope). The production model is `Market (EU) + GeoCountry (DE/FR/IT/NL) + currency`; the schema split is roadmap. Firecrawl's `location.country` control is only sent for real countries (US/GB/IN/JP/CA/AU/SG/BR); EU is never sent as a country.
- **#7 Product identity**: tiered hierarchy (SKU → GTIN/UPC → brand+model → normalized name → fuzzy → LLM confirmation). This build implements T3 (normalized name) deterministically. Variant markers (ANC, Gen 2/3, v2, Wireless) are PRESERVED, so materially different products do not collapse; names with <2 significant tokens never collapse.
- **#17 Deployment**: `DATABASE_URL=sqlite:///data/competitor.db` + free Render web service = DEMO ONLY. Production path (documented, not shipped): FastAPI + PostgreSQL + pgvector, persistent storage for reports/checkpoints/reviews/raw evidence.
- **#26 Raw evidence**: each scraped page's markdown + SHA-256 content hash + source_url + scrape time + run_id is stored in `raw_evidence` (60 KB/page cap) for debugging bad extractions and re-extraction.
