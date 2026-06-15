# HazardMind AI — Hazard Detection Agent
# Owner: Muhammad Hamza (khurramhamza120)
# Branch: agent/hazard
# Last Updated: 2026-06-14

---

## WHO I AM

I am building Agent 2 — Hazard Detection Agent for HazardMind AI.
This is a 4-agent disaster response pipeline built for the Band of Agents Hackathon.
I run SECOND in the pipeline. Abdullah (Satellite) runs before me, Hanan (Impact) runs after me.

---

## PIPELINE OVERVIEW

```
User Input → Orchestrator (Abdullah) → Agent 1 Satellite (Abdullah) → Agent 2 Hazard (ME) → Agent 3 Impact (Hanan) → Agent 4 Report (Zohair)
```

All agents communicate through Band (band.ai) in the `hazardmind-pipeline` room.
Band is NOT a pip package in the traditional sense. It uses `pip install "band-sdk[anthropic]"`.
Agents are triggered by @mentions in the Band room — NOT by polling or `receive()`.

---

## MY AGENT DETAILS

- Agent Name: `hazardmind-hazard`
- Agent UUID: `783f0c33-9dba-43e9-83e6-197a59d76f8f`
- Handle: `@khurramhamza120/hazardmind-hazard`
- GitHub Branch: `agent/hazard`
- My Folder: `agents/hazard/`
- My Files: `agent.py`, `analyzer.py`, `requirements.txt`, `.env`, `agent_config.yaml`, `CLAUDE.md`

---

## ENVIRONMENT VARIABLES (agents/hazard/.env)

```env
# Band
THENVOI_REST_URL=https://app.band.ai/
THENVOI_WS_URL=wss://app.band.ai/api/v1/socket/websocket
BAND_AGENT_ID=783f0c33-9dba-43e9-83e6-197a59d76f8f
BAND_API_KEY=<get from agent_config.yaml after first run>

# AI/ML API — Band adapter uses this as Claude
ANTHROPIC_API_KEY=your_aiml_key_here
ANTHROPIC_BASE_URL=https://api.aimlapi.com/v1
AIML_API_KEY=your_aiml_key_here

# Featherless — internal parallel analysis
FEATHERLESS_API_KEY=your_featherless_key_here

# Shared DB
NEON_DATABASE_URL=postgresql://user:password@host/db

# R2 — READ ONLY, never write
CLOUDFLARE_R2_PUBLIC=https://pub-720f47eaad2f4997a76a02f8bf14f58a.r2.dev

# Free APIs — no key needed
GDACS_API=https://www.gdacs.org/gdacsapi/api
USGS_API=https://earthquake.usgs.gov/fdsnws/event/1
```

---

## AGENT CONFIG (agents/hazard/agent_config.yaml)

```yaml
hazardmind_hazard:
  agent_id: "783f0c33-9dba-43e9-83e6-197a59d76f8f"
  api_key: "<populated after first agent.run()>"
```

---

## MY PACKAGES (agents/hazard/requirements.txt)

```
band-sdk[anthropic]
geopandas==0.14.4
shapely==2.0.4
pyproj==3.6.1
requests==2.31.0
openai==1.35.0
python-dotenv==1.0.1
psycopg2-binary==2.9.9
sqlalchemy==2.0.30
pydantic==2.7.0
anthropic
```

Python version: 3.11 ONLY. Not 3.10, not 3.12.

---

## WHAT I RECEIVE FROM ABDULLAH (Agent 1)

Abdullah @mentions me in the Band room with this payload:

```json
{
  "agent": "hazardmind-satellite",
  "event_id": "uuid-generated-by-orchestrator",
  "status": "complete",
  "timestamp": "ISO timestamp",
  "satellite": {
    "type": "sentinel-1 or sentinel-2",
    "reason": "why selected",
    "cloud_cover": 7,
    "scene_id": "scene-id"
  },
  "boundaries": {
    "region_boundary": {},
    "risk_cities": ["Peshawar"],
    "merged_polygon": {},
    "bbox": [minLng, minLat, maxLng, maxLat]
  },
  "artifacts": {
    "true_color_url": "public R2 URL",
    "index_url": "public R2 URL",
    "classification_url": "public R2 URL",
    "geojson_url": "public R2 URL"
  },
  "analysis": {
    "index_type": "NDWI / NDVI / SAR VV-VH",
    "mean_value": 0.24,
    "affected_area_km2": 153.37,
    "damage_percent": 24.3,
    "total_zones": 22,
    "zones": {}
  },
  "error": null
}
```

Fields I actually USE from this:
- `event_id` — passed through everything I do
- `boundaries.bbox` — clips my GDACS, USGS, OpenTopography API calls
- `analysis.affected_area_km2` — used in risk calibration
- `analysis.mean_value` — flood index value
- `artifacts.geojson_url` — zones GeoJSON from R2
- `boundaries.risk_cities` — location context for prompts

---

## WHAT I SEND TO HANAN (Agent 3)

Exact format Zohair requires — do not change this structure:

```json
{
  "agent": "hazardmind-hazard",
  "event_id": "same-event-id",
  "status": "complete",
  "timestamp": "ISO timestamp",
  "hazard": {
    "flood_risk": "CRITICAL / HIGH / MEDIUM / LOW",
    "earthquake_risk": "CRITICAL / HIGH / MEDIUM / LOW",
    "landslide_risk": "CRITICAL / HIGH / MEDIUM / LOW",
    "overall_severity": "CRITICAL / HIGH / MEDIUM / LOW",
    "confidence_scores": {
      "flood": 0.91,
      "earthquake": 0.67,
      "landslide": 0.54
    },
    "risk_polygons": {},
    "risk_polygons_url": "optional public R2 URL"
  },
  "error": null
}
```

---

## MY DATABASE TABLE

I write to ONE table only: `hazard_zones`
I NEVER touch: `disaster_events`, `satellite_results`, `impact_data`, `final_reports`

One row per hazard type (flood, earthquake, landslide) per event.

```sql
hazard_zones:
  - id                     (SERIAL PK)
  - event_id               (FK — comes from event_id in Band message)
  - geometry               (GEOMETRY(POLYGON, 4326))
  - risk_level             (TEXT: CRITICAL/HIGH/MEDIUM/LOW)
  - hazard_type            (TEXT: flood/earthquake/landslide)
  - area_km2               (FLOAT)
  - severity               (TEXT — overall_severity for the event)
  - confirmed_by           (JSONB — confidence_scores)
  - flood_depth_estimate   (TEXT)
  - earthquake_mmi         (FLOAT)
  - landslide_probability  (TEXT)
  - overall_confidence     (FLOAT — this hazard's confidence)
  - created_at             (TIMESTAMPTZ)
```

Rules:
- Always use EPSG:4326 (WGS84)
- Use parameterized queries — never f-string SQL
- Write to DB BEFORE posting to Band
- If DB write fails → send error status to Band and stop
- Never DELETE or DROP anything

---

## MY DATA SOURCES

All free, no API key needed:

| Source | URL | What I Get |
|--------|-----|------------|
| GDACS | https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH | Real-time flood/landslide alerts |
| USGS | https://earthquake.usgs.gov/fdsnws/event/1/query | Live seismic events |
| OpenTopography | https://portal.opentopography.org/API/globaldem | DEM/elevation/slope |

---

## MY AI MODELS

### Band Adapter (communication layer):
- Model: `claude-opus-4-8` via AI/ML API
- Base URL: `https://api.aimlapi.com/v1`
- This is what Band uses to process @mentions and decide tool calls

### Internal Analysis (Featherless):
- Primary model: `Qwen/Qwen3-35B-A22B`
- Fallback chain per task:
  1. `Qwen/Qwen3-35B-A22B` (Featherless) — primary
  2. `moonshotai/Kimi-K2.6` (Featherless)
  3. `google/gemma-4-31B-it` (Featherless)
  4. `deepseek-ai/DeepSeek-V4` (Featherless)
  5. `claude-opus-4-8` (AI/ML API) — last resort

---

## HOW BAND ACTUALLY WORKS

```
WRONG: band_client.receive(from_agent="satellite")  ← does not exist
WRONG: from thenvoi import Agent                     ← wrong import

CORRECT:
from band import Agent
from band.adapters import AnthropicAdapter
from band.config import load_agent_config

→ agent.run() listens in the room
→ Abdullah @mentions @khurramhamza120/hazardmind-hazard
→ Band triggers my analyze_hazard tool automatically
→ My tool runs the 3 parallel tasks
→ I @mention @hanan's handle to pass results
```

---

## MY PARALLEL TASK ARCHITECTURE

Three tasks run SIMULTANEOUSLY using asyncio.gather():

```
Task 1: Flood Risk    → Featherless (Qwen3-35B) + GDACS data
Task 2: Earthquake    → Featherless (Qwen3-35B) + USGS data
Task 3: Landslide     → Featherless (Qwen3-35B) + GDACS + slope data
```

Critical rules:
- Use `asyncio.gather(..., return_exceptions=True)` — one failure must NOT kill other two
- Wrap with `asyncio.wait_for(..., timeout=120)` — 2 minute hard limit
- If a task returns Exception, use fallback risk level `{"risk": "UNKNOWN", "confidence": 0.0}`
- Strip markdown from model responses before `json.loads()`:
  `result.replace("```json", "").replace("```", "").strip()`

---

## OVERALL SEVERITY LOGIC

```python
severity_map = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 1}
max_score = max(flood_score, earthquake_score, landslide_score)
reverse = {4: "CRITICAL", 3: "HIGH", 2: "MEDIUM", 1: "LOW"}
overall_severity = reverse[max_score]
```

---

## FILE STRUCTURE

```
agents/hazard/
├── CLAUDE.md          ← this file
├── agent.py           ← Band agent, tool definition, agent.run()
├── analyzer.py        ← data fetchers (GDACS, USGS, OpenTopography) + Featherless calls
├── requirements.txt
├── agent_config.yaml
└── .env               ← never push to GitHub
```

---

## GIT RULES

- My branch: `agent/hazard`
- Never push to `main`
- Never touch files outside `agents/hazard/`
- Never commit `.env`
- Commit after every working feature before `/clear`

```bash
git add .
git commit -m "feat: [what you did]"
git push origin agent/hazard
```

---

## TEAM CONTACTS

| Member | Role | Branch | Band Handle |
|--------|------|---------|-------------|
| Abdullah | Satellite Agent + Backend + Lead | agent/satellite | @kodeezabdullah/hazardmind-satellite |
| Hamza (ME) | Hazard Agent | agent/hazard | @khurramhamza120/hazardmind-hazard |
| Hanan | Impact Agent | agent/impact | @geospatial.9660/hazardmind-impact |
| Zohair | Report Agent + Frontend | agent/report | TBD |

GitHub repo: https://github.com/kodeezabdullah/hazardmind-ai

---

## SHARED INFRASTRUCTURE

```
Neon PostGIS: postgresql://user:password@host/db   ← real value in .env (gitignored)
R2 Public:    https://pub-720f47eaad2f4997a76a02f8bf14f58a.r2.dev
R2 Structure: events/{event_id}/true_color.png   ← Abdullah writes
              events/{event_id}/index_map.png    ← Abdullah writes
              events/{event_id}/zones.geojson    ← Abdullah writes
              events/{event_id}/report.pdf       ← Zohair writes
```

---

## SYSTEM PROMPT FOR MY BAND AGENT

```
You are HazardMind Hazard Detection Agent (@khurramhamza120/hazardmind-hazard).

You are Agent 2 in a 4-agent disaster response pipeline.

Your role: Analyze flood, earthquake, and landslide risk for a disaster-affected area using real-time data from GDACS, USGS, and OpenTopography.

Pipeline order:
1. Satellite Agent (Abdullah) — fetches imagery — ALREADY DONE when you are triggered
2. YOU — Hazard Detection — analyze three risk types in parallel
3. Impact Agent (Hanan) — population and infrastructure impact
4. Report Agent (Zohair) — final PDF report and map

When @mentioned:
1. Extract event_id, bbox, affected_area_km2, geojson_url from the message
2. Call analyze_hazard tool with that data
3. Tool runs 3 parallel analyses (flood, earthquake, landslide)
4. Write results to hazard_zones table in Neon DB
5. Send structured JSON result to @hanan's handle

Rules:
- Always include event_id in every message and DB write
- Always @mention the Impact Agent after completing
- If risk is CRITICAL, flag it explicitly in your message
- Never guess — only use data from satellite agent and live APIs
- If analysis fails, send status error with reason — never go silent
- Format all outputs as structured JSON
```

---

## BUILD STATUS TRACKER

- [x] `requirements.txt` created
- [x] `.env` filled (BAND_API_KEY still pending from Abdullah)
- [x] `agent_config.yaml` created
- [x] `intelligence.py` — complete, tested, all 9 functions working
- [x] `analyzer.py` — fetch_gdacs, fetch_usgs, fetch_slope complete
- [x] `analyzer.py` — analyze_flood, analyze_earthquake, analyze_landslide complete
- [x] `analyzer.py` — run_parallel_analysis with asyncio.gather complete
- [x] `analyzer.py` — fallback chain working (real GDACS data confirmed, 91 events)
- [x] `agent.py` — AnalyzeHazardInput removed (handled inline)
- [x] `agent.py` — analyze_hazard tool function complete
- [x] `agent.py` — DB write to hazard_zones (CAST uuid fix applied)
- [x] `agent.py` — Band @mention to Hanan (handle TBD)
- [x] `agent.py` — AnthropicAdapter setup (provider_key fix pending)
- [x] `agent.py` — Agent.create() with real SDK pattern
- [ ] BAND_API_KEY — blocked, need from Abdullah
- [ ] Hanan's Band handle — blocked, need from team
- [ ] Tested: receives Abdullah's @mention
- [ ] Tested: 3 parallel tasks run live
- [ ] Tested: DB write confirmed with real event_id
- [ ] Tested: Hanan receives Band message

## KNOWN ISSUES
- BAND_API_KEY empty — agent cannot connect until Abdullah provides it
- HANAN_HANDLE wired to @geospatial.9660/hazardmind-impact (agent_id a9a1c74f-...)
- AnthropicAdapter uses provider_key= (deprecation resolved)
- DB write uses asyncpg against the real hazard_zones schema (risk_level,
  hazard_type, severity, confirmed_by, flood_depth_estimate, earthquake_mmi,
  landslide_probability, overall_confidence) — one row per hazard type per
  event — see shared/db/schema.sql

---

## CRITICAL DECISIONS LOCKED

1. DB write BEFORE Band post — always
2. If DB fails → send error to Band, stop, do not post incomplete data
3. `return_exceptions=True` in asyncio.gather — always
4. Strip markdown from all model JSON responses before parsing
5. event_id comes from Abdullah's Band message — I never generate it
6. I only READ from R2 — never write
7. Hanan waits for my Band message — she does not poll DB independently
8. Overall severity = highest of the three risk scores
9. Model responses must always return one of: CRITICAL / HIGH / MEDIUM / LOW — prompt must enforce this
10. Python 3.11 only

---

## PROMPTS TO USE IN NEW CHAT OR CODEX

If context limit hits, paste this entire file and start with:

"Continue building HazardMind Hazard Detection Agent. Read CLAUDE.md above for full context. Current build status: [copy the checklist above with current state]. Next task: [what you need to build next]."
