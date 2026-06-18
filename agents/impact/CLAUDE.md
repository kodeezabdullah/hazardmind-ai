# Impact Assessment Agent (Agent 3) — HazardMind

## Architecture

Two modes of operation:

| Mode | Entry point | Purpose |
|------|-------------|---------|
| **Production** | `python agent.py` | Band SDK agent — listens for @mentions via WebSocket |
| **Local API** | `uvicorn main:app --reload --port 8001` | FastAPI service — `POST /assess-impact`, `GET /health` |

## Quick Start
```bash
cd agents/impact
pip install -r requirements.txt
cp .env.example .env   # fill in API keys

# Local API (no Band connection needed):
USE_MOCK_BAND=true uvicorn main:app --reload --port 8001
# POST hazard JSON or Band message to /assess-impact (see /docs)

# Production (connects to Band WebSocket):
python agent.py
```

## Project Structure
```
agents/impact/
  agent.py              # Band SDK entry point — production
  main.py               # FastAPI service — port 8001 (/assess-impact, /health)
  agent_config.yaml     # Band agent_id, api_key, handle
  tasks/
    population.py       # Task 1 — GeoNames real population + LLM reasoning
    infrastructure.py   # Task 2 — Overpass OSM real counts + LLM reasoning
    vulnerability.py    # Task 3 — combined context + evacuation routing
  services/
    band_client.py      # send_to_band_room(), send_anomaly_to_band()
    db.py               # asyncpg → single impact_data table (Neon)
    llm_router.py       # smart_llm_call() with Featherless chain + AIML fallback
    featherless.py      # call_with_fallback() — model orchestration
    cost_tracker.py     # per-session token cost tracking
    r2_reader.py        # Cloudflare R2 satellite URL lookup
  .env.example
  requirements.txt
```

## Input Formats Accepted
The `/assess-impact` endpoint accepts two formats:

**Raw hazard JSON** (flat):
```json
{"event_id": "flood-001", "flood_risk": "HIGH", "bbox": [67.0, 24.8, 67.5, 25.2], "risk_cities": ["Karachi"]}
```

**Band message format** (from hazardmind-hazard Agent 2):
```json
{"event_id": "flood-001", "step": "hazard", "data": {"risk_level": "HIGH", "bounds": {"west": 67.0, "south": 24.8, "east": 67.5, "north": 25.2}, "risk_cities": ["Karachi"]}}
```

`_normalise_hazard()` in `main.py` bridges both formats.

## Execution Flow
1. Agent receives hazard data (@mention via Band WebSocket, or POST /assess-impact)
2. `_normalise_hazard()` unwraps Band format if needed
3. `asyncio.gather(Task1, Task2)` — population + infrastructure run in **parallel**
   - Task 1 fetches real population from **GeoNames API**
   - Task 2 fetches real OSM data from **Overpass API** (3 endpoints, failover)
4. LLM receives real API data and reasons about disaster impact (NOT estimation)
5. Task 3 runs sequentially — vulnerability scoring + evacuation routing
6. DB write to `impact_data` table (non-fatal)
7. Anomaly checks: hospitals > 10 → NDMA Level-3 alert; confidence < 0.7 → field verification
8. Completion signal sent to `@hazardmind-orchestrator` via Band

## Data Strategy
| Task | External API | LLM Role |
|------|-------------|----------|
| Population | GeoNames `/searchJSON` → real city population | Reason what % is in flood zone |
| Infrastructure | Overpass OSM → real hospital/school/bridge counts | Reason which subset is affected |
| Vulnerability | No external API (uses Task 1+2 results) | Score, zones, evacuation routes |

APIs provide numbers. LLM provides intelligence.

## Model Chain
| Criticality | Featherless Chain | AIML Fallback |
|-------------|-----------------|---------------|
| normal | gemma-4-31B-it → Kimi-K2.6 → Qwen3-32B | claude-opus-4-8 (last resort) |
| high | Kimi-K2.6 → gemma-4-31B-it → Qwen3-32B | claude-opus-4-8 |
| critical | Qwen3-32B → Kimi-K2.6 → gemma-4-31B-it | claude-opus-4-8 → GPT-4.5 |

`claude-opus-4-8` via AIML API — only when ALL Featherless models fail.
`GPT-4.5` — only when Opus throws exception or timeout.

## Environment Variables
| Variable | Description |
|----------|-------------|
| `FEATHERLESS_API_KEY` | Featherless API (gemma/Kimi/Qwen models) |
| `AIML_API_KEY` | AIML API key (claude-opus-4-8 fallback) |
| `ANTHROPIC_BASE_URL` | `https://api.aimlapi.com/v1` |
| `BAND_AGENT_ID` | Band agent UUID |
| `BAND_API_KEY` | Band API key |
| `BAND_HANDLE` | `@geospatial.9660/hazardmind-impact` |
| `THENVOI_REST_URL` | `https://app.band.ai/` |
| `THENVOI_WS_URL` | `wss://app.band.ai/api/v1/socket/websocket` |
| `NEON_DATABASE_URL` | PostgreSQL DSN for Neon |
| `CLOUDFLARE_R2_PUBLIC` | R2 bucket public URL for satellite images |
| `GEONAMES_USERNAME` | `hazardmind` (free account, 30k credits/day) |
| `USE_MOCK_BAND` | `true` to log Band sends instead of actually posting |

## Database Schema
Single `impact_data` table — idempotent via `ON CONFLICT (event_id) DO UPDATE`:
```sql
CREATE TABLE IF NOT EXISTS impact_data (
    id                       SERIAL PRIMARY KEY,
    event_id                 TEXT UNIQUE NOT NULL,
    total_affected           INTEGER,
    high_risk_people         INTEGER,
    medium_risk_people       INTEGER,
    hospitals_at_risk        INTEGER,
    schools_at_risk          INTEGER,
    roads_blocked            INTEGER,
    bridges_at_risk          INTEGER,
    vulnerability_score      TEXT,
    evacuation_routes        JSONB,
    estimated_evacuation_time TEXT,
    created_at               TIMESTAMPTZ DEFAULT NOW(),
    updated_at               TIMESTAMPTZ DEFAULT NOW()
);
```

## event_id truncation hardening (`agent.py`)

The Band LangGraph adapter's LLM sometimes truncates the UUID `event_id` to its
leading 8-char segment. `impact_data.event_id` is **UUID-typed** (per
`shared/db/schema.sql` — note the schema block below is stale and shows TEXT), so
a short id would break the INSERT. Fix (mirrors satellite/hazard):
- `_BoundEventIdAdapter.on_message` snapshots the full `event_id: <uuid>` from the
  inbound dispatch **before the LLM runs** and binds it to the room
  (`_bind_room_event_id`, keyed by the LangGraph `thread_id`).
- `_resolve_event_id(event_id, room_id)` (called at the top of
  `run_impact_analysis`) prefers that room-bound full UUID over the LLM-supplied
  tool argument.

## Key Rules
- Never hardcode API keys — all from environment
- DB writes are non-fatal — log error, continue
- Band sends are non-fatal — log error, continue
- `asyncio.gather()` is mandatory for Task 1 + Task 2
- `risk_cities` list drives geographic context for all tasks
- Always include `event_id` from orchestrator — never generate one
- Band completion message = natural text + JSON payload (both required)
