# HazardMind AI

HazardMind AI — Autonomous multi-agent disaster risk intelligence platform.
Detects floods, earthquakes & landslides anywhere in the world using satellite
imagery, GIS analysis & AI agents collaborating through Band.

## Agents
- Satellite Agent — imagery processing
- Hazard Detection Agent — risk analysis
- Impact Assessment Agent — population + infra
- Executive Report Agent — map + PDF output

## Tech Stack
- Python + FastAPI + Band SDK
- Next.js + MapLibre GL JS
- Neon PostGIS + Cloudflare R2
- AI/ML API + Featherless AI

## Setup
cp .env.example .env
# Fill in your API keys
pip install -r requirements.txt
