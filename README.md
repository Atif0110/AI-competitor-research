# AI Competitor Research

**Evidence-backed competitive intelligence across products, prices, regions, and customer reviews.**

AI Competitor Research turns a target company + competitor set into a repeatable research run:

**Discover → region fan-out → geo-aware scrape → structured extraction → validation → PostgreSQL/SQLite → competitive insights → cited RAG → PDF/reporting.**

It is designed as a real engineering system rather than a prompt demo. The pipeline includes idempotent observation keys, corrective extraction retries, explicit region enforcement, scraper fallbacks, raw evidence retention, provider circuit breaking, run-scoped SQL analysis, scheduled runs, and a production PostgreSQL + pgvector path.

## What makes this project interesting

- **Multi-provider LLM resilience:** Claude and GPT can be selected automatically from the keys available. The non-primary provider becomes a fallback, with Groq available as an additional trailing fallback.
- **Region-aware research:** every discovered URL is fanned out across the requested execution regions. Region attribution is enforced during extraction.
- **Evidence first:** each scraped page is retained with a SHA-256 content hash, source URL, timestamp, and run ID.
- **Idempotent observations:** `offer_key` is derived from canonical URL, canonical product, region, competitor, seller, and snapshot hour. `run_id` is metadata, so reruns do not create duplicate observations.
- **Corrective structured extraction:** schema/semantic failures feed the previous error into the next extraction attempt.
- **Run isolation:** insight queries are scoped at the SQL layer with `run_id`, preventing cross-run leakage.
- **Production storage:** PostgreSQL is supported for relational data. PostgreSQL review storage uses pgvector when OpenAI embeddings are available and falls back deterministically when they are not.
- **Real frontend:** React/Vite frontend communicates with FastAPI over HTTP. The UI never imports the Python pipeline directly.
- **Security:** protected write/LLM-expensive endpoints use a constant-time `X-API-Key` check and can be rate-limited at the deployment layer.
- **Operational proof:** CI runs tests and the deterministic end-to-end demo on every push/PR.

## Architecture

```text
                         React Frontend
                              │ HTTPS
                              ▼
                         FastAPI API
                    API key + CORS boundary
                              │
                         Orchestrator
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
      Discovery           Scraping            Scheduler
          │                   │
          │          ┌────────┼────────┐
          │          ▼        ▼        ▼
          │      Firecrawl Playwright HTTP
          │
          ▼
      Region Fan-out
          │
          ▼
    Structured Extraction
          │
      ┌───┼──────────────┐
      ▼   ▼              ▼
    Claude GPT           Groq
      │   │              │
      └───┼──────────────┘
          ▼
   Schema + semantic validation
          │
          ▼
  PostgreSQL + pgvector / SQLite
          │
     ┌────┼─────────────┐
     ▼    ▼             ▼
  Insights  RAG       Evidence
     │      │             │
     └──────┼─────────────┘
            ▼
       Reports + alerts

       CI + evaluation benchmark
```

## Repository layout

```text
app/
  analysis/          price, identity, sentiment and competitive insights
  llm/               provider routing + structured extraction
  scrapers/          Firecrawl / Playwright / HTTP / demo chain
  storage/           relational offers + review vector storage
  output/            PDF reports and alerts
  api.py             FastAPI application boundary
  orchestrator.py    end-to-end research pipeline
frontend/             React/Vite product UI
eval/                 hand-labelled extraction benchmark scaffold
scripts/              demo, live smoke, benchmark, report and ops utilities
tests/                unit + failure-mode + production-readiness tests
deploy/               deployment notes
.github/workflows/    CI
```

## Quick start: deterministic demo

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python research.py
python scripts/generate_report.py
pytest -q

uvicorn app.api:app --reload
```

Open the API at `http://localhost:8000/docs`.

For the React frontend:

```bash
cd frontend
npm install
npm run dev
```

The frontend defaults to `http://localhost:8000`. Set `VITE_API_URL` if the API lives elsewhere.

## Production local stack

Copy `.env.example` to `.env`, set a strong `API_KEY`, and provide the provider/scraper credentials you want to use.

Then:

```bash
docker compose up -d --build
```

This starts:

- PostgreSQL 16 + pgvector on `5432`
- FastAPI on `8000`
- React frontend on `5173`

The included Compose stack makes PostgreSQL the application database. SQLite remains available for the offline/demo path.

## Provider routing

The provider router intentionally makes the available API keys determine the default chain:

| Configuration | Chain |
|---|---|
| Claude only | Claude |
| GPT only | GPT |
| GPT + Claude | selected primary → other provider |
| GPT/Claude + Groq | selected primary → other provider → Groq |
| `LLM_PROVIDER=groq` | Groq only |
| `LLM_PROVIDER=demo` | deterministic demo provider |
| No keys | deterministic demo provider |

When both GPT and Claude keys exist, set `LLM_PROVIDER=openai` or `LLM_PROVIDER=anthropic` to choose the primary. `/health` exposes the active provider and resolved chain.

## API security

Protected endpoints require `X-API-Key` when `API_KEY_REQUIRED=true` and `API_KEY` is configured:

- `POST /research`
- `POST /research/jobs`
- `GET /research/jobs/{job_id}`
- `POST /insights/reviews/query`
- `POST /schedules`
- `DELETE /schedules/{id}`

Generate a strong key with:

```bash
python scripts/generate_api_key.py
```

Demo mode stays usable without a key so the project can be evaluated offline. Live deployments should set `API_KEY_REQUIRED=true` and a long random `API_KEY`.

## Live smoke test: human step required

Real internet execution is intentionally not faked. You provide the credentials and target companies, then the repository produces auditable artifacts.

1. Copy `config/live_smoke.example.json` to `config/live_smoke.json`.
2. Put one target and two real competitors in it.
3. Configure `FIRECRAWL_API_KEY` and at least one real LLM key.
4. Start with `regions: ["US"]` and no proxy. Expand to UK/IN after the first successful run.
5. Run:

```bash
python scripts/run_live_smoke.py --config config/live_smoke.json
```

Artifacts are written to `data/live_runs/<run_id>/`:

- `manifest.json` with run metrics and evidence metadata
- `result.json` with the full pipeline result
- the same run ID can be used to generate a PDF report

```bash
python scripts/generate_report.py --run-id <run_id>
```

**Do not put API keys in the repository.**

## Extraction evaluation benchmark

The benchmark is intentionally based on human-labelled real pages, not synthetic self-reported accuracy.

1. Copy `eval/ground_truth.template.json` to `eval/ground_truth.json`.
2. Capture 15–20 real product pages into `eval/pages/`.
3. Label `product_name`, `price`, `currency`, `availability`, `region`, and `seller`.
4. Run:

```bash
python scripts/evaluate_extraction.py \
  --dataset eval/ground_truth.json \
  --out eval/artifacts/benchmark.json
```

The output reports field-level accuracy and macro accuracy separately for Anthropic, OpenAI, and Groq when their keys are configured. Missing providers are explicitly marked as skipped.

The benchmark is the right place to publish real accuracy numbers in the README after the human labelling step is complete. **This repository never fabricates benchmark results.**

## Frontend screens

The React console is deliberately small and data-dense:

1. **Overview** — KPIs, recent runs, competitive alerts
2. **New research** — target/competitor/region configuration + live run console
3. **Offers** — filterable offer explorer with source links and extraction confidence
4. **Insights** — undercuts, price moves, and regional snapshots
5. **Ask** — RAG Q&A with source citations
6. **Reports** — generated PDF reports

The UI talks to FastAPI only. It does not import `app.orchestrator` or `app.storage`.

## CI

GitHub Actions runs:

- Python dependency installation
- `pytest -q`
- deterministic demo pipeline
- frontend dependency installation
- frontend production build

The live smoke and human-labelled benchmark intentionally remain separate because they require external credentials/data and should not run on every PR.

## Important limitations

These are real engineering boundaries, not hidden claims:

- Real scraping can fail because sites change markup, block automated clients, require authentication, or vary content by geography.
- Firecrawl's API-side location control is distinct from client-side proxy routing.
- `Region.EU` is treated as a market in the current schema. A future production model can separate market from individual geo-country.
- Product identity currently implements deterministic normalized-name identity. SKU/GTIN/fuzzy/LLM confirmation remain extension points.
- The in-process research job registry is designed for a single API process. A multi-instance deployment should move job state to durable infrastructure.
- PostgreSQL is production-ready for the relational path, while reports/checkpoints/files still use the local filesystem. A horizontally scaled deployment should move those artifacts to durable object storage.
- Live extraction accuracy must be measured using `eval/ground_truth.json` before any benchmark number is presented as a project result.

## 9.5 readiness checklist

The project should not be called a 9.5 portfolio system until the following human-verifiable evidence exists:

- [x] Core pipeline, validation, fallback chain, run scoping and evidence storage
- [x] GPT/Claude automatic provider selection
- [x] FastAPI application boundary
- [x] React frontend using HTTP API only
- [x] API key protection for expensive/mutating operations
- [x] PostgreSQL relational backend
- [x] PostgreSQL + pgvector review storage path
- [x] GitHub Actions CI
- [x] Live-run artifact generator
- [x] Independent extraction benchmark harness
- [ ] One successful real-internet run with saved metrics/evidence
- [ ] Provider switching verified with real GPT and Claude keys
- [ ] 15–20 page hand-labelled benchmark completed and published
- [ ] Real screenshots captured from the frontend
- [ ] 60–90 second demo video recorded
- [ ] README updated with real dated run metrics and benchmark results
- [ ] Deployment of the frontend + API using production secrets

The unchecked items are intentionally human steps. They require real credentials, real websites, and human-labelled ground truth and therefore should never be fabricated by the codebase.
