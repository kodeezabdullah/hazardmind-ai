from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    location: str
    disaster_type: str = Field(..., description="flood | earthquake | landslide")
    magnitude: Optional[float] = None


class AnalyzeResponse(BaseModel):
    job_id: str
    status: str
    message: str
    band_room_id: Optional[str] = None


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


class BandLogResponse(BaseModel):
    job_id: str
    messages: List[dict]
