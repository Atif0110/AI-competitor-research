# Deployment guide

This project ships as a **FastAPI service** with a persistent volume for
`data/` (sqlite + reports). Three supported paths:

## A. Render.com (easiest)

1. Push the repo to GitHub.
2. In Render: **New → Blueprint** → select the repo → it reads `render.yaml`.
3. Go to the service dashboard → **Environment** → add keys from `.env.example`
   (FIRECRAWL_API_KEY, GROQ_API_KEY, …). Keep `LLM_PROVIDER=demo` until you
   have keys, so the service is live immediately.
4. Health check: `https://<your-service>.onrender.com/health`.
5. Add a **Cron Job** service (free) pointing at:
   `python scripts/run_demo.py` (or your live targets) on a schedule.

> Persistent disk: Render free tier gives ephemeral filesystem; enable the
> **Disk** add-on and mount it at `/data` (set `DATABASE_URL=sqlite:///data/competitor.db`).

## B. Railway / Fly.io (from a Dockerfile)

```bash
# Railway: connect repo, it auto-detects Dockerfile; add volume at /data.
# Fly.io:
fly launch --dockerfile Dockerfile
fly volumes create data --size 1
fly secrets set FIRECRAWL_API_KEY=... GROQ_API_KEY=...
```

## C. Local server / VPS

```bash
docker compose up -d --build     # .env is read automatically; ./data persists
```

## Scheduling runs

Use a plain cron on your VPS, or the platform cron (Render/Railway):

```cron
0 3 * * *  cd /opt/ai-competitor && /usr/bin/python scripts/run_demo.py >> logs/run.log 2>&1
```

Then send alerts from the run with `scripts/send_alerts.py` (Slack/SMTP).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `FIRECRAWL_API_KEY not set` | add the key, or rely on Playwright/basic fallback |
| Extraction all failed | check LLM keys / quota; watch logs for `provider 'x' failed` |
| PDF empty tables | you ran before any data — run the pipeline first |
| 500 on `/research` | check `data/` is writable; Postgres URL needs driver (`pip install psycopg2-binary`) |
