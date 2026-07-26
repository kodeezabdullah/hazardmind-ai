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

# hf_app.py serves a health check on $PORT for Hugging Face. The report
# pipeline itself runs as a LangGraph node (node.py) driven by the backend
# orchestrator, not as a standalone process in this container.
EXPOSE 7860
CMD ["python", "-u", "hf_app.py"]
