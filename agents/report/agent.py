import asyncio
import argparse
import json
from pathlib import Path

from generator import generate_report


def parse_args():
    parser = argparse.ArgumentParser(description="Generate a HazardMind report JSON.")
    parser.add_argument("event_id", nargs="?", default="demo-peshawar-flood")
    parser.add_argument("--output", help="Optional path to save the generated JSON.")
    return parser.parse_args()


async def main():
    args = parse_args()
    report = await generate_report(args.event_id)
    report_json = json.dumps(report, indent=2)
    print(report_json)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(f"{report_json}\n", encoding="utf-8")
        print(f"Saved report JSON to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
