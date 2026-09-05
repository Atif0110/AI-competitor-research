# ---- API ----
FROM python:3.12-slim

WORKDIR /app

# Playwright browser is optional (used only as a fallback scraper layer).
# Build with:  docker build --build-arg WITH_PLAYWRIGHT=true .
ARG WITH_PLAYWRIGHT=false

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
