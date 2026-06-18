#!/usr/bin/env bash
# Container entrypoint.
#
# agent_config.yaml carries the Band agent_id + api_key. The api_key is a SECRET,
# so the file is NOT committed. Instead we generate it at startup from the Space
# secrets (BAND_AGENT_ID, BAND_API_KEY) — keeping the real key out of the repo.
set -e

cat > /app/agent_config.yaml <<YAML
orchestrator_agent:
  agent_id: "${BAND_AGENT_ID}"
  api_key: "${BAND_API_KEY}"
YAML

# Hugging Face injects $PORT (7860); default to 8000 locally.
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
