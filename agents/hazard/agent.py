import asyncio
import json
import os
from datetime import datetime, timezone

import asyncpg
import httpx
from band import Agent
from band.adapters import AnthropicAdapter
from dotenv import load_dotenv

from analyzer import run_parallel_analysis
from intelligence import quality_check, write_band_message

load_dotenv()

HANAN_HANDLE = "@geospatial.9660/hazardmind-impact"
HANAN_AGENT_ID = "a9a1c74f-5d5e-4195-9177-87ede2807650"

BAND_AGENT_ID = os.getenv("BAND_AGENT_ID")
AIML_API_KEY = os.getenv("AIML_API_KEY")
NEON_DATABASE_URL = os.getenv("NEON_DATABASE_URL")
BAND_API_KEY = os.getenv("BAND_API_KEY", "")
BAND_ROOM_ID = os.getenv("BAND_ROOM_ID", "c4a9708d-d784-41cc-a639-b482a00a3379")
THENVOI_REST_URL = os.getenv("THENVOI_REST_URL", "https://app.band.ai/")
THENVOI_WS_URL = os.getenv(
    "THENVOI_WS_URL", "wss://app.band.ai/api/v1/socket/websocket"
)


async def write_to_db(result: dict) -> None:
    """Write hazard results to the hazard_zones table (matches shared/db/schema.sql)."""
    conn = await asyncpg.connect(NEON_DATABASE_URL)
    try:
        await conn.execute(
            """
            INSERT INTO hazard_zones (
                event_id, flood_risk, earthquake_risk,
                landslide_risk, overall_severity, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
            result["event_id"],
            result["flood_risk"],
            result["earthquake_risk"],
            result["landslide_risk"],
            result["overall_severity"],
            datetime.now(timezone.utc),
        )
    finally:
        await conn.close()


async def send_to_band(message_text: str, agent_id: str = HANAN_AGENT_ID) -> None:
    """Post a message into the Band room, @mentioning the target agent."""
    url = f"{THENVOI_REST_URL.rstrip('/')}/api/v1/agent/chats/{BAND_ROOM_ID}/messages"
    headers = {"X-API-Key": BAND_API_KEY}
    body = {
        "message": {
            "content": message_text,
            "mentions": [{"id": agent_id}],
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()


async def analyze_hazard(satellite_payload: dict, send_message) -> dict:
    event_id = satellite_payload.get("event_id", "unknown")
    try:
        satellite_data = {
            "event_id": event_id,
            "boundaries": satellite_payload.get(
                "boundaries", {"bbox": [], "risk_cities": []}
            ),
            "analysis": satellite_payload.get(
                "analysis", {"affected_area_km2": 0, "mean_value": 0}
            ),
            "artifacts": satellite_payload.get("artifacts", {}),
            "satellite": satellite_payload.get("satellite", {}),
        }

        raw_result = await run_parallel_analysis(satellite_data)

        qc = await quality_check(raw_result)
        if not qc["passed"]:
            error_msg = json.dumps(
                {
                    "agent": "hazardmind-hazard",
                    "event_id": event_id,
                    "status": "error",
                    "error": "quality check failed",
                }
            )
            await send_message(HANAN_HANDLE, error_msg)
            return {"status": "error"}

        # DB write BEFORE Band post — if it fails, report error and stop.
        await write_to_db(raw_result)

        payload = {
            "agent": "hazardmind-hazard",
            "event_id": event_id,
            "status": "complete",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "hazard": {
                "flood_risk": raw_result["flood_risk"],
                "earthquake_risk": raw_result["earthquake_risk"],
                "landslide_risk": raw_result["landslide_risk"],
                "overall_severity": raw_result["overall_severity"],
                "confidence_scores": raw_result["confidence_scores"],
                "risk_polygons": {},
                "risk_polygons_url": "",
            },
            "error": None,
        }

        message = await write_band_message(raw_result, HANAN_HANDLE)
        full_message = message + "\n\n" + json.dumps(payload, indent=2)
        await send_message(HANAN_HANDLE, full_message)

        return payload

    except Exception as e:
        error_msg = json.dumps(
            {
                "agent": "hazardmind-hazard",
                "event_id": event_id,
                "status": "error",
                "error": str(e),
            }
        )
        await send_message(HANAN_HANDLE, error_msg)
        return {"status": "error", "reason": str(e)}


async def analyze_hazard_tool(satellite_payload: dict) -> dict:
    """Band tool entrypoint. Runs analysis and posts the handoff to Hanan via Band."""

    async def send_message(handle: str, message: str) -> None:
        await send_to_band(message, agent_id=HANAN_AGENT_ID)

    return await analyze_hazard(satellite_payload, send_message)


SYSTEM_PROMPT = """You are HazardMind Hazard Detection Agent (@khurramhamza120/hazardmind-hazard).
You are Agent 2 in a 4-agent disaster response pipeline.
When @mentioned by the Satellite Agent, extract the JSON payload from the message.
Parse event_id, boundaries, analysis, and artifacts fields.
Call analyze_hazard with the full parsed payload immediately.
Always include event_id in every response.
If overall_severity is CRITICAL flag it explicitly.
Never go silent — if analysis fails send error status to Hanan."""

adapter = AnthropicAdapter(
    model="claude-opus-4-8",
    provider_key=os.getenv("AIML_API_KEY"),
    system_prompt=SYSTEM_PROMPT,
    max_tokens=4096,
)

if __name__ == "__main__":
    agent = Agent.create(
        adapter=adapter,
        agent_id=os.getenv("BAND_AGENT_ID"),
        api_key=os.getenv("BAND_API_KEY"),
        ws_url=THENVOI_WS_URL,
        rest_url=THENVOI_REST_URL,
    )
    asyncio.run(agent.run())
