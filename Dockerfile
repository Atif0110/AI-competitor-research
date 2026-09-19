FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright-browsers

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Install the exact Chromium browser matching Playwright 1.55.0
RUN python -m playwright install chromium

# Fail the Docker build if Chromium wasn't actually installed.
RUN test -d /app/.playwright-browsers \
    && find /app/.playwright-browsers -type f -name "chrome" | grep -q .

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
