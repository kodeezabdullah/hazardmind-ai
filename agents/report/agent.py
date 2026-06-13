import asyncio
import json
import sys

from generator import generate_report


async def main():
    event_id = sys.argv[1] if len(sys.argv) > 1 else "demo-dhaka-flood"
    report = await generate_report(event_id)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
