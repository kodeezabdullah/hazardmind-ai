"""Verify the satellite agent can connect to Band.

Connects to the Band platform using the Anthropic adapter, prints the
registered agent name, then disconnects. Reads credentials from .env.

Run:
    python verify_setup.py
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

from band import Agent
from band.adapters.anthropic import AnthropicAdapter


def _require(name: str) -> str:
    """Return env var `name` or exit with a clear message if missing."""
    value = os.getenv(name)
    if not value:
        sys.exit(f"Missing required environment variable: {name} (set it in .env)")
    return value


async def main() -> None:
    load_dotenv()

    agent_id = _require("BAND_AGENT_ID")
    api_key = _require("BAND_API_KEY")
    anthropic_api_key = _require("ANTHROPIC_API_KEY")
    rest_url = os.getenv("THENVOI_REST_URL", "https://app.band.ai")
    ws_url = os.getenv(
        "THENVOI_WS_URL", "wss://app.band.ai/api/v1/socket/websocket"
    )

    adapter = AnthropicAdapter(
        provider_key=anthropic_api_key,
        prompt="Satellite agent connectivity check.",
    )

    agent = Agent.create(
        adapter=adapter,
        agent_id=agent_id,
        api_key=api_key,
        ws_url=ws_url,
        rest_url=rest_url,
    )

    print("Connecting to Band...")
    await agent.start()
    try:
        print(f"Connected as: {agent.agent_name}")
    finally:
        await agent.stop()
        print("Disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
