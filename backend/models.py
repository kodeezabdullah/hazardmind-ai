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
