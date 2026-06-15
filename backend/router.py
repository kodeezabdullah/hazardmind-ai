import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from band_client import inbound_store
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

    await create_disaster_event(
        event_id=event_id,
        location=request.location,
        disaster_type=request.disaster_type,
        magnitude=request.magnitude,
    )

    # Hand off to the orchestrator: sets status -> processing/satellite and
    # mentions the satellite agent on Band.
    await orchestrator.start_pipeline(event_id, disaster_data)

    # Watch the pipeline in the background so the request returns immediately.
    asyncio.create_task(_monitor(event_id))

    return AnalyzeResponse(
        job_id=event_id,
        status="processing",
        message="Pipeline started",
    )


async def _monitor(event_id: str) -> None:
    try:
        await orchestrator.monitor_progress(event_id)
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
