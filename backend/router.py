import uuid

from fastapi import APIRouter, HTTPException

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
    event_id = str(uuid.uuid4())
    # TODO: persist to disaster_events and kick off orchestrator
    return AnalyzeResponse(
        job_id=event_id,
        status="received",
        message="Pipeline queued (stub).",
    )


@router.get("/status/{job_id}", response_model=StatusResponse)
async def get_status(job_id: str):
    # TODO: read from disaster_events
    raise HTTPException(status_code=501, detail="status endpoint not implemented yet")


@router.get("/results/{job_id}", response_model=ResultsResponse)
async def get_results(job_id: str):
    # TODO: aggregate satellite/hazard/impact/report rows
    raise HTTPException(status_code=501, detail="results endpoint not implemented yet")


@router.get("/band-log/{job_id}", response_model=BandLogResponse)
async def get_band_log(job_id: str):
    # TODO: fetch Band conversation transcript
    raise HTTPException(status_code=501, detail="band-log endpoint not implemented yet")
