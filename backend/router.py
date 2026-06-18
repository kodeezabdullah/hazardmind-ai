import asyncio
import logging
import os
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from band_client import create_event_room, inbound_store
from db import (
    create_disaster_event,
    get_event_results,
    get_event_status,
)
from orchestrator import OrchestratorAgent
from models import (
    AnalyzeRequest,
    AnalyzeResponse,
    BandLogResponse,
    ResultsResponse,
    StatusResponse,
)

router = APIRouter()

logger = logging.getLogger("hazardmind.router")

# Single orchestrator instance shared across requests.
orchestrator = OrchestratorAgent()


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(request: AnalyzeRequest):
    # event_id is generated ONCE here and reused by every agent.
    event_id = str(uuid.uuid4())

    disaster_data = {
        "location": request.location,
        "disaster_type": request.disaster_type,
        "magnitude": request.magnitude,
    }

    # Per-event Band room.
    #
    # DYNAMIC_BAND_ROOMS (default OFF) gates a per-event room. It is OFF because
    # the orchestrator's Band API key can only populate a room it creates with
    # *same-owner* agents (the satellite agent auto-joins; it shares the
    # orchestrator's owner). The hazard/impact/report agents belong to different
    # Band owner accounts and CANNOT be added by this key — an explicit add is
    # 403 and @mentioning a non-member is 422 (verified live). So a dynamic room
    # would never receive those agents and the pipeline would hang. Until all
    # pipeline agents share one Band owner (or Band exposes an invite flow this
    # key can drive), /analyze uses the shared static BAND_ROOM_ID that all five
    # agents were manually invited into. See backend/CLAUDE.md "Dynamic rooms".
    room_id = os.getenv("BAND_ROOM_ID")
    if os.getenv("DYNAMIC_BAND_ROOMS", "false").strip().lower() in ("1", "true", "yes"):
        try:
            room_id = await create_event_room(event_id, request.location)
        except Exception:  # noqa: BLE001 - dynamic room is best-effort
            logger.exception(
                "event_id=%s: room creation failed; using static room", event_id
            )
            room_id = os.getenv("BAND_ROOM_ID")

    await create_disaster_event(
        event_id=event_id,
        location=request.location,
        disaster_type=request.disaster_type,
        magnitude=request.magnitude,
        band_room_id=room_id,
    )

    # Hand off to the orchestrator: sets status -> processing/satellite and
    # mentions the satellite agent in the event's Band room.
    await orchestrator.start_pipeline(event_id, disaster_data, room_id=room_id)

    # Watch the pipeline in the background so the request returns immediately.
    asyncio.create_task(_monitor(event_id, room_id))

    return AnalyzeResponse(
        job_id=event_id,
        status="processing",
        band_room_id=room_id,
        message="Pipeline started",
    )


async def _monitor(event_id: str, room_id: str | None = None) -> None:
    try:
        await orchestrator.monitor_progress(event_id, room_id=room_id)
    except Exception:  # noqa: BLE001 - background task must not crash silently
        logger.exception("monitor_progress failed for event_id=%s", event_id)


@router.get("/status/{job_id}", response_model=StatusResponse)
async def get_status(job_id: str):
    event = await get_event_status(job_id)
    if event is None:
        raise HTTPException(status_code=404, detail="job not found")

    return StatusResponse(
        job_id=str(event["event_id"]),
        status=event["status"],
        step=event["step"],
        progress=event["progress"],
        created_at=event["created_at"],
        updated_at=event["updated_at"],
    )


@router.get("/results/{job_id}", response_model=ResultsResponse)
async def get_results(job_id: str):
    event = await get_event_results(job_id)
    if event is None:
        raise HTTPException(status_code=404, detail="job not found")

    if event["status"] != "complete":
        return JSONResponse(
            status_code=202,
            content={
                "status": "processing",
                "step": event["step"],
                "message": "Pipeline still running",
            },
        )

    return ResultsResponse(
        job_id=str(event["event_id"]),
        status="complete",
        satellite=event["satellite"],
        hazard=event["hazard"],
        impact=event["impact"],
        report=event["report"],
    )


@router.get("/band-log/{job_id}", response_model=BandLogResponse)
async def get_band_log(job_id: str):
    event = await get_event_status(job_id)
    if event is None:
        raise HTTPException(status_code=404, detail="job not found")

    # Inbound messages are buffered by the orchestrator's recording adapter;
    # Band's REST history is empty for this agent.
    messages = [
        {
            "agent": msg.get("agent"),
            "content": msg.get("content", ""),
            "timestamp": msg.get("timestamp"),
            "type": msg.get("type", "text"),
        }
        for msg in inbound_store.for_event(job_id)
    ]

    return BandLogResponse(job_id=job_id, messages=messages)
