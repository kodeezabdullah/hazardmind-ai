---
title: HazardMind Impact Agent
emoji: 👥
colorFrom: yellow
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# HazardMind AI — Impact Agent

Band-connected agent. Assesses population and infrastructure exposure from real
GeoNames data, with an honest no-significant-impact gate that reports zero
affected when risk is low. Triggered by `@mention` in the event's Band room;
hands off to the report agent.

`hf_app.py` runs `agent.py` unchanged plus a health server on `$PORT` (7860).

## Space secrets

`NEON_DATABASE_URL`, `BAND_API_KEY`, `BAND_AGENT_ID`, `THENVOI_REST_URL`,
`THENVOI_WS_URL`, GeoNames + LLM provider keys.
