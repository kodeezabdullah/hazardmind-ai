import asyncio
import argparse
import json
import time
from pathlib import Path

from db_client import is_valid_uuid, write_final_report_metadata
from generator import generate_report
from llm_clients import featherless_health_check
from map_generator import generate_static_map
from pdf_generator import generate_pdf_report
from storage_client import upload_file_to_r2


def parse_args():
    parser = argparse.ArgumentParser(description="Generate a HazardMind report JSON.")
    parser.add_argument("event_id", nargs="?", default="demo-peshawar-flood")
    parser.add_argument("--output", help="Optional path to save the generated JSON.")
    parser.add_argument("--pdf-output", help="Optional path to save the generated PDF report.")
    parser.add_argument("--map-output", help="Optional path to save the generated static map PNG.")
    parser.add_argument("--upload-r2", action="store_true", help="Upload generated PDF and map artifacts to Cloudflare R2.")
    parser.add_argument("--write-db", action="store_true", help="Write final report metadata to Neon.")
    parser.add_argument("--llm-health-check", action="store_true", help="Check Featherless model availability and exit.")
    return parser.parse_args()


async def main():
    started_at = time.perf_counter()
    args = parse_args()
    if args.llm_health_check:
        for label, status in await featherless_health_check():
            print(f"{label}: {status}")
        return

    report = await generate_report(args.event_id)
    map_output_path = None
    pdf_output_path = None

    if args.map_output:
        map_output_path = Path(args.map_output)
        report["report"]["map_url"] = public_url_for(map_output_path)
        saved_map_path = generate_static_map(report, map_output_path)
        append_report_log(report, "Static cartography map generated locally", "2026-06-13T18:04:00Z")
        print(f"Saved cartographic map to {saved_map_path}")

    if args.pdf_output:
        pdf_output_path = Path(args.pdf_output)
        report["report"]["pdf_url"] = public_url_for(pdf_output_path)
        saved_pdf_path = generate_pdf_report(report, pdf_output_path, map_output_path=map_output_path)
        append_report_log(report, "PDF generated locally", "2026-06-13T18:04:20Z")
        print(f"Saved PDF report to {saved_pdf_path}")

    if args.upload_r2:
        if not pdf_output_path or not pdf_output_path.exists():
            raise RuntimeError("R2 upload requires --pdf-output to generate a local PDF first.")
        if not map_output_path or not map_output_path.exists():
            raise RuntimeError("R2 upload requires --map-output to generate a local map first.")

        event_id = report["event_id"]
        pdf_url = upload_file_to_r2(
            str(pdf_output_path),
            f"events/{event_id}/report.pdf",
            "application/pdf",
        )
        report["report"]["pdf_url"] = pdf_url
        append_report_log(report, "PDF uploaded to Cloudflare R2", "2026-06-13T18:05:00Z")
        print("PDF uploaded to R2")

        map_url = upload_file_to_r2(
            str(map_output_path),
            f"events/{event_id}/risk_map.png",
            "image/png",
        )
        report["report"]["map_url"] = map_url
        append_report_log(report, "Map uploaded to Cloudflare R2", "2026-06-13T18:05:20Z")
        print("Map uploaded to R2")

    if args.write_db:
        if not is_valid_uuid(str(report.get("event_id", ""))):
            print("DB write skipped: event_id is not a UUID. Real Band/backend event IDs must be UUIDs.")
        else:
            total_time_secs = round(time.perf_counter() - started_at)
            await write_final_report_metadata(report, total_time_secs=total_time_secs)
            append_report_log(report, "Final report metadata written to Neon", "2026-06-13T18:05:40Z")
            print("Final report metadata written to Neon")

    if args.output:
        append_report_log(report, "JSON written locally", "2026-06-13T18:04:40Z")

    report_json = json.dumps(report, indent=2)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(f"{report_json}\n", encoding="utf-8")
        print(f"Saved report JSON to {output_path}")
    else:
        print(report_json)


def append_report_log(report: dict, message: str, timestamp: str) -> None:
    report.setdefault("agent_log", []).append(
        {
            "agent": "hazardmind-report",
            "status": "complete",
            "message": message,
            "timestamp": timestamp,
        }
    )


def public_url_for(path: Path) -> str:
    parts = path.parts
    if "public" in parts:
        public_index = parts.index("public")
        return "/" + "/".join(parts[public_index + 1 :])
    return f"/demo-results/{path.name}"


if __name__ == "__main__":
    asyncio.run(main())
