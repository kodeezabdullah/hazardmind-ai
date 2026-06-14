from anthropic import AsyncAnthropic
from band import Agent, AgentConfig
from band.adapters import AnthropicAdapter
import asyncio, json, os
from datetime import datetime, timezone
from dotenv import load_dotenv
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from analyzer import run_parallel_analysis
from intelligence import quality_check, write_band_message

load_dotenv()

HANAN_HANDLE = "TBD_HANAN_HANDLE"
BAND_AGENT_ID = os.getenv("BAND_AGENT_ID")
AIML_API_KEY = os.getenv("AIML_API_KEY")
NEON_DATABASE_URL = os.getenv("NEON_DATABASE_URL")
BAND_API_KEY = os.getenv("BAND_API_KEY", "")
THENVOI_REST_URL = os.getenv("THENVOI_REST_URL", "https://app.band.ai")
THENVOI_WS_URL = os.getenv("THENVOI_WS_URL", "wss://app.band.ai/api/v1/socket/websocket")

engine = create_engine(NEON_DATABASE_URL)

def write_to_db(result: dict) -> None:
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO hazard_zones
              (event_id, flood_risk, earthquake_risk, landslide_risk,
               overall_severity, created_at)
            VALUES
              (CAST(:event_id AS uuid), :flood_risk, :earthquake_risk,
               :landslide_risk, :overall_severity, :created_at)
        """), {
            "event_id": str(result["event_id"]),
            "flood_risk": result["flood_risk"],
            "earthquake_risk": result["earthquake_risk"],
            "landslide_risk": result["landslide_risk"],
            "overall_severity": result["overall_severity"],
            "created_at": datetime.now(timezone.utc)
        })
        conn.commit()

async def analyze_hazard(satellite_payload: dict, send_message) -> dict:
    event_id = satellite_payload.get("event_id", "unknown")
    try:
        satellite_data = {
            "event_id": event_id,
            "boundaries": satellite_payload.get("boundaries", {"bbox": [], "risk_cities": []}),
            "analysis": satellite_payload.get("analysis", {"affected_area_km2": 0, "mean_value": 0}),
            "artifacts": satellite_payload.get("artifacts", {})
        }

        raw_result = await run_parallel_analysis(satellite_data)

        qc = await quality_check(raw_result)
        if not qc["passed"]:
            error_msg = json.dumps({"agent":"hazardmind-hazard","event_id":event_id,"status":"error","error":"quality check failed"})
            await send_message(HANAN_HANDLE, error_msg)
            return {"status": "error"}

        write_to_db(raw_result)

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
                "risk_polygons_url": ""
            },
            "error": None
        }

        message = await write_band_message(raw_result, HANAN_HANDLE)
        full_message = message + "\n\n" + json.dumps(payload, indent=2)
        await send_message(HANAN_HANDLE, full_message)

        return payload

    except Exception as e:
        error_msg = json.dumps({"agent":"hazardmind-hazard","event_id":event_id,"status":"error","error":str(e)})
        await send_message(HANAN_HANDLE, error_msg)
        return {"status": "error", "reason": str(e)}


class AnalyzeHazardInput(BaseModel):
    satellite_payload: dict


async def analyze_hazard_tool(input: AnalyzeHazardInput) -> dict:
    async def send_message(handle: str, message: str) -> None:
        print(f"Message to {handle}: {message}")

    return await analyze_hazard(input.satellite_payload, send_message)

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
    max_tokens=4096
)

if __name__ == "__main__":
    agent = Agent.create(
        adapter=adapter,
        agent_id=os.getenv("BAND_AGENT_ID"),
        api_key=os.getenv("BAND_API_KEY"),
        ws_url=os.getenv("THENVOI_WS_URL", "wss://app.band.ai/api/v1/socket/websocket"),
        rest_url=os.getenv("THENVOI_REST_URL", "https://app.band.ai/")
    )
    import asyncio
    asyncio.run(agent.run())
