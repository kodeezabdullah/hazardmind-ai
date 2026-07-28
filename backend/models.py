from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    location: str
    disaster_type: str = Field(..., description="flood | earthquake | landslide")
    magnitude: Optional[float] = None
    # Coverage-tolerance / search-budget overrides (2026-07-28,
    # fix/coverage-tolerance). All optional; defaults match
    # agents/satellite/processor.py's own defaults, which remain the ultimate
    # fallback when a caller supplies nothing. min_coverage_percent is
    # clamped server-side into [80, 100] regardless of what's sent here.
    min_coverage_percent: Optional[float] = 90.0
    max_scenes: Optional[int] = 3
    max_download_gb: Optional[float] = 4.0
    max_search_seconds: Optional[float] = 900.0


class AnalyzeResponse(BaseModel):
    job_id: str
    status: str
    message: str


class StatusResponse(BaseModel):
    job_id: str
    status: str
    step: str
    progress: int = Field(..., ge=0, le=100)
    created_at: datetime
    updated_at: datetime


class ResultsResponse(BaseModel):
    job_id: str
    status: str
    satellite: Optional[dict] = None
    hazard: Optional[dict] = None
    impact: Optional[dict] = None
    report: Optional[dict] = None


class PipelineLogResponse(BaseModel):
    job_id: str
    status: str
    step: str
    errors: List[dict]
    anomalies: List[dict]
    confidence_scores: dict


class EvidenceResponse(BaseModel):
    """Full durable-evidence trail for a past event (feat/durable-evidence-trail).

    Distinct from ResultsResponse: /results returns the summary fields a
    frontend renders; this returns every diagnostics/confidence/provenance
    field the durable-evidence-trail migration added, across satellite,
    hazard (one entry per hazard_type) and impact — so a past run can be
    audited (e.g. "why LOW?") without re-running the pipeline. A NULL/None
    value anywhere in these dicts means "not recorded" (the row predates
    this migration, or the field never reached a DB column) — never coerce
    it to 0/False; callers must render it as unknown.
    """

    job_id: str
    disaster_type: Optional[str] = None
    location: Optional[str] = None
    status: str
    step: str
    satellite: Optional[dict] = None
    hazard_zones: Optional[List[dict]] = None
    impact: Optional[dict] = None
