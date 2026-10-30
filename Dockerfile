FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

WORKDIR /app

ENV PYTHONUNBUFFERED=1
# This repository is deployed as a visitor-facing public research showcase.
# Upstream model credentials remain server-side; override to false if hosting privately.
ENV PUBLIC_WORKSPACE_MODE=true

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
