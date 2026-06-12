from dataclasses import dataclass
from typing import Optional, List
from datetime import datetime

@dataclass
class DisasterEvent:
    id: str
    disaster_type: str
    location: str
    bbox: List[float]
    created_at: datetime

@dataclass
class BandMessage:
    agent: str
    event_id: str
    status: str
    timestamp: str
    data: dict
    error: Optional[str] = None

@dataclass
class SatelliteData:
    image_url: str
    affected_area_km2: float
    land_cover: str
    bbox: List[float]

@dataclass
class HazardData:
    flood_risk: str
    earthquake_risk: str
    landslide_risk: str
    overall_severity: str
    risk_polygons_id: str

@dataclass
class ImpactData:
    population_affected: int
    hospitals_at_risk: int
    roads_blocked_km: float
    schools_affected: int
    vulnerability_score: float

@dataclass
class ReportData:
    map_url: str
    pdf_url: str
    summary: str
