import asyncio
import logging
import os
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from db import (
    count_active_events,
    create_disaster_event,
    get_event_evidence,
    get_event_results,
    get_event_status,
    get_pipeline_log,
    update_event_status,
)
from orchestrator import OrchestratorAgent
from models import (
    AnalyzeRequest,
    AnalyzeResponse,
    EvidenceResponse,
    PipelineLogResponse,
    ResultsResponse,
    StatusResponse,
)

router = APIRouter()

logger = logging.getLogger("hazardmind.router")

# Single orchestrator instance shared across requests.
orchestrator = OrchestratorAgent()


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(request: AnalyzeRequest):
    # Concurrency cap: the Featherless free tier allows only 4 concurrency units
    # and each LLM request costs 2, so more than 2 events running at once causes
    # 429 "concurrency limit exceeded" failures (the report agent in particular).
    # We cap to MAX_CONCURRENT_EVENTS (default 2). Rather than fail the request
    # (which the frontend would surface as an error), we WAIT here until a slot is
    # free, so the frontend just sees a slightly slower /analyze and never errors.
    max_concurrent = int(os.getenv("MAX_CONCURRENT_EVENTS", "2"))
    wait_deadline = asyncio.get_event_loop().time() + 8 * 60  # cap the wait at 8 min
    while await count_active_events() >= max_concurrent:
        if asyncio.get_event_loop().time() > wait_deadline:
            break  # don't hold the request forever; let it through after the cap
        await asyncio.sleep(5)

    # event_id is generated ONCE here and reused by every agent via PipelineState.
    event_id = str(uuid.uuid4())

    disaster_data = {
        "location": request.location,
        "disaster_type": request.disaster_type,
        "magnitude": request.magnitude,
        # Coverage-tolerance / search-budget overrides (fix/coverage-tolerance).
        "min_coverage_percent": request.min_coverage_percent,
        "max_scenes": request.max_scenes,
        "max_download_gb": request.max_download_gb,
        "max_search_seconds": request.max_search_seconds,
    }

    await create_disaster_event(
        event_id=event_id,
        location=request.location,
        disaster_type=request.disaster_type,
        magnitude=request.magnitude,
    )

    # Run the graph in the background so the request returns immediately; the
    # frontend polls /status and /results the same way it always has.
    asyncio.create_task(_run_pipeline(event_id, disaster_data))

    return AnalyzeResponse(
        job_id=event_id,
        status="processing",
        message="Pipeline started",
    )


async def _run_pipeline(event_id: str, disaster_data: dict) -> None:
    try:
        await orchestrator.start_pipeline(event_id, disaster_data)
    except Exception:  # noqa: BLE001 - background task must not crash silently
        logger.exception("start_pipeline failed for event_id=%s", event_id)
        try:
            await update_event_status(event_id, status="failed", step="failed")
        except Exception:  # noqa: BLE001
            pass


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


@router.get("/results/{job_id}/evidence", response_model=EvidenceResponse)
async def get_results_evidence(job_id: str):
    """Return the full durable-evidence trail for a past event.

    A new, additive endpoint rather than an expansion of GET /results:
    /results is the summary the frontend already renders on every poll, and
    its ResultsResponse shape is a load-bearing contract other readers may
    already depend on. The evidence trail (confidence/coverage/provenance/
    diagnostics — everything the durable-evidence-trail migration added) is
    materially larger and is read far less often (an audit/"why LOW?" case,
    not every poll), so it gets its own endpoint instead of bloating every
    /results response with fields most callers never look at.

    Available regardless of whether the event has finished (satellite/
    hazard/impact rows are readable as soon as each stage completes) — this
    endpoint does not gate on status == "complete" the way /results does,
    since an evidence trail for a partially-run event is still useful for
    debugging why a stage failed.
    """
    event = await get_event_evidence(job_id)
    if event is None:
        raise HTTPException(status_code=404, detail="job not found")

    return EvidenceResponse(
        job_id=str(event["event_id"]),
        disaster_type=event.get("disaster_type"),
        location=event.get("location"),
        status=event["status"],
        step=event["step"],
        satellite=event.get("satellite"),
        hazard_zones=event.get("hazard_zones"),
        impact=event.get("impact"),
    )


@router.get("/pipeline-log/{job_id}", response_model=PipelineLogResponse)
async def get_pipeline_log_route(job_id: str):
    """Return the LangGraph run's errors/anomalies/confidence_scores trail.

    Replaces GET /band-log now that there is no Band transcript. Reads the
    pipeline_log persisted by orchestrator.start_pipeline() at the end of the
    run rather than a live message store.
    """
    event = await get_pipeline_log(job_id)
    if event is None:
        raise HTTPException(status_code=404, detail="job not found")

    log = event.get("pipeline_log") or {}
    return PipelineLogResponse(
        job_id=str(event["event_id"]),
        status=event["status"],
        step=event["step"],
        errors=log.get("errors") or [],
        anomalies=log.get("anomalies") or [],
        confidence_scores=log.get("confidence_scores") or {},
    )
