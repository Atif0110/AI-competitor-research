# Deployment

## Local production-style stack

The included Docker Compose stack runs PostgreSQL + pgvector, FastAPI, and the React frontend.

```bash
cp .env.example .env
# set API_KEY and the provider/scraper credentials you need
docker compose up -d --build
```

- Frontend: `http://localhost:5173`
- API: `http://localhost:8000`
- Swagger: `http://localhost:8000/docs`
- PostgreSQL: `localhost:5432`

The browser talks to FastAPI over HTTP. It never imports the Python pipeline.

## Production notes

For a horizontally scaled deployment, move these local filesystem artifacts to durable object storage:

- `data/reports/`
- `data/live_runs/`
- `data/vectors/` when using the offline vector fallback
- checkpoints

PostgreSQL is the production relational backend. PostgreSQL review storage uses pgvector when an OpenAI embedding key is available.

Set:

```text
DATABASE_URL=postgresql://...
API_KEY_REQUIRED=true
API_KEY=<long-random-secret>
CORS_ORIGINS=https://your-frontend.example
```

Do not commit `.env` or API keys.

## Render

`render.yaml` remains a simple FastAPI demo deployment. Render's free filesystem is ephemeral, so it should not be presented as durable production storage. For durable production, attach a managed PostgreSQL database and external object storage.

## Live validation

After deployment, use `scripts/run_live_smoke.py` with a real target configuration. It refuses to run in demo mode and saves the resulting metrics/evidence manifest under `data/live_runs/<run_id>/`.
