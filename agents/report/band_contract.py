import json


REPORT_AGENT_NAME = "hazardmind-report"
ORCHESTRATOR_MENTION = "@hazardmind-orchestrator"


def extract_trailing_json(message: str) -> dict:
    """
    Extract and parse the final JSON object from a Band natural-language message.
    The message may contain natural text before the JSON.
    Must handle whitespace, markdown fences, and extra text before JSON.
    Raise ValueError with safe message if no JSON is found.
    """
    if not isinstance(message, str) or not message.strip():
        raise ValueError("No JSON object found in message.")

    candidates = _json_object_candidates(message)
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("No valid JSON object found in message.")


def parse_report_trigger_message(message: str) -> dict:
    """
    Parse orchestrator -> report Band message.
    """
    payload = extract_trailing_json(message)
    event_id = str(payload.get("event_id") or "").strip()
    sender = str(payload.get("from") or "").strip()
    recipient = str(payload.get("to") or "").strip()
    data = payload.get("data")
    anomalies = payload.get("anomalies", [])

    if not event_id:
        raise ValueError("Report trigger message missing event_id.")
    if not sender:
        raise ValueError("Report trigger message missing from.")
    if not _is_report_recipient(recipient):
        raise ValueError("Report trigger message recipient is not hazardmind-report.")
    if not isinstance(data, dict):
        raise ValueError("Report trigger message data must be an object.")
    if anomalies is None:
        anomalies = []
    if not isinstance(anomalies, list):
        raise ValueError("Report trigger message anomalies must be a list.")

    return {
        "event_id": event_id,
        "from": sender,
        "to": REPORT_AGENT_NAME,
        "impact_data": data,
        "data": data,
        "anomalies": anomalies,
        "raw_payload": payload,
    }


def build_report_completion_message(result: dict) -> str:
    """
    Build natural text + JSON completion signal for Band.
    """
    if result.get("status") == "failed":
        return build_report_failure_message(result)

    report = result.get("report", {}) if isinstance(result, dict) else {}
    report_section = report.get("report", {}) if isinstance(report, dict) else {}
    impact = report.get("impact", {}) if isinstance(report, dict) else {}
    event_id = str(result.get("event_id") or report.get("event_id") or "")
    location = report.get("location") or "the affected area"
    hazard_type = report.get("hazard_type") or "disaster"
    severity = report.get("overall_severity") or "UNKNOWN"
    affected = _first_present(
        impact.get("population_affected"),
        impact.get("total_affected"),
        result.get("population_affected"),
    )
    hospitals = _first_present(impact.get("hospitals_at_risk"), result.get("hospitals_at_risk"))
    pdf_url = result.get("pdf_url") or report_section.get("pdf_url") or ""
    map_url = result.get("map_url") or report_section.get("map_url") or ""
    executive_summary = result.get("summary") or report_section.get("summary") or ""
    confidence_level = result.get("confidence_level") or report_section.get("confidence_level") or ""
    response_level = (
        result.get("recommended_response_level")
        or report_section.get("recommended_response_level")
        or report.get("recommended_response_level")
        or "NDMA Level-1"
    )

    natural_text = (
        f"{ORCHESTRATOR_MENTION} Report Agent completed the executive report for "
        f"{location} ({hazard_type}, severity {severity}). "
        f"Affected people: {_format_number(affected)}; hospitals at risk: {_format_number(hospitals)}. "
        "PDF and map outputs are ready."
    )
    completion = {
        "event_id": event_id,
        "agent": REPORT_AGENT_NAME,
        "status": "complete",
        "step": "report",
        "data": {
            "pdf_url": pdf_url,
            "map_url": map_url,
            "executive_summary": executive_summary,
            "confidence_level": confidence_level,
            "recommended_response_level": response_level,
        },
    }
    return f"{natural_text}\n\n{json.dumps(completion, indent=2)}"


def build_report_failure_message(result: dict) -> str:
    event_id = str(result.get("event_id") or "")
    error = str(result.get("error") or "LLM generation failed")
    natural_text = (
        f"{ORCHESTRATOR_MENTION} Report Agent failed for event {event_id}. "
        "No production disaster report was completed."
    )
    failure = {
        "event_id": event_id,
        "agent": REPORT_AGENT_NAME,
        "status": "failed",
        "step": "report",
        "data": {
            "error": error,
            "confidence_level": "LOW",
        },
    }
    return f"{natural_text}\n\n{json.dumps(failure, indent=2)}"


def _json_object_candidates(text: str) -> list[str]:
    candidates = []
    stack = 0
    start = None
    in_string = False
    escaped = False

    for index, char in enumerate(text):
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
            if stack == 0:
                start = index
            stack += 1
        elif char == "}" and stack:
            stack -= 1
            if stack == 0 and start is not None:
                candidates.append(text[start : index + 1])
                start = None
    return candidates


def _is_report_recipient(value: str) -> bool:
    normalized = value.strip().lower().lstrip("@")
    return normalized == REPORT_AGENT_NAME or normalized.endswith(f"/{REPORT_AGENT_NAME}")


def _first_present(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def _format_number(value) -> str:
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return str(value or "unknown")
