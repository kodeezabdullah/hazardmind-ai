# HazardMind Backend API (FastAPI orchestrator)
#
# Exposes the REST API (/analyze, /status, /results, /health) and coordinates
# the agent pipeline. Lightweight: no GDAL.
#
# Build:  docker build -t hazardmind-backend backend
# Run:    docker run -p 8000:8000 --env-file backend/.env hazardmind-backend

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Cloud Run / most PaaS inject $PORT; default to 8000 for local/VM use.
ENV PORT=8000
EXPOSE 8000

# Shell form so $PORT expands at runtime.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
