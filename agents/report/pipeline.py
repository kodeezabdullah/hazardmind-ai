import json
import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parents[1]
FRONTEND_DEMO_DIR = REPO_ROOT / "frontend" / "public" / "demo-results"

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from .db_client import fetch_report_context_from_db, is_valid_uuid, write_final_report_metadata
    from .generator import MOCK_EVENT_DATA, generate_report
    from .map_generator import generate_static_map
    from .pdf_generator import generate_pdf_report
    from .storage_client import upload_file_to_r2
except ImportError:
    from db_client import fetch_report_context_from_db, is_valid_uuid, write_final_report_metadata
    from generator import MOCK_EVENT_DATA, generate_report
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

        report = await generate_report(event_id, context=context)
        paths = _resolve_output_paths(
            event_id=event_id,
            frontend_demo_mode=frontend_demo_mode,
            output_dir=output_dir,
            json_output_path=json_output_path,
            pdf_output_path=pdf_output_path,
            map_output_path=map_output_path,
        )

        report["report"]["map_url"] = _public_url_for(paths["map"])
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
                map_url = upload_file_to_r2(
                    str(paths["map"]),
                    f"events/{event_id}/risk_map.png",
                    "image/png",
                )
            except Exception as exc:
                warnings.append(f"R2 upload failed: {_safe_error_message(exc)}")
            else:
                report["report"]["pdf_url"] = pdf_url
                report["report"]["map_url"] = map_url
                r2_uploaded = True
                _append_report_log(report, "PDF uploaded to Cloudflare R2", "2026-06-13T18:05:00Z")
                _append_report_log(report, "Map uploaded to Cloudflare R2", "2026-06-13T18:05:20Z")

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
            report=report,
        )
    except Exception as exc:
        return _result(
            event_id=event_id,
            status="failed",
            warnings=[f"Report pipeline failed: {type(exc).__name__}: {_safe_error_message(exc)}"],
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


def _confidence_level(report: dict) -> str:
    criticality = report.get("intelligence", {}).get("criticality", {})
    confidence = criticality.get("overall_confidence")
    label = criticality.get("criticality") or report.get("overall_severity")
    if confidence is None:
        return str(label or "unknown")
    return f"{label}:{round(float(confidence) * 100)}%"


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
    }
    if report is not None:
        result["report"] = report
    return result
