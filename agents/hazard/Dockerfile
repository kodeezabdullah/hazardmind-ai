# HazardMind Hazard Agent
#
# Needs the GDAL/GEOS/PROJ system libraries for geopandas/pyogrio/shapely.
#
# Build:  docker build -t hazardmind-hazard agents/hazard
# Run:    docker run --env-file agents/hazard/.env hazardmind-hazard

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        gdal-bin \
        libgdal-dev \
        libgeos-dev \
        libproj-dev \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Band listener (no HTTP server of its own).
CMD ["python", "-u", "agent.py"]
