import asyncio
import ast
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI


AIML_BASE_URL = "https://api.aimlapi.com/v1"
AIML_OPUS = "claude-opus-4-8"
AIML_GPT_LAST_RESORT = "gpt-4.5"
AIML_MODEL = AIML_OPUS
FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"
FEATHERLESS_KIMI = "moonshotai/Kimi-K2.6"
FEATHERLESS_GEMMA = "google/gemma-4-31B-it"
FEATHERLESS_QWEN = "Qwen/Qwen3.6-35B-A3B"
FEATHERLESS_DEEPSEEK = "deepseek-ai/DeepSeek-V4-Pro"
FEATHERLESS_DEFAULT_MODEL = FEATHERLESS_KIMI
PROVIDER_TIMEOUT_SECONDS = 30
FEATHERLESS_TIMEOUT_SECONDS = 90
AIML_TIMEOUT_SECONDS = 35
FEATHERLESS_RETRY_TIMEOUT_SECONDS = 75
FEATHERLESS_JSON_TOKENS = 800
FEATHERLESS_DETAILED_TOKENS = 1600
FEATHERLESS_MAP_TOKENS = 900
FEATHERLESS_RECOMMENDATION_TOKENS = 1200
FEATHERLESS_CHECK_TOKENS = 900
MAX_RETRY_TOKENS = 2500
FEATHERLESS_CONCURRENCY = 2
_FEATHERLESS_SEMAPHORE = asyncio.Semaphore(FEATHERLESS_CONCURRENCY)
BASE_DIR = Path(__file__).resolve().parent

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
            "Return final JSON only. Do not include reasoning. Keep output concise. No markdown."
        ),
        max_tokens=FEATHERLESS_DETAILED_TOKENS,
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
        max_tokens=FEATHERLESS_DETAILED_TOKENS,
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


async def generate_composite_detailed_report_with_featherless(context: dict) -> dict:
    incident_task = generate_incident_interpretation(context)
    technical_task = generate_technical_analysis(context)
    assumptions_task = generate_assumptions_limitations(context)
    incident, technical, assumptions = await asyncio.gather(incident_task, technical_task, assumptions_task)

    incident_data, incident_source = incident
    technical_data, technical_source = technical
    assumptions_data, assumptions_source = assumptions
    component_sources = {
        "incident_interpretation": incident_source,
        "technical_analysis": technical_source,
        "assumptions_limitations": assumptions_source,
    }
    any_featherless = any(source.startswith("featherless:") for source in component_sources.values())
    fallback = deterministic_detailed_report(context)
    if not any_featherless:
        return {
            "ok": False,
            "source": "deterministic_fallback",
            "featherless_model": get_featherless_model(),
            "component_sources": component_sources,
            "data": fallback,
        }

    data = {
        "detailed_body": str(incident_data.get("detailed_body") or fallback["detailed_body"]),
        "situation_interpretation": str(incident_data.get("situation_interpretation") or ""),
        "operational_concern": str(incident_data.get("operational_concern") or ""),
        "technical_analysis": str(technical_data.get("technical_analysis") or fallback["technical_analysis"]),
        "data_confidence_notes": list_or_default(
            technical_data.get("data_confidence_notes"),
            ["Satellite-derived estimates require field validation."],
        ),
        "spatial_risk_drivers": list_or_default(
            technical_data.get("spatial_risk_drivers"),
            ["Critical and high flood zones intersect exposed population and hospital corridors."],
        ),
        "recommendations": FALLBACK_RECOMMENDATIONS,
        "response_priorities": FALLBACK_PRIORITIES,
        "assumptions": list_or_default(assumptions_data.get("assumptions"), fallback["assumptions"]),
        "limitations": list_or_default(assumptions_data.get("limitations"), fallback["limitations"]),
    }
    return {
        "ok": True,
        "source": "featherless:composite",
        "featherless_model": get_featherless_model(),
        "component_sources": component_sources,
        "data": data,
    }


async def generate_incident_interpretation(context: dict) -> tuple[dict, str]:
    return await featherless_json_cascade(
        purpose="incident_interpretation",
        prompt=_focused_json_prompt(
            "Generate core incident interpretation for the final disaster report.",
            context,
            {
                "detailed_body": "max 120 words",
                "situation_interpretation": "max 70 words",
                "operational_concern": "max 50 words",
            },
        ),
        system="You are Kimi K2.6 producing final disaster intelligence JSON. Return final JSON only. No markdown. No reasoning.",
        primary_model=FEATHERLESS_KIMI,
        fallback_models=[FEATHERLESS_DEEPSEEK, FEATHERLESS_GEMMA],
        max_tokens=900,
        timeout_seconds=FEATHERLESS_TIMEOUT_SECONDS,
        required_keys=["detailed_body", "situation_interpretation", "operational_concern"],
    )


async def generate_technical_analysis(context: dict) -> tuple[dict, str]:
    return await featherless_json_cascade(
        purpose="technical_analysis",
        prompt=_focused_json_prompt(
            "Generate concise technical analysis for the final disaster report.",
            context,
            {
                "technical_analysis": "max 110 words",
                "data_confidence_notes": ["short note"],
                "spatial_risk_drivers": ["short driver"],
            },
        ),
        system="You are a technical disaster data analyst. Return final JSON only. No markdown. No reasoning.",
        primary_model=FEATHERLESS_KIMI,
        fallback_models=[FEATHERLESS_QWEN, FEATHERLESS_DEEPSEEK],
        max_tokens=900,
        timeout_seconds=FEATHERLESS_TIMEOUT_SECONDS,
        required_keys=["technical_analysis", "data_confidence_notes", "spatial_risk_drivers"],
    )


async def generate_assumptions_limitations(context: dict) -> tuple[dict, str]:
    return await featherless_json_cascade(
        purpose="assumptions_limitations",
        prompt=_focused_json_prompt(
            "Generate assumptions and limitations for a disaster intelligence report.",
            context,
            {
                "assumptions": ["2 to 4 concise assumptions"],
                "limitations": ["2 to 4 concise limitations"],
            },
        ),
        system="You are a disaster report quality analyst. Return final JSON only. No markdown. No reasoning.",
        primary_model=FEATHERLESS_QWEN,
        fallback_models=[FEATHERLESS_KIMI, FEATHERLESS_GEMMA],
        max_tokens=800,
        timeout_seconds=FEATHERLESS_TIMEOUT_SECONDS,
        required_keys=["assumptions", "limitations"],
    )


async def generate_executive_summary_with_aiml(context: dict, detailed_report: dict) -> dict:
    response = await call_aiml(
        build_executive_summary_prompt(context, detailed_report),
        system="You write clear, concise executive disaster response summaries.",
        max_tokens=900,
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


async def call_featherless(prompt: str, system: str = "", max_tokens: int = FEATHERLESS_JSON_TOKENS) -> dict:
    return await call_featherless_model(
        prompt,
        system=system,
        model=get_featherless_model(),
        max_tokens=max_tokens,
    )


async def call_featherless_model(
    prompt: str,
    system: str = "",
    model: str | None = None,
    max_tokens: int = FEATHERLESS_JSON_TOKENS,
    timeout_seconds: int | None = None,
) -> dict:
    api_key = os.getenv("FEATHERLESS_API_KEY")
    selected_model = model or get_featherless_model()
    if not api_key:
        return {
            "ok": False,
            "provider": "featherless",
            "model": selected_model,
            "content": "",
            "error": "FEATHERLESS_API_KEY is not set",
        }

    return await _call_openai_compatible(
        provider="featherless",
        api_key=api_key,
        base_url=FEATHERLESS_BASE_URL,
        model=selected_model,
        system=system,
        prompt=prompt,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
    )


async def call_aiml(
    prompt: str,
    system: str = "",
    model: str | None = None,
    max_tokens: int = 1200,
    timeout_seconds: int | None = None,
) -> dict:
    api_key = os.getenv("AIML_API_KEY")
    selected_model = model or AIML_MODEL
    if not api_key:
        return {
            "ok": False,
            "provider": "aiml",
            "model": selected_model,
            "content": "",
            "error": "AIML_API_KEY is not set",
        }

    return await _call_openai_compatible(
        provider="aiml",
        api_key=api_key,
        base_url=AIML_BASE_URL,
        model=selected_model,
        system=system,
        prompt=prompt,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
    )


async def featherless_json_cascade(
    purpose: str,
    prompt: str,
    system: str,
    primary_model: str,
    fallback_models: list[str],
    max_tokens: int,
    timeout_seconds: int,
    required_keys: list[str] | None = None,
) -> tuple[dict, str]:
    """
    Try primary and fallback Featherless models until strict-enough JSON is produced.
    """
    del purpose
    models = [primary_model, *fallback_models]
    for selected_model in models:
        async with _FEATHERLESS_SEMAPHORE:
            response = await call_featherless_model(
                prompt,
                system=system,
                model=selected_model,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
            )
        if not response["ok"]:
            continue
        try:
            parsed = parse_json_object(response["content"])
        except (json.JSONDecodeError, ValueError, SyntaxError):
            continue
        if not parsed:
            continue
        if required_keys and any(key not in parsed for key in required_keys):
            continue
        return parsed, f"featherless:{_model_label(selected_model)}"

    return {}, "deterministic_fallback"


async def featherless_json_call(
    prompt: str,
    system: str,
    model: str,
    fallback_models: list[str] | None = None,
    max_tokens: int = FEATHERLESS_JSON_TOKENS,
) -> dict:
    """
    Calls Featherless and parses strict JSON.
    """
    data, source = await featherless_json_cascade(
        purpose="json_call",
        prompt=prompt,
        system=system,
        primary_model=model,
        fallback_models=fallback_models or [],
        max_tokens=max_tokens,
        timeout_seconds=FEATHERLESS_TIMEOUT_SECONDS,
    )
    return {"ok": source != "deterministic_fallback", "source": source, "data": data}


async def aiml_text_call(prompt: str, system: str, model: str | None = None, max_tokens: int = 1200) -> str:
    """
    Calls AI/ML API and returns text. Tries GPT-4.5 as a last-resort fallback if Opus fails.
    """
    selected_model = model or AIML_OPUS
    response = await call_aiml(prompt, system=system, model=selected_model, max_tokens=max_tokens)
    if response["ok"] and response["content"]:
        return response["content"]

    if selected_model != AIML_GPT_LAST_RESORT:
        fallback = await call_aiml(prompt, system=system, model=AIML_GPT_LAST_RESORT, max_tokens=max_tokens)
        if fallback["ok"] and fallback["content"]:
            return fallback["content"]

    return ""


async def smart_critical_call(prompt: str, system: str, criticality: str) -> str:
    """
    Routes high/critical work to Opus first and lower-severity work to Featherless first.
    """
    if criticality.lower() in {"high", "critical"}:
        aiml_response = await aiml_text_call(prompt, system=system, model=AIML_OPUS)
        if aiml_response:
            return aiml_response
        featherless_response = await call_featherless_model(
            prompt,
            system=system,
            model=FEATHERLESS_KIMI,
            max_tokens=FEATHERLESS_JSON_TOKENS,
        )
        return featherless_response["content"] if featherless_response["ok"] else ""

    featherless_response = await call_featherless_model(
        prompt,
        system=system,
        model=FEATHERLESS_KIMI,
        max_tokens=FEATHERLESS_JSON_TOKENS,
    )
    if featherless_response["ok"] and featherless_response["content"]:
        return featherless_response["content"]
    return await aiml_text_call(prompt, system=system, model=AIML_OPUS)


async def featherless_health_check() -> list[tuple[str, str]]:
    load_dotenv(BASE_DIR / ".env")
    checks = [
        ("Kimi K2.6", FEATHERLESS_KIMI),
        ("Gemma 4 31B", FEATHERLESS_GEMMA),
        ("DeepSeek V4 Pro", FEATHERLESS_DEEPSEEK),
        ("Qwen 3.6 35B", FEATHERLESS_QWEN),
    ]
    results = []
    for label, model in checks:
        response = await call_featherless_model(
            'Return final JSON only. No markdown. No reasoning. {"status":"ok"}',
            system="Return final JSON only. No markdown. No reasoning.",
            model=model,
            max_tokens=200,
            timeout_seconds=25,
        )
        if response["ok"]:
            try:
                parse_json_object(response["content"])
                status = "OK"
            except (json.JSONDecodeError, ValueError, SyntaxError):
                status = "FAILED_PARSE"
        else:
            error = response.get("error", "")
            status = "FAILED_TIMEOUT" if "TimeoutError" in error else "FAILED"
        results.append((label, status))
    return results


async def _call_openai_compatible(
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int,
    timeout_seconds: int | None = None,
) -> dict:
    effective_timeout = timeout_seconds or (FEATHERLESS_TIMEOUT_SECONDS if provider == "featherless" else AIML_TIMEOUT_SECONDS)
    retry_timeout = FEATHERLESS_RETRY_TIMEOUT_SECONDS if provider == "featherless" else effective_timeout
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=float(max(effective_timeout, retry_timeout)))
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        response = await _create_completion_with_retry(
            client=client,
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            timeout_seconds=effective_timeout,
            retry_timeout_seconds=retry_timeout,
        )
        content = response.choices[0].message.content or ""
        if not content.strip():
            return {
                "ok": False,
                "provider": provider,
                "model": model,
                "content": "",
                "error": "empty_content",
            }
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


async def _create_completion_with_retry(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    max_tokens: int,
    timeout_seconds: int,
    retry_timeout_seconds: int,
):
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
        ),
        timeout=timeout_seconds,
    )
    choice = response.choices[0]
    content = choice.message.content or ""
    finish_reason = choice.finish_reason
    reasoning_exists = bool(getattr(choice.message, "reasoning", None))

    if content.strip():
        return response

    if finish_reason == "length" or reasoning_exists or not content.strip():
        retry_tokens = min(MAX_RETRY_TOKENS, max_tokens * 2)
        return await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=retry_tokens,
            ),
            timeout=retry_timeout_seconds,
        )

    return response


def get_featherless_model() -> str:
    return os.getenv("FEATHERLESS_MODEL") or FEATHERLESS_DEFAULT_MODEL


def _model_label(model: str) -> str:
    label = model.split("/")[-1]
    mapping = {
        "Kimi-K2.6": "kimi-k2.6",
        "gemma-4-31B-it": "gemma-4-31b-it",
        "Qwen3.6-35B-A3B": "qwen-3.6-35b-a3b",
        "DeepSeek-V4-Pro": "deepseek-v4-pro",
    }
    return mapping.get(label, label.replace("_", "-").lower())


def build_detailed_report_prompt(context: dict) -> str:
    prompt_context = compact_report_context(context)
    return f"""
Generate a structured detailed disaster risk report for emergency managers.
Return final JSON only. Do not include reasoning. Keep output concise. No markdown.

Required JSON shape:
{{
  "detailed_body": "concise paragraph, max 120 words",
  "technical_analysis": "concise paragraph, max 100 words",
  "recommendations": ["3 to 5 short actions"],
  "response_priorities": ["3 to 5 short priorities"],
  "assumptions": ["2 to 4 short assumptions"],
  "limitations": ["2 to 4 short limitations"]
}}

Writing style:
- executive but technical
- clear for emergency managers
- concise
- suitable for a high-stakes disaster intelligence report

Use event ID, location, hazard type, severity, satellite metadata, affected area, damage percent,
hazard zones, population impact, hospitals at risk, roads blocked, schools affected,
vulnerability score, available routes, and critical facilities.

Context JSON:
{json.dumps(prompt_context, indent=2)}
""".strip()


def _focused_json_prompt(task: str, context: dict, schema: dict) -> str:
    return f"""
{task}
Return final JSON only. No markdown. No reasoning.
Schema:
{json.dumps(schema, indent=2)}

Compact event context:
{json.dumps(compact_report_context(context), indent=2)}
""".strip()


def build_executive_summary_prompt(context: dict, detailed_report: dict) -> str:
    summary_context = {
        **compact_report_context(context),
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
        parsed = parse_json_object(text)
    except (json.JSONDecodeError, ValueError, SyntaxError):
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
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    start = cleaned.find("{")
    if start == -1:
        return cleaned

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start : index + 1]

    end = cleaned.rfind("}")
    if end != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


def parse_json_object(text: str) -> dict:
    extracted = extract_json(text)
    try:
        parsed = json.loads(extracted)
    except json.JSONDecodeError:
        parsed = ast.literal_eval(extracted)
    if not isinstance(parsed, dict):
        raise ValueError("Parsed JSON content is not an object")
    return parsed


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


def compact_report_context(context: dict) -> dict:
    zones = context.get("analysis", {}).get("zones", {}).get("features", [])
    route_features = context.get("routes", {}).get("evacuation_routes", {}).get("features", [])
    return {
        "event_id": context.get("event_id"),
        "location": context.get("location"),
        "hazard_type": context.get("hazard_type"),
        "overall_severity": context.get("overall_severity"),
        "satellite": context.get("satellite"),
        "analysis": {
            "index_type": context.get("analysis", {}).get("index_type"),
            "mean_value": context.get("analysis", {}).get("mean_value"),
            "affected_area_km2": context.get("analysis", {}).get("affected_area_km2"),
            "damage_percent": context.get("analysis", {}).get("damage_percent"),
            "total_zones": context.get("analysis", {}).get("total_zones"),
            "zones": [
                {
                    "zone_id": feature.get("properties", {}).get("zone_id"),
                    "severity": feature.get("properties", {}).get("severity"),
                    "class_name": feature.get("properties", {}).get("class_name"),
                    "area_km2": feature.get("properties", {}).get("area_km2"),
                }
                for feature in zones
            ],
        },
        "hazard": context.get("hazard"),
        "impact": {
            **context.get("impact", {}),
            "critical_facilities": [
                {
                    "name": facility.get("name"),
                    "type": facility.get("type"),
                    "risk": facility.get("risk"),
                }
                for facility in context.get("impact", {}).get("critical_facilities", [])
            ],
        },
        "routes": {
            "evacuation_route_count": len(route_features),
            "route_names": [feature.get("properties", {}).get("name") for feature in route_features],
        },
    }
