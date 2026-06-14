import asyncio
import argparse
import json
from pathlib import Path

from generator import generate_report
from map_generator import generate_static_map
from pdf_generator import generate_pdf_report


def parse_args():
    parser = argparse.ArgumentParser(description="Generate a HazardMind report JSON.")
    parser.add_argument("event_id", nargs="?", default="demo-peshawar-flood")
    parser.add_argument("--output", help="Optional path to save the generated JSON.")
    parser.add_argument("--pdf-output", help="Optional path to save the generated PDF report.")
    parser.add_argument("--map-output", help="Optional path to save the generated static map PNG.")
    return parser.parse_args()


async def main():
    args = parse_args()
    report = await generate_report(args.event_id)
    map_output_path = None

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

    if args.output:
        append_report_log(report, "JSON written locally", "2026-06-13T18:04:40Z")

    report_json = json.dumps(report, indent=2)
    print(report_json)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(f"{report_json}\n", encoding="utf-8")
        print(f"Saved report JSON to {output_path}")


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
