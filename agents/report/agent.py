import argparse
import asyncio

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
    parser.add_argument("--llm-health-check", action="store_true", help="Check Featherless model availability and exit.")
    return parser.parse_args()


async def main():
    args = parse_args()
    if args.llm_health_check:
        for label, status in await featherless_health_check():
            print(f"{label}: {status}")
        return

    result = await run_report_pipeline(
        event_id=args.event_id,
        fetch_from_db=args.from_db,
        upload_r2=args.upload_r2,
        write_db=args.write_db,
        json_output_path=args.output,
        pdf_output_path=args.pdf_output,
        map_output_path=args.map_output,
        frontend_demo_mode=not args.from_db and args.event_id == "demo-peshawar-flood",
    )
    print_pipeline_summary(result)


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
