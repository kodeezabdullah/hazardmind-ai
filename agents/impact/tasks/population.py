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


_DISASTER_LABELS = {
    "flood": "flood",
    "earthquake": "earthquake",
    "landslide": "landslide",
}


def _build_prompt(
    city: str,
    area: float,
    hazard_data: dict,
    real_pop: int | None,
    exposure: dict | None = None,
) -> str:
    severity = hazard_data.get("severity", "moderate")
    disaster_type = str(hazard_data.get("disaster_type") or "flood").lower()
    disaster_label = _DISASTER_LABELS.get(disaster_type, disaster_type)
    # Read the risk level for the ACTUAL disaster type, not always flood_risk —
    # mirrors vulnerability.py's existing correct flood/eq/ls branching (H#1
    # Part B: this prompt previously hardcoded "flood" regardless of the real
    # disaster_type, misleading the LLM on every earthquake/landslide event).
    risk_key = {
        "flood": "flood_risk",
        "earthquake": "earthquake_risk",
        "landslide": "landslide_risk",
    }.get(disaster_type, "flood_risk")
    risk = hazard_data.get(risk_key, "UNKNOWN")
    bbox = hazard_data.get("bbox", [])

    # Phase 6a: when a real gridded exposure figure exists it is stated as
    # AUTHORITATIVE and the model is told not to re-estimate it. The old
    # "2x to 5x the administrative figure" instruction is gone on this path
    # — that multiplier was asserted in prompt text, never computed, and was
    # the mechanism by which an LLM produced the number every NDMA response
    # threshold depends on.
    if exposure and exposure.get("population") is not None:
        pop_context = (
            f"AUTHORITATIVE population exposure: {exposure['population']:,}\n"
            f"Source: {exposure.get('source')} "
            f"({exposure.get('method')}), {exposure.get('pixels')} populated "
            f"cells inside the hazard extent "
            f"({exposure.get('polygon_area_km2')} sq km).\n"
            "This figure was computed geospatially by intersecting a gridded "
            "population raster with the ACTUAL hazard extent polygon. Use it "
            "as population_affected verbatim — do NOT re-estimate it, do NOT "
            "scale it by an urbanisation multiplier, and do NOT substitute "
            "your own geographic knowledge for it. Your job here is to "
            "INTERPRET this number (who is at risk, what it implies for "
            "response), not to produce it.\n"
            + (
                f"GeoNames administrative population, for context only: {real_pop:,}\n"
                if real_pop
                else ""
            )
        )
    elif real_pop:
        pop_context = (
            f"GeoNames administrative population: {real_pop:,}\n"
            "NOTE: This is the old city boundary figure only.\n"
            "The actual metro/urban area population is significantly "
            "higher - typically 2x to 5x the administrative figure.\n"
            f"Use your geographic knowledge of {city} to estimate "
            "the TRUE metro population in the affected bbox.\n"
            "GeoNames figure is a minimum floor only, not the ceiling.\n"
            "(No gridded exposure was available for this run — this is an "
            "LLM estimate, not a geospatial measurement.)"
        )
    else:
        pop_context = (
            f"GeoNames unavailable — estimate based on your geographic knowledge of {city} "
            f"and the {area:.0f} sq km affected area."
        )

    return f"""You are a senior UN disaster analyst.

Disaster event: {severity} {disaster_label}
Cities affected: {city}
Affected area: {area:.0f} sq km  (bbox: {bbox})
Risk level: {risk}

REAL DATA:
{pop_context}

Analyze this real data for {city}.
Apply risk levels to determine actual disaster impact.
Use geographic knowledge for district names only — do NOT invent population numbers if real data is provided.

Your task is REASONING, not estimation:
- What percentage of the real population is in the high-risk {disaster_label} zone?
- What percentage is medium risk (adjacent, evacuation zone)?
- Typical age distribution for {city} — children under 5 + elderly over 65?
- 3 specific local vulnerability factors unique to {city}?

Base population_affected on the REAL {disaster_label} extent and risk level. If the risk is
genuinely high and people are in the {disaster_label} impact zone, report the real exposed count.
If the affected area is tiny or the risk is low, a small or zero figure is the
honest answer — do NOT inflate it.
{"Derive population_affected from the real GeoNames figure using your " + disaster_label + "-zone reasoning." if real_pop else "Estimate based on city size and affected area."}

Return ONLY valid JSON, no other text:
{{
    "population_affected": <integer people in the {disaster_label} impact zone — honest, may be small>,
    "high_risk_people": <integer — approx 20% in direct {disaster_label} zone>,
    "medium_risk_people": <integer — approx 50% in adjacent zones>,
    "vulnerable_population": <children under 5 + elderly over 65>,
    "local_risk_factors": [
        "<specific risk factor 1 for {city}>",
        "<specific risk factor 2 for {city}>",
        "<specific risk factor 3 for {city}>"
    ],
    "confidence": <0.7-0.95>
}}"""


async def _gridded_exposure(hazard_data: dict) -> dict:
    """Phase 6a: WorldPop population inside the real hazard extent.

    Sources the hazard polygon from the satellite agent's vectorised GeoJSON
    (the same artifact hazard_zones.geometry is written from) and sums
    WorldPop 100 m count pixels inside it. Falls back to the AOI bbox ONLY
    when no hazard polygon is available, and says so via `method` — a bbox
    is the analysis area, not the flooded area, so a caller must be able to
    tell the two apart. Never raises.
    """
    try:
        import asyncio

        from services.population_exposure import population_in_polygon

        geom = None
        source = "hazard_polygon"
        url = (hazard_data.get("artifacts") or {}).get("geojson_url") or hazard_data.get(
            "geojson_url"
        )
        if url:
            import requests
            from shapely.geometry import shape
            from shapely.ops import unary_union

            def _fetch():
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                return resp.json()

            fc = await asyncio.to_thread(_fetch)
            geoms = [
                shape(f["geometry"])
                for f in (fc.get("features") or [])
                if f.get("geometry")
            ]
            if geoms:
                geom = unary_union(geoms)
        if geom is None:
            bbox = hazard_data.get("bbox") or []
            if len(bbox) == 4:
                from shapely.geometry import box

                geom = box(*bbox)
                source = "aoi_bbox_fallback"
        iso3 = hazard_data.get("iso3") or _iso3_from_hazard(hazard_data)
        res = await asyncio.to_thread(population_in_polygon, geom, iso3)
        res["geometry_source"] = source
        if source == "aoi_bbox_fallback" and res.get("population") is not None:
            res["notes"] = (
                res.get("notes", "")
                + " NOTE: no hazard polygon was available, so this is exposure "
                "over the whole ANALYSIS AOI, not the flooded extent — an "
                "upper bound, not the affected population."
            )
        return res
    except Exception as exc:  # noqa: BLE001 — exposure is best-effort
        logger.warning("[population] gridded exposure unavailable: %s", exc)
        return {
            "population": None,
            "method": "unavailable",
            "source": None,
            "pixels": 0,
            "polygon_area_km2": 0.0,
            "notes": f"exposure not computed: {exc}",
        }


def _iso3_from_hazard(hazard_data: dict) -> str | None:
    """Best-effort ISO3 for the WorldPop per-country raster.

    WorldPop is published per country, so an ISO3 is required. Derived from
    the location string's country tail via a small map of the countries this
    deployment actually serves, extended by geopy-free lookup when the
    country name is already explicit. Returns None when unknown — the
    exposure step then reports `unavailable` rather than guessing a country.
    """
    text = " ".join(
        str(hazard_data.get(k) or "")
        for k in ("location", "region", "country", "city")
    ).lower()
    for name, iso3 in _COUNTRY_ISO3.items():
        if name in text:
            return iso3
    return None


# Countries this deployment serves plus the validation-harness reference
# events' countries. Extend as coverage grows — an unknown country reports
# exposure as unavailable rather than silently using the wrong raster.
_COUNTRY_ISO3 = {
    "pakistan": "PAK",
    "greece": "GRC",
    "spain": "ESP",
    "united kingdom": "GBR",
    "scotland": "GBR",
    "england": "GBR",
    "wales": "GBR",
    "india": "IND",
    "bangladesh": "BGD",
    "nepal": "NPL",
    "philippines": "PHL",
    "indonesia": "IDN",
    "afghanistan": "AFG",
}


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

    # Phase 6a (science/full-pass): the REAL geospatial exposure figure —
    # WorldPop 100 m population-count pixels summed inside the satellite's
    # actual hazard extent. When this succeeds it is AUTHORITATIVE and the
    # LLM's role shrinks to interpretation; the LLM should not be producing
    # the population number at all. Best-effort: if the raster or the hazard
    # polygon is unavailable the pipeline keeps its previous LLM-estimate
    # behaviour, with the basis recorded either way so a reader can tell
    # which produced the number.
    exposure = await _gridded_exposure(hazard_data)
    if exposure.get("population") is not None:
        print(
            f"[DEBUG][Population] Gridded exposure: {exposure['population']:,} "
            f"({exposure['method']}, {exposure['pixels']} px over "
            f"{exposure['polygon_area_km2']} km2)",
            flush=True,
        )

    prompt = _build_prompt(city, area, hazard_data, real_pop, exposure)

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

    # Phase 6a: the gridded exposure figure OVERRIDES the LLM estimate when
    # it is available. This is the substantive change — population_affected
    # becomes a geospatial calculation (WorldPop pixels inside the real
    # hazard polygon) instead of an LLM point estimate anchored to one
    # administrative figure times a prompt-asserted multiplier. Both numbers
    # are kept so the change is auditable and the LLM's estimate remains
    # visible for comparison.
    result["population_llm_estimate"] = pop
    result["population_exposure"] = exposure
    result["population_basis"] = exposure.get("method")
    if exposure.get("population") is not None:
        pop = int(exposure["population"])
        logger.info(
            "[population] Using GRIDDED exposure %d (%s) in place of the LLM "
            "estimate %d", pop, exposure["method"], result["population_llm_estimate"],
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
