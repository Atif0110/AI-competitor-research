FROM python:3.12-slim

WORKDIR /app

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright itself.
RUN pip install --no-cache-dir playwright==1.55.0

# Install Chromium into the exact directory used at runtime.
RUN PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    python -m playwright install chromium

# Verify the browser was actually installed during the image build.
RUN test -x /ms-playwright/chromium-1187/chrome-linux/chrome

# Make browsers executable at runtime.
RUN chmod -R 755 /ms-playwright

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
