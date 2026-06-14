import asyncio
import json
import os
import uuid as uuid_lib
from datetime import datetime, timezone

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from band import Agent
from band.adapters import AnthropicAdapter
from band.config import load_agent_config

from analyzer import run_parallel_analysis
from intelligence import quality_check, write_band_message, interpret_results


load_dotenv()

try:
    config = load_agent_config("agent_config.yaml")
except FileNotFoundError:
    config = (os.getenv("BAND_AGENT_ID"), os.getenv("BAND_API_KEY", ""))

engine = create_engine(os.getenv("NEON_DATABASE_URL"))


class AnalyzeHazardInput(BaseModel):
    event_id: str
    bbox: list
    affected_area_km2: float
    mean_value: float
    geojson_url: str = ""
    risk_cities: list = []


def write_to_db(result: dict) -> None:
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO hazard_zones
              (event_id, flood_risk, earthquake_risk, landslide_risk,
               overall_severity, created_at)
            VALUES
              (CAST(:event_id AS uuid), :flood_risk, :earthquake_risk, :landslide_risk,
               :overall_severity, :created_at)
        """), {
            "event_id": str(result["event_id"]),
            "flood_risk": result["flood_risk"],
            "earthquake_risk": result["earthquake_risk"],
            "landslide_risk": result["landslide_risk"],
            "overall_severity": result["overall_severity"],
            "created_at": datetime.now(timezone.utc)
        })
        conn.commit()


try:
    adapter = AnthropicAdapter(
        api_key=os.getenv("AIML_API_KEY"),
        base_url="https://api.aimlapi.com/v1",
        model="claude-opus-4-8",
    )
except TypeError:
    adapter = AnthropicAdapter(
        api_key=os.getenv("AIML_API_KEY"),
        model="claude-opus-4-8",
    )
    adapter.client = AsyncAnthropic(
        api_key=os.getenv("AIML_API_KEY"),
        base_url="https://api.aimlapi.com/v1",
    )

try:
    agent = Agent(
        agent_id=os.getenv("BAND_AGENT_ID"),
        adapter=adapter,
        config=config,
    )
except TypeError:
    config_agent_id, config_api_key = config
    agent = Agent.create(
        agent_id=os.getenv("BAND_AGENT_ID") or config_agent_id,
        api_key=os.getenv("BAND_API_KEY") or config_api_key,
        adapter=adapter,
        ws_url=os.getenv("THENVOI_WS_URL", "wss://app.band.ai/api/v1/socket/websocket"),
        rest_url=os.getenv("THENVOI_REST_URL", "https://app.band.ai"),
    )

HANAN_HANDLE = "TBD_HANAN_HANDLE"  # replace when known


if not hasattr(agent, "tool"):
    def _register_tool(func):
        if hasattr(adapter, "_custom_tools"):
            adapter._custom_tools.append((AnalyzeHazardInput, func))
        setattr(agent, func.__name__, func)
        return func

    agent.tool = _register_tool


if not hasattr(agent, "send"):
    async def _send(handle: str, message: str) -> None:
        print(f"Message to {handle}: {message}")

    agent.send = _send


_agent_run = agent.run


def _run_with_system_prompt(system_prompt: str = "", **kwargs):
    if system_prompt:
        adapter.system_prompt = system_prompt
    return asyncio.run(_agent_run(**kwargs))


agent.run = _run_with_system_prompt


@agent.tool
async def analyze_hazard(input: AnalyzeHazardInput) -> dict:
    satellite_data = {
        "event_id": input.event_id,
        "boundaries": {"bbox": input.bbox, "risk_cities": input.risk_cities},
        "analysis": {
            "affected_area_km2": input.affected_area_km2,
            "mean_value": input.mean_value,
        },
        "artifacts": {"geojson_url": input.geojson_url},
    }

    raw_result = await run_parallel_analysis(satellite_data)

    qc = await quality_check(raw_result)
    if not qc["passed"]:
        await agent.send(
            HANAN_HANDLE,
            json.dumps(
                {
                    "agent": "hazardmind-hazard",
                    "event_id": input.event_id,
                    "status": "error",
                    "error": qc,
                }
            ),
        )
        return {"status": "error", "reason": "quality check failed"}

    try:
        write_to_db(raw_result)
    except Exception as e:
        await agent.send(
            HANAN_HANDLE,
            json.dumps(
                {
                    "agent": "hazardmind-hazard",
                    "event_id": input.event_id,
                    "status": "error",
                    "error": str(e),
                }
            ),
        )
        return {"status": "error", "reason": f"db write failed: {e}"}

    payload = {
        "agent": "hazardmind-hazard",
        "event_id": input.event_id,
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
    await agent.send(HANAN_HANDLE, full_message)

    return payload


if __name__ == "__main__":
    agent.run(
        system_prompt="""You are HazardMind Hazard Detection Agent (@khurramhamza120/hazardmind-hazard).
You are Agent 2 in a 4-agent disaster response pipeline.
When @mentioned by the Satellite Agent, extract event_id, bbox, affected_area_km2, mean_value, geojson_url from the message and call analyze_hazard tool immediately.
Always include event_id in every response.
If overall_severity is CRITICAL, flag it explicitly.
Never go silent — if analysis fails send error status to Hanan."""
    )
