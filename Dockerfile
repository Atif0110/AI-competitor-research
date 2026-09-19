FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Explicitly install Chromium for the Playwright runtime.
RUN python -m playwright install chromium

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
