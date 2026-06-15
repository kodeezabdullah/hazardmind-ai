"""Determine routing criticality from task output data."""

import logging

logger = logging.getLogger(__name__)


def determine_criticality(task_name: str, data: dict) -> str:
    """Return 'low' | 'normal' | 'high' | 'critical' based on task output."""

    if task_name == "population":
        pop = data.get("population_affected", data.get("population_count", 0))
        if pop > 1_000_000:
            level = "critical"
        elif pop > 500_000:
            level = "high"
        elif pop > 100_000:
            level = "normal"
        else:
            level = "low"

    elif task_name == "infrastructure":
        osm_text = str(data.get("osm_assets", "")).lower()
        if any(k in osm_text for k in ["dam", "nuclear", "power_plant"]):
            level = "critical"
        elif data.get("hospitals_at_risk", 0) > 10:
            level = "high"
        elif data.get("hospitals_at_risk", 0) > 5:
            level = "normal"
        else:
            level = "low"

    elif task_name == "vulnerability":
        if data.get("all_routes_blocked"):
            level = "critical"
        elif data.get("vulnerability_score", 0) > 8.0:
            level = "high"
        elif data.get("vulnerability_score", 0) > 5.0:
            level = "normal"
        else:
            level = "low"

    else:
        logger.warning("determine_criticality: unknown task_name '%s' → normal", task_name)
        level = "normal"

    logger.info("[criticality] task=%s level=%s data_keys=%s", task_name, level, list(data.keys()))
    return level
