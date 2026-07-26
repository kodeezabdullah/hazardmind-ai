#!/usr/bin/env bash
# Container entrypoint.
set -e

# Hugging Face injects $PORT (7860); default to 8000 locally.
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
