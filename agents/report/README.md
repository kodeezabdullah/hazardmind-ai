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

Final stage of the disaster-intelligence pipeline. Generates the executive
report (JSON), a static risk map, and a PDF, then uploads them to R2 and writes
`final_reports`. Run as a LangGraph node (`node.py`) driven directly by the
backend orchestrator's `StateGraph` — no external chat-room transport.

`hf_app.py` serves a health check on `$PORT` (7860) for the Hugging Face Space.

## Space secrets

`NEON_DATABASE_URL`, R2 + LLM provider keys.
