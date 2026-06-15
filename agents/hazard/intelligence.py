import asyncio
import ast
import json
import os
import re
from typing import Any

import openai
from dotenv import load_dotenv


load_dotenv()

FEATHERLESS_MODELS = [
    "google/gemma-4-31B-it",       # primary
    "moonshotai/Kimi-K2.6",        # fallback
    "Qwen/Qwen3.6-35B-A3B",        # fallback
    "deepseek-ai/DeepSeek-V4-Pro", # fallback
]

featherless_client = openai.OpenAI(
    api_key=os.getenv("FEATHERLESS_API_KEY"),
    base_url="https://api.featherless.ai/v1",
)

opus_client = openai.AsyncOpenAI(
    api_key=os.getenv("AIML_API_KEY"),
    base_url="https://api.aimlapi.com/v1",
)


def strip_markdown(text: str | None) -> str:
    """Remove common markdown wrappers from model output."""
    if not text:
        return ""

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json|JSON|python|text)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _extract_json_object(text: str) -> dict[str, Any] | None:
    cleaned = strip_markdown(text)

    for candidate in (cleaned, _between_braces(cleaned)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(candidate)
            except (SyntaxError, ValueError):
                continue

        if isinstance(parsed, dict):
            return parsed

    return None


def _between_braces(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


def _message_content(response: Any) -> str:
    content = response.choices[0].message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
            for part in content
        )
    return str(content or "")


async def featherless_call(prompt: str, system: str = "", timeout: int = 30) -> str | None:
    """Call Featherless models in order and return the first successful response."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    for model in FEATHERLESS_MODELS:
        try:
            response = await asyncio.to_thread(
                featherless_client.chat.completions.create,
                model=model,
                messages=messages,
                temperature=0.1,
                timeout=timeout,
            )
            text = strip_markdown(_message_content(response))
            if text:
                print(f"Featherless model worked: {model}")
                return text
        except Exception as exc:
            print(f"Featherless model failed: {model} ({exc})")
            await asyncio.sleep(1.5)

    return None


async def opus_fallback(prompt: str, system: str = "") -> str | None:
    """Call Claude Opus through AIML API and return cleaned text."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        response = await opus_client.chat.completions.create(
            model="claude-opus-4-8",
            messages=messages,
            temperature=0.1,
        )
        return strip_markdown(_message_content(response))
    except Exception as exc:
        print(f"Opus fallback failed: {exc}")
        return None


async def smart_llm_call(
    prompt: str,
    system: str = "",
    criticality: str = "normal",
) -> str | None:
    """Route requests by criticality across Featherless and Opus."""
    criticality = (criticality or "normal").lower()

    if criticality == "low":
        return await featherless_call(prompt, system=system)

    if criticality == "high":
        return await opus_fallback(prompt, system=system)

    if criticality == "critical":
        opus_text = await opus_fallback(prompt, system=system)
        if not opus_text:
            return None

        verify_prompt = (
            "Verify the following answer against the original request. "
            "If it is valid, return it unchanged. If it has format or logic errors, "
            "return a corrected final answer only.\n\n"
            f"ORIGINAL REQUEST:\n{prompt}\n\n"
            f"ANSWER TO VERIFY:\n{opus_text}"
        )
        verified = await featherless_call(verify_prompt, system=system)
        return verified or opus_text

    featherless_text = await featherless_call(prompt, system=system)
    if featherless_text:
        return featherless_text
    return await opus_fallback(prompt, system=system)


async def parse_input(raw_message: str) -> dict:
    """Extract normalized satellite handoff fields from a raw Band message."""
    system = (
        "You extract disaster satellite handoff fields. Return JSON only with keys: "
        "event_id, bbox, affected_area_km2, geojson_url, risk_cities, urgency. "
        "Use null for unknown scalar values and [] for unknown lists."
    )
    prompt = (
        "Extract these fields from the raw message as strict JSON:\n"
        "- event_id: string or null\n"
        "- bbox: [minLng, minLat, maxLng, maxLat] or null\n"
        "- affected_area_km2: number or null\n"
        "- geojson_url: string or null\n"
        "- risk_cities: list of strings\n"
        "- urgency: CRITICAL, HIGH, MEDIUM, LOW, or null\n\n"
        f"RAW MESSAGE:\n{raw_message}"
    )

    response = await smart_llm_call(prompt, system=system, criticality="low")
    parsed = _extract_json_object(response or "") if response else None
    fallback = _fallback_parse_input(raw_message)

    if not parsed:
        return fallback

    return {
        "event_id": parsed.get("event_id") or fallback["event_id"],
        "bbox": _normalize_bbox(parsed.get("bbox")) or fallback["bbox"],
        "affected_area_km2": _to_float(parsed.get("affected_area_km2"))
        if parsed.get("affected_area_km2") is not None
        else fallback["affected_area_km2"],
        "geojson_url": parsed.get("geojson_url") or fallback["geojson_url"],
        "risk_cities": _normalize_str_list(parsed.get("risk_cities"))
        or fallback["risk_cities"],
        "urgency": _normalize_risk(parsed.get("urgency")) or fallback["urgency"],
    }


async def devise_strategy(parsed_input: dict, available_data: dict) -> dict:
    """Ask the LLM for a hazard-analysis strategy with safe defaults."""
    system = (
        "You are the HazardMind hazard strategy planner. Return strict JSON only. "
        "Focus on practical analysis choices for flood, earthquake, and landslide risk."
    )
    prompt = (
        "Create a hazard analysis strategy from the parsed input and available data. "
        "Return JSON with keys: primary_approach, fallback, risks, confidence, "
        "focus_hazard.\n\n"
        f"PARSED INPUT:\n{json.dumps(parsed_input, ensure_ascii=True)}\n\n"
        f"AVAILABLE DATA:\n{json.dumps(available_data, ensure_ascii=True)}"
    )

    response = await smart_llm_call(prompt, system=system, criticality="normal")
    parsed = _extract_json_object(response or "") if response else None
    if not parsed:
        return _fallback_strategy(parsed_input, available_data)

    fallback = _fallback_strategy(parsed_input, available_data)
    return {
        "primary_approach": parsed.get("primary_approach")
        or fallback["primary_approach"],
        "fallback": parsed.get("fallback") or fallback["fallback"],
        "risks": _normalize_str_list(parsed.get("risks")) or fallback["risks"],
        "confidence": _clamp_confidence(parsed.get("confidence"), fallback["confidence"]),
        "focus_hazard": _normalize_focus_hazard(parsed.get("focus_hazard"))
        or fallback["focus_hazard"],
    }


async def interpret_results(raw_results: dict, disaster_type: str) -> dict:
    """Interpret hazard outputs through an expert disaster response lens."""
    system = "You are a disaster response expert with 20 years field experience."
    prompt = (
        "Interpret these disaster analysis results. Return strict JSON only with keys: "
        "expert_summary, severity_assessment, anomalies, confidence, recommendations, "
        "needs_verification.\n"
        "severity_assessment must be one of CRITICAL, HIGH, MEDIUM, LOW. "
        "confidence must be a number from 0 to 1.\n\n"
        f"DISASTER TYPE:\n{disaster_type}\n\n"
        f"RAW RESULTS:\n{json.dumps(raw_results, ensure_ascii=True)}"
    )

    response = await smart_llm_call(prompt, system=system, criticality="normal")
    interpreted = _normalize_interpretation(_extract_json_object(response or ""), raw_results)

    if interpreted["severity_assessment"] == "CRITICAL":
        critical_response = await smart_llm_call(prompt, system=system, criticality="critical")
        critical_interpreted = _normalize_interpretation(
            _extract_json_object(critical_response or ""),
            raw_results,
        )
        if critical_interpreted:
            return critical_interpreted

    return interpreted


async def handle_anomaly(anomaly_type: str, context: dict) -> dict:
    """Ask Opus for recovery guidance when an anomaly blocks normal processing."""
    prompt = (
        "Create a recovery strategy for this hazard-analysis anomaly. Return strict JSON "
        "with keys: what_went_wrong, can_continue, alternative_approach, alert_human, "
        "confidence_in_recovery.\n\n"
        f"ANOMALY TYPE:\n{anomaly_type}\n\n"
        f"CONTEXT:\n{json.dumps(context, ensure_ascii=True)}"
    )
    response = await smart_llm_call(prompt, criticality="high")
    parsed = _extract_json_object(response or "") if response else None
    if not parsed:
        return _anomaly_parse_failure(anomaly_type)

    return {
        "what_went_wrong": parsed.get("what_went_wrong") or anomaly_type,
        "can_continue": bool(parsed.get("can_continue", False)),
        "alternative_approach": parsed.get("alternative_approach")
        or "manual review required",
        "alert_human": bool(parsed.get("alert_human", True)),
        "confidence_in_recovery": _clamp_confidence(
            parsed.get("confidence_in_recovery"),
            0.0,
        ),
    }


async def write_band_message(
    results: dict,
    next_agent_handle: str,
    anomalies: list = [],
) -> str:
    """Write a concise natural Band handoff message for the next agent."""
    handle = f"@{str(next_agent_handle).lstrip('@')}"
    risks = _extract_risk_fields(results)
    confidence = results.get("confidence")
    suffix = f"event_id: {results.get('event_id')} and confidence: {confidence}"
    anomaly_text = ", ".join(_normalize_str_list(anomalies)) if anomalies else "none"

    prompt = (
        "Write a natural agent-to-agent disaster pipeline handoff message. It must not "
        "be JSON. It must start with the provided handle, include flood_risk, "
        "earthquake_risk, landslide_risk, overall_severity, include a confidence score, "
        "flag anomalies if any, stay under 150 words, and end with the exact suffix.\n\n"
        f"HANDLE:\n{handle}\n\n"
        f"SUFFIX:\n{suffix}\n\n"
        f"RESULTS:\n{json.dumps(results, ensure_ascii=True)}\n\n"
        f"ANOMALIES:\n{anomaly_text}"
    )
    response = await smart_llm_call(prompt, criticality="normal")
    if not response:
        return _fallback_band_message(handle, risks, results, anomalies, suffix)

    message = strip_markdown(response)
    if not message.startswith(handle):
        message = f"{handle} {message.lstrip('@')}"
    if not message.endswith(suffix):
        message = re.sub(r"\s*event_id:.*$", "", message).strip()
        message = f"{message} {suffix}"
    if len(message.split()) > 150:
        return _fallback_band_message(handle, risks, results, anomalies, suffix)

    return message


async def quality_check(result: dict) -> dict:
    """Validate the outgoing hazard result before DB write or Band handoff."""
    risks = _extract_risk_fields(result)
    confidence_scores = _extract_confidence_scores(result)
    event_id = result.get("event_id")

    checks = {
        "flood_risk": risks["flood_risk"]
        in {"CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"},
        "earthquake_risk": risks["earthquake_risk"]
        in {"CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"},
        "landslide_risk": risks["landslide_risk"]
        in {"CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"},
        "overall_severity": risks["overall_severity"]
        in {"CRITICAL", "HIGH", "MEDIUM", "LOW"},
        "confidence_scores": all(
            key in confidence_scores for key in ("flood", "earthquake", "landslide")
        ),
        "event_id": event_id is not None and str(event_id).strip() != "",
    }

    if all(checks.values()):
        return {"status": "ready", "passed": True, "checks": checks}

    failed_checks = {key: value for key, value in checks.items() if not value}
    strategy = await handle_anomaly("quality_check_failed", failed_checks)
    return {
        "status": "failed",
        "passed": False,
        "checks": checks,
        "recovery": strategy,
    }


def _fallback_parse_input(raw_message: str) -> dict[str, Any]:
    data = _extract_json_object(raw_message) or {}

    boundaries = data.get("boundaries") if isinstance(data.get("boundaries"), dict) else {}
    artifacts = data.get("artifacts") if isinstance(data.get("artifacts"), dict) else {}
    analysis = data.get("analysis") if isinstance(data.get("analysis"), dict) else {}

    event_id = data.get("event_id") or _regex_value(raw_message, r'"?event_id"?\s*[:=]\s*"([^"]+)"')
    bbox = _normalize_bbox(boundaries.get("bbox") or data.get("bbox"))
    affected_area = _to_float(analysis.get("affected_area_km2") or data.get("affected_area_km2"))
    geojson_url = (
        artifacts.get("geojson_url")
        or data.get("geojson_url")
        or _regex_value(raw_message, r"https?://[^\s\"']+\.geojson")
    )
    risk_cities = _normalize_str_list(boundaries.get("risk_cities") or data.get("risk_cities"))

    return {
        "event_id": event_id,
        "bbox": bbox,
        "affected_area_km2": affected_area,
        "geojson_url": geojson_url,
        "risk_cities": risk_cities,
        "urgency": _infer_urgency(affected_area),
    }


def _fallback_strategy(parsed_input: dict, available_data: dict) -> dict[str, Any]:
    affected_area = _to_float(parsed_input.get("affected_area_km2")) or 0.0
    has_bbox = bool(parsed_input.get("bbox"))
    data_keys = {key for key, value in available_data.items() if value}

    focus_hazard = "flood" if affected_area >= 25 else "multi_hazard"
    if "earthquakes" in data_keys or "usgs" in data_keys:
        focus_hazard = "earthquake" if affected_area < 25 else focus_hazard
    if "slope" in data_keys or "dem" in data_keys:
        focus_hazard = "landslide" if affected_area < 25 else focus_hazard

    return {
        "primary_approach": (
            "Run flood, earthquake, and landslide scoring using the satellite bbox, "
            "affected area, live GDACS/USGS events, and terrain data where available."
        ),
        "fallback": (
            "If live APIs or model calls fail, use satellite affected area, bbox overlap, "
            "and conservative UNKNOWN/LOW confidence defaults."
        ),
        "risks": [
            "Live disaster APIs may be delayed or sparse for local events.",
            "Satellite-derived affected area may need calibration against ground reports.",
            "Terrain-driven landslide estimates are weaker without high-resolution slope data.",
        ],
        "confidence": 0.7 if has_bbox and affected_area else 0.45,
        "focus_hazard": focus_hazard,
    }


def _normalize_interpretation(parsed: dict[str, Any] | None, raw_results: dict) -> dict:
    fallback_severity = _normalize_risk(
        raw_results.get("overall_severity")
        or raw_results.get("severity")
        or raw_results.get("risk")
    )
    parsed = parsed or {}
    severity = _normalize_risk(parsed.get("severity_assessment")) or fallback_severity or "LOW"

    return {
        "expert_summary": parsed.get("expert_summary")
        or "Hazard results reviewed with available automated analysis.",
        "severity_assessment": severity,
        "anomalies": _normalize_str_list(parsed.get("anomalies")),
        "confidence": _clamp_confidence(parsed.get("confidence"), 0.5),
        "recommendations": _normalize_str_list(parsed.get("recommendations"))
        or ["Continue pipeline handoff and preserve event_id for downstream impact analysis."],
        "needs_verification": _normalize_str_list(parsed.get("needs_verification")),
    }


def _anomaly_parse_failure(anomaly_type: str) -> dict[str, Any]:
    return {
        "can_continue": False,
        "alert_human": True,
        "what_went_wrong": anomaly_type,
        "alternative_approach": "manual review required",
        "confidence_in_recovery": 0.0,
    }


def _extract_risk_fields(result: dict) -> dict[str, str | None]:
    hazard = result.get("hazard") if isinstance(result.get("hazard"), dict) else {}
    return {
        "flood_risk": _normalize_risk(result.get("flood_risk") or hazard.get("flood_risk")),
        "earthquake_risk": _normalize_risk(
            result.get("earthquake_risk") or hazard.get("earthquake_risk")
        ),
        "landslide_risk": _normalize_risk(
            result.get("landslide_risk") or hazard.get("landslide_risk")
        ),
        "overall_severity": _normalize_risk(
            result.get("overall_severity") or hazard.get("overall_severity")
        ),
    }


def _extract_confidence_scores(result: dict) -> dict:
    hazard = result.get("hazard") if isinstance(result.get("hazard"), dict) else {}
    confidence_scores = result.get("confidence_scores") or hazard.get("confidence_scores")
    return confidence_scores if isinstance(confidence_scores, dict) else {}


def _fallback_band_message(
    handle: str,
    risks: dict[str, str | None],
    results: dict,
    anomalies: list,
    suffix: str,
) -> str:
    anomaly_text = (
        f" Anomalies flagged: {', '.join(_normalize_str_list(anomalies))}."
        if anomalies
        else " No anomalies flagged."
    )
    message = (
        f"{handle} Hazard analysis complete. flood_risk: {risks['flood_risk']}, "
        f"earthquake_risk: {risks['earthquake_risk']}, "
        f"landslide_risk: {risks['landslide_risk']}, "
        f"overall_severity: {risks['overall_severity']}. "
        f"Confidence score: {results.get('confidence')}."
        f"{anomaly_text} {suffix}"
    )
    words = message.split()
    if len(words) <= 150:
        return message

    trimmed = " ".join(words[: max(0, 150 - len(suffix.split()) - 1)])
    return f"{trimmed} {suffix}"


def _regex_value(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _normalize_bbox(value: Any) -> list[float] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            numbers = re.findall(r"-?\d+(?:\.\d+)?", value)
            value = numbers

    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None

    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return None


def _normalize_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_risk(value: Any) -> str | None:
    if not value:
        return None
    risk = str(value).upper()
    return risk if risk in {"CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"} else None


def _normalize_focus_hazard(value: Any) -> str | None:
    if not value:
        return None
    hazard = str(value).lower()
    return hazard if hazard in {"flood", "earthquake", "landslide", "multi_hazard"} else None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp_confidence(value: Any, default: float) -> float:
    confidence = _to_float(value)
    if confidence is None:
        return default
    return max(0.0, min(1.0, confidence))


def _infer_urgency(affected_area_km2: float | None) -> str | None:
    if affected_area_km2 is None:
        return None
    if affected_area_km2 >= 150:
        return "CRITICAL"
    if affected_area_km2 >= 75:
        return "HIGH"
    if affected_area_km2 >= 20:
        return "MEDIUM"
    return "LOW"


if __name__ == "__main__":
    import asyncio

    async def test():
        sample = '{"agent":"hazardmind-satellite","event_id":"test-123","status":"complete","boundaries":{"bbox":[71.5,33.9,72.1,34.3],"risk_cities":["Peshawar"]},"analysis":{"affected_area_km2":153.37,"mean_value":0.24},"artifacts":{"geojson_url":"https://pub-720f47eaad2f4997a76a02f8bf14f58a.r2.dev/events/test-123/zones.geojson"}}'
        result = await parse_input(sample)
        print("parse_input:", result)

    asyncio.run(test())
