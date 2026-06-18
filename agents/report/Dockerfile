# HazardMind Report Agent
#
# Lightweight: generates PDFs (reportlab) and map rasters (pillow) and uploads
# them to Cloudflare R2. No GDAL needed.
#
# Build:  docker build -t hazardmind-report agents/report
# Run:    docker run --env-file agents/report/.env hazardmind-report

FROM python:3.12-slim

# Minimal system libs for pillow's image/font support.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        zlib1g \
        libfreetype6 \
        curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Band listener; hf_app.py adds a health server on $PORT for Hugging Face and
# runs band_agent.py (the report entry point) unchanged in the background.
EXPOSE 7860
CMD ["python", "-u", "hf_app.py"]
