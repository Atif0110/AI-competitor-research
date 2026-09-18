# ---- API ----
FROM python:3.12-slim

WORKDIR /app

# Install Playwright and Chromium for the fallback scraper layer.
ARG WITH_PLAYWRIGHT=true

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN if [ "$WITH_PLAYWRIGHT" = "true" ]; then \
      pip install --no-cache-dir playwright \
      && python -m playwright install --with-deps chromium; \
    fi

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
