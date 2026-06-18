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
RUN chmod +x entrypoint.sh

# Hugging Face Spaces inject $PORT (7860); default 8000 for local/VM use.
ENV PORT=7860
EXPOSE 7860

# entrypoint.sh writes agent_config.yaml from the Space secrets (keeping the
# Band api_key out of the repo) and then launches uvicorn on $PORT.
CMD ["bash", "entrypoint.sh"]
