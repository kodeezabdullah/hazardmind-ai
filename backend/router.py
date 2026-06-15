import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from band_client import notify_satellite
from db import create_disaster_event, get_event_results, get_event_status
from models import (
    AnalyzeRequest,
    AnalyzeResponse,
    BandLogResponse,
    ResultsResponse,
    StatusResponse,
)

router = APIRouter()


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(request: AnalyzeRequest):
    # event_id is generated ONCE here and reused by every agent.
    event_id = str(uuid.uuid4())

    await create_disaster_event(
        event_id=event_id,
        location=request.location,
        disaster_type=request.disaster_type,
        magnitude=request.magnitude,
    )

    await notify_satellite(
        event_id=event_id,
        location=request.location,
        disaster_type=request.disaster_type,
        magnitude=request.magnitude,
    )

    return AnalyzeResponse(
        job_id=event_id,
        status="received",
        message="Pipeline started",
    )


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
    # TODO: fetch Band conversation transcript
    raise HTTPException(status_code=501, detail="band-log endpoint not implemented yet")
