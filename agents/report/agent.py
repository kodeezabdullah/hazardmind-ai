import argparse
import asyncio
from pathlib import Path

from band_contract import build_report_completion_message, parse_report_trigger_message
from llm_clients import featherless_health_check
from pipeline import run_report_pipeline


def parse_args():
    parser = argparse.ArgumentParser(description="Generate a HazardMind report JSON.")
    parser.add_argument("event_id", nargs="?", default="demo-peshawar-flood")
    parser.add_argument("--output", help="Optional path to save the generated JSON.")
    parser.add_argument("--pdf-output", help="Optional path to save the generated PDF report.")
    parser.add_argument("--map-output", help="Optional path to save the generated static map PNG.")
    parser.add_argument("--upload-r2", action="store_true", help="Upload generated PDF and map artifacts to Cloudflare R2.")
    parser.add_argument("--write-db", action="store_true", help="Write final report metadata to Neon.")
    parser.add_argument("--from-db", action="store_true", help="Fetch report context from Neon using a UUID event_id.")
    parser.add_argument("--band-message-file", help="Path to a Band natural-language message with trailing JSON.")
    parser.add_argument("--emit-band-response", action="store_true", help="Print the Band completion message.")
    parser.add_argument("--band-response-output", help="Optional path to save the Band completion message.")
    parser.add_argument("--llm-health-check", action="store_true", help="Check Featherless model availability and exit.")
    return parser.parse_args()


async def main():
    args = parse_args()
    if args.llm_health_check:
        for label, status in await featherless_health_check():
            print(f"{label}: {status}")
        return

    incoming_payload = None
    event_id = args.event_id
    if args.band_message_file:
        incoming_payload = parse_report_trigger_message(Path(args.band_message_file).read_text(encoding="utf-8"))
        event_id = incoming_payload["event_id"]

    result = await run_report_pipeline(
        event_id=event_id,
        fetch_from_db=args.from_db,
        upload_r2=args.upload_r2,
        write_db=args.write_db,
        incoming_payload=incoming_payload,
        json_output_path=args.output,
        pdf_output_path=args.pdf_output,
        map_output_path=args.map_output,
        frontend_demo_mode=not args.from_db and event_id == "demo-peshawar-flood",
    )
    print_pipeline_summary(result)

    if args.emit_band_response or args.band_response_output:
        message = build_report_completion_message(result)
        if args.band_response_output:
            output_path = Path(args.band_response_output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(f"{message}\n", encoding="utf-8")
            print(f"band_response_output: {output_path}")
        if args.emit_band_response:
            print()
            print(message)


def print_pipeline_summary(result: dict) -> None:
    print("Report pipeline complete")
    print(f"event_id: {result.get('event_id', '')}")
    print(f"status: {result.get('status', '')}")
    print(f"pdf_url: {result.get('pdf_url', '')}")
    print(f"map_url: {result.get('map_url', '')}")
    print(f"r2_uploaded: {str(result.get('r2_uploaded', False)).lower()}")
    print(f"db_written: {str(result.get('db_written', False)).lower()}")
    print(f"warnings: {len(result.get('warnings', []))}")


if __name__ == "__main__":
    asyncio.run(main())
