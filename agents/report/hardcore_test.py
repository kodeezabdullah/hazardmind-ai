import argparse
import asyncio
import json
import os
import sys
from copy import deepcopy
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parents[1]
FIXTURE_DIR = BASE_DIR / "test_fixtures"
DEFAULT_OUTPUT_DIR = BASE_DIR / "generated" / "hardcore-test"

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from band_contract import build_report_completion_message, extract_trailing_json, parse_report_trigger_message
from generator import MOCK_EVENT_DATA
from geometry_utils import (
    calculate_bbox_from_geojson,
    explain_shapefile_handling,
    is_valid_feature_collection,
    normalize_geojson,
    validate_polygon_coordinates,
    validate_report_geometries,
)
from llm_clients import featherless_health_check
from map_generator import generate_static_map
from pdf_generator import generate_pdf_report, model_source_note
from pipeline import run_report_pipeline


EXPECTED_FIXTURES = {
    "valid_zones.geojson": True,
    "multipolygon_zones.geojson": True,
    "circular_buffers.geojson": True,
    "large_zones.geojson": True,
    "malformed_zones.geojson": False,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run hardcore Report Agent validation tests.")
    parser.add_argument("--quick", action="store_true", help="Run fast local geometry/map/PDF/safety checks only.")
    parser.add_argument("--include-llm", action="store_true", help="Run LLM health checks and full local pipeline.")
    parser.add_argument("--include-r2", action="store_true", help="Upload test PDF/map artifacts to R2.")
    parser.add_argument("--include-db", action="store_true", help="Exercise DB-write safety. Non-UUID demo IDs skip writes.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for generated test artifacts.")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    load_dotenv(BASE_DIR / ".env")

    harness = Harness()
    print("HazardMind Report Agent Hardcore Test")
    print(f"output_dir: {output_dir}")
    print()

    run_environment_safety_check(harness, include_r2=args.include_r2, include_db=args.include_db)
    run_band_contract_tests(harness)
    fixtures = run_geometry_fixture_tests(harness)
    run_report_geometry_tests(harness, fixtures)
    run_artifact_tests(harness, fixtures, output_dir)
    await run_pipeline_safety_tests(harness, output_dir, include_r2=args.include_r2, include_db=args.include_db)

    if args.include_llm:
        await run_llm_health_check(harness)

    if not args.quick or args.include_llm:
        await run_full_local_pipeline_test(harness, output_dir)

    run_shapefile_readiness_test(harness)
    print()
    harness.print_summary()
    return 0 if harness.failed == 0 else 1


class Harness:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        if condition:
            self.passed += 1
        else:
            self.failed += 1
        suffix = f" - {detail}" if detail else ""
        print(f"[{status}] {name}{suffix}")

    def print_summary(self) -> None:
        print(f"summary: passed={self.passed} failed={self.failed}")


def run_environment_safety_check(harness: Harness, *, include_r2: bool, include_db: bool) -> None:
    env_vars = [
        "AIML_API_KEY",
        "FEATHERLESS_API_KEY",
        "BAND_API_KEY",
        "NEON_DATABASE_URL",
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_R2_KEY",
        "CLOUDFLARE_R2_SECRET",
        "CLOUDFLARE_R2_BUCKET",
        "CLOUDFLARE_R2_ENDPOINT",
        "CLOUDFLARE_R2_PUBLIC",
        "CLOUDFLARE_R2_PUBLIC_URL",
    ]
    print("Environment Safety")
    for name in env_vars:
        print(f"  {name}: {'present' if bool(os.getenv(name)) else 'missing'}")
    r2_ready = (
        all(os.getenv(name) for name in ("CLOUDFLARE_R2_KEY", "CLOUDFLARE_R2_SECRET", "CLOUDFLARE_R2_BUCKET"))
        and bool(os.getenv("CLOUDFLARE_R2_ENDPOINT") or os.getenv("CLOUDFLARE_ACCOUNT_ID"))
        and bool(os.getenv("CLOUDFLARE_R2_PUBLIC_URL") or os.getenv("CLOUDFLARE_R2_PUBLIC"))
    )
    db_ready = bool(os.getenv("NEON_DATABASE_URL"))
    harness.check("environment check prints booleans only", True)
    harness.check("R2 env ready when --include-r2 is used", r2_ready or not include_r2)
    harness.check("DB env ready when --include-db is used", db_ready or not include_db)
    print()


def run_band_contract_tests(harness: Harness) -> None:
    print("Band Contract")
    fixture_path = FIXTURE_DIR / "report_trigger_message.txt"
    harness.check("report trigger fixture exists", fixture_path.exists())
    message = fixture_path.read_text(encoding="utf-8") if fixture_path.exists() else ""

    try:
        parsed_payload = parse_report_trigger_message(message)
    except ValueError as exc:
        harness.check("parse Band trigger message", False, str(exc))
        parsed_payload = {}
    else:
        harness.check("parse Band trigger message", parsed_payload["to"] == "hazardmind-report")
        harness.check("Band trigger has event_id", bool(parsed_payload.get("event_id")))
        harness.check("Band trigger impact data parsed", bool(parsed_payload.get("impact_data")))

    fenced = "Report payload follows.\n```json\n{\"ok\": true, \"nested\": {\"value\": 1}}\n```"
    harness.check("extract trailing JSON from fenced text", extract_trailing_json(fenced).get("ok") is True)
    try:
        extract_trailing_json("No JSON object in this Band message.")
    except ValueError:
        harness.check("reject message with no JSON safely", True)
    else:
        harness.check("reject message with no JSON safely", False)

    event_id = parsed_payload.get("event_id", "8f4f7c66-7d9c-4df8-8a4f-78c9bfaeaf21")
    report = build_report_context(_load_fixture("valid_zones.geojson"))
    report["event_id"] = event_id
    report["report"]["pdf_url"] = f"https://public-r2.example/events/{event_id}/report.pdf"
    report["report"]["map_url"] = f"https://hazardmind.vercel.app/map/{event_id}"
    report["report"]["recommended_response_level"] = "NDMA Level-3"
    result = {
        "event_id": event_id,
        "status": "complete",
        "pdf_url": report["report"]["pdf_url"],
        "map_url": report["report"]["map_url"],
        "summary": report["report"]["summary"],
        "confidence_level": "HIGH",
        "recommended_response_level": "NDMA Level-3",
        "report": report,
    }
    completion_message = build_report_completion_message(result)
    completion_json = extract_trailing_json(completion_message)
    completion_data = completion_json.get("data", {})
    harness.check("completion message mentions orchestrator", "@hazardmind-orchestrator" in completion_message)
    harness.check("completion JSON event_id matches", completion_json.get("event_id") == event_id)
    harness.check("completion JSON agent matches", completion_json.get("agent") == "hazardmind-report")
    harness.check("completion JSON status complete", completion_json.get("status") == "complete")
    harness.check("completion JSON step report", completion_json.get("step") == "report")
    for key in ("pdf_url", "map_url", "executive_summary", "confidence_level", "recommended_response_level"):
        harness.check(f"completion JSON data.{key}", bool(completion_data.get(key)))
    print()


async def run_llm_health_check(harness: Harness) -> None:
    print("LLM Health")
    try:
        results = await featherless_health_check()
    except Exception as exc:
        harness.check("LLM health check did not crash", False, type(exc).__name__)
        return
    for label, status in results:
        print(f"  {label}: {status}")
        harness.check(f"LLM health {label}", status == "OK", status)
    print()


def run_geometry_fixture_tests(harness: Harness) -> dict[str, dict]:
    print("Geometry Fixtures")
    fixtures: dict[str, dict] = {}
    for fixture_name, should_be_valid in EXPECTED_FIXTURES.items():
        path = FIXTURE_DIR / fixture_name
        harness.check(f"{fixture_name} exists", path.exists())
        if not path.exists():
            continue

        data = json.loads(path.read_text(encoding="utf-8"))
        fixtures[fixture_name] = data
        normalized = normalize_geojson(data)
        polygon_errors = _polygon_errors(normalized)
        bbox = calculate_bbox_from_geojson(normalized)
        feature_count = len(normalized.get("features", []))
        is_valid = is_valid_feature_collection(normalized) and not polygon_errors
        harness.check(
            f"{fixture_name} validity expectation",
            is_valid is should_be_valid,
            f"features={feature_count} polygon_errors={len(polygon_errors)} bbox={bbox}",
        )

    circular = normalize_geojson(fixtures.get("circular_buffers.geojson", {}))
    circular_errors = _polygon_errors(circular)
    harness.check("circular buffer GeoJSON validates", not circular_errors, f"errors={len(circular_errors)}")
    harness.check(
        "large_zones has 100+ features",
        len(normalize_geojson(fixtures.get("large_zones.geojson", {})).get("features", [])) >= 100,
    )
    print()
    return fixtures


def run_report_geometry_tests(harness: Harness, fixtures: dict[str, dict]) -> None:
    print("Report Geometry Compatibility")
    variants = {
        "valid zones": build_report_context(fixtures["valid_zones.geojson"]),
        "circular buffers": build_report_context(fixtures["circular_buffers.geojson"]),
        "multipolygons": build_report_context(fixtures["multipolygon_zones.geojson"]),
        "large zones": build_report_context(fixtures["large_zones.geojson"]),
        "missing boundaries": build_report_context(fixtures["valid_zones.geojson"], missing_boundaries=True),
        "empty zones": build_report_context({"type": "FeatureCollection", "features": []}),
    }
    for name, report in variants.items():
        result = validate_report_geometries(report)
        harness.check(
            f"report geometry validates: {name}",
            result["valid"] is True,
            f"warnings={len(result['warnings'])} errors={len(result['errors'])} bbox={result['bbox']}",
        )
        if name == "empty zones":
            harness.check("empty zones report returns warning", bool(result["warnings"]))

    malformed_report = build_report_context(fixtures["malformed_zones.geojson"])
    malformed_result = validate_report_geometries(malformed_report)
    harness.check(
        "malformed geometry fails safely",
        not malformed_result["valid"] and bool(malformed_result["errors"]),
        f"errors={len(malformed_result['errors'])}",
    )
    print()


def run_artifact_tests(harness: Harness, fixtures: dict[str, dict], output_dir: Path) -> None:
    print("Map and PDF Artifacts")
    artifact_dir = output_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    map_variants = {
        "normal": build_report_context(fixtures["valid_zones.geojson"]),
        "circular": build_report_context(fixtures["circular_buffers.geojson"]),
        "multipolygon": build_report_context(fixtures["multipolygon_zones.geojson"]),
        "large": build_report_context(fixtures["large_zones.geojson"]),
        "no-zones": build_report_context({"type": "FeatureCollection", "features": []}),
    }
    generated_maps: dict[str, Path] = {}
    for name, report in map_variants.items():
        path = artifact_dir / f"{name}-map.png"
        try:
            generate_static_map(report, path)
        except Exception as exc:
            harness.check(f"static map works with {name}", False, type(exc).__name__)
            continue
        generated_maps[name] = path
        harness.check(f"static map works with {name}", path.exists() and path.stat().st_size > 0)

    pdf_report = build_report_context(fixtures["valid_zones.geojson"])
    pdf_path = artifact_dir / "report.pdf"
    try:
        generate_pdf_report(pdf_report, pdf_path, map_output_path=generated_maps.get("normal"))
    except Exception as exc:
        harness.check("PDF generation works", False, type(exc).__name__)
    else:
        harness.check("PDF generation works", pdf_path.exists() and pdf_path.stat().st_size > 0)
        note = model_source_note(pdf_report)
        harness.check(
            "PDF data includes intelligence/model source sections",
            bool(pdf_report.get("intelligence")) and "Intelligence sources" in note,
        )
    print()


async def run_pipeline_safety_tests(
    harness: Harness,
    output_dir: Path,
    *,
    include_r2: bool,
    include_db: bool,
) -> None:
    print("Pipeline Safety")
    invalid_fetch = await run_report_pipeline("demo-peshawar-flood", fetch_from_db=True)
    harness.check(
        "non-UUID --from-db fails before DB fetch",
        invalid_fetch["status"] == "failed"
        and "Cannot fetch DB context because event_id is not UUID." in invalid_fetch.get("warnings", []),
    )

    result = await _run_pipeline_with_fake_report(
        output_dir / "db-skip",
        upload_r2=include_r2,
        write_db=True,
    )
    expected_db_warning = any("DB write skipped" in warning for warning in result.get("warnings", []))
    harness.check(
        "non-UUID DB write skips safely",
        result["status"] in {"complete_with_warnings", "complete"} and expected_db_warning,
        f"status={result['status']} warnings={len(result.get('warnings', []))}",
    )
    harness.check("pipeline map_url uses frontend route", "/map/demo-peshawar-flood" in result.get("map_url", ""))
    if include_r2:
        harness.check("optional R2 upload completed", result.get("r2_uploaded") is True, result.get("status", ""))
    print()


async def run_full_local_pipeline_test(harness: Harness, output_dir: Path) -> None:
    print("Full Local Pipeline")
    result = await run_report_pipeline(
        "demo-peshawar-flood",
        output_dir=str(output_dir / "full-pipeline"),
        frontend_demo_mode=False,
    )
    harness.check(
        "local run_report_pipeline completes",
        result["status"] in {"complete", "complete_with_warnings"},
        f"status={result['status']} warnings={len(result.get('warnings', []))}",
    )
    report = result.get("report", {})
    harness.check("pipeline returns model sources", bool(result.get("model_sources") or report.get("model_sources")))
    harness.check("pipeline writes JSON", bool(result.get("json_path")) and Path(result["json_path"]).exists())
    harness.check("pipeline writes PDF", bool(result.get("pdf_path")) and Path(result["pdf_path"]).exists())
    harness.check("pipeline writes map", bool(result.get("map_path")) and Path(result["map_path"]).exists())
    print()


def run_shapefile_readiness_test(harness: Harness) -> None:
    print("Shapefile Readiness")
    note = explain_shapefile_handling()
    harness.check("shapefile handling explains required sidecar files", ".shp" in note["shapefile_components"])
    harness.check("shapefile handling requires GeoJSON for frontend", note["frontend_format"] == "GeoJSON FeatureCollection")
    harness.check("circular buffer contract uses Polygon features", "Polygon" in note["circular_buffers"])
    print()


async def _run_pipeline_with_fake_report(output_dir: Path, *, upload_r2: bool, write_db: bool) -> dict:
    import pipeline

    original_generate_report = pipeline.generate_report

    async def fake_generate_report(event_id: str, context: dict | None = None) -> dict:
        del context
        report = build_report_context(_load_fixture("valid_zones.geojson"))
        report["event_id"] = event_id
        return report

    pipeline.generate_report = fake_generate_report
    try:
        return await run_report_pipeline(
            "demo-peshawar-flood",
            upload_r2=upload_r2,
            write_db=write_db,
            output_dir=str(output_dir),
            frontend_demo_mode=False,
        )
    finally:
        pipeline.generate_report = original_generate_report


def build_report_context(
    zones_geojson: dict,
    *,
    missing_boundaries: bool = False,
) -> dict:
    report = deepcopy(MOCK_EVENT_DATA)
    zones = normalize_geojson(zones_geojson)
    bbox = calculate_bbox_from_geojson(zones) or report["boundaries"]["bbox"]
    report["analysis"]["zones"] = zones
    report["analysis"]["total_zones"] = len(zones.get("features", []))
    report["boundaries"]["bbox"] = bbox
    if missing_boundaries:
        report["boundaries"] = {"bbox": bbox, "region_boundary": {"type": "FeatureCollection", "features": []}}

    report["report"] = {
        "summary": "Hardcore test summary for local Report Agent validation.",
        "detailed_body": "Synthetic report body used to validate map, PDF, geometry, and frontend data contracts.",
        "technical_analysis": "Synthetic technical analysis confirms GeoJSON overlay compatibility.",
        "recommendations": ["Validate flood zones.", "Confirm evacuation route passability."],
        "response_priorities": ["Protect hospital access.", "Clear priority roads."],
        "assumptions": ["Synthetic geometry fixtures are local test data."],
        "limitations": ["No live Band messages are consumed in hardcore tests."],
        "pdf_url": "",
        "map_url": "",
        "recommended_response_level": "NDMA Level-3",
    }
    report["recommended_response_level"] = "NDMA Level-3"
    report["intelligence"] = {
        "criticality": {
            "criticality": "critical",
            "overall_confidence": 0.88,
            "escalation_required": True,
            "rationale": "Synthetic critical flood exposure remains operationally significant.",
            "trigger_factors": ["Critical flood confidence", "Hospital exposure"],
        },
        "anomalies": {"anomalies_detected": False, "anomalies": []},
        "map_narrative": {
            "map_narrative": "Synthetic GeoJSON overlays render inside the Peshawar analysis bbox.",
            "key_spatial_findings": ["Zones intersect the demo analysis area."],
            "hotspots": ["Peshawar demo corridor"],
            "map_limitations": ["Fixture geometries are synthetic."],
        },
        "priority_timeline": {
            "next_6_hours": ["Validate critical zones."],
            "next_24_hours": ["Refresh satellite overlays."],
            "next_72_hours": ["Prepare recovery map products."],
            "resource_priorities": ["GIS validation support"],
            "coordination_priorities": ["Report Agent handoff"],
        },
        "decision_brief": {
            "decision_brief": "Synthetic test decision brief.",
            "official_summary": "Synthetic official summary.",
            "key_decisions_required": ["Approve map overlay validation."],
            "human_review_required": False,
        },
        "quality_check": {
            "status": "ready",
            "checks": {
                "event_id_present": True,
                "satellite_data_present": True,
                "hazard_data_present": True,
                "impact_data_present": True,
                "map_artifacts_present": True,
                "recommendations_present": True,
                "confidence_above_threshold": True,
            },
            "warnings": [],
            "blocking_issues": [],
        },
        "band_ready_message": {"message": "Synthetic final message.", "status": "COMPLETE", "confidence": 0.88},
    }
    report["model_sources"] = {
        "detailed_report": "deterministic_test_fixture",
        "executive_summary": "deterministic_test_fixture",
        "fallback_used": False,
        "featherless_model": "test-fixture",
        "intelligence": {
            "criticality": "deterministic_test_fixture",
            "anomaly_check": "deterministic_test_fixture",
            "map_narrative": "deterministic_test_fixture",
            "priority_recommendations": "deterministic_test_fixture",
            "decision_brief": "deterministic_test_fixture",
            "quality_check": "deterministic_test_fixture",
            "band_ready_message": "deterministic_test_fixture",
        },
    }
    return report


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _polygon_errors(feature_collection: dict) -> list[str]:
    errors = []
    for index, feature in enumerate(feature_collection.get("features", [])):
        geometry = feature.get("geometry") or {}
        if geometry.get("type") in {"Polygon", "MultiPolygon"}:
            valid, geometry_errors = validate_polygon_coordinates(geometry)
            if not valid:
                errors.extend(f"features[{index}]: {error}" for error in geometry_errors)
    return errors


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
