# AI Competitor Research

**Evidence-backed competitive intelligence across products, prices, regions, and customer reviews.**

AI Competitor Research turns a target company and competitor set into a repeatable research workflow:

**Discover → Region Fan-out → Geo-aware Scrape → Structured Extraction → Validation → Storage → Competitive Insights → Evidence-backed RAG → Reports**

The project is designed as an engineering system rather than a simple prompt-based demo. It combines automated discovery, web scraping, structured LLM extraction, validation, evidence retention, persistent storage, cross-run analysis, review intelligence, reporting, and a React/FastAPI application boundary.

---

# What the Application Does

The application is built to answer questions such as:

- What products or commercial offerings does a company currently expose?
- What prices and currencies are shown?
- Which competitors offer similar products?
- How do prices differ across regions?
- Which products are newly observed or have changed between research runs?
- What customer-review themes are visible in the stored evidence?
- What evidence supports a particular competitive insight?
- Can the research results be exported into a report?

A typical research run follows this flow:

```text
Target + Competitors
        │
        ▼
   URL Discovery
        │
        ▼
   Region Fan-out
        │
        ▼
    Web Scraping
        │
        ├── Firecrawl
        ├── Playwright
        └── HTTP fallback
        │
        ▼
 Structured LLM Extraction
        │
        ├── Gemini
        ├── Groq
        ├── OpenAI
        ├── Anthropic
        └── Demo provider
        │
        ▼
Schema + Semantic Validation
        │
        ▼
Evidence + Observation Storage
        │
        ├── SQLite
        └── PostgreSQL / pgvector
        │
        ├── Competitive Insights
        ├── Cross-run Changes
        ├── Review RAG
        └── Reports / Alerts
```

## Key Capabilities

### Multi-provider LLM routing

The application supports multiple LLM providers through a centralized provider router:

- Google Gemini
- OpenAI / GPT
- Anthropic / Claude
- Groq
- Deterministic demo provider

Provider selection is controlled through `LLM_PROVIDER`.

Supported modes include:

| Configuration | Behavior |
|---|---|
| `gemini` | Gemini first |
| `groq` | Groq first |
| `openai` | OpenAI first |
| `anthropic` | Anthropic first |
| `demo` | Deterministic offline provider |
| `auto` | Select from configured providers |

When fallback providers are configured, the router can move to another configured provider when the current provider is unavailable.

Provider fallback includes cooldown-based failover so a temporarily unavailable provider does not have to be selected repeatedly.

Model names and provider-compatible base URLs are configurable through environment variables.

The `/health` endpoint exposes:

- active provider
- resolved provider chain
- configured model names
- application mode

It does not expose API secrets.

### Region-aware Competitive Research

Research runs can be executed for one or more requested regions.

The system:

- Discovers candidate URLs.
- Associates research with the requested region.
- Scrapes the discovered pages.
- Passes regional context into structured extraction.
- Validates the extracted region before accepting the observation.

This allows the same competitor research workflow to be executed across different markets.

**Current limitation**

`Region.EU` is currently represented as a market-level region in the schema. A future production model could distinguish individual countries from broader markets.

### Evidence-first Research

The system retains scraped evidence rather than keeping only the final LLM answer.

Stored evidence can include:

- source URL
- research run ID
- timestamp
- region
- scraped content
- SHA-256 content hash

The extracted observation remains connected to its underlying source evidence.

This makes competitive findings traceable back to the pages used during research.

### Structured Product Extraction

The extraction pipeline converts scraped web content into validated structured observations.

The extraction process evaluates fields such as:

- product name
- price
- currency
- availability
- region
- seller
- competitor
- source URL
- evidence
- confidence

The system validates both the schema and semantic requirements before accepting an observation.

Informational pages, documentation, support pages, careers pages, blogs, legal pages, and other non-commercial content can be rejected instead of being incorrectly treated as commercial product offers.

### Corrective Extraction

Structured extraction failures can be fed back into a subsequent extraction attempt.

The previous extraction error can be included in the next prompt so the model has additional context about what failed.

Provider quota/rate-limit and provider-unavailable errors are handled separately so the system does not unnecessarily repeat requests when an external provider is already refusing requests.

### Idempotent Observations

Observation identity is designed to prevent duplicate records when the same research is repeated.

The observation key is derived from normalized identifying attributes such as:

- canonical URL
- canonical product
- region
- competitor
- seller
- snapshot hour

The `run_id` is retained as metadata rather than being used as the identity of the observation.

This means rerunning research does not automatically create duplicate observations for the same underlying observation.

### Competitive Insights

Stored observations can be analyzed to identify competitive signals such as:

- price movements
- competitive undercuts
- newly observed products
- regional differences
- changes between research runs

Cross-run analysis is scoped by research run and target so results from unrelated research runs are not mixed together.

The API exposes cross-run change information through:

```text
GET /insights/changes
```

### Review Intelligence and RAG

The application includes a review-intelligence layer for customer feedback.

Stored reviews can be retrieved using scoped search and used as evidence for review-based questions.

The system is designed to avoid presenting unrelated reviews as evidence.

If the retrieval layer does not find sufficiently relevant evidence, the application can return a no-relevant-evidence response instead of fabricating an answer.

RAG sources can include metadata such as:

- product
- region
- source URL
- rating
- research run ID

This makes review-based answers traceable to stored evidence.

### Storage

The application supports multiple storage configurations.

**SQLite**

SQLite provides the lightweight local/development storage path.

It is suitable for:

- local development
- deterministic demonstrations
- lightweight deployments
- testing

**PostgreSQL**

PostgreSQL is supported as the relational production storage path.

The application can store relational competitive observations and related research data in PostgreSQL.

**pgvector**

The PostgreSQL review-storage path can use pgvector when vector embeddings are configured.

When embeddings are unavailable, the application has deterministic fallback behavior rather than silently producing unsupported results.

### Scraping Architecture

The application supports multiple scraping mechanisms:

```text
Firecrawl
    ↓
Playwright
    ↓
HTTP
    ↓
Demo / deterministic path
```

This provides multiple ways to obtain page content depending on the target website and execution environment.

**Important real-world limitation**

Live scraping depends on external websites and scraping providers.

A real website may:

- change its markup
- block automated clients
- require authentication
- return different content by geography
- impose rate limits
- become temporarily unavailable

Firecrawl's API rate limits are also external to the application.

Therefore, successful scraping of every discovered URL cannot be guaranteed.

### Discovery

The discovery system identifies candidate URLs for competitive research.

It can use:

- homepage discovery
- commercial/intelligence path detection
- sitemap traversal
- Firecrawl map results
- candidate scoring and ranking
- duplicate/canonical URL filtering

The discovery stage intentionally prioritizes pages that are more likely to contain commercial intelligence.

Non-public and low-value paths can be filtered before extraction.

The final candidate set is bounded to prevent an unrestricted discovery result from producing an unbounded number of downstream scraping and LLM requests.

### Frontend

The project includes a React/Vite frontend.

The frontend communicates with the FastAPI backend through HTTP and does not directly import or execute the Python research pipeline.

The UI is structured as an intelligence workspace rather than a generic administration panel.

**Overview**

Provides visibility into:

- latest signals
- KPI summaries
- research runs
- live events
- pipeline health

**Research**

Provides guided configuration for:

- target company
- target website
- competitors
- region
- research focus
- research execution

**Evidence**

Provides searchable and filterable structured observations with source/evidence information.

**Signals**

Displays competitive signals such as:

- price movements
- competitive undercuts
- regional snapshots
- cross-run changes

**Ask AI**

Provides review-based RAG interaction using stored review evidence.

**Reports**

Provides access to generated PDF reports.

### Reports and Alerts

The project includes report generation and alerting components.

Research results can be converted into PDF reports.

The output layer also contains alerting functionality for supported notification channels.

Reports and other generated artifacts currently use the local filesystem.

**Production limitation**

For a horizontally scaled deployment, generated reports, checkpoints, and other files should be moved to durable object storage.

### Scheduled Research

The application contains scheduling support for recurring research execution.

The scheduler can be configured through the application/API rather than requiring the research logic to be manually executed every time.

**Production limitation**

The current scheduler and research job registry are designed around a single API process.

A horizontally scaled production deployment should move job state and scheduling responsibilities to durable/distributed infrastructure.

---

# API

The backend is implemented using FastAPI.

Application entry point:

```text
app.api:app
```

Run locally with:

```bash
python -m uvicorn app.api:app --reload
```

The local API is normally available at:

```text
http://127.0.0.1:8000
```

Swagger/OpenAPI:

```text
http://127.0.0.1:8000/docs
```

OpenAPI JSON:

```text
http://127.0.0.1:8000/openapi.json
```

Health endpoint:

```text
http://127.0.0.1:8000/health
```

> There is no `app.main` module. The application entry point is `app.api:app`.

## API Security

Protected write and LLM-expensive endpoints can use an `X-API-Key` check.

API access requirements can be controlled through environment configuration.

The frontend does not contain provider secrets.

API keys belong in:

- `.env` during local development
- deployment environment/secrets in production

**Never:**

- hard-code API keys into Python files
- commit `.env`
- place provider secrets in frontend source code
- publish secrets in screenshots
- publish secrets in documentation

---

# Environment Configuration

Copy the example environment file:

### Windows PowerShell

```powershell
Copy-Item .env.example .env
```

### macOS / Linux

```bash
cp .env.example .env
```

Typical integrations include:

```text
GEMINI_API_KEY
OPENAI_API_KEY
ANTHROPIC_API_KEY
GROQ_API_KEY
FIRECRAWL_API_KEY
API_KEY
API_KEY_REQUIRED
DATABASE_URL
```

The complete list of supported variables is documented in:

```text
.env.example
```

The application reads provider credentials from environment configuration rather than hard-coding them into the application source.

---

# Demo Mode

The repository includes a deterministic offline/demo path.

The demo path does not require:

- live scraping
- external LLM credentials
- production provider access

Run the deterministic research path with:

```bash
python research.py
```

Generate a report from the resulting run with:

```bash
python scripts/generate_report.py
```

The demo path is intended for:

- development
- CI
- local evaluation
- demonstrations
- environments without external credentials

Demo mode is separate from live provider execution.

---

# Live Smoke Test

The repository includes a live smoke-test workflow for real websites and external providers.

## 1. Create the configuration

Copy:

```text
config/live_smoke.example.json
```

to:

```text
config/live_smoke.json
```

## 2. Configure targets

Add:

- one target company
- two real competitors

Start with:

```json
{
  "regions": ["US"]
}
```

and no proxy.

Additional regions can be tested after the initial run.

## 3. Configure credentials

Provide:

```text
FIRECRAWL_API_KEY
```

and at least one real LLM provider key.

## 4. Run the smoke test

```bash
python scripts/run_live_smoke.py --config config/live_smoke.json
```

Artifacts are written under:

```text
data/live_runs/<run_id>/
```

including:

```text
manifest.json
result.json
```

The manifest contains run metrics and evidence metadata.

## 5. Generate a report

```bash
python scripts/generate_report.py --run-id <run_id>
```

**Do not place API keys in the repository.**

---

# Extraction Evaluation Benchmark

The repository includes infrastructure for evaluating extraction quality against human-labelled data.

The benchmark is intentionally based on real pages and manually labelled ground truth rather than synthetic or self-reported accuracy.

## Fields evaluated

The benchmark can evaluate:

- `product_name`
- `price`
- `currency`
- `availability`
- `region`
- `seller`

## Create the ground truth

Copy:

```text
eval/ground_truth.template.json
```

to:

```text
eval/ground_truth.json
```

Add approximately 15–20 real product pages under:

```text
eval/pages/
```

Label the expected fields.

## Run the benchmark

```bash
python scripts/evaluate_extraction.py \
  --dataset eval/ground_truth.json \
  --out eval/artifacts/benchmark.json
```

The output reports field-level and macro accuracy for configured providers.

Providers that are not configured are explicitly skipped.

**Important**

No benchmark percentage should be presented as a project result until the human-labelled benchmark has actually been completed.

---

# CI and Automated Testing

GitHub Actions runs the automated project checks, including:

- Python dependency installation
- Python tests
- production-readiness checks
- frontend dependency installation
- frontend production build

Run the local test suite with:

```bash
python -m pytest -q
```

The test suite covers areas including:

- core pipeline behavior
- storage
- structured extraction
- API behavior
- RAG retrieval
- failure modes
- production-readiness behavior

Live smoke tests and human-labelled extraction benchmarks remain separate because they require:

- external credentials
- real websites
- manually labelled ground truth

---

# Repository Structure

```text
app/
├── analysis/            Price, identity, sentiment and competitive insights
├── llm/                 Provider routing and structured extraction
├── scrapers/            Firecrawl / Playwright / HTTP / demo chain
├── storage/             Relational offers and review/vector storage
├── output/              PDF reports and alerts
├── api.py               FastAPI application entry point
├── config.py            Application configuration
├── discovery.py         Target/competitor discovery
├── orchestrator.py      End-to-end research pipeline
├── scheduler.py         Scheduled research execution
└── schemas.py           API/data schemas

frontend/                React/Vite product UI
eval/                    Human-labelled extraction benchmark scaffold
scripts/                 Demo, live smoke, benchmark, report and operations utilities
tests/                   Unit, failure-mode and production-readiness tests
deploy/                  Deployment notes
.github/workflows/       GitHub Actions CI
```

---

# Architecture

```text
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
             │        ┌───────┼────────┐
             │        ▼       ▼        ▼
             │    Firecrawl Playwright HTTP
             │
             ▼
        Region Fan-out
             │
             ▼
    Structured Extraction
             │
      ┌──────┼───────────────┐
      ▼      ▼       ▼       ▼
    Gemini  Groq   OpenAI  Anthropic
      │      │       │       │
      └──────┼───────┼───────┘
             ▼
    Schema + Semantic Validation
             │
             ▼
       PostgreSQL / SQLite
             │
       ┌─────┼─────────────┐
       ▼     ▼             ▼
    Insights RAG        Evidence
       │     │             │
       └─────┼─────────────┘
             ▼
       Reports + Alerts

             │
             ▼
        CI + Evaluation
```

---

# Important Engineering Limitations

These are real system boundaries and are intentionally documented.

**Live websites**

Real scraping can fail because websites can:

- change markup
- block automated clients
- require authentication
- vary content by geography
- impose provider or API rate limits

**Firecrawl**

Firecrawl API-side location control is distinct from client-side proxy routing.

Firecrawl availability and rate limits are external dependencies.

**Region model**

`Region.EU` is currently treated as a market-level region in the schema.

A future production model could separate:

- Market
- Country
- Locale

more explicitly.

**Product identity**

Product identity currently uses deterministic normalized-name identity.

SKU/GTIN matching, fuzzy matching, and additional LLM confirmation remain possible future extensions.

**Job state**

The in-process research job registry is designed for a single API process.

A multi-instance deployment should move job state to durable shared infrastructure.

**Generated files**

PostgreSQL provides the production relational path, while reports, checkpoints, and generated files currently use the local filesystem.

A horizontally scaled deployment should move generated artifacts to durable object storage.

**Extraction accuracy**

Live extraction accuracy must be measured using:

```text
eval/ground_truth.json
```

before benchmark numbers are presented as project results.

**External provider quotas**

Live LLM execution depends on the configured provider's current quota, rate limits, billing state, and model availability.

A provider can return a rate-limit or quota error even when the application itself is functioning correctly.

The provider router can attempt configured fallback providers when available.

---

# Current Verification Status

The repository contains automated tests for the core application.

Run:

```bash
python -m pytest -q
```

A clean test run should be completed before pushing changes.

The following items require real-world verification and should not be claimed as completed unless they have actually been performed:

- [ ] Successful real-internet research run with saved metrics/evidence
- [ ] Provider switching verified with real Gemini credentials
- [ ] Provider switching verified with real OpenAI credentials
- [ ] Provider switching verified with real Anthropic credentials
- [ ] Groq execution verified with a real Groq API key
- [ ] 15–20 page human-labelled extraction benchmark completed
- [ ] Real frontend screenshots captured
- [ ] 60–90 second demonstration video recorded
- [ ] README updated with dated live-run metrics
- [ ] README updated with real benchmark results
- [ ] Frontend + API deployed using production secrets

Unchecked items are intentionally human-verifiable tasks.

They require real credentials, real websites, or human-labelled ground truth and therefore should not be fabricated by the codebase or documentation.

---

# Portfolio Readiness Checklist

### Engineering

- [x] Core research pipeline
- [x] Target and competitor discovery
- [x] Structured extraction
- [x] Schema and semantic validation
- [x] Corrective extraction handling
- [x] Scraper fallback chain
- [x] Region-aware execution
- [x] Evidence retention
- [x] Run scoping
- [x] Idempotent observations
- [x] FastAPI application boundary
- [x] React frontend using HTTP API
- [x] Review RAG retrieval
- [x] Cross-run change detection
- [x] PostgreSQL relational backend
- [x] PostgreSQL + pgvector review path
- [x] API-key protection
- [x] CI
- [x] Deterministic offline/demo path
- [x] Multi-provider LLM routing
- [x] Provider fallback and cooldown-based failover
- [x] PDF report generation
- [x] Alerting components
- [x] Scheduled research support

### Real-world verification

- [ ] Successful real-internet research run with saved metrics/evidence
- [ ] Real provider switching verified across configured providers
- [ ] Human-labelled extraction benchmark completed
- [ ] Real frontend screenshots captured
- [ ] 60–90 second demo video recorded
- [ ] README updated with dated live-run metrics
- [ ] README updated with real benchmark results

---

# Recommended Verification Sequence

For a fresh clone:

```bash
# 1. Create environment
python -m venv .venv

# Windows
.\.venv\Scripts\Activate.ps1

# macOS/Linux
source .venv/bin/activate
```

Then:

```bash
# 2. Install dependencies
pip install -r requirements.txt

# 3. Run tests
python -m pytest -q

# 4. Start API
python -m uvicorn app.api:app --reload
```

Open:

```text
http://127.0.0.1:8000/docs
```

Then start the frontend in a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open:

```text
http://localhost:5173
```

For production-style testing, configure:

- `.env`
- PostgreSQL/pgvector where required
- real provider credentials
- Firecrawl credentials
- live smoke-test configuration

separately from the deterministic demo path.

---

# Security Notes

Never commit:

- `.env`
- real API keys
- provider secrets
- deployment secrets

Use:

```text
.env.example
```

for variable names and placeholders only.

For deployment, configure secrets through the deployment platform's environment/secrets mechanism.

---

## License

See the repository's license file for the applicable project license.
