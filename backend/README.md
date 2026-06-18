---
title: HazardMind Backend
emoji: 🛰️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: HazardMind AI — autonomous multi-agent disaster intelligence
---

# HazardMind AI — Backend (Orchestrator API)

FastAPI service that drives the multi-agent disaster-intelligence pipeline.
It creates the per-event Band room, dispatches the satellite team, and exposes
the REST API the frontend talks to:

- `POST /analyze` — start a pipeline run for `{ location, disaster_type }`
- `GET /status/{job_id}` — pipeline progress
- `GET /results/{job_id}` — final result (satellite/hazard/impact/report)
- `GET /band-log/{job_id}` — live Band room conversation
- `GET /health` — health check

## Configuration (Space secrets)

Set these as **Settings → Variables and secrets** on the Space:

- `NEON_DATABASE_URL`
- `BAND_API_KEY`, `BAND_AGENT_ID`, `BAND_ROOM_ID`
- `SATELLITE_AGENT_ID`, `HAZARD_AGENT_ID`, `IMPACT_AGENT_ID`, `REPORT_AGENT_ID`
- `THENVOI_REST_URL`, `THENVOI_WS_URL`
- R2 / LLM provider keys as used by the pipeline

The container listens on `$PORT` (Hugging Face sets `7860`).
