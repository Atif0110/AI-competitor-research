Four startup demos, all zero-credential:

1. **`python scripts/run_demo.py`** — full pipeline against demo competitors
2. **`python scripts/generate_report.py`** — PDF report (open in any reader)
3. **`pytest`** — 12+ tests incl. end-to-end demo run
4. **`uvicorn app.api:app --reload`** — FastAPI + Swagger at /docs

Then swap in real Firecrawl + LLM keys in `.env` and point `_urls_for()`
(app/orchestrator.py) at your first real competitor catalog page.
