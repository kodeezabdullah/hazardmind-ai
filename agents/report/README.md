---
title: HazardMind Report Agent
emoji: 📄
colorFrom: purple
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# HazardMind AI — Report Agent

Band-connected agent. Generates the final executive report (JSON), a static risk
map, and a PDF, then uploads them to R2 and writes `final_reports`. Triggered by
`@mention` in the event's Band room; posts the completion + verdict.

`hf_app.py` runs `band_agent.py` unchanged plus a health server on `$PORT` (7860).

## Space secrets

`NEON_DATABASE_URL`, `BAND_API_KEY`, `BAND_AGENT_ID`, `THENVOI_REST_URL`,
`THENVOI_WS_URL`, R2 + LLM provider keys.
