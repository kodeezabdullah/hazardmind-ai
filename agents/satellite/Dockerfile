# HazardMind Satellite Agent
#
# Heaviest image of the fleet: rasterio / geopandas / pyogrio / pyproj need the
# GDAL + GEOS + PROJ system libraries, which are NOT installed by pip. We install
# them at the OS level, then the Python wheels link against them.
#
# Build:  docker build -t hazardmind-satellite agents/satellite
# Run:    docker run --env-file agents/satellite/.env hazardmind-satellite

FROM python:3.12-slim

# System geospatial libraries required by rasterio/geopandas/pyogrio/pyproj,
# plus build tooling for any wheels that compile from source.
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

# Install Python deps first for better layer caching.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Application code.
COPY . .

# The satellite agent is a Band listener (no HTTP server of its own). hf_app.py
# adds a tiny health server on $PORT (Hugging Face requires it) and runs agent.py
# unchanged in the background. On a plain VM you can still run `python agent.py`.
EXPOSE 7860
CMD ["python", "-u", "hf_app.py"]
