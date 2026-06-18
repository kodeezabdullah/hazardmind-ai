---
title: HazardMind Hazard Agent
emoji: 📡
colorFrom: red
colorTo: orange
sdk: docker
app_port: 7860
pinned: false
---

# HazardMind AI — Hazard Agent

Band-connected agent. Converts the satellite result into multi-hazard risk
levels: flood (NDWI), earthquake (USGS seismicity), and landslide (real SRTM DEM
slope), with severity + confidence. Triggered by `@mention` in the event's Band
room; hands off to the impact agent.

`hf_app.py` runs `agent.py` unchanged plus a health server on `$PORT` (7860).

## Space secrets

`NEON_DATABASE_URL`, `BAND_API_KEY`, agent IDs, `THENVOI_REST_URL`,
`THENVOI_WS_URL`, USGS + LLM provider keys.
