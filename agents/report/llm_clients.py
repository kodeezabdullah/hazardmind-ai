import asyncio
import json
import os

from openai import AsyncOpenAI


AIML_BASE_URL = "https://api.aimlapi.com/v1"
AIML_MODEL = "claude-opus-4-8"
FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"
FEATHERLESS_DEFAULT_MODEL = "moonshotai/Kimi-K2.6"
PROVIDER_TIMEOUT_SECONDS = 30

FALLBACK_RECOMMENDATIONS = [
    "Prioritize evacuation in critical flood zones.",
    "Deploy emergency medical support near hospitals at risk.",
    "Clear blocked road corridors for rescue access.",
    "Open temporary shelters for displaced families.",
    "Monitor flood expansion using updated satellite imagery.",
]

FALLBACK_PRIORITIES = [
    "Activate evacuation support for FZ-01 and FZ-02.",
    "Protect hospital access and stage medical surge teams near high-risk facilities.",
    "Clear blocked road corridors needed for rescue and logistics movement.",
]

FALLBACK_ASSUMPTIONS = [
    "Satellite, hazard, and impact values are local demo data until upstream agents publish live outputs.",
    "Risk zone geometries are simplified mock polygons for dashboard and report demonstration.",
    "Facility and route coordinates are approximate and intended for prototype visualization.",
]

FALLBACK_LIMITATIONS = [
    "No live Band messages are consumed in this local demo run.",
    "No field validation, hydrologic model, or live basemap tiles are used for artifact generation.",
    "Impact estimates should be treated as decision-support indicators, not official counts.",
]


async def generate_detailed_report_with_featherless(context: dict) -> dict:
    response = await call_featherless(
        build_detailed_report_prompt(context),
        system=(
            "You are Kimi K2.6 writing structured disaster-risk intelligence. "
            "Return strict JSON only. Do not reveal hidden reasoning."
        ),
    )
    if not response["ok"]:
        return {
            "ok": False,
            "source": "featherless:kimi-k2.6",
            "featherless_model": response["model"],
            "data": deterministic_detailed_report(context),
        }

    return {
        "ok": True,
        "source": "featherless:kimi-k2.6",
        "featherless_model": response["model"],
        "data": normalize_detailed_report(response["content"], context),
    }


async def generate_detailed_report_with_aiml_fallback(context: dict) -> dict:
    response = await call_aiml(
        build_detailed_report_prompt(context),
        system="You generate structured disaster response reports as strict JSON.",
    )
    if response["ok"]:
        data = normalize_detailed_report(response["content"], context)
    else:
        data = deterministic_detailed_report(context)

    return {
        "ok": response["ok"],
        "source": "aiml_fallback",
        "featherless_model": get_featherless_model(),
        "data": data,
    }


async def generate_executive_summary_with_aiml(context: dict, detailed_report: dict) -> dict:
    response = await call_aiml(
        build_executive_summary_prompt(context, detailed_report),
        system="You write clear, concise executive disaster response summaries.",
    )
    if response["ok"] and response["content"]:
        return {
            "ok": True,
            "source": "aiml",
            "summary": response["content"],
        }

    return {
        "ok": False,
        "source": "deterministic_fallback",
        "summary": deterministic_summary(context),
    }


async def call_featherless(prompt: str, system: str = "") -> dict:
    api_key = os.getenv("FEATHERLESS_API_KEY")
    if not api_key:
        return {
            "ok": False,
            "provider": "featherless",
            "model": get_featherless_model(),
            "content": "",
            "error": "FEATHERLESS_API_KEY is not set",
        }

    return await _call_openai_compatible(
        provider="featherless",
        api_key=api_key,
        base_url=FEATHERLESS_BASE_URL,
        model=get_featherless_model(),
        system=system,
        prompt=prompt,
    )


async def call_aiml(prompt: str, system: str = "") -> dict:
    api_key = os.getenv("AIML_API_KEY")
    if not api_key:
        return {
            "ok": False,
            "provider": "aiml",
            "model": AIML_MODEL,
            "content": "",
            "error": "AIML_API_KEY is not set",
        }

    return await _call_openai_compatible(
        provider="aiml",
        api_key=api_key,
        base_url=AIML_BASE_URL,
        model=AIML_MODEL,
        system=system,
        prompt=prompt,
    )


async def _call_openai_compatible(
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    system: str,
    prompt: str,
) -> dict:
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=float(PROVIDER_TIMEOUT_SECONDS))
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=messages,
            ),
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )
        content = response.choices[0].message.content or ""
        return {
            "ok": True,
            "provider": provider,
            "model": model,
            "content": content.strip(),
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": provider,
            "model": model,
            "content": "",
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        await client.close()


def get_featherless_model() -> str:
    return os.getenv("FEATHERLESS_MODEL") or FEATHERLESS_DEFAULT_MODEL


def build_detailed_report_prompt(context: dict) -> str:
    prompt_context = {
        "event_id": context.get("event_id"),
        "location": context.get("location"),
        "hazard_type": context.get("hazard_type"),
        "overall_severity": context.get("overall_severity"),
        "satellite": context.get("satellite"),
        "analysis": context.get("analysis"),
        "hazard": context.get("hazard"),
        "impact": context.get("impact"),
        "routes": context.get("routes"),
    }
    return f"""
Generate a structured detailed disaster risk report for emergency managers.
Return strict JSON only. Do not include markdown, tables, chain-of-thought, hidden reasoning, or commentary.

Required JSON shape:
{{
  "detailed_body": "string",
  "technical_analysis": "string",
  "recommendations": ["string"],
  "response_priorities": ["string"],
  "assumptions": ["string"],
  "limitations": ["string"]
}}

Writing style:
- executive but technical
- clear for emergency managers
- concise but not shallow
- suitable for a high-stakes disaster intelligence report

Use event ID, location, hazard type, severity, satellite metadata, affected area, damage percent,
hazard zones, population impact, hospitals at risk, roads blocked, schools affected,
vulnerability score, available routes, and critical facilities.

Context JSON:
{json.dumps(prompt_context, indent=2)}
""".strip()


def build_executive_summary_prompt(context: dict, detailed_report: dict) -> str:
    summary_context = {
        "event_id": context.get("event_id"),
        "location": context.get("location"),
        "hazard_type": context.get("hazard_type"),
        "overall_severity": context.get("overall_severity"),
        "satellite": context.get("satellite"),
        "analysis": context.get("analysis"),
        "hazard": context.get("hazard"),
        "impact": context.get("impact"),
        "detailed_report": detailed_report,
    }
    return f"""
Write a short executive summary for senior emergency decision makers.
Return only 3 to 5 polished sentences, with no markdown.
Use concise decision-maker wording suitable for the dashboard and PDF.

Context JSON:
{json.dumps(summary_context, indent=2)}
""".strip()


def normalize_detailed_report(text: str, context: dict) -> dict:
    fallback = deterministic_detailed_report(context)
    try:
        parsed = json.loads(extract_json(text))
    except json.JSONDecodeError:
        parsed = {"detailed_body": text}

    return {
        "detailed_body": str(parsed.get("detailed_body") or text or fallback["detailed_body"]),
        "technical_analysis": str(parsed.get("technical_analysis") or fallback["technical_analysis"]),
        "recommendations": list_or_default(parsed.get("recommendations"), FALLBACK_RECOMMENDATIONS),
        "response_priorities": list_or_default(parsed.get("response_priorities"), FALLBACK_PRIORITIES),
        "assumptions": list_or_default(parsed.get("assumptions"), FALLBACK_ASSUMPTIONS),
        "limitations": list_or_default(parsed.get("limitations"), FALLBACK_LIMITATIONS),
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


def deterministic_detailed_report(context: dict) -> dict:
    return {
        "detailed_body": (
            f"{context['location']} is experiencing a {context['overall_severity']} "
            f"{context['hazard_type'].lower()} scenario with {context['analysis']['affected_area_km2']} km2 "
            f"affected and {context['impact']['population_affected']:,} people exposed. "
            f"Flood risk is classified as {context['hazard']['flood_risk']} with hospitals, roads, "
            "and schools requiring immediate operational attention."
        ),
        "technical_analysis": (
            f"Sentinel-1 SAR was selected because cloud cover is {context['satellite']['cloud_cover']}%. "
            f"The analysis uses {context['analysis']['index_type']} with mean value "
            f"{context['analysis']['mean_value']} and identifies {context['analysis']['total_zones']} zones. "
            f"Damage is estimated at {context['analysis']['damage_percent']}%, with "
            f"{context['impact']['roads_blocked_km']} km of roads blocked and a vulnerability score of "
            f"{context['impact']['vulnerability_score']}."
        ),
        "recommendations": FALLBACK_RECOMMENDATIONS,
        "response_priorities": FALLBACK_PRIORITIES,
        "assumptions": FALLBACK_ASSUMPTIONS,
        "limitations": FALLBACK_LIMITATIONS,
    }


def deterministic_summary(context: dict) -> str:
    return (
        f"Critical flood risk has been detected across high-density areas of {context['location']}. "
        f"Satellite-derived classification identifies {context['analysis']['total_zones']} zones and "
        f"{context['analysis']['affected_area_km2']} km2 of affected area, with "
        f"{context['impact']['population_affected']:,} people exposed. Immediate evacuation, hospital support, "
        "road clearance, and shelter activation are recommended."
    )
