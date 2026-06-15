import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

from llm_clients import (
    generate_detailed_report_with_aiml_fallback,
    generate_composite_detailed_report_with_featherless,
    generate_executive_summary_with_aiml,
)
from intelligence import (
    assess_event_criticality,
    detect_anomalies,
    generate_band_ready_message,
    generate_decision_brief,
    generate_map_narrative,
    generate_priority_recommendations,
    run_quality_check,
    strip_sources,
)

BASE_DIR = Path(__file__).resolve().parent

MOCK_EVENT_DATA = {
    "event_id": "demo-peshawar-flood",
    "location": "Peshawar, Pakistan",
    "hazard_type": "Flood",
    "overall_severity": "CRITICAL",
    "satellite": {
        "type": "sentinel-1",
        "reason": "cloud_cover_above_30_percent_sar_selected",
        "cloud_cover": 42,
        "scene_id": "S1A_DEMO_PESHAWAR_20260613",
    },
    "boundaries": {
        "region_boundary": {
            "type": "FeatureCollection",
            "features": [],
        },
        "risk_cities": ["Peshawar"],
        "merged_polygon": {
            "type": "Feature",
            "properties": {"name": "Peshawar analysis area"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [71.40, 33.90],
                        [71.65, 33.90],
                        [71.65, 34.10],
                        [71.40, 34.10],
                        [71.40, 33.90],
                    ]
                ],
            },
        },
        "bbox": [71.40, 33.90, 71.65, 34.10],
    },
    "artifacts": {
        "true_color_url": "",
        "index_url": "",
        "classification_url": "",
        "geojson_url": "",
    },
    "analysis": {
        "index_type": "SAR VV/VH ratio",
        "mean_value": 0.24,
        "affected_area_km2": 153.37,
        "damage_percent": 24.3,
        "total_zones": 22,
        "zones": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "zone_id": "FZ-01",
                        "severity": "critical",
                        "class_name": "deep_water",
                        "area_km2": 12.4,
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [71.47, 33.96],
                                [71.56, 33.96],
                                [71.56, 34.03],
                                [71.47, 34.03],
                                [71.47, 33.96],
                            ]
                        ],
                    },
                },
                {
                    "type": "Feature",
                    "properties": {
                        "zone_id": "FZ-02",
                        "severity": "high",
                        "class_name": "water",
                        "area_km2": 8.7,
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [71.43, 33.93],
                                [71.50, 33.93],
                                [71.50, 33.98],
                                [71.43, 33.98],
                                [71.43, 33.93],
                            ]
                        ],
                    },
                },
            ],
        },
    },
    "hazard": {
        "flood_risk": "CRITICAL",
        "earthquake_risk": "MEDIUM",
        "landslide_risk": "LOW",
        "confidence_scores": {
            "flood": 0.91,
            "earthquake": 0.67,
            "landslide": 0.54,
        },
    },
    "impact": {
        "population_affected": 540000,
        "hospitals_at_risk": 14,
        "roads_blocked_km": 89,
        "schools_affected": 67,
        "vulnerability_score": 8.2,
        "critical_facilities": [
            {
                "name": "Lady Reading Hospital",
                "type": "hospital",
                "lat": 34.015,
                "lng": 71.570,
                "risk": "HIGH",
            },
            {
                "name": "Khyber Teaching Hospital",
                "type": "hospital",
                "lat": 33.998,
                "lng": 71.487,
                "risk": "MEDIUM",
            },
        ],
    },
    "routes": {
        "evacuation_routes": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"name": "Evacuation Route 1"},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [71.49, 33.97],
                            [71.53, 34.00],
                            [71.59, 34.06],
                        ],
                    },
                }
            ],
        }
    },
    "agent_log": [
        {
            "agent": "hazardmind-satellite",
            "status": "complete",
            "message": "Sentinel-1 SAR selected due to cloud cover above 30%. Zones vectorized and uploaded.",
            "timestamp": "2026-06-13T18:00:00Z",
        },
        {
            "agent": "hazardmind-hazard",
            "status": "complete",
            "message": "Flood risk classified as CRITICAL. Earthquake and landslide risks assessed.",
            "timestamp": "2026-06-13T18:01:00Z",
        },
        {
            "agent": "hazardmind-impact",
            "status": "complete",
            "message": "Population and infrastructure exposure calculated.",
            "timestamp": "2026-06-13T18:02:00Z",
        },
        {
            "agent": "hazardmind-report",
            "status": "complete",
            "message": "Executive report and dashboard output generated.",
            "timestamp": "2026-06-13T18:03:00Z",
        },
    ],
}

RECOMMENDATIONS = [
    "Prioritize evacuation in critical flood zones.",
    "Deploy emergency medical support near hospitals at risk.",
    "Clear blocked road corridors for rescue access.",
    "Open temporary shelters for displaced families.",
    "Monitor flood expansion using updated satellite imagery.",
]

RESPONSE_PRIORITIES = [
    "Activate evacuation support for FZ-01 and FZ-02.",
    "Protect hospital access and stage medical surge teams near high-risk facilities.",
    "Clear blocked road corridors needed for rescue and logistics movement.",
]

ASSUMPTIONS = [
    "Satellite, hazard, and impact values are local demo data until upstream agents publish live outputs.",
    "Risk zone geometries are simplified mock polygons for dashboard and report demonstration.",
    "Facility and route coordinates are approximate and intended for prototype visualization.",
]

LIMITATIONS = [
    "No live Band messages are consumed in this local demo run.",
    "No field validation, hydrologic model, or live basemap tiles are used for artifact generation.",
    "Impact estimates should be treated as decision-support indicators, not official counts.",
]


def build_detailed_report_prompt(data: dict) -> str:
    scenario_json = json.dumps(data, indent=2)
    return f"""
Generate the detailed operational report body for a disaster response dashboard.
Return strict JSON only. Do not include markdown or commentary.

Required JSON shape:
{{
  "detailed_body": "string",
  "recommendations": ["string"],
  "response_priorities": ["string"],
  "assumptions": ["string"],
  "limitations": ["string"]
}}

Focus on detailed incident analysis, operational risk interpretation, recommendations,
response priorities, assumptions, and limitations.

Use this full scenario data:
{scenario_json}
""".strip()


def build_executive_summary_prompt(data: dict, detailed_report: dict) -> str:
    scenario_json = json.dumps(data, indent=2)
    detailed_json = json.dumps(detailed_report, indent=2)
    return f"""
Write a short executive summary for senior emergency decision makers.
Return only 3 to 5 polished sentences, with no markdown.
Use concise language suitable for the dashboard and PDF.

Base event data:
{scenario_json}

Detailed operational report:
{detailed_json}
""".strip()


async def generate_report(event_id: str, context: dict | None = None):
    if context is None and event_id != MOCK_EVENT_DATA["event_id"]:
        raise ValueError(
            f"Only local mock event '{MOCK_EVENT_DATA['event_id']}' is available right now."
        )

    load_dotenv(BASE_DIR / ".env")

    source_context = context or MOCK_EVENT_DATA
    result = json.loads(json.dumps(source_context))
    result["event_id"] = event_id
    agent_log = [
        _report_log("Report Agent received event context", "2026-06-13T18:03:00Z"),
    ]

    detailed_report, detailed_source, featherless_fallback_used, featherless_model = await generate_detailed_report(result)
    if detailed_source.startswith("featherless:"):
        agent_log.append(
            _report_log("Featherless composite generated detailed disaster report", "2026-06-13T18:03:20Z")
        )
    else:
        agent_log.append(
            _report_log("Featherless cascades unavailable, AI/ML fallback used", "2026-06-13T18:03:20Z")
        )

    summary, summary_source, summary_fallback_used = await generate_executive_summary(
        result,
        detailed_report,
    )
    agent_log.append(
        _report_log("AI/ML generated executive summary", "2026-06-13T18:03:40Z")
        if summary_source == "aiml"
        else _report_log("AI/ML unavailable, deterministic executive summary used", "2026-06-13T18:03:40Z")
    )

    result["report"] = {
        "summary": summary,
        "detailed_body": detailed_report["detailed_body"],
        "technical_analysis": detailed_report["technical_analysis"],
        "recommendations": detailed_report["recommendations"],
        "response_priorities": detailed_report["response_priorities"],
        "assumptions": detailed_report["assumptions"],
        "limitations": detailed_report["limitations"],
        "pdf_url": "",
        "map_url": "",
    }

    intelligence_with_sources = await generate_intelligence(result)
    intelligence, intelligence_sources = strip_sources(intelligence_with_sources)
    decision_summary = intelligence.get("decision_brief", {}).get("official_summary")
    if decision_summary:
        result["report"]["summary"] = decision_summary
        summary_source = intelligence_sources.get("decision_brief", summary_source)

    priority_timeline = intelligence.get("priority_timeline", {})
    result["report"]["recommendations"] = merge_recommendations(
        result["report"]["recommendations"],
        priority_timeline.get("next_6_hours", []),
        priority_timeline.get("resource_priorities", []),
    )

    result["intelligence"] = intelligence
    result["model_sources"] = {
        "detailed_report": detailed_source,
        "executive_summary": summary_source,
        "fallback_used": featherless_fallback_used or summary_fallback_used,
        "featherless_model": featherless_model,
        "intelligence": {
            "criticality": intelligence_sources.get("criticality", "deterministic_fallback"),
            "anomaly_check": intelligence_sources.get("anomaly_check", "deterministic_fallback"),
            "map_narrative": intelligence_sources.get("map_narrative", "deterministic_fallback"),
            "priority_recommendations": intelligence_sources.get(
                "priority_recommendations", "deterministic_fallback"
            ),
            "decision_brief": intelligence_sources.get("decision_brief", "deterministic_fallback"),
            "quality_check": intelligence_sources.get("quality_check", "deterministic_fallback"),
            "band_ready_message": intelligence_sources.get("band_ready_message", "deterministic_template"),
        },
    }
    agent_log.extend(
        [
            _report_log("Criticality assessed", "2026-06-13T18:03:45Z"),
            _report_log("Anomalies checked", "2026-06-13T18:03:50Z"),
            _report_log("Map narrative generated", "2026-06-13T18:03:55Z"),
            _report_log("Priority timeline generated", "2026-06-13T18:04:00Z"),
            _report_log("Decision brief generated with Opus", "2026-06-13T18:04:05Z"),
            _report_log("Quality check completed", "2026-06-13T18:04:10Z"),
            _report_log("Band-ready final message prepared", "2026-06-13T18:04:15Z"),
        ]
    )
    result["agent_log"] = agent_log

    return {
        key: result[key]
        for key in (
            "event_id",
            "location",
            "hazard_type",
            "overall_severity",
            "satellite",
            "boundaries",
            "artifacts",
            "analysis",
            "hazard",
            "impact",
            "routes",
            "report",
            "intelligence",
            "model_sources",
            "agent_log",
        )
    }


async def generate_intelligence(report_context: dict) -> dict:
    criticality = await assess_event_criticality(report_context)
    anomalies, map_narrative, priority_timeline = await asyncio.gather(
        detect_anomalies(report_context),
        generate_map_narrative(report_context),
        generate_priority_recommendations(report_context),
    )
    partial = {"criticality": criticality}
    partial.update(
        {
            "anomalies": anomalies,
            "map_narrative": map_narrative,
            "priority_timeline": priority_timeline,
        }
    )
    decision_brief = await generate_decision_brief(report_context, partial)
    partial["decision_brief"] = decision_brief
    quality_check = await run_quality_check(report_context, partial)
    partial["quality_check"] = quality_check
    partial["band_ready_message"] = await generate_band_ready_message(report_context, partial)
    return partial


def merge_recommendations(*groups: list[str]) -> list[str]:
    merged = []
    seen = set()
    for group in groups:
        for item in group or []:
            cleaned = str(item).strip()
            if cleaned and cleaned.lower() not in seen:
                merged.append(cleaned)
                seen.add(cleaned.lower())
    return merged[:10]


async def generate_detailed_report(data: dict) -> tuple[dict, str, bool, str]:
    featherless_result = await generate_composite_detailed_report_with_featherless(data)
    if featherless_result["ok"]:
        return (
            featherless_result["data"],
            featherless_result["source"],
            False,
            featherless_result["featherless_model"],
        )

    fallback_result = await generate_detailed_report_with_aiml_fallback(data)
    return (
        fallback_result["data"],
        "aiml_fallback",
        True,
        fallback_result["featherless_model"],
    )


async def generate_executive_summary(data: dict, detailed_report: dict) -> tuple[str, str, bool]:
    response = await generate_executive_summary_with_aiml(data, detailed_report)
    return response["summary"], response["source"], not response["ok"]


def normalize_detailed_report(text: str) -> dict:
    try:
        parsed = json.loads(extract_json(text))
    except json.JSONDecodeError:
        parsed = {"detailed_body": text}

    return {
        "detailed_body": str(parsed.get("detailed_body") or text or deterministic_detailed_report(MOCK_EVENT_DATA)["detailed_body"]),
        "recommendations": list_or_default(parsed.get("recommendations"), RECOMMENDATIONS),
        "response_priorities": list_or_default(parsed.get("response_priorities"), RESPONSE_PRIORITIES),
        "assumptions": list_or_default(parsed.get("assumptions"), ASSUMPTIONS),
        "limitations": list_or_default(parsed.get("limitations"), LIMITATIONS),
    }


def extract_json(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


def list_or_default(value, default: list[str]) -> list[str]:
    if isinstance(value, list):
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        if cleaned:
            return cleaned
    return default


def deterministic_detailed_report(data: dict) -> dict:
    return {
        "detailed_body": (
            f"{data['location']} is experiencing a {data['overall_severity']} {data['hazard_type'].lower()} "
            f"scenario with {data['analysis']['affected_area_km2']} km2 affected and "
            f"{data['impact']['population_affected']:,} people exposed. Flood risk is classified as "
            f"{data['hazard']['flood_risk']} with hospitals, roads, and schools requiring immediate operational attention."
        ),
        "recommendations": RECOMMENDATIONS,
        "response_priorities": RESPONSE_PRIORITIES,
        "assumptions": ASSUMPTIONS,
        "limitations": LIMITATIONS,
    }


def deterministic_summary(data: dict) -> str:
    return (
        f"Critical flood risk has been detected across high-density areas of {data['location']}. "
        f"Satellite-derived classification identifies {data['analysis']['total_zones']} zones and "
        f"{data['analysis']['affected_area_km2']} km2 of affected area, with "
        f"{data['impact']['population_affected']:,} people exposed. Immediate evacuation, hospital support, "
        "road clearance, and shelter activation are recommended."
    )


def _report_log(message: str, timestamp: str) -> dict:
    return {
        "agent": "hazardmind-report",
        "status": "complete",
        "message": message,
        "timestamp": timestamp,
    }
