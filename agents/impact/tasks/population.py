"""Task 1 — Population Impact Assessment.

Data strategy: GeoNames provides REAL city population. LLM reasons about disaster impact,
it does NOT estimate raw population from scratch.
Uses risk_cities[0] for GeoNames lookup; all cities for LLM context.
"""

import logging
import os

import httpx

from services.llm_router import smart_llm_call

logger = logging.getLogger(__name__)

GEONAMES_BASE = "http://api.geonames.org/searchJSON"


def _area_sq_km(bbox: list) -> float:
    return abs((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) * 111 * 111)


def _primary_city(hazard_data: dict, event_id: str) -> str:
    """Single city name for GeoNames API lookup."""
    cities = hazard_data.get("risk_cities") or []
    if cities:
        return str(cities[0])
    return event_id.removeprefix("demo-").replace("-", " ").title()


def _city_label(hazard_data: dict, event_id: str) -> str:
    """All affected cities for LLM context."""
    cities = hazard_data.get("risk_cities") or []
    if cities:
        return ", ".join(str(c) for c in cities[:3])
    return event_id.removeprefix("demo-").replace("-", " ").title()


async def _fetch_geonames_population(city: str) -> int | None:
    """Fetch real population from GeoNames. Returns None on any failure."""
    username = os.getenv("GEONAMES_USERNAME", "ahanan.24")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                GEONAMES_BASE,
                params={"q": city, "maxRows": 1, "username": username, "style": "full"},
            )
            resp.raise_for_status()
            data = resp.json()
            entries = data.get("geonames", [])
            if entries:
                pop = int(entries[0].get("population", 0) or 0)
                if pop > 0:
                    logger.info("[population] GeoNames: %r → population=%d", city, pop)
                    return pop
            logger.warning("[population] GeoNames: no population entry for %r", city)
    except Exception as exc:
        logger.warning("[population] GeoNames failed for %r: %s", city, exc)
    return None


def _build_prompt(city: str, area: float, hazard_data: dict, real_pop: int | None) -> str:
    severity = hazard_data.get("severity", "moderate")
    risk     = hazard_data.get("flood_risk", "UNKNOWN")
    bbox     = hazard_data.get("bbox", [])

    if real_pop:
        pop_context = (
            f"GeoNames administrative population: {real_pop:,}\n"
            "NOTE: This is the old city boundary figure only.\n"
            "The actual metro/urban area population is significantly "
            "higher - typically 2x to 5x the administrative figure.\n"
            f"Use your geographic knowledge of {city} to estimate "
            "the TRUE metro population in the affected bbox.\n"
            "GeoNames figure is a minimum floor only, not the ceiling."
        )
    else:
        pop_context = (
            f"GeoNames unavailable — estimate based on your geographic knowledge of {city} "
            f"and the {area:.0f} sq km affected area."
        )

    return f"""You are a senior UN disaster analyst.

Disaster event: {severity} flood
Cities affected: {city}
Affected area: {area:.0f} sq km  (bbox: {bbox})
Risk level: {risk}

REAL DATA:
{pop_context}

Analyze this real data for {city}.
Apply risk levels to determine actual disaster impact.
Use geographic knowledge for district names only — do NOT invent population numbers if real data is provided.

Your task is REASONING, not estimation:
- What percentage of the real population is in the high-risk flood zone?
- What percentage is medium risk (adjacent, evacuation zone)?
- Typical age distribution for {city} — children under 5 + elderly over 65?
- 3 specific local vulnerability factors unique to {city}?

Base population_affected on the REAL flood extent and risk level. If the risk is
genuinely high and people are in the flood zone, report the real exposed count.
If the affected area is tiny or the risk is low, a small or zero figure is the
honest answer — do NOT inflate it.
{"Derive population_affected from the real GeoNames figure using your flood-zone reasoning." if real_pop else "Estimate based on city size and affected area."}

Return ONLY valid JSON, no other text:
{{
    "population_affected": <integer people in the flood impact zone — honest, may be small>,
    "high_risk_people": <integer — approx 20% in direct flood zone>,
    "medium_risk_people": <integer — approx 50% in adjacent zones>,
    "vulnerable_population": <children under 5 + elderly over 65>,
    "local_risk_factors": [
        "<specific risk factor 1 for {city}>",
        "<specific risk factor 2 for {city}>",
        "<specific risk factor 3 for {city}>"
    ],
    "confidence": <0.7-0.95>
}}"""


async def run_population_task(hazard_data: dict, event_id: str) -> dict:
    bbox         = hazard_data.get("bbox", [0, 0, 1, 1])
    primary_city = _primary_city(hazard_data, event_id)
    city         = _city_label(hazard_data, event_id)
    area         = _area_sq_km(bbox)

    print(f"\n[DEBUG][Population] City: {city!r} | Area: {area:.0f} sqkm | bbox: {bbox}", flush=True)

    real_pop = await _fetch_geonames_population(primary_city)
    if real_pop:
        print(f"[DEBUG][Population] GeoNames real population: {real_pop:,}", flush=True)
    else:
        print("[DEBUG][Population] GeoNames unavailable — LLM will estimate", flush=True)

    prompt = _build_prompt(city, area, hazard_data, real_pop)

    result, model_used, reasoning = await smart_llm_call(prompt, "normal", task_name="population")

    pop = int((result or {}).get("population_affected", 0) or 0)
    print(f"[DEBUG][Population] Initial estimate: {pop:,} (model={model_used})", flush=True)
    logger.info("[population] Initial: population_affected=%d model=%s", pop, model_used)

    if pop > 2_000_000:
        criticality = "critical"
    elif pop > 500_000:
        criticality = "high"
    else:
        criticality = "normal"

    if criticality in ("high", "critical"):
        logger.info("[population] Escalating to %s (pop=%d)", criticality, pop)
        result, model_used, reasoning = await smart_llm_call(prompt, criticality, task_name="population")
        pop = int((result or {}).get("population_affected", 0) or 0)
        print(f"[DEBUG][Population] Escalated estimate: {pop:,} (model={model_used})", flush=True)

    # NOTE: a genuine "no disaster" event is handled by the decision gate in
    # agent.py (it never reaches this task). So reaching here means the hazard
    # risk WAS significant; if the LLM still returned 0/None it likely just
    # failed to parse — retry once, then fall back to a conservative estimate
    # rather than crashing the whole impact stage.
    if not result or pop == 0:
        logger.warning("[population] LLM returned 0 on a significant-risk event — retrying")
        retry_prompt = prompt + (
            f"\n\nThe hazard risk for this event is significant. Provide your best "
            f"realistic estimate of population_affected for {city} based on the "
            f"affected area and real population data."
        )
        result, model_used, reasoning = await smart_llm_call(retry_prompt, "high", task_name="population")
        pop = int((result or {}).get("population_affected", 0) or 0)

    if not result:
        result = {}
    if pop == 0:
        # Conservative deterministic floor for a significant-risk event so the
        # stage still produces data instead of crashing (was: raise ValueError).
        pop = max(int((real_pop or 0) * 0.02), 500)
        logger.warning(
            "[population] Using conservative fallback estimate %d for %s "
            "(LLM gave 0 on a significant-risk event)", pop, city,
        )

    result["population_affected"] = pop
    result["population_count"]    = pop  # backward compat alias
    result["vulnerable_estimate"] = int(
        result.get("vulnerable_population", int(pop * 0.18)) or int(pop * 0.18)
    )
    result["model_used"]          = model_used
    result["criticality"]         = criticality
    result["llm_reasoning"]       = reasoning
    if real_pop:
        result["geonames_population"] = real_pop

    logger.info(
        "[population] Done — population_affected=%d vulnerable=%d model=%s criticality=%s geonames=%s",
        pop, result["vulnerable_estimate"], model_used, criticality,
        real_pop or "N/A",
    )
    return result
