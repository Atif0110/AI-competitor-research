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

Use any of these supported configurations:

```env
# Claude
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-sonnet-4-6

# OpenAI / GPT
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.6-luna

# Groq
GROQ_API_KEY=...
GROQ_MODEL=openai/gpt-oss-120b
```

You can provide one, two, or all three. With multiple providers, the router uses the configured primary and falls back automatically when a provider fails. Set `LLM_PROVIDER=anthropic`, `openai`, or `groq` to control the primary provider. `LLM_PROVIDER=auto` resolves the chain from the keys that exist.

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

## Demo without any provider key

The application automatically uses its deterministic demo provider when no LLM keys are configured. This keeps local evaluation and tests reproducible.

## Production recommendation

Use PostgreSQL + pgvector, set `API_KEY`, keep `API_KEY_REQUIRED=true`, configure `CORS_ORIGINS`, and store all provider credentials in deployment secrets.

Never commit `.env` or real API keys.
