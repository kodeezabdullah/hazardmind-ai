import json
import os
import sys
import time
from copy import deepcopy
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parents[1]
FRONTEND_DEMO_DIR = REPO_ROOT / "frontend" / "public" / "demo-results"

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from .db_client import fetch_report_context_from_db, is_valid_uuid, write_final_report_metadata
    from .generator import MOCK_EVENT_DATA, determine_recommended_response_level, generate_report
    from .map_generator import generate_static_map
    from .pdf_generator import generate_pdf_report
    from .storage_client import upload_file_to_r2
except ImportError:
    from db_client import fetch_report_context_from_db, is_valid_uuid, write_final_report_metadata
    from generator import MOCK_EVENT_DATA, determine_recommended_response_level, generate_report
    from map_generator import generate_static_map
    from pdf_generator import generate_pdf_report
    from storage_client import upload_file_to_r2


async def run_report_pipeline(
    event_id: str,
    *,
    fetch_from_db: bool = False,
    upload_r2: bool = False,
    write_db: bool = False,
    output_dir: str | None = None,
    frontend_demo_mode: bool = False,
    incoming_payload: dict | None = None,
    use_llm: bool = True,
    allow_contract_side_effects: bool = False,
    json_output_path: str | None = None,
    pdf_output_path: str | None = None,
    map_output_path: str | None = None,
) -> dict:
    """
    Backend/SDK-ready Report Agent pipeline.
    """
    started_at = time.perf_counter()
    warnings: list[str] = []
    r2_uploaded = False
    db_written = False

    try:
        if not use_llm and (upload_r2 or write_db) and not allow_contract_side_effects:
            return _result(
                event_id=event_id,
                status="failed",
                error="Contract test mode blocks R2/DB side effects unless explicitly allowed.",
                warnings=["Contract test mode cannot upload R2 or write DB by default."],
            )

        context = None
        if fetch_from_db:
            if not is_valid_uuid(event_id):
                return _result(
                    event_id=event_id,
                    status="failed",
                    warnings=["Cannot fetch DB context because event_id is not UUID."],
                )
            context = await fetch_report_context_from_db(event_id)
            missing_context = _missing_context_warnings(context)
            warnings.extend(missing_context)

        if incoming_payload:
            if context is None:
                context = deepcopy(MOCK_EVENT_DATA)
            _merge_incoming_payload_into_context(context, incoming_payload)
            warnings.extend(_incoming_payload_warnings(incoming_payload))

        report = await generate_report(event_id, context=context, use_llm=use_llm)
        if incoming_payload:
            _preserve_incoming_payload(report, incoming_payload)

        paths = _resolve_output_paths(
            event_id=event_id,
            frontend_demo_mode=frontend_demo_mode,
            output_dir=output_dir,
            json_output_path=json_output_path,
            pdf_output_path=pdf_output_path,
            map_output_path=map_output_path,
        )

        report["report"]["map_url"] = _frontend_map_url(event_id)
        paths["map"].parent.mkdir(parents=True, exist_ok=True)
        generate_static_map(report, paths["map"])
        _append_report_log(report, "Static cartography map generated locally", "2026-06-13T18:04:00Z")

        report["report"]["pdf_url"] = _public_url_for(paths["pdf"])
        paths["pdf"].parent.mkdir(parents=True, exist_ok=True)
        generate_pdf_report(report, paths["pdf"], map_output_path=paths["map"])
        _append_report_log(report, "PDF generated locally", "2026-06-13T18:04:20Z")

        if upload_r2:
            try:
                pdf_url = upload_file_to_r2(
                    str(paths["pdf"]),
                    f"events/{event_id}/report.pdf",
                    "application/pdf",
                )
            except Exception as exc:
                warnings.append(f"R2 upload failed: {_safe_error_message(exc)}")
            else:
                report["report"]["pdf_url"] = pdf_url
                r2_uploaded = True
                _append_report_log(report, "PDF uploaded to Cloudflare R2", "2026-06-13T18:05:00Z")

        total_time_secs = round(time.perf_counter() - started_at)
        if write_db:
            if not is_valid_uuid(event_id):
                warnings.append("DB write skipped: event_id is not a UUID. Real Band/backend event IDs must be UUIDs.")
            else:
                await write_final_report_metadata(report, total_time_secs=total_time_secs)
                db_written = True
                _append_report_log(report, "Final report metadata written to Neon", "2026-06-13T18:05:40Z")

        _append_report_log(report, "JSON written locally", "2026-06-13T18:04:40Z")
        paths["json"].parent.mkdir(parents=True, exist_ok=True)
        paths["json"].write_text(f"{json.dumps(report, indent=2)}\n", encoding="utf-8")

        status = "complete_with_warnings" if warnings else "complete"
        return _result(
            event_id=event_id,
            status=status,
            summary=report.get("report", {}).get("summary", ""),
            pdf_url=report.get("report", {}).get("pdf_url", ""),
            map_url=report.get("report", {}).get("map_url", ""),
            json_path=str(paths["json"]),
            pdf_path=str(paths["pdf"]),
            map_path=str(paths["map"]),
            r2_uploaded=r2_uploaded,
            db_written=db_written,
            warnings=warnings,
            model_sources=report.get("model_sources", {}),
            confidence_level=_confidence_level(report),
            recommended_response_level=_recommended_response_level(report),
            report=report,
        )
    except Exception as exc:
        error_message = _safe_error_message(exc)
        return _result(
            event_id=event_id,
            status="failed",
            error=error_message if error_message.startswith("LLM generation failed") else f"Report pipeline failed: {type(exc).__name__}: {error_message}",
            warnings=[error_message if error_message.startswith("LLM generation failed") else f"Report pipeline failed: {type(exc).__name__}: {error_message}"],
        )


def _resolve_output_paths(
    *,
    event_id: str,
    frontend_demo_mode: bool,
    output_dir: str | None,
    json_output_path: str | None,
    pdf_output_path: str | None,
    map_output_path: str | None,
) -> dict[str, Path]:
    if json_output_path or pdf_output_path or map_output_path:
        base = Path(output_dir) if output_dir else BASE_DIR / "generated" / _safe_path_part(event_id)
        return {
            "json": Path(json_output_path) if json_output_path else base / "report.json",
            "pdf": Path(pdf_output_path) if pdf_output_path else base / "report.pdf",
            "map": Path(map_output_path) if map_output_path else base / "risk_map.png",
        }

    if frontend_demo_mode:
        return {
            "json": FRONTEND_DEMO_DIR / "demo-peshawar-flood.json",
            "pdf": FRONTEND_DEMO_DIR / "demo-peshawar-flood-report.pdf",
            "map": FRONTEND_DEMO_DIR / "demo-peshawar-flood-map.png",
        }

    base = Path(output_dir) if output_dir else BASE_DIR / "generated" / _safe_path_part(event_id)
    return {
        "json": base / "report.json",
        "pdf": base / "report.pdf",
        "map": base / "risk_map.png",
    }


def _public_url_for(path: Path) -> str:
    parts = path.parts
    if "public" in parts:
        public_index = parts.index("public")
        return "/" + "/".join(parts[public_index + 1 :])
    return str(path)


def _frontend_map_url(event_id: str) -> str:
    load_dotenv(BASE_DIR / ".env")
    base_url = (
        os.getenv("FRONTEND_BASE_URL")
        or os.getenv("NEXT_PUBLIC_FRONTEND_URL")
        or "https://hazardmind.vercel.app"
    )
    return f"{base_url.rstrip('/')}/map/{event_id}"


def _append_report_log(report: dict, message: str, timestamp: str) -> None:
    report.setdefault("agent_log", []).append(
        {
            "agent": "hazardmind-report",
            "status": "complete",
            "message": message,
            "timestamp": timestamp,
        }
    )


def _missing_context_warnings(context: dict) -> list[str]:
    warnings = []
    if not context.get("satellite", {}).get("scene_id"):
        warnings.append("DB context warning: satellite scene is missing.")
    if not context.get("analysis", {}).get("zones", {}).get("features"):
        warnings.append("DB context warning: hazard zones are missing.")
    if not context.get("impact"):
        warnings.append("DB context warning: impact data is missing.")
    return warnings


def _merge_incoming_payload_into_context(context: dict, incoming_payload: dict) -> None:
    data = _incoming_data(incoming_payload)
    impact = context.setdefault("impact", {})
    mappings = {
        "total_affected": "population_affected",
        "high_risk_people": "high_risk_people",
        "medium_risk_people": "medium_risk_people",
        "hospitals_at_risk": "hospitals_at_risk",
        "schools_at_risk": "schools_affected",
        "roads_blocked": "roads_blocked_km",
        "bridges_at_risk": "bridges_at_risk",
        "vulnerability_score": "vulnerability_score",
        "estimated_evacuation_time": "estimated_evacuation_time",
        "overall_confidence": "overall_confidence",
    }
    for source_key, target_key in mappings.items():
        if source_key in data:
            impact[target_key] = data[source_key]
    if "total_affected" in data:
        impact["total_affected"] = data["total_affected"]

    context["incoming_anomalies"] = incoming_payload.get("anomalies") or []

    routes = _routes_from_incoming(data.get("evacuation_routes"))
    if routes is not None:
        context.setdefault("routes", {})["evacuation_routes"] = routes

    confidence = data.get("overall_confidence")
    if confidence is not None:
        context.setdefault("hazard", {}).setdefault("confidence_scores", {})["overall"] = confidence


def _preserve_incoming_payload(report: dict, incoming_payload: dict) -> None:
    anomalies = incoming_payload.get("anomalies") or []
    report["incoming_payload"] = {
        "from": incoming_payload.get("from", ""),
        "to": incoming_payload.get("to", ""),
        "anomalies": anomalies,
    }
    report["incoming_anomalies"] = anomalies
    if anomalies:
        report.setdefault("intelligence", {}).setdefault("anomalies", {})["incoming_anomalies"] = anomalies
        report.setdefault("report", {})["incoming_anomalies"] = anomalies
    _append_report_log(
        report,
        f"Impact Agent handoff merged with {len(anomalies)} incoming anomalies.",
        "2026-06-13T18:03:10Z",
    )
    response_level = determine_recommended_response_level(report)
    report["recommended_response_level"] = response_level
    report.setdefault("report", {})["recommended_response_level"] = response_level


def _incoming_payload_warnings(incoming_payload: dict) -> list[str]:
    warnings = []
    data = _incoming_data(incoming_payload)
    if not data:
        warnings.append("Incoming Impact Agent payload did not include data fields.")
    if incoming_payload.get("anomalies"):
        warnings.append(f"Incoming payload contains {len(incoming_payload['anomalies'])} anomalies.")
    return warnings


def _incoming_data(incoming_payload: dict) -> dict:
    data = incoming_payload.get("impact_data") or incoming_payload.get("data") or {}
    return data if isinstance(data, dict) else {}


def _routes_from_incoming(routes) -> dict | None:
    if routes is None:
        return None
    if isinstance(routes, dict) and routes.get("type") == "FeatureCollection":
        return routes
    if isinstance(routes, dict):
        routes = [routes]
    if not isinstance(routes, list):
        return None

    features = []
    for index, route in enumerate(routes, start=1):
        if not isinstance(route, dict):
            continue
        geojson = route.get("geojson") if isinstance(route.get("geojson"), dict) else {}
        if geojson.get("type") == "FeatureCollection":
            features.extend(geojson.get("features", []))
            continue
        if geojson.get("type") == "Feature":
            features.append(geojson)
            continue
        geometry = geojson if geojson.get("type") in {"LineString", "MultiLineString"} else {"type": "LineString", "coordinates": []}
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "name": route.get("name") or f"Route {index}",
                    "distance_km": route.get("distance_km"),
                    "status": route.get("status"),
                },
                "geometry": geometry,
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _confidence_level(report: dict) -> str:
    criticality = report.get("intelligence", {}).get("criticality", {})
    confidence = criticality.get("overall_confidence")
    if confidence is None:
        confidence = report.get("impact", {}).get("overall_confidence")
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if confidence_value >= 0.8:
        return "HIGH"
    if confidence_value >= 0.6:
        return "MEDIUM"
    return "LOW"


def _recommended_response_level(report: dict) -> str:
    return (
        report.get("recommended_response_level")
        or report.get("report", {}).get("recommended_response_level")
        or determine_recommended_response_level(report)
    )


def _safe_path_part(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value)


def _safe_error_message(exc: Exception) -> str:
    message = str(exc) or type(exc).__name__
    for name in (
        "AIML_API_KEY",
        "BAND_API_KEY",
        "FEATHERLESS_API_KEY",
        "NEON_DATABASE_URL",
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_R2_KEY",
        "CLOUDFLARE_R2_SECRET",
    ):
        value = os.getenv(name)
        if value:
            message = message.replace(value, "[redacted]")
    return message[:500]


def _result(
    *,
    event_id: str,
    status: str,
    summary: str = "",
    pdf_url: str = "",
    map_url: str = "",
    json_path: str = "",
    pdf_path: str = "",
    map_path: str = "",
    r2_uploaded: bool = False,
    db_written: bool = False,
    warnings: list[str] | None = None,
    model_sources: dict | None = None,
    confidence_level: str = "",
    recommended_response_level: str = "",
    error: str = "",
    report: dict | None = None,
) -> dict:
    result = {
        "event_id": event_id,
        "status": status,
        "summary": summary,
        "pdf_url": pdf_url,
        "map_url": map_url,
        "json_path": json_path,
        "pdf_path": pdf_path,
        "map_path": map_path,
        "r2_uploaded": r2_uploaded,
        "db_written": db_written,
        "warnings": warnings or [],
        "model_sources": model_sources or {},
        "confidence_level": confidence_level,
        "recommended_response_level": recommended_response_level,
        "error": error,
    }
    if report is not None:
        result["report"] = report
    return result
