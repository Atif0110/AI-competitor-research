AI Competitor Research

Evidence-backed competitive intelligence across products, prices, regions, and customer reviews.

AI Competitor Research turns a target company + competitor set into a repeatable research run:

Discover → region fan-out → geo-aware scrape → structured extraction → validation → PostgreSQL/SQLite → competitive insights → cited RAG → PDF/reporting.

It is designed as an engineering system rather than a prompt demo. The pipeline includes idempotent observation keys, corrective extraction retries, explicit region enforcement, scraper fallbacks, raw evidence retention, provider circuit breaking, run-scoped SQL analysis, scheduled runs, and a production PostgreSQL + pgvector path.

What Makes This Project Interesting
Multi-provider LLM resilience: Supports Google/Gemini, OpenAI/GPT, Anthropic/Claude, and Groq through a configurable provider router with fallback behavior.
Region-aware research: Discovered URLs are evaluated across requested execution regions, with region attribution enforced during extraction.
Evidence first: Scraped pages are retained with a SHA-256 content hash, source URL, timestamp, and run ID.
Idempotent observations: offer_key is derived from canonical URL, canonical product, region, competitor, seller, and snapshot hour. run_id is metadata, so reruns do not create duplicate observations.
Corrective structured extraction: Schema and semantic failures feed the previous error into the next extraction attempt.
Run isolation: Insight queries are scoped at the SQL layer with run_id, preventing cross-run leakage.
Production storage: PostgreSQL is supported for relational data. PostgreSQL review storage can use pgvector when embeddings are configured and falls back deterministically when they are not.
Real frontend: React/Vite communicates with FastAPI over HTTP. The frontend never imports the Python pipeline directly.
Security: Protected write/LLM-expensive endpoints support an X-API-Key check and can be rate-limited at the deployment layer.
Operational proof: CI runs automated tests and the deterministic end-to-end demo on every push/PR.
Architecture
text
                         React Frontend
                               │
                              HTTPS
                               │
                               ▼
                         FastAPI API
                     API key + CORS boundary
                               │
                               ▼
                          Orchestrator
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
         Discovery          Scraping         Scheduler
              │                │
              │       ┌────────┼────────┐
              │       ▼        ▼        ▼
              │   Firecrawl  Playwright  HTTP
              │
              ▼
        Region Fan-out
              │
              ▼
      Structured Extraction
              │
        ┌───────┬───────┬───────┐
        ▼       ▼       ▼       ▼
      Claude   GPT    Gemini   Groq
        │       │       │       │
        └───────┼───────┼───────┘
              ▼
     Schema + semantic validation
              │
              ▼
     PostgreSQL + pgvector / SQLite
              │
       ┌──────┼─────────────┐
       ▼      ▼             ▼
    Insights  RAG        Evidence
       │      │             │
       └──────┼─────────────┘
              ▼
        Reports + alerts

        CI + evaluation benchmark
Repository Layout
text
app/
├── analysis/            Price, identity, sentiment and competitive insights
├── llm/                 Provider routing + structured extraction
├── scrapers/            Firecrawl / Playwright / HTTP / demo chain
├── storage/             Relational offers + review vector storage
├── output/              PDF reports and alerts
├── api.py               FastAPI application entry point
├── config.py            Application configuration
├── discovery.py         Target/competitor discovery
├── orchestrator.py      End-to-end research pipeline
├── scheduler.py         Scheduled research execution
└── schemas.py           API/data schemas

frontend/                React/Vite product UI
eval/                    Human-labelled extraction benchmark scaffold
scripts/                 Demo, live smoke, benchmark, report and ops utilities
tests/                   Unit + failure-mode + production-readiness tests
deploy/                  Deployment notes
.github/workflows/       GitHub Actions CI
Quick Start
1. Create a virtual environment
Windows PowerShell
powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1

If PowerShell blocks activation, you can run the project using the virtual-environment Python directly:

powershell
.\.venv\Scripts\python.exe -m pytest -q
macOS / Linux
bash
python3 -m venv .venv
source .venv/bin/activate
2. Install Python dependencies
bash
pip install -r requirements.txt
3. Run the automated tests
bash
python -m pytest -q

The test suite covers core pipeline behavior, storage, extraction, API behavior, RAG retrieval, failure modes, and production-readiness checks.

Run the API

The FastAPI application entry point is:

text
app.api:app

Start it with:

bash
python -m uvicorn app.api:app --reload

Or:

bash
uvicorn app.api:app --reload

The API will be available at:

text
http://127.0.0.1:8000

Swagger/OpenAPI documentation:

text
http://127.0.0.1:8000/docs

OpenAPI JSON:

text
http://127.0.0.1:8000/openapi.json

Health endpoint:

text
http://127.0.0.1:8000/health

Important: The project uses app.api:app. There is no app.main module.

Run the React Frontend

Open a second terminal.

bash
cd frontend
npm install
npm run dev

The frontend normally runs at:

text
http://localhost:5173

The frontend communicates with the FastAPI backend through HTTP.

By default, the frontend expects:

text
http://localhost:8000

If the API is hosted elsewhere, configure:

text
VITE_API_URL

For example:

bash
VITE_API_URL=http://localhost:8000
Deterministic Demo

The repository includes an offline/demo path that does not require live scraping or paid LLM credentials.

From the project root:

bash
python research.py

To generate a report from the resulting run:

bash
python scripts/generate_report.py

This path is intended for local evaluation, development, CI, and demonstrations without external credentials.

Environment Configuration

Copy the example environment file:

Windows PowerShell
powershell
Copy-Item .env.example .env
macOS / Linux
bash
cp .env.example .env

Then configure the credentials and settings required for the execution mode you want.

Typical integrations include:

text
GEMINI_API_KEY
OPENAI_API_KEY
ANTHROPIC_API_KEY
GROQ_API_KEY
FIRECRAWL_API_KEY
API_KEY
API_KEY_REQUIRED
DATABASE_URL

The exact supported variables are documented in .env.example.

Never commit .env or real API keys to Git.

LLM Provider Routing

The provider router supports:

Google/Gemini
OpenAI/GPT
Anthropic/Claude
Groq
Deterministic demo mode

Provider selection can be controlled through:

text
LLM_PROVIDER

Supported modes include:

Configuration	Behavior
gemini	Gemini first
groq	Groq first
openai	OpenAI first
anthropic	Anthropic first
demo	Deterministic offline provider
auto	Select from configured providers

When fallback providers are configured, the router can move to another available provider when the primary provider is unavailable.

Model names and provider-compatible base URLs are environment-configurable.

The /health endpoint exposes the active provider, resolved chain, and non-secret model information.

API key security

API keys must be stored in .env locally or in deployment secrets.

Never:

hard-code API keys into Python files
commit .env
place secrets in frontend source code
publish secrets in screenshots or documentation
API Security

Protected endpoints require X-API-Key when:

text
API_KEY_REQUIRED=true

and an API key is configured.

Protected operations include:

text
POST /research
POST /research/jobs
GET  /research/jobs/{job_id}
POST /insights/reviews/query
POST /schedules
DELETE /schedules/{id}

Generate a strong API key with:

bash
python scripts/generate_api_key.py

For local demo/evaluation, the application can remain usable without API-key protection.

For live deployments, enable API-key protection and use a long random secret.

Production Local Stack

For a local PostgreSQL + pgvector environment, configure .env and run:

bash
docker compose up -d --build

The Compose stack provides:

PostgreSQL 16 + pgvector on port 5432
FastAPI on port 8000
React frontend on port 5173

The Compose configuration makes PostgreSQL the application database.

SQLite remains available for the offline/demo path.

Live Smoke Test

Real internet execution is intentionally not simulated.

A live smoke test requires external credentials and real target companies.

1. Create the configuration

Copy:

text
config/live_smoke.example.json

to:

text
config/live_smoke.json
2. Configure targets

Add:

one target company
two real competitors

Start with:

json
{
  "regions": ["US"]
}

and no proxy.

After a successful US run, additional regions can be tested.

3. Configure credentials

Provide:

text
FIRECRAWL_API_KEY

and at least one real LLM provider key.

4. Run the smoke test
bash
python scripts/run_live_smoke.py --config config/live_smoke.json

Artifacts are written to:

text
data/live_runs/<run_id>/

including:

text
manifest.json
result.json

The manifest contains run metrics and evidence metadata.

5. Generate a report

Use the resulting run ID:

bash
python scripts/generate_report.py --run-id <run_id>

Do not put API keys in the repository.

Extraction Evaluation Benchmark

The benchmark is intentionally based on human-labelled real pages rather than synthetic or self-reported accuracy.

1. Create the ground-truth file

Copy:

text
eval/ground_truth.template.json

to:

text
eval/ground_truth.json
2. Capture real product pages

Add approximately 15–20 real product pages to:

text
eval/pages/
3. Label the fields

The benchmark evaluates:

product_name
price
currency
availability
region
seller
4. Run the benchmark
bash
python scripts/evaluate_extraction.py \
  --dataset eval/ground_truth.json \
  --out eval/artifacts/benchmark.json

The output reports field-level and macro accuracy for configured providers.

Providers that are not configured are explicitly skipped.

Do not publish benchmark numbers until the human-labelled benchmark has actually been completed.

Frontend UX

The frontend is designed as an intelligence workspace rather than a generic administration panel.

The main areas are:

Overview

Latest signals, KPI summaries, run history, live events, and pipeline health.

Research

Guided research configuration with region selection and run execution.

Evidence

Searchable and filterable observations with source/evidence details.

Signals

Competitive undercuts, price movements, regional snapshots, and cross-run changes.

Ask AI

Review-based RAG with source citations and scoped retrieval.

Reports

Generated PDF report library.

The UI communicates with FastAPI through HTTP and does not directly import or execute the Python pipeline.

RAG and Evidence

The review intelligence layer uses scoped retrieval to answer questions about customer feedback.

The system is designed to avoid presenting arbitrary reviews as evidence.

If retrieved reviews do not contain sufficient relevance to the question, the system returns an honest no-relevant-evidence response instead of inventing an answer.

Sources returned by the RAG endpoint include available evidence metadata such as:

product
region
source URL
rating
run ID

This makes customer-insight answers traceable to stored review evidence.

Cross-Run Intelligence
text
GET /insights/changes

compares recent stored research runs for the same target.

It can identify meaningful changes such as:

price movements
newly observed products
competitive observations that changed between runs

This makes repeated scheduled research useful rather than treating every run as an isolated snapshot.

CI

GitHub Actions runs:

Python dependency installation
pytest -q
production-readiness checks
frontend dependency installation
frontend production build

Live smoke tests and human-labelled extraction benchmarks remain separate because they require external credentials, real websites, and manually labelled data.

Important Limitations

These are real engineering boundaries and should not be hidden:

Real scraping can fail because websites change markup, block automated clients, require authentication, or vary content by geography.
Firecrawl API-side location control is distinct from client-side proxy routing.
Region.EU is currently treated as a market in the schema. A future production model could separate market from individual geo-country.
Product identity currently uses deterministic normalized-name identity. SKU/GTIN/fuzzy/LLM confirmation remain possible extension points.
The in-process research job registry is designed for a single API process. A multi-instance deployment should move job state to durable infrastructure.
PostgreSQL provides the production relational path, while reports, checkpoints, and files currently use the local filesystem. A horizontally scaled deployment should move these artifacts to durable object storage.
Live extraction accuracy must be measured using eval/ground_truth.json before benchmark numbers are presented as project results.
Current Verification Status

The repository includes automated tests for the core application.

The local test suite should be run with:

bash
python -m pytest -q

A clean test run is required before pushing changes.

The following production-evidence items require real-world verification and should not be claimed unless completed:

successful real-internet research run
real provider switching
human-labelled extraction benchmark
production deployment
frontend screenshots
demonstration video
dated live metrics
Portfolio Readiness Checklist
Engineering
 Core research pipeline
 Structured extraction
 Validation and corrective retries
 Scraper fallback chain
 Region-aware execution
 Evidence retention
 Run scoping
 Idempotent observations
 FastAPI application boundary
 React frontend using HTTP API
 RAG review retrieval
 Cross-run change detection
 PostgreSQL relational backend
 PostgreSQL + pgvector review path
 API-key protection
 CI
 Deterministic offline/demo path
Real-world verification
 Successful real-internet run with saved metrics/evidence
 Provider switching verified with real Google/Gemini credentials
 Provider switching verified with real OpenAI/GPT credentials
 Provider switching verified with real Anthropic/Claude credentials
 Groq execution verified with a real Groq API key
 15–20 page human-labelled extraction benchmark completed
 Real frontend screenshots captured
 60–90 second demo video recorded
 README updated with dated live-run metrics
 README updated with real benchmark results
 Frontend + API deployed using production secrets

Unchecked items are intentionally human-verifiable tasks. They require real credentials, real websites, or human-labelled ground truth and therefore must not be fabricated by the codebase.

Recommended Verification Sequence

For a fresh clone, use this order:

bash
# 1. Create environment
python -m venv .venv

# 2. Activate environment
# Windows:
.\.venv\Scripts\Activate.ps1

# macOS/Linux:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run tests
python -m pytest -q

# 5. Start API
python -m uvicorn app.api:app --reload

# 6. Open API documentation
# http://127.0.0.1:8000/docs

# 7. Start frontend in a second terminal
cd frontend
npm install
npm run dev

# 8. Open frontend
# http://localhost:5173

For production-style testing, configure .env, PostgreSQL/pgvector, real provider credentials, and the live smoke-test configuration separately.

License

See the repository's license file for the applicable project license.
