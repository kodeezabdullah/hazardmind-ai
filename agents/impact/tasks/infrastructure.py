"""Task 2 — Infrastructure Assessment.

Data strategy: Overpass OSM provides REAL infrastructure counts (hospitals, schools, bridges).
LLM reasons about flood impact — it does NOT invent counts.
Three Overpass endpoints with automatic failover.
"""

import logging

import httpx

from services.llm_router import smart_llm_call

logger = logging.getLogger(__name__)

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]


def _area_sq_km(bbox: list) -> float:
    return abs((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) * 111 * 111)


def _city_label(hazard_data: dict, event_id: str) -> str:
    cities = hazard_data.get("risk_cities") or []
    if cities:
        return ", ".join(str(c) for c in cities[:3])
    return event_id.removeprefix("demo-").replace("-", " ").title()


def _overpass_query(bbox: list) -> str:
    """Overpass QL — uses exact tag matching (no regex) for maximum endpoint compatibility.
    bbox format: [west, south, east, north] → converted to south,west,north,east for Overpass.
    """
    w, s, e, n = bbox[0], bbox[1], bbox[2], bbox[3]
    bb = f"{s},{w},{n},{e}"
    return f"""[out:json][timeout:30][maxsize:10485760];
(
  node["amenity"="hospital"]({bb});
  way["amenity"="hospital"]({bb});
  node["amenity"="clinic"]({bb});
  way["amenity"="clinic"]({bb});
  node["amenity"="school"]({bb});
  way["amenity"="school"]({bb});
  node["amenity"="university"]({bb});
  way["amenity"="university"]({bb});
  way["highway"="primary"]({bb});
  way["highway"="secondary"]({bb});
  way["highway"="trunk"]({bb});
  way["highway"="motorway"]({bb});
  way["bridge"="yes"]({bb});
);
out;"""


async def _fetch_overpass(bbox: list) -> dict | None:
    """Try each Overpass endpoint in order. First success with non-empty elements wins."""
    query = _overpass_query(bbox)
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            async with httpx.AsyncClient(timeout=35) as client:
                resp = await client.post(endpoint, data={"data": query})
                resp.raise_for_status()
                data = resp.json()
                elements = data.get("elements", [])
                logger.info(
                    "[infrastructure] Overpass %s → %d raw elements",
                    endpoint, len(elements),
                )
                print(
                    f"[DEBUG][Infrastructure] Overpass {endpoint} → {len(elements)} elements",
                    flush=True,
                )

                if not elements:
                    logger.warning("[infrastructure] Overpass %s returned 0 elements — trying next", endpoint)
                    continue

                hospitals   = sum(1 for e in elements if e.get("tags", {}).get("amenity") in ("hospital", "clinic"))
                schools     = sum(1 for e in elements if e.get("tags", {}).get("amenity") in ("school", "university"))
                bridges     = sum(1 for e in elements if e.get("tags", {}).get("bridge") == "yes")
                major_roads = sum(
                    1 for e in elements
                    if e.get("tags", {}).get("highway") in ("primary", "secondary", "trunk", "motorway")
                )

                result = {
                    "hospitals":   hospitals,
                    "schools":     schools,
                    "bridges":     bridges,
                    "major_roads": major_roads,
                    "source":      endpoint,
                }
                logger.info(
                    "[infrastructure] Overpass OK (%s): hospitals=%d schools=%d bridges=%d roads=%d",
                    endpoint, hospitals, schools, bridges, major_roads,
                )
                return result
        except Exception as exc:
            logger.warning("[infrastructure] Overpass endpoint failed (%s): %s", endpoint, exc)

    logger.error("[infrastructure] All 3 Overpass endpoints failed — LLM will estimate")
    return None


_DISASTER_LABELS = {
    "flood": "flood",
    "earthquake": "earthquake",
    "landslide": "landslide",
}


def _build_prompt(city: str, area: float, hazard_data: dict, osm: dict | None) -> str:
    severity = hazard_data.get("severity", "moderate")
    disaster_type = str(hazard_data.get("disaster_type") or "flood").lower()
    disaster_label = _DISASTER_LABELS.get(disaster_type, disaster_type)
    # Read the risk level for the ACTUAL disaster type, not always flood_risk
    # (H#1 Part B — mirrors vulnerability.py's existing correct branching).
    risk_key = {
        "flood": "flood_risk",
        "earthquake": "earthquake_risk",
        "landslide": "landslide_risk",
    }.get(disaster_type, "flood_risk")
    risk = hazard_data.get(risk_key, "UNKNOWN")
    bbox = hazard_data.get("bbox", [])
    osm_quality = "real_osm" if osm else "llm_estimate"

    if osm:
        real_data = (
            f"Real hospitals/clinics from OSM: {osm['hospitals']}\n"
            f"Real schools/universities from OSM: {osm['schools']}\n"
            f"Real bridges from OSM: {osm['bridges']}\n"
            f"Real major road segments from OSM: {osm['major_roads']}"
        )
        instructions = (
            "Base hospitals_at_risk and schools_at_risk on the real OSM counts above.\n"
            f"Determine which fraction is in the actual {disaster_label} zone given the severity.\n"
            f"Estimate roads_blocked_km from road segment count and {disaster_label} extent."
        )
    else:
        real_data = "OSM data unavailable — estimate from your geographic knowledge of the city."
        instructions = "Estimate all values based on city size, density, and your knowledge."

    return f"""You are a senior UN infrastructure analyst.

Disaster: {severity} {disaster_label} in {city}
Affected area: {area:.0f} sq km  (bbox: {bbox})
Risk level: {risk}

REAL DATA:
{real_data}

Your task is REASONING, not estimation:
{instructions}

Name real hospitals, roads, districts, landmarks if you know them.
Do NOT invent OSM counts — use the real numbers above as your baseline.

Return ONLY valid JSON, no other text:
{{
    "hospitals_at_risk": <integer — subset of real OSM hospitals in flood zone>,
    "schools_at_risk": <integer>,
    "roads_blocked_km": <integer — realistic estimate from road segments and flood extent>,
    "bridges_at_risk": <integer — subset of real OSM bridges in flood zone>,
    "power_stations_at_risk": <integer>,
    "critical_assets": [
        "<specific named asset 1 in {city}>",
        "<specific named asset 2 in {city}>"
    ],
    "building_stock": "<description of {city} building quality>",
    "estimated_evacuation_time": "<X-Y hours>",
    "all_routes_blocked": <bool>,
    "osm_data_quality": "{osm_quality}",
    "confidence": <0.7-0.95>
}}"""


async def run_infrastructure_task(hazard_data: dict, event_id: str) -> dict:
    bbox = hazard_data.get("bbox", [0, 0, 1, 1])
    city = _city_label(hazard_data, event_id)
    area = _area_sq_km(bbox)

    print(f"\n[DEBUG][Infrastructure] City: {city!r} | Area: {area:.0f} sqkm | bbox: {bbox}", flush=True)

    osm = await _fetch_overpass(bbox)
    if osm:
        print(
            f"[DEBUG][Infrastructure] OSM: hospitals={osm['hospitals']} schools={osm['schools']} "
            f"bridges={osm['bridges']} roads={osm['major_roads']} src={osm['source']}",
            flush=True,
        )
    else:
        print("[DEBUG][Infrastructure] OSM: all endpoints failed — LLM will estimate", flush=True)

    prompt = _build_prompt(city, area, hazard_data, osm)

    result, model_used, reasoning = await smart_llm_call(prompt, "normal", task_name="infrastructure")

    hospitals = int((result or {}).get("hospitals_at_risk", 0) or 0)
    print(f"[DEBUG][Infrastructure] Initial: hospitals_at_risk={hospitals} (model={model_used})", flush=True)
    logger.info("[infrastructure] Initial: hospitals_at_risk=%d model=%s", hospitals, model_used)

    if hospitals > 10:
        criticality = "high"
        logger.info("[infrastructure] Escalating to high (hospitals=%d)", hospitals)
        result, model_used, reasoning = await smart_llm_call(prompt, criticality, task_name="infrastructure")
        hospitals = int((result or {}).get("hospitals_at_risk", 0) or 0)
        print(f"[DEBUG][Infrastructure] Escalated: hospitals_at_risk={hospitals} (model={model_used})", flush=True)
    else:
        criticality = "normal"

    if result is None:
        result = {
            "hospitals_at_risk":       osm["hospitals"] if osm else 0,
            "schools_at_risk":         osm["schools"]   if osm else 0,
            "roads_blocked_km":        0,
            "bridges_at_risk":         osm["bridges"]   if osm else 0,
            "all_routes_blocked":      False,
            "estimated_evacuation_time": "unknown",
            "confidence":              0.3,
        }

    # Phase 6b (science/full-pass): CLAMP the LLM's at-risk counts against
    # the real OSM totals. The base counts are real (Overpass), but the
    # "at risk" fraction was an unconstrained LLM guess with no code-side
    # check — so the model could (and per SYSTEM_ANALYSIS.md H#15 did)
    # report more facilities at risk than exist in the AOI. A count that
    # exceeds reality is not a conservative estimate, it is a wrong number a
    # responder would act on. Each clamp records its basis so a reader can
    # see whether the figure came from geometry, a clamped guess, or an
    # unclamped one.
    try:
        from services.population_exposure import clamp_at_risk

        for field, osm_key in (
            ("hospitals_at_risk", "hospitals"),
            ("schools_at_risk", "schools"),
            ("bridges_at_risk", "bridges"),
        ):
            real_total = (osm or {}).get(osm_key)
            decision = clamp_at_risk(
                result.get(field), real_total=real_total, geometric=None
            )
            if decision["value"] is not None:
                if decision["clamped"]:
                    logger.warning(
                        "[infrastructure] %s clamped %s -> %s (real OSM total)",
                        field, result.get(field), decision["value"],
                    )
                result[field] = decision["value"]
            result[f"{field}_basis"] = decision["basis"]
    except Exception as exc:  # noqa: BLE001 — clamping must never fail a run
        logger.warning("[infrastructure] at-risk clamping unavailable: %s", exc)

    result["model_used"]    = model_used
    result["criticality"]   = criticality
    result["llm_reasoning"] = reasoning
    if osm:
        result["osm_source"] = osm["source"]

    logger.info(
        "[infrastructure] Done — hospitals=%d schools=%d roads_km=%s bridges=%d evac=%s model=%s osm=%s",
        result.get("hospitals_at_risk", 0),
        result.get("schools_at_risk", 0),
        result.get("roads_blocked_km"),
        result.get("bridges_at_risk", 0),
        result.get("estimated_evacuation_time"),
        model_used,
        osm["source"] if osm else "N/A",
    )
    return result
