# HazardMind E2E Pipeline Test

End-to-end test of the post-Band LangGraph pipeline
(`satellite → hazard → impact → report`), driven exactly like the backend's
`/analyze` route: generate `event_id` once, `create_disaster_event`, then
`OrchestratorAgent.start_pipeline()`.

## What it does

`test_full_pipeline.py` runs one real pipeline for
`location="Rawalpindi", disaster_type="flood"` against the **live Neon DB**
(quota extended; the original "local postgis only" constraint was the exhausted
quota) + live R2 / Copernicus / Gemini / geoBoundaries, then asserts on the DB
rows and the final `PipelineState`. It wraps each graph node to (a) time it and
(b) capture the `event_id` seen at that node, so the #1 pre-migration bug class
(the LLM truncating the UUID) is caught byte-for-byte.

## Running

```bash
# 1. one-time: build the union venv (backend + all 4 agents in one process)
python -m venv tests/e2e/.venv-e2e
tests/e2e/.venv-e2e/Scripts/python -m pip install -r tests/e2e/requirements-e2e.txt

# 2. pre-flight (stops before spending a Sentinel download if anything is down)
tests/e2e/.venv-e2e/Scripts/python tests/e2e/preflight.py

# 3. the full run (writes a blunt report to tests/e2e/report_<ts>.md)
tests/e2e/.venv-e2e/Scripts/python tests/e2e/test_full_pipeline.py
```

To isolate off Neon onto a local Postgres instead, export `HAZARDMIND_TEST_DSN`
(a full DSN) before running — `_env.py` uses it to override `NEON_DATABASE_URL`.
`docker-compose.yml` brings up a PostGIS on `:5433` for that path (machines with
Docker); `schema-test.sql` is the corrected schema to apply to it.

## Corrected schema — `schema-test.sql`

Derived from the **live Neon DB** (introspected 2026-07-26), cross-checked
against what the code actually INSERTs/SELECTs. `shared/db/schema.sql` is stale
and must not be trusted. Mismatch table below (column | schema.sql says | code /
live Neon expects):

### `disaster_events`
| column | schema.sql | code / live Neon |
|---|---|---|
| event_id | UUID PK | UUID PK ✓ |
| disaster_type | VARCHAR | VARCHAR ✓ |
| location | VARCHAR | VARCHAR ✓ |
| bbox | FLOAT[] | FLOAT[] ✓ |
| created_at | TIMESTAMP | TIMESTAMP ✓ |
| **magnitude** | *absent* | DOUBLE PRECISION — `create_disaster_event` writes it |
| **status** | *absent* | VARCHAR — read/written every request |
| **step** | *absent* | VARCHAR — read/written every request |
| **progress** | *absent* | INTEGER — read/written every request |
| **updated_at** | *absent* | TIMESTAMP — bumped on every status update |
| **pipeline_log** | *absent* | JSONB — added at runtime by `update_pipeline_log` (ADD COLUMN IF NOT EXISTS) |
| band_room_id | *absent* | TEXT on live Neon — **dead post-migration** (not recreated in test schema) |

### `satellite_results` — the biggest drift
| column | schema.sql | code / live Neon |
|---|---|---|
| id | UUID PK | **SERIAL/int** PK (schema.sql wrong) |
| **image_url** | TEXT | *gone* — not written |
| **land_cover** | TEXT | *gone* — not written |
| affected_area_km2 | FLOAT | DOUBLE PRECISION ✓ (only surviving column) |
| **satellite_type** | *absent* | TEXT |
| **cloud_cover** | *absent* | DOUBLE PRECISION |
| **scene_id** | *absent* | TEXT |
| **true_color_url** | *absent* | TEXT |
| **index_url** | *absent* | TEXT |
| **classification_url** | *absent* | TEXT |
| **geojson_url** | *absent* | TEXT |
| **damage_percent** | *absent* | DOUBLE PRECISION |
| **total_zones** | *absent* | INTEGER |
| **bounds** | *absent* | JSONB |
| **bbox** | *absent* | JSONB |
| **risk_cities** | *absent* | JSONB |

Source: the 14-column INSERT in `agents/satellite/agent.py`.

### `hazard_zones` — matches
schema.sql agrees with live Neon and `agents/hazard/agent.py` + the report
SELECT. The **`UNIQUE (event_id, hazard_type)`** index
(`hazard_zones_event_hazard_uniq` on live Neon) is load-bearing: the hazard
`INSERT ... ON CONFLICT (event_id, hazard_type)` fails without it. `geometry
GEOMETRY(POLYGON,4326)` is the only real PostGIS column (GIST-indexed). NB the
hazard INSERT does **not** write `geometry`/`area_km2` (the `risk_polygons`-always-
`{}` gap), so those stay NULL — not a schema mismatch, an unimplemented feature.

### `impact_data` — the `overall_confidence` split
| column | schema.sql | impact agent DDL (`services/db.py`) | live Neon / report SELECT |
|---|---|---|---|
| id | UUID PK | SERIAL PK | SERIAL/int PK |
| event_id | UUID | **TEXT** UNIQUE | **UUID** UNIQUE |
| **population_affected** | INTEGER | *absent* | *absent* |
| **total_affected** | *absent* | INTEGER | INTEGER |
| high_risk_people / medium_risk_people | *absent* | INTEGER | INTEGER |
| hospitals_at_risk | INTEGER | INTEGER | INTEGER |
| **schools_affected** | INTEGER | *absent* | *absent* |
| **schools_at_risk** | *absent* | INTEGER | INTEGER |
| **roads_blocked_km** | FLOAT | *absent* | *absent* |
| **roads_blocked** | *absent* | INTEGER | INTEGER |
| bridges_at_risk | *absent* | INTEGER | INTEGER |
| vulnerability_score | FLOAT | **TEXT** | **TEXT** |
| evacuation_routes | *absent* | JSONB | JSONB |
| estimated_evacuation_time | *absent* | TEXT | TEXT |
| **overall_confidence** | *absent* | **absent (the gap)** | **DOUBLE PRECISION — present on live** |

The much-flagged gap: the impact agent's own DDL has **no `overall_confidence`**,
but `agents/report/db_client.py` SELECTs it. On live Neon the column **exists**
(added out of band), so the report read succeeds today. A fresh DB created only
by the impact agent's `CREATE TABLE IF NOT EXISTS` would be missing it and the
report read would raise `UndefinedColumnError`. `schema-test.sql` includes it so
the corrected schema matches live and the report stage works.

### `final_reports` — matches
Agrees with `agents/report/db_client.py` and live Neon. `id` is SERIAL/int.

## Files
- `docker-compose.yml` — portable local PostGIS (`:5433`) for the off-Neon path.
- `schema-test.sql` — corrected schema (CREATE EXTENSION postgis + 5 tables +
  the hazard unique index + `pipeline_log`).
- `_env.py` — merges all service `.env`s into `os.environ`; `HAZARDMIND_TEST_DSN`
  optionally overrides the DB target.
- `preflight.py` — Neon / R2 / CDSE / Gemini / geoBoundaries reachability.
- `test_full_pipeline.py` — the run + assertions + blunt report writer.
- `requirements-e2e.txt` — union of all five services' requirements.
