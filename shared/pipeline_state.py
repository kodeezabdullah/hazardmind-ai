"""Shared LangGraph state for the satellite -> hazard -> impact -> report pipeline.

Replaces the Band room + DB-fallback-read hand-off with a single in-memory
StateGraph state passed node-to-node. DB writes remain a side effect of each
node (the durable record), not the hand-off mechanism itself. See CLAUDE.md
"PipelineState Schema" for the authoritative spec and per-field rules.
"""

from typing import Literal, Optional, TypedDict

from typing_extensions import NotRequired


class PipelineState(TypedDict):
    # Identity — set once by backend, never regenerated
    event_id: str  # full UUID, generated once in backend/router.py
    location: str
    disaster_type: Literal["flood", "earthquake", "landslide"]
    magnitude: NotRequired[Optional[float]]

    # Coverage-tolerance / search-budget overrides (2026-07-28,
    # fix/coverage-tolerance), threaded from AnalyzeRequest through to
    # agents/satellite/processor.py's process_satellite_imagery. All
    # optional — the innermost function's own defaults are the ultimate
    # fallback, never hardcoded anywhere along this chain.
    min_coverage_percent: NotRequired[Optional[float]]
    max_scenes: NotRequired[Optional[int]]
    max_download_gb: NotRequired[Optional[float]]
    max_search_seconds: NotRequired[Optional[float]]

    # Per-stage results (populated as the graph advances; None until that node runs)
    satellite_result: NotRequired[Optional[dict]]   # see satellite_results DB columns
    hazard_result: NotRequired[Optional[dict]]       # see hazard_zones DB columns (3 rows: flood/eq/landslide)
    impact_result: NotRequired[Optional[dict]]       # see impact_data DB columns
    report_result: NotRequired[Optional[dict]]       # see final_reports DB columns

    # Pipeline control
    status: Literal["received", "satellite", "hazard", "impact", "report", "complete", "failed"]
    current_step: str
    progress: int  # 0-100, mirrors backend StatusResponse.progress

    # Cross-cutting
    errors: NotRequired[list[dict]]        # [{stage, error, timestamp}], accumulate, never overwrite
    anomalies: NotRequired[list[dict]]      # cross_validate_and_discuss findings, carried forward
    confidence_scores: NotRequired[dict]    # {satellite, hazard, impact, report} — each stage's self-reported confidence

    created_at: NotRequired[str]
    updated_at: NotRequired[str]
