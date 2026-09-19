FROM python:3.12-slim

WORKDIR /app

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

RUN pip install --no-cache-dir playwright

# Install the exact Chromium browser required by Playwright
RUN PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    python -m playwright install chromium

# Make the browser directory readable/executable at runtime
RUN chmod -R 755 /ms-playwright

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
