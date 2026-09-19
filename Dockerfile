# ---- API ----
FROM python:3.12-slim

WORKDIR /app

# System dependencies required by Playwright/Chromium and Python packages.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright and Chromium unconditionally.
# The fallback scraper requires the browser binary at runtime.
RUN pip install --no-cache-dir playwright \
    && python -m playwright install --with-deps chromium

# Copy application.
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
