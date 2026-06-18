---
title: HazardMind Satellite Agent
emoji: 🛰️
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# HazardMind AI — Satellite Agent

Band-connected agent. Resolves a location to its real administrative boundary,
selects and downloads the latest Sentinel-2/1 scene, computes NDWI, classifies
the surface, vectorizes hazard zones, and uploads imagery products to R2. It is
triggered by an `@mention` from the orchestrator in the event's Band room and
hands off to the hazard agent.

`hf_app.py` runs `agent.py` unchanged plus a health server on `$PORT` (7860).

## Space secrets

`NEON_DATABASE_URL`, `BAND_API_KEY`, agent IDs, `THENVOI_REST_URL`,
`THENVOI_WS_URL`, Copernicus + R2 + LLM provider keys.
