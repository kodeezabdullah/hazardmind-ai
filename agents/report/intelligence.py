import json

from llm_clients import (
    AIML_GPT_LAST_RESORT,
    AIML_OPUS,
    FEATHERLESS_DEEPSEEK,
    FEATHERLESS_CHECK_TOKENS,
    FEATHERLESS_GEMMA,
    FEATHERLESS_JSON_TOKENS,
    FEATHERLESS_KIMI,
    FEATHERLESS_MAP_TOKENS,
    FEATHERLESS_QWEN,
    FEATHERLESS_RECOMMENDATION_TOKENS,
    call_aiml,
    featherless_json_cascade,
)


async def assess_event_criticality(report_context: dict) -> dict:
    """
    Determines criticality level, escalation need, and why.
    """
    fallback = _fallback_criticality(report_context)
    data, source = await featherless_json_cascade(
        purpose="criticality",
        prompt=
        _json_prompt(
            "Assess event criticality for emergency leadership.",
            report_context,
            {
                "criticality": "low|normal|high|critical",
                "overall_confidence": "number from 0.0 to 1.0",
                "escalation_required": "boolean",
                "rationale": "short user-facing explanation",
                "trigger_factors": ["short factors"],
            },
        ),
        system=_json_system("You are a disaster-risk criticality analyst."),
        primary_model=FEATHERLESS_KIMI,
        fallback_models=[FEATHERLESS_DEEPSEEK, FEATHERLESS_GEMMA],
        max_tokens=FEATHERLESS_JSON_TOKENS,
        timeout_seconds=45,
        required_keys=["criticality", "overall_confidence", "escalation_required", "rationale", "trigger_factors"],
    )
    result = _coerce_criticality(data, fallback)
    result["_source"] = source
    return result


async def detect_anomalies(report_context: dict) -> dict:
    """
    Detects abnormal or suspicious conditions in the pipeline data.
    """
    validation = _validate_anomaly_inputs(report_context)
    if not validation["anomalies"] and not validation["warnings"]:
        return {
            "source": "data_validation",
            "status": "clear",
            "priority": "low",
            "anomalies_detected": False,
            "anomalies": [],
            "warnings": [],
            "summary": "Data validation found no incoming anomalies, missing required sections, or consistency conflicts.",
            "_source": "data_validation",
        }

    data, source = await featherless_json_cascade(
        purpose="anomaly_check",
        prompt=_anomaly_prompt(report_context, validation),
        system=_json_system("You are a disaster data quality analyst."),
        primary_model=FEATHERLESS_QWEN,
        fallback_models=[FEATHERLESS_KIMI, FEATHERLESS_GEMMA],
        max_tokens=FEATHERLESS_CHECK_TOKENS,
        timeout_seconds=45,
        required_keys=["status", "priority", "anomalies", "summary"],
    )
    if source == "deterministic_fallback":
        result = _coerce_anomalies({}, validation)
        result["source"] = "llm_required"
        result["_source"] = "llm_required_failed"
        result["summary"] = "Rule-based validation detected anomalies, but live LLM interpretation did not complete."
        return result

    result = _coerce_anomalies(data, validation)
    result["source"] = "llm_interpretation"
    result["_source"] = source
    return result


async def generate_map_narrative(report_context: dict) -> dict:
    """
    Explains what the map means in operational terms.

    LLM enhancement path: Featherless Gemma first, then Kimi/DeepSeek.
    If that live map narration fails, this section falls back to an honest
    cartographic_data_summary built from spatial metadata, not fake LLM text.
    """
    data, source = await featherless_json_cascade(
        purpose="map_narrative",
        prompt=
        _json_prompt(
            "Explain the static risk map in operational terms for emergency managers.",
            report_context,
            {
                "map_narrative": "concise paragraph",
                "key_spatial_findings": ["finding"],
                "hotspots": ["hotspot"],
                "map_limitations": ["limitation"],
            },
        ),
        system=_json_system("You are a geospatial disaster intelligence analyst."),
        primary_model=FEATHERLESS_GEMMA,
        fallback_models=[FEATHERLESS_KIMI, FEATHERLESS_DEEPSEEK],
        max_tokens=FEATHERLESS_MAP_TOKENS,
        timeout_seconds=45,
        required_keys=["map_narrative", "key_spatial_findings", "hotspots", "map_limitations"],
    )
    cartographic_summary = build_cartographic_data_summary(report_context, report_context)
    if source == "deterministic_fallback":
        cartographic_summary["warnings"].append("Live map narrative unavailable; using cartographic data summary.")
        cartographic_summary["_source"] = "cartographic_data_summary"
        return cartographic_summary

    result = _coerce_map_narrative(data, cartographic_summary)
    result["source"] = source
    result["llm_used"] = True
    result["cartographic_summary"] = cartographic_summary
    result["_source"] = source
    return result


async def generate_priority_recommendations(report_context: dict) -> dict:
    """
    Generates action priorities for the next 6, 24, and 72 hours.
    """
    fallback = _fallback_priority_timeline(report_context)
    data, source = await featherless_json_cascade(
        purpose="priority_recommendations",
        prompt=
        _json_prompt(
            "Generate concise operational disaster response priorities. Keep each item under 12 words.",
            report_context,
            {
                "next_6_hours": ["three immediate actions"],
                "next_24_hours": ["three stabilization actions"],
                "next_72_hours": ["three recovery actions"],
                "resource_priorities": ["three resource needs"],
                "coordination_priorities": ["three coordination needs"],
            },
        ),
        system=_json_system("You are an emergency operations planning analyst."),
        primary_model=FEATHERLESS_DEEPSEEK,
        fallback_models=[FEATHERLESS_KIMI, FEATHERLESS_GEMMA],
        max_tokens=FEATHERLESS_RECOMMENDATION_TOKENS,
        timeout_seconds=45,
        required_keys=None,
    )
    result = _coerce_priority_timeline(data, fallback)
    result["_source"] = source
    return result


async def generate_decision_brief(report_context: dict, intelligence: dict) -> dict:
    """
    Generates a concise official decision-maker brief.
    """
    fallback = _fallback_decision_brief(report_context, intelligence)
    prompt = _json_prompt(
        "Generate an official decision-maker brief for senior responders.",
        {"event": _compact_context(report_context), "intelligence": intelligence},
        {
            "decision_brief": "concise official brief",
            "official_summary": "3 to 5 sentence official summary",
            "key_decisions_required": ["decision"],
            "human_review_required": "boolean",
        },
    )
    system = _json_system("You are Opus writing official high-stakes emergency briefs. Return strict JSON only.")

    response = await call_aiml(prompt, system=system, model=AIML_OPUS, max_tokens=1000, purpose="decision_brief")
    source = "aiml:opus-4.8"
    if not response["ok"] or not response["content"]:
        response = await call_aiml(
            prompt,
            system=system,
            model=AIML_GPT_LAST_RESORT,
            max_tokens=1000,
            purpose="decision_brief",
        )
        source = f"aiml:{AIML_GPT_LAST_RESORT}" if response["ok"] and response["content"] else "deterministic_fallback"

    try:
        data = json.loads(_extract_json(response["content"])) if response["ok"] else fallback
    except json.JSONDecodeError:
        data = fallback
        source = "deterministic_fallback"

    data = _coerce_decision_brief(data, fallback)
    data["_source"] = source
    return data


async def run_quality_check(report_context: dict, intelligence: dict) -> dict:
    """
    Checks completeness and readiness before final report.
    """
    fallback = _fallback_quality_check(report_context, intelligence)
    review, source = await featherless_json_cascade(
        purpose="quality_check",
        prompt=_json_prompt(
            "Review the deterministic checklist and add concise warnings or blocking issues if needed.",
            {"event": _compact_context(report_context), "intelligence": intelligence, "deterministic_check": fallback},
            {
                "warnings": ["warning"],
                "blocking_issues": ["issue"],
                "review_note": "short readiness note",
            },
        ),
        system=_json_system("You are a disaster-report quality controller."),
        primary_model=FEATHERLESS_QWEN,
        fallback_models=[FEATHERLESS_KIMI, FEATHERLESS_GEMMA],
        max_tokens=FEATHERLESS_CHECK_TOKENS,
        timeout_seconds=45,
        required_keys=["warnings", "blocking_issues"],
    )
    result = dict(fallback)
    if source != "deterministic_fallback":
        result["warnings"] = _merge_lists(fallback["warnings"], review.get("warnings", []))
        result["blocking_issues"] = _merge_lists(fallback["blocking_issues"], review.get("blocking_issues", []))
        if result["blocking_issues"]:
            result["status"] = "not_ready"
        elif result["warnings"]:
            result["status"] = "ready_with_warnings"
        result["review_note"] = str(review.get("review_note") or "")
        result["_source"] = f"hybrid:deterministic+{source}"
        return result

    result["_source"] = "deterministic_fallback"
    return result


async def generate_band_ready_message(report_context: dict, intelligence: dict) -> dict:
    """
    Generates a final message that can later be sent to Band.
    """
    criticality = intelligence.get("criticality", {})
    quality = intelligence.get("quality_check", {})
    confidence = float(criticality.get("overall_confidence", 0.75))
    status = "COMPLETE"
    if quality.get("status") == "not_ready":
        status = "NEEDS_REVIEW"
    elif quality.get("status") == "ready_with_warnings":
        status = "COMPLETE_WITH_WARNINGS"

    message = (
        f"HazardMind Report Agent complete for {report_context.get('event_id')} "
        f"({report_context.get('location')}, {report_context.get('hazard_type')}). "
        f"Criticality: {criticality.get('criticality', 'high')}; "
        f"confidence: {round(confidence * 100)}%. "
        f"Summary: {intelligence.get('decision_brief', {}).get('official_summary') or report_context.get('report', {}).get('summary')}"
    )
    return {
        "target": "@muhammad-abdullah",
        "message": message,
        "status": status,
        "confidence": round(confidence, 2),
        "_source": "template+intelligence",
    }


def strip_sources(intelligence: dict) -> tuple[dict, dict]:
    cleaned = {}
    sources = {}
    source_keys = {
        "criticality": "criticality",
        "anomalies": "anomaly_check",
        "map_narrative": "map_narrative",
        "priority_timeline": "priority_recommendations",
        "decision_brief": "decision_brief",
        "quality_check": "quality_check",
        "band_ready_message": "band_ready_message",
    }
    for key, value in intelligence.items():
        if isinstance(value, dict):
            item = dict(value)
            sources[source_keys.get(key, key)] = item.pop("_source", "deterministic_fallback")
            cleaned[key] = item
        else:
            cleaned[key] = value
    return cleaned, sources


def _json_system(role: str) -> str:
    return f"{role} Return final JSON only. Do not include reasoning. Keep output concise. No markdown."


def _json_prompt(task: str, context: dict, schema: dict) -> str:
    return f"""
{task}
Return final JSON only. No markdown. No reasoning.
Match this schema:
{json.dumps(schema, indent=2)}

Use concise user-facing rationales. Do not include hidden reasoning.

Context:
{json.dumps(_prompt_context(context), indent=2)}
""".strip()


def _anomaly_prompt(context: dict, validation: dict) -> str:
    schema = {
        "status": "clear | watch | critical",
        "priority": "low | medium | high",
        "anomalies": [
            {
                "type": "low_confidence | data_conflict | critical_infrastructure | missing_data | unusual_extent",
                "description": "short factual description",
                "action": "short handling instruction",
            }
        ],
        "summary": "one concise sentence",
    }
    prompt_context = {
        "event_id": context.get("event_id"),
        "disaster_type": context.get("hazard_type"),
        "location": context.get("location"),
        "severity": context.get("overall_severity"),
        "confidence": _confidence_snapshot(context),
        "incoming_anomalies": context.get("incoming_anomalies", []),
        "affected_area_km2": context.get("analysis", {}).get("affected_area_km2"),
        "total_affected": _first_present(
            context.get("impact", {}).get("population_affected"),
            context.get("impact", {}).get("total_affected"),
        ),
        "hospitals_at_risk": context.get("impact", {}).get("hospitals_at_risk"),
        "data_conflicts": validation.get("anomalies", []),
        "warnings": validation.get("warnings", []),
    }
    return f"""
Interpret and prioritize the detected disaster-data anomalies.
Return JSON only. No markdown. No reasoning.
Match this schema:
{json.dumps(schema, indent=2)}

Use only the compact context below. Do not invent new facts.

Context:
{json.dumps(prompt_context, indent=2)}
""".strip()


def build_cartographic_data_summary(report_context: dict, report: dict) -> dict:
    """
    Build an honest map narrative from actual spatial/map metadata.
    """
    del report
    analysis = report_context.get("analysis", {})
    impact = report_context.get("impact", {})
    boundaries = report_context.get("boundaries", {})
    routes = report_context.get("routes", {})
    report_section = report_context.get("report", {})
    zones = analysis.get("zones", {}) if isinstance(analysis.get("zones"), dict) else {}
    zone_features = zones.get("features", []) if isinstance(zones.get("features"), list) else []
    bbox = boundaries.get("bbox") if isinstance(boundaries.get("bbox"), list) else []
    route_count = _feature_count(routes.get("evacuation_routes"))
    facility_count = len(impact.get("critical_facilities", [])) if isinstance(impact.get("critical_facilities"), list) else 0
    hazard_zone_count = len(zone_features)
    satellite_total_zones = analysis.get("total_zones")
    affected_area = analysis.get("affected_area_km2")
    risk_levels = _risk_levels(zone_features)
    layers = _map_layers(report_context, hazard_zone_count, route_count, facility_count)
    warnings = []
    if not bbox:
        warnings.append("Map bbox is not available.")
    if hazard_zone_count == 0 and not satellite_total_zones:
        warnings.append("Hazard-zone geometry is not available for map narration.")
    if route_count == 0:
        warnings.append("No evacuation route geometry is available.")
    if not report_section.get("map_url"):
        warnings.append("Map URL is not assigned yet; static map generation runs later in the pipeline.")

    findings = []
    if affected_area not in (None, ""):
        findings.append(f"Affected area metadata reports {affected_area} km2.")
    if satellite_total_zones not in (None, ""):
        findings.append(f"Satellite analysis reports {satellite_total_zones} total zones.")
    if hazard_zone_count:
        findings.append(f"GeoJSON contains {hazard_zone_count} hazard-zone features.")
    if route_count:
        findings.append(f"Evacuation route layer contains {route_count} route feature(s).")
    if facility_count:
        findings.append(f"Critical facility layer contains {facility_count} facility marker(s).")
    if risk_levels:
        findings.append(f"Hazard risk levels present: {', '.join(risk_levels)}.")

    summary_parts = [
        f"The map metadata describes {report_context.get('hazard_type', 'hazard')} conditions for {report_context.get('location', 'the event area')}."
    ]
    summary_parts.extend(findings[:3])
    summary = " ".join(summary_parts)
    if not findings:
        summary += " Spatial layers are limited, so the map narrative is restricted to available metadata."

    return {
        "source": "cartographic_data_summary",
        "llm_used": False,
        "summary": summary,
        "map_narrative": summary,
        "layers": layers,
        "bbox": bbox,
        "hazard_zone_count": hazard_zone_count,
        "route_count": route_count,
        "warnings": warnings,
        "key_spatial_findings": findings,
        "hotspots": _hotspots(zone_features),
        "map_limitations": warnings or ["Map narrative is derived from available cartographic metadata."],
    }


def _prompt_context(context: dict) -> dict:
    if "event" in context or "intelligence" in context:
        return {
            "event": _compact_context(context.get("event", {})),
            "intelligence": context.get("intelligence", {}),
        }
    return _compact_context(context)


def _compact_context(context: dict) -> dict:
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
            "zone_features": len(context.get("analysis", {}).get("zones", {}).get("features", [])),
        },
        "hazard": context.get("hazard"),
        "impact": context.get("impact"),
        "routes": {
            "evacuation_route_count": len(context.get("routes", {}).get("evacuation_routes", {}).get("features", []))
        },
        "report": {
            "summary": context.get("report", {}).get("summary"),
            "recommendations": context.get("report", {}).get("recommendations", []),
            "pdf_url": context.get("report", {}).get("pdf_url"),
            "map_url": context.get("report", {}).get("map_url"),
        },
    }


def _extract_json(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


def _as_list(value, fallback: list[str]) -> list[str]:
    if isinstance(value, list):
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        if cleaned:
            return cleaned
    return fallback


def _as_bool(value, fallback: bool) -> bool:
    return value if isinstance(value, bool) else fallback


def _as_confidence(value, fallback: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return fallback


def _merge_lists(first: list[str], second) -> list[str]:
    merged = []
    seen = set()
    for value in [*(first or []), *(second if isinstance(second, list) else [])]:
        text = str(value).strip()
        if text and text.lower() not in seen:
            merged.append(text)
            seen.add(text.lower())
    return merged


def _feature_count(feature_collection) -> int:
    if isinstance(feature_collection, dict) and isinstance(feature_collection.get("features"), list):
        return len(feature_collection["features"])
    return 0


def _risk_levels(features: list[dict]) -> list[str]:
    levels = []
    seen = set()
    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties", {}) if isinstance(feature.get("properties"), dict) else {}
        level = str(properties.get("severity") or properties.get("risk_level") or "").upper()
        if level and level not in seen:
            levels.append(level)
            seen.add(level)
    return levels


def _map_layers(context: dict, hazard_zone_count: int, route_count: int, facility_count: int) -> list[dict]:
    artifacts = context.get("artifacts", {})
    layers = []
    if context.get("boundaries", {}).get("bbox"):
        layers.append({"name": "analysis_bbox", "status": "available"})
    layers.append({"name": "hazard_zones", "status": "available" if hazard_zone_count else "missing", "count": hazard_zone_count})
    layers.append({"name": "evacuation_routes", "status": "available" if route_count else "missing", "count": route_count})
    if facility_count:
        layers.append({"name": "critical_facilities", "status": "available", "count": facility_count})
    for key in ("true_color_url", "index_url", "classification_url", "geojson_url"):
        if artifacts.get(key):
            layers.append({"name": key.replace("_url", ""), "status": "available"})
    return layers


def _hotspots(features: list[dict]) -> list[str]:
    hotspots = []
    for index, feature in enumerate(features, start=1):
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties", {}) if isinstance(feature.get("properties"), dict) else {}
        severity = str(properties.get("severity") or properties.get("risk_level") or "").upper()
        if severity in {"CRITICAL", "HIGH"}:
            label = properties.get("zone_id") or properties.get("id") or properties.get("name") or f"zone {index}"
            hotspots.append(f"{label} ({severity})")
    return hotspots[:5]


def _validate_anomaly_inputs(context: dict) -> dict:
    anomalies: list[dict] = []
    warnings: list[str] = []

    incoming_anomalies = context.get("incoming_anomalies") or []
    if isinstance(incoming_anomalies, list):
        for item in incoming_anomalies:
            if isinstance(item, dict):
                anomalies.append(
                    _anomaly_item(
                        item.get("type") or "data_conflict",
                        item.get("description") or item.get("message") or "Incoming upstream anomaly was reported.",
                        item.get("action") or item.get("recommended_handling") or "Review upstream anomaly before final response.",
                        item.get("severity") or "medium",
                    )
                )
            elif str(item).strip():
                anomalies.append(
                    _anomaly_item(
                        "data_conflict",
                        str(item).strip(),
                        "Review upstream anomaly before final response.",
                        "medium",
                    )
                )
    elif incoming_anomalies:
        anomalies.append(
            _anomaly_item(
                "data_conflict",
                "Incoming anomalies field is not a list.",
                "Ask upstream agent to resend anomalies as a list.",
                "medium",
            )
        )

    impact = context.get("impact")
    analysis = context.get("analysis") or {}
    hazard = context.get("hazard") or {}
    zones = analysis.get("zones", {}).get("features", []) if isinstance(analysis.get("zones"), dict) else []

    if not isinstance(impact, dict) or not impact:
        anomalies.append(
            _anomaly_item(
                "missing_data",
                "Impact data is missing.",
                "Do not finalize operational impact claims until impact data is available.",
                "high",
            )
        )
    if not zones:
        anomalies.append(
            _anomaly_item(
                "missing_data",
                "Hazard zone geometry is missing.",
                "Require hazard-zone GeoJSON before using map outputs operationally.",
                "high",
            )
        )

    confidence = _minimum_confidence(context)
    if confidence is not None and confidence < 0.7:
        anomalies.append(
            _anomaly_item(
                "low_confidence",
                f"One or more confidence scores are below 70% ({round(confidence * 100)}%).",
                "Escalate for human review before issuing final response guidance.",
                "medium",
            )
        )

    affected_area = _as_float(analysis.get("affected_area_km2"))
    population = _as_float((impact or {}).get("population_affected") or (impact or {}).get("total_affected"))
    if affected_area is not None and population is not None and affected_area > 500 and population < 10000:
        anomalies.append(
            _anomaly_item(
                "unusual_extent",
                "Affected area is unusually large compared with exposed population.",
                "Validate spatial extent and exposure overlay before publication.",
                "medium",
            )
        )

    satellite = context.get("satellite") or {}
    cloud_cover = _as_float(satellite.get("cloud_cover"))
    if cloud_cover is not None and cloud_cover > 30 and satellite.get("type") != "sentinel-1":
        anomalies.append(
            _anomaly_item(
                "data_conflict",
                "Cloud cover is high but selected satellite source is not SAR.",
                "Verify sensor selection before finalizing map products.",
                "medium",
            )
        )

    return {"anomalies": anomalies, "warnings": warnings}


def _anomaly_item(item_type: str, description: str, action: str, severity: str = "medium") -> dict:
    return {
        "type": str(item_type or "data_conflict"),
        "severity": str(severity or "medium"),
        "description": str(description or ""),
        "action": str(action or ""),
        "recommended_handling": str(action or ""),
    }


def _confidence_snapshot(context: dict) -> dict:
    hazard_scores = context.get("hazard", {}).get("confidence_scores", {})
    return {
        "overall": _first_present(
            context.get("impact", {}).get("overall_confidence"),
            hazard_scores.get("overall"),
            hazard_scores.get("flood"),
        ),
        "flood": hazard_scores.get("flood"),
        "earthquake": hazard_scores.get("earthquake"),
        "landslide": hazard_scores.get("landslide"),
    }


def _minimum_confidence(context: dict) -> float | None:
    snapshot = _confidence_snapshot(context)
    active_hazard = str(context.get("hazard_type") or "").lower()
    active_key = "flood" if "flood" in active_hazard else active_hazard
    values = [_as_float(snapshot.get("overall"))]
    if active_key:
        values.append(_as_float(snapshot.get(active_key)))
    values = [value for value in values if value is not None]
    return min(values) if values else None


def _first_present(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_criticality(data: dict, fallback: dict) -> dict:
    criticality = str(data.get("criticality") or fallback["criticality"]).lower()
    if criticality not in {"low", "normal", "high", "critical"}:
        criticality = fallback["criticality"]
    return {
        "criticality": criticality,
        "overall_confidence": _as_confidence(data.get("overall_confidence"), fallback["overall_confidence"]),
        "escalation_required": _as_bool(data.get("escalation_required"), fallback["escalation_required"]),
        "rationale": str(data.get("rationale") or fallback["rationale"]),
        "trigger_factors": _as_list(data.get("trigger_factors"), fallback["trigger_factors"]),
    }


def _coerce_anomalies(data: dict, fallback: dict) -> dict:
    anomalies = data.get("anomalies")
    if not isinstance(anomalies, list):
        anomalies = fallback["anomalies"]
    cleaned = []
    for item in anomalies:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or item.get("recommended_handling") or "")
        cleaned.append(
            {
                "type": str(item.get("type") or "other"),
                "severity": str(item.get("severity") or "low"),
                "description": str(item.get("description") or ""),
                "action": action,
                "recommended_handling": action,
            }
        )
    status = str(data.get("status") or ("watch" if cleaned else "clear")).lower()
    if status not in {"clear", "watch", "critical"}:
        status = "watch" if cleaned else "clear"
    priority = str(data.get("priority") or ("medium" if cleaned else "low")).lower()
    if priority not in {"low", "medium", "high"}:
        priority = "medium" if cleaned else "low"
    warnings = data.get("warnings") if isinstance(data.get("warnings"), list) else fallback.get("warnings", [])
    return {
        "anomalies_detected": _as_bool(data.get("anomalies_detected"), bool(cleaned)),
        "status": status,
        "priority": priority,
        "anomalies": cleaned,
        "warnings": [str(item) for item in warnings if str(item).strip()],
        "summary": str(data.get("summary") or ("Detected anomalies require review." if cleaned else "No anomalies detected.")),
    }


def _coerce_map_narrative(data: dict, fallback: dict) -> dict:
    return {
        "source": fallback.get("source", "cartographic_data_summary"),
        "llm_used": False,
        "summary": str(data.get("summary") or data.get("map_narrative") or fallback.get("summary") or fallback["map_narrative"]),
        "map_narrative": str(data.get("map_narrative") or fallback["map_narrative"]),
        "key_spatial_findings": _as_list(data.get("key_spatial_findings"), fallback["key_spatial_findings"]),
        "hotspots": _as_list(data.get("hotspots"), fallback["hotspots"]),
        "map_limitations": _as_list(data.get("map_limitations"), fallback["map_limitations"]),
        "layers": fallback.get("layers", []),
        "bbox": fallback.get("bbox", []),
        "hazard_zone_count": fallback.get("hazard_zone_count", 0),
        "route_count": fallback.get("route_count", 0),
        "warnings": fallback.get("warnings", []),
    }


def _coerce_priority_timeline(data: dict, fallback: dict) -> dict:
    return {
        "next_6_hours": _as_list(data.get("next_6_hours"), fallback["next_6_hours"]),
        "next_24_hours": _as_list(data.get("next_24_hours"), fallback["next_24_hours"]),
        "next_72_hours": _as_list(data.get("next_72_hours"), fallback["next_72_hours"]),
        "resource_priorities": _as_list(data.get("resource_priorities"), fallback["resource_priorities"]),
        "coordination_priorities": _as_list(data.get("coordination_priorities"), fallback["coordination_priorities"]),
    }


def _coerce_decision_brief(data: dict, fallback: dict) -> dict:
    return {
        "decision_brief": str(data.get("decision_brief") or fallback["decision_brief"]),
        "official_summary": str(data.get("official_summary") or fallback["official_summary"]),
        "key_decisions_required": _as_list(data.get("key_decisions_required"), fallback["key_decisions_required"]),
        "human_review_required": _as_bool(data.get("human_review_required"), fallback["human_review_required"]),
    }


def _coerce_quality_check(data: dict, fallback: dict) -> dict:
    checks = data.get("checks") if isinstance(data.get("checks"), dict) else fallback["checks"]
    status = str(data.get("status") or fallback["status"])
    if status not in {"ready", "ready_with_warnings", "not_ready"}:
        status = fallback["status"]
    return {
        "status": status,
        "checks": {key: bool(checks.get(key)) for key in fallback["checks"]},
        "warnings": _as_list(data.get("warnings"), fallback["warnings"]) if data.get("warnings") else fallback["warnings"],
        "blocking_issues": _as_list(data.get("blocking_issues"), fallback["blocking_issues"])
        if data.get("blocking_issues")
        else fallback["blocking_issues"],
    }


def _fallback_criticality(context: dict) -> dict:
    flood_confidence = float(context.get("hazard", {}).get("confidence_scores", {}).get("flood", 0.75))
    population = int(context.get("impact", {}).get("population_affected", 0))
    hospitals = int(context.get("impact", {}).get("hospitals_at_risk", 0))
    severity = str(context.get("overall_severity", "")).upper()
    critical = severity == "CRITICAL" or population >= 250000 or hospitals >= 10
    return {
        "criticality": "critical" if critical else "high",
        "overall_confidence": round(min(0.95, max(0.65, flood_confidence - 0.03)), 2),
        "escalation_required": critical,
        "rationale": (
            f"{context.get('location')} shows {severity} {context.get('hazard_type', 'hazard').lower()} risk with "
            f"{population:,} people exposed and {hospitals} hospitals at risk."
        ),
        "trigger_factors": [
            f"Overall severity is {severity}",
            f"Flood confidence is {round(flood_confidence * 100)}%",
            f"Population affected is {population:,}",
            f"Hospitals at risk: {hospitals}",
        ],
    }


def _fallback_anomalies(context: dict) -> dict:
    anomalies = []
    if not context.get("analysis", {}).get("zones", {}).get("features"):
        anomalies.append(
            {
                "type": "missing_area",
                "severity": "high",
                "description": "No hazard zone polygons are available for the event.",
                "recommended_handling": "Require spatial validation before operational deployment.",
            }
        )
    if context.get("satellite", {}).get("cloud_cover", 0) > 30 and context.get("satellite", {}).get("type") != "sentinel-1":
        anomalies.append(
            {
                "type": "conflicting_data",
                "severity": "medium",
                "description": "Cloud cover is high but the selected satellite source is not SAR.",
                "recommended_handling": "Verify sensor selection before finalizing map products.",
            }
        )
    return {"anomalies_detected": bool(anomalies), "anomalies": anomalies}


def _fallback_map_narrative(context: dict) -> dict:
    return {
        "map_narrative": (
            "The risk map concentrates critical and high flood polygons inside the Peshawar analysis boundary, "
            "with one evacuation route crossing the affected corridor and hospital markers near mapped flood zones."
        ),
        "key_spatial_findings": [
            "FZ-01 is the critical deep-water hotspot inside the analysis boundary.",
            "FZ-02 marks a high-severity water zone southwest of the critical zone.",
            "The evacuation route intersects the mapped flood corridor and should be validated before mass movement.",
        ],
        "hotspots": ["FZ-01 critical zone", "Lady Reading Hospital access corridor", "FZ-02 high zone"],
        "map_limitations": [
            "Risk polygons are local demo outputs and require field validation.",
            "Static map does not show live flood progression.",
        ],
    }


def _fallback_priority_timeline(context: dict) -> dict:
    return {
        "next_6_hours": [
            "Confirm evacuation route passability and deploy rescue teams to FZ-01.",
            "Protect access to Lady Reading Hospital and stage medical surge support.",
            "Issue targeted public alerts for critical and high flood zones.",
        ],
        "next_24_hours": [
            "Clear blocked road corridors serving hospitals and shelters.",
            "Open shelters outside the mapped flood boundary.",
            "Validate flood extent with field teams and updated satellite tasking.",
        ],
        "next_72_hours": [
            "Maintain SAR-based monitoring and update the dashboard with revised zones.",
            "Transition from rescue to relief logistics and public-health surveillance.",
            "Document facility and school impacts for recovery planning.",
        ],
        "resource_priorities": [
            "Swift-water rescue assets",
            "Medical surge teams and backup power",
            "Road clearance equipment",
            "Shelter supplies and water purification",
        ],
        "coordination_priorities": [
            "Emergency operations center",
            "Hospital administrators",
            "Transport and public works teams",
            "School and shelter coordinators",
        ],
    }


def _fallback_decision_brief(context: dict, intelligence: dict) -> dict:
    summary = (
        f"{context.get('location')} requires immediate escalation for a {context.get('overall_severity')} "
        f"{context.get('hazard_type', 'hazard').lower()} event affecting "
        f"{int(context.get('impact', {}).get('population_affected', 0)):,} people. "
        "Critical decisions are needed on evacuation routing, hospital continuity, road clearance, and shelter activation."
    )
    return {
        "decision_brief": summary,
        "official_summary": summary,
        "key_decisions_required": [
            "Authorize immediate evacuation support for critical flood zones.",
            "Prioritize hospital access and medical continuity operations.",
            "Assign road clearance resources to rescue and logistics corridors.",
        ],
        "human_review_required": False,
    }


def _fallback_quality_check(context: dict, intelligence: dict) -> dict:
    recommendations = context.get("report", {}).get("recommendations", [])
    confidence = intelligence.get("criticality", {}).get("overall_confidence", 0.0)
    checks = {
        "event_id_present": bool(context.get("event_id")),
        "satellite_data_present": bool(context.get("satellite", {}).get("scene_id")),
        "hazard_data_present": bool(context.get("hazard")),
        "impact_data_present": bool(context.get("impact")),
        "map_artifacts_present": bool(context.get("analysis", {}).get("zones", {}).get("features")),
        "recommendations_present": bool(recommendations),
        "confidence_above_threshold": confidence >= 0.7,
    }
    warnings = []
    if not context.get("artifacts", {}).get("geojson_url"):
        warnings.append("GeoJSON artifact URL is not available in local demo mode.")
    if not context.get("report", {}).get("map_url"):
        warnings.append("Map URL is generated later by the local artifact step.")
    blocking = [label for label, passed in checks.items() if not passed and label != "map_artifacts_present"]
    return {
        "status": "not_ready" if blocking else ("ready_with_warnings" if warnings else "ready"),
        "checks": checks,
        "warnings": warnings,
        "blocking_issues": blocking,
    }
