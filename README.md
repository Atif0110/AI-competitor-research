# AI Competitor Research

**Evidence-backed competitive intelligence across products, prices, regions, and customer reviews — plus a deep research engine and chat grounded in cited page evidence.**

AI Competitor Research turns a target company and competitor set into a repeatable research workflow:

**Discover → Region Fan-out → Geo-aware Scrape → Structured Extraction → Validation → Storage → Competitive Insights → Evidence-backed RAG → Reports**

It also ships a second, independent path for non-vague research on a **URL, topic, or comparison set**: a budgeted crawler with heading-aware extraction, planned sub-questions, a fully cited Markdown report, explicit research gaps, and chat that answers only from the evidence a run captured. See [Deep Research Engine](#deep-research-engine).

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
        ├── APInex (free tier)
        ├── Groq / Gemini (free tier)
        ├── Anthropic / OpenAI
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

  ── in parallel: the deep research engine ──
  URL / topic / compare → plan → crawl → sections + facts →
  cited report + gaps + retrievable evidence + grounded chat
```

## Key Capabilities

### Free-tier-first multi-provider LLM routing

The application routes every model call through a single provider chain that prefers **free-tier providers** and only falls through to paid providers when a key is actually configured:

```text
APInex (free models) → Groq (free tier) → Gemini (free tier) → Anthropic / OpenAI → demo
```

Providers in order:

- **APInex** — OpenAI-compatible gateway (`https://apinex.bond/v1`). Free model IDs include
  `free/all`, `free/gpt-6-luna`, `free/gpt-5.6-luna`, `free/claude-sonnet-4.6`,
  `free/gemini-3.8-flash`, `free/deepseek-v4.1-flash`, `free/glm-5.3-flash`, `free/kimi-k3`.
  Set `APINEX_API_KEY` to enable it; `APINEX_WEB_TOOLS=true` additionally exposes its
  `/tools/web/search`, `/tools/web/contents` and `/tools/web/research` endpoints to the crawler.
- **Groq** and **Google Gemini** free tiers
- **Anthropic** / **OpenAI** (paid, only used when a key is present)
- Deterministic demo provider (no key, no network, fully offline)

Provider selection is controlled through `LLM_PROVIDER`.

Supported modes include:

| Configuration | Behavior |
|---|---|
| `apinex` | APInex first |
| `gemini` | Gemini first |
| `groq` | Groq first |
| `openai` | OpenAI first |
| `anthropic` | Anthropic first |
| `demo` | Deterministic offline provider |
| `auto` | Walk the free-first chain, skipping unconfigured providers |

When fallback providers are configured, the router moves to the next provider when the current one fails, and puts a provider into a per-provider cooldown on quota/rate-limit responses so it is not retried immediately.

**No paid usage happens implicitly.** A provider only joins the chain when its key is configured; with no keys at all the system runs fully offline on the deterministic provider.

Model names and provider-compatible base URLs are configurable through environment variables.

The `/health` endpoint exposes:

- active provider and model
- resolved provider chain and which providers are in cooldown
- configured model names per provider (keys are never exposed)
- provider tiers (`free` / `paid`)
- application mode
- deep-research budgets and chat retrieval settings

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

### Deep Research Engine

Beyond the structured competitor pipeline, the application includes a second engine for **non-vague, evidence-backed research on a URL, a topic, or a comparison set**.

```text
Request (url | topic | compare)
        │
        ▼
   Plan ────────── objective, subject, sub-questions, search queries, page types
        │           (LLM-assisted when a provider is available, deterministic otherwise)
        ▼
   Crawl ───────── budgeted: max pages, max depth, per-host caps, relevance-ranked
        │           frontier, robots-aware, retry/backoff, optional proxies
        ▼
   Extract ─────── HTML → heading-aware blocks → text + Markdown
        │           → sections (heading-anchored) → facts (values + quotes)
        ▼
   Store ────────── runs, pages, sections, facts, citations; SQLite/FTS5 or PostgreSQL/tsvector
        │
        ▼
   Synthesize ───── cited Markdown report, findings, and explicit research gaps
```

What makes the output "deep" rather than vague:

- **Every claim is cited.** Report lines and facts carry `[S#]` markers resolved against a
  citation registry, and failed pages still get a reference so gaps stay auditable.
- **Questions are planned up front** and every planned question is answered or reported as a
  gap with a reason. A question with no supporting evidence is never silently dropped.
- **Evidence is retrievable, not just stored.** Sections are chunked with overlap and indexed
  (FTS5 / tsvector) so chat and follow-up queries can find them again.
- **Extraction works with no model at all.** Prices, limits, percentages and dates are pulled
  deterministically, so the system stays useful on the free/offline path.

Run budgets are controlled by `DEEP_MAX_PAGES`, `DEEP_MAX_DEPTH`, `DEEP_PER_HOST_DELAY`,
`DEEP_PAGE_CHAR_BUDGET`, `DEEP_SECTION_MIN_CHARS`, `DEEP_CHUNK_CHARS` and `DEEP_CHUNK_OVERLAP`.

`ResearchStatus.partial` is reserved for runs with a genuine coverage shortfall — a single 404
does not downgrade a run, and `coverage` always records what failed.

### Research Chat

Chat answers questions **only** from the evidence a research run captured:

- Retrieval ranks the run's sections, with a fact-level fallback for values captured as facts.
- Answers are assembled from retrieved passages and keep their `[S#]` markers; resolved
  citations are returned with the answer.
- When the evidence does not cover the question, the engine says so instead of guessing. With no
  model available it enforces a vocabulary-coverage threshold (`CHAT_MIN_COVERAGE`) before it is
  allowed to answer at all.
- History is persisted per session (`CHAT_HISTORY_TURNS` of context carried forward).

| Endpoint | Purpose |
|---|---|
| `POST /research/chat` | Ask a question about a run, returns answer, citations and answerability |
| `GET /research/chat/{session_id}` | Retrieve a stored chat session |

### Deep Research API

| Endpoint | Purpose |
|---|---|
| `POST /research/deep` | Run url / topic / compare research, returns the full result with report, findings, gaps, citations and coverage |
| `GET /research/deep/runs` | Recent research runs for the UI |
| `GET /research/deep/runs/{run_id}` | Reload a stored run from the database |

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

The project includes a React/Vite frontend with a modern, professional design system.

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

**Deep Research**

Provides URL / Topic / Compare modes with configurable page budgets, search toggles, and real-time progress.

**Evidence**

Provides searchable and filterable structured observations with source/evidence information.

**Signals**

Displays competitive signals such as:

- price movements
- competitive undercuts
- regional snapshots
- cross-run changes

**Research Chat**

Provides evidence-backed RAG interaction using stored research evidence. Answers only from captured evidence.

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

# Deployment

## Free Hosting Options (No Credit Card Required)

### Northflank (Recommended for Backend)
- **Free tier**: 1 service, 400 build mins/mo, free PostgreSQL, free Redis
- **No credit card required**
- Deploys from Dockerfile directly
- Free PostgreSQL addon available
- GitHub auto-deploy on push

**Quick Setup:**
1. Create project at https://northflank.com
2. New Service → GitHub repo `Atif0110/AI-competitor-research` → `main` branch
4. Build: Dockerfile (auto-detected)
5. Port: `8000` (critical — your app listens on 8000)
6. Health Check: `/health`
7. Plan: Free
8. Add environment variables (see below)

### Render (Frontend — Already Working)
- **Static Site**: Free, auto-deploys from GitHub
- **Current URL**: `https://ai-competitor-research-web.onrender.com`
- Build: `npm --prefix frontend ci && npm --prefix frontend run build`
- Publish: `./frontend/dist`

### Fly.io (Alternative Backend)
- 3 shared-CPU VMs, 256MB RAM each
- No credit card for verification
- `fly deploy` uses your Dockerfile directly

### Koyeb (Truly Free, No Card)
- 1 service, 512MB RAM, 1 vCPU
- `koyeb deploy` from CLI

---

## Environment Variables

### Required for Production

**Secrets (never in repo):**
```
API_KEY=your-long-random-string
API_KEY_REQUIRED=true
APINEX_API_KEY=sk-apx1fda57e76a7d9ba702e6b2aee30fe8e00f7c0e34bf0b8b0
APINEX_BASE_URL=https://api.apinex.bond/v1
CORS_ORIGINS=https://your-frontend-url.onrender.com
GROQ_API_KEY=your-key-if-you-have
GEMINI_API_KEY=your-key-if-you-have
ANTHROPIC_API_KEY=your-key-if-you-have
OPENAI_API_KEY=your-key-if-you-have
```

**Environment Variables (non-secret):**
```
DATABASE_URL=sqlite:///data/competitor.db
VECTOR_STORE_DIR=data/vectors
CHECKPOINT_DIR=data/checkpoints
REPORT_OUTPUT_DIR=data/reports
LLM_PROVIDER=auto
DEMO_MODE=false
DEEP_LLM_ENABLED=true
DEEP_MAX_PAGES=12
DEEP_MAX_DEPTH=2
DEEP_SEARCH_ENABLED=true
DEEP_SECTION_MIN_CHARS=120
CHAT_TOP_K=6
CHAT_MIN_RELEVANCE=0.15
CHAT_MIN_COVERAGE=0.34
API_KEY_REQUIRED=true
PORT=8000
```

### Optional (if you have free keys)
```
GROQ_API_KEY=your-groq-key
GEMINI_API_KEY=your-gemini-key
ANTHROPIC_API_KEY=your-anthropic-key
OPENAI_API_KEY=your-openai-key
```

---

## Quick Start (Local)

```bash
# 1. Create environment
python -m venv .venv

# Windows
.\.venv\Scripts\Activate.ps1

# macOS/Linux
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run tests
python -m pytest -q

# 4. Start API
python -m uvicorn app.api:app --reload
```

API runs at `http://127.0.0.1:8000` with docs at `/docs`.

**Frontend (separate terminal):**
```bash
cd frontend
npm install
npm run dev
```

Frontend at `http://localhost:5173`.

---

## Live Deployment (Free)

### Backend on Northflank (Free, No Card)

1. **Create account** at https://northflank.com
2. **New Project** → `ai-competitor-research`
3. **New Service** → GitHub → `Atif0110/AI-competitor-research` → `main`
4. **Build**: Dockerfile (auto-detected)
5. **Port**: `8000` (critical!)
6. **Health Check**: `/health`
6. **Plan**: Free
7. Add all environment variables above
7. **Deploy**

### Frontend on Render Static (Free, Already Working)

Current: `https://ai-competitor-research-web.onrender.com`

Update `VITE_API_URL` to your Northflank URL, then **Manual Deploy → Clear build cache → Deploy**.

---

## Keep Warm (Free, Prevents Spin-Down)

Added `.github/workflows/keep-warm.yml` — pings `/health` every 10 minutes via GitHub Actions (free forever).

```yaml
name: Keep API Warm
on:
  schedule:
    - cron: '*/10 * * * *'
jobs:
  ping:
    runs-on: ubuntu-latest
    steps:
      - run: curl -fsS https://your-api-url/health
```

---

## API Reference

### Deep Research
| Endpoint | Purpose |
|---|---|
| `POST /research/deep` | Run url/topic/compare research |
| `GET /research/deep/runs` | List recent runs |
| `GET /research/deep/runs/{run_id}` | Reload stored run |

### Research Chat
| Endpoint | Purpose |
|---|---|
| `POST /research/chat` | Ask question about a run |
| `GET /research/chat/{session_id}` | Get chat history |

### Health
```
GET /health
```

---

## Running Tests

```bash
python -m pytest -q
```

77 tests passing (core pipeline, storage, extraction, API, RAG, production readiness, deep research, chat).

---

## Project Structure

```
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
├── schemas.py           API/data schemas
├── research/            Deep research engine (new)
│   ├── models.py
│   ├── pipeline.py
│   ├── crawler.py
│   ├── extract.py
│   ├── synthesis.py
│   ├── chat.py
│   └── ...
frontend/                React/Vite product UI (redesigned)
eval/                    Human-labelled extraction benchmark scaffold
scripts/                 Demo, live smoke, benchmark, report utilities
tests/                   Unit, failure-mode and production-readiness tests
.github/workflows/       GitHub Actions CI + keep-warm
```

---

## Current Status

| Component | Status | URL |
|---|---|---|
| Backend API | ✅ Live on Northflank | `https://p01--ai-research-platform--tqfz8cfnnfsv.code.run` |
| Frontend | ✅ Live on Render Static | `https://ai-competitor-research-web.onrender.com` |
| CI/CD | ✅ GitHub Actions | All green |
| Tests | ✅ 77 passing | `pytest -q` |
| Deep Research | ✅ Implemented | `/research/deep` |
| Research Chat | ✅ Implemented | `/research/chat` |
| Frontend UI | ✅ Redesigned | Space Grotesk + grain + depth |

---

## Verification Checklist

- [x] Core research pipeline
- [x] Deep research engine (URL/Topic/Compare)
- [x] Research chat (grounded, cited, gap-aware)
- [x] Free-tier-first LLM routing (APInex → Groq → Gemini → Paid → Demo)
- [x] Provider fallback with cooldowns
- [x] Research chat (grounded, cited, gap-aware)
- [x] API security (API key + CORS)
- [x] CI/CD (GitHub Actions: Python tests + Frontend build)
- [x] Keep-warm workflow (GitHub Actions, free)
- [x] Backend deployed on Northflank (free, no card)
- [x] Frontend on Render Static (free, auto-deploy)
- [x] Frontend redesigned (Space Grotesk, grain, depth, skeletons)
- [x] 77 tests passing
- [x] API health endpoint working
- [x] Deep research + chat endpoints working
- [x] Keep-warm workflow added

---

## Security Notes

Never commit:
- `.env`
- real API keys
- provider secrets
- deployment secrets

Use `.env.example` for variable names and placeholders only.

For deployment, configure secrets through the deployment platform's environment/secrets mechanism.

---

## License

See the repository's license file for the applicable project license.