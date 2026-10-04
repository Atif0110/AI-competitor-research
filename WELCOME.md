# Welcome

This repository is a production-oriented AI competitor research system.

## Fastest setup

### 1. Backend

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Add your provider keys

Copy `.env.example` to `.env`.

The router is **free-tier first**. It walks APInex → Groq → Gemini → Anthropic/OpenAI → demo, and a
provider only joins the chain when its key is present, so you never pay for a model you did not key.

```env
# Free tier, no card needed (OpenAI-compatible gateway)
APINEX_API_KEY=...
APINEX_BASE_URL=https://apinex.bond/v1
APINEX_MODEL=free/all

# Free tiers
GROQ_API_KEY=...
GROQ_MODEL=openai/gpt-oss-120b

GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash

# Paid fallbacks, only used when explicitly keyed
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-sonnet-4-6

OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.6-luna
```

Add as many as you like. When a provider fails or rate-limits, the router moves on and puts that
provider in a short cooldown. `LLM_PROVIDER=auto` resolves the chain from the keys that exist;
`LLM_PROVIDER=apinex|groq|gemini|anthropic|openai|demo` pins a primary.

### 3. Optional scraping key

For live web research, add:

```env
FIRECRAWL_API_KEY=...
```

The scraper still has fallback layers, but live sites can block or change behavior, so no scraping system can guarantee every URL will work forever.

### 4. Run the API

```bash
uvicorn app.api:app --reload
```

API docs: `http://localhost:8000/docs`

### 5. Run the frontend

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

## Two ways to research

| Path | Use it for | Endpoints |
|---|---|---|
| Competitive pipeline | A company + competitor set → structured offers, regional prices, insights | `POST /research`, `GET /insights/*` |
| Deep research engine | One URL, a topic, or a comparison → cited report + grounded chat | `POST /research/deep`, `POST /research/chat` |

Deep research works with no model configured: pages are parsed into heading-aware sections, values
are extracted deterministically, and every report line carries a `[S#]` citation. Chat only answers
from what a run actually captured and says so when it cannot.

## Demo without any provider key

The application automatically uses its deterministic demo provider when no LLM keys are configured. This keeps local evaluation and tests reproducible.

## Production recommendation

Use PostgreSQL + pgvector, set `API_KEY`, keep `API_KEY_REQUIRED=true`, configure `CORS_ORIGINS`, and store all provider credentials in deployment secrets.

Never commit `.env` or real API keys.

## Honest status

- Engineering checks pass locally and in CI (backend tests + frontend build).
- Extraction quality has been verified with automated tests, **not** against a hand-labelled
  benchmark; no accuracy percentage is claimed here.
- Live provider and web-tool payloads are unverified without real keys. APInex web-tool request
  shapes are implemented defensively and degrade to normal crawling when they fail.
- Deployment is configured through `render.yaml` (API + static frontend), but live deploys still
  require setting secrets in the Render dashboard.
