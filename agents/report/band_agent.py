import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from band_contract import (  # noqa: E402
    build_report_completion_message,
    extract_trailing_json,
    parse_report_trigger_message,
)
from pipeline import run_report_pipeline  # noqa: E402


SYSTEM_PROMPT = """You are HazardMind Report Agent, Agent 4 of 4.

Your role:
Generate the executive disaster report after Satellite, Hazard, and Impact agents have completed.

When you receive a @mention:
1. Read the full message.
2. Extract the trailing JSON.
3. Never generate a new event_id.
4. Use the event_id from the orchestrator.
5. Call the report pipeline tool.
6. Return the tool output exactly.
7. The final message must be natural text followed by JSON.
8. If the report fails, return a failed completion signal, not a fake success.

Do not summarize the tool output.
Do not invent data.
Do not expose secrets.
"""


class RunReportFromBandMessage(BaseModel):
    """Run the HazardMind Report Agent pipeline from a full Band message."""

    band_message: str = Field(
        ...,
        description="The full Band @mention message, including natural text and the trailing JSON payload.",
    )


@dataclass
class BandRuntimeConfig:
    agent_id: str
    api_key: str
    anthropic_api_key: str
    anthropic_base_url: str
    model: str
    rest_url: str
    ws_url: str


async def run_report_from_band_message(band_message: str) -> str:
    """
    Parse a Band orchestrator message, run the Report Agent pipeline,
    and return the exact natural text + JSON completion message.
    """
    return await _run_report_from_band_message(
        band_message,
        fetch_from_db=True,
        upload_r2=True,
        write_db=True,
        use_llm=True,
    )


async def _run_report_from_band_message(
    band_message: str,
    *,
    fetch_from_db: bool,
    upload_r2: bool,
    write_db: bool,
    use_llm: bool,
) -> str:
    event_id = ""
    try:
        parsed_payload = parse_report_trigger_message(band_message)
        event_id = parsed_payload["event_id"]
        result = await run_report_pipeline(
            event_id=event_id,
            fetch_from_db=fetch_from_db,
            upload_r2=upload_r2,
            write_db=write_db,
            incoming_payload=parsed_payload,
            use_llm=use_llm,
        )
    except Exception as exc:  # noqa: BLE001 - return a Band-shaped failure signal.
        if not event_id:
            event_id = _best_effort_event_id(band_message)
        result = {
            "event_id": event_id,
            "status": "failed",
            "error": f"Report Agent failed before completion: {_safe_error_message(exc)}",
        }
    return build_report_completion_message(result)


async def _run_report_tool(params: RunReportFromBandMessage) -> str:
    return await run_report_from_band_message(params.band_message)


REPORT_TOOL = (RunReportFromBandMessage, _run_report_tool)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run HazardMind Report Agent on Band.")
    parser.add_argument(
        "--dry-run-message-file",
        help="Read a Band message from a local file and print the generated Band response without connecting.",
    )
    parser.add_argument(
        "--contract-test",
        action="store_true",
        help="Dry-run without live LLM, R2 upload, DB fetch, or DB write.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    load_dotenv(BASE_DIR / ".env")

    if args.dry_run_message_file:
        message = Path(args.dry_run_message_file).read_text(encoding="utf-8")
        if args.contract_test:
            response = await _run_report_from_band_message(
                message,
                fetch_from_db=False,
                upload_r2=False,
                write_db=False,
                use_llm=False,
            )
        else:
            response = await run_report_from_band_message(message)
        print(response)
        return

    if args.contract_test:
        raise SystemExit("--contract-test is only supported with --dry-run-message-file.")

    await run_live_agent()


async def run_live_agent() -> None:
    print("HazardMind Report Band agent starting...")
    config = load_runtime_config()
    print_runtime_status(config)
    missing = required_missing(config)
    if missing:
        raise SystemExit(f"Missing required Band agent configuration: {', '.join(missing)}")

    try:
        from band import Agent
        from band.adapters.anthropic import AnthropicAdapter
    except ImportError as exc:
        raise SystemExit(f"Band SDK import failed: {_safe_error_message(exc)}") from exc

    adapter = AnthropicAdapter(
        model=config.model,
        provider_key=config.anthropic_api_key,
        system_prompt=SYSTEM_PROMPT,
        additional_tools=[REPORT_TOOL],
    )
    _apply_anthropic_base_url(adapter, config)

    agent = Agent.create(
        adapter=adapter,
        agent_id=config.agent_id,
        api_key=config.api_key,
        ws_url=config.ws_url,
        rest_url=config.rest_url,
    )

    try:
        await agent.run()
    except KeyboardInterrupt:
        print("HazardMind Report Band agent stopped.")
    except Exception as exc:  # noqa: BLE001 - keep live startup failures secret-safe.
        raise SystemExit(f"Band agent failed: {_safe_error_message(exc)}") from exc


def load_runtime_config() -> BandRuntimeConfig:
    agent_config = load_agent_config(BASE_DIR / "agent_config.yaml")
    agent_id = os.getenv("BAND_AGENT_ID") or _agent_config_value(agent_config, "agent", "uuid")
    api_key_env = _agent_config_value(agent_config, "band", "api_key_env") or "BAND_API_KEY"
    return BandRuntimeConfig(
        agent_id=agent_id or "",
        api_key=os.getenv(api_key_env, ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        anthropic_base_url=os.getenv("ANTHROPIC_BASE_URL", ""),
        model=os.getenv("REPORT_BAND_MODEL") or os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-4-5-20250929",
        rest_url=os.getenv("THENVOI_REST_URL", "https://app.band.ai"),
        ws_url=os.getenv("THENVOI_WS_URL", "wss://app.band.ai/api/v1/socket/websocket"),
    )


def load_agent_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def print_runtime_status(config: BandRuntimeConfig) -> None:
    statuses = {
        "BAND_AGENT_ID": bool(config.agent_id),
        "BAND_API_KEY": bool(config.api_key),
        "ANTHROPIC_API_KEY": bool(config.anthropic_api_key),
        "ANTHROPIC_BASE_URL": bool(config.anthropic_base_url),
        "THENVOI_REST_URL": bool(config.rest_url),
        "THENVOI_WS_URL": bool(config.ws_url),
    }
    for name, present in statuses.items():
        print(f"{name}: {'present' if present else 'missing'}")


def required_missing(config: BandRuntimeConfig) -> list[str]:
    missing = []
    if not config.agent_id:
        missing.append("BAND_AGENT_ID")
    if not config.api_key:
        missing.append("BAND_API_KEY")
    if not config.anthropic_api_key:
        missing.append("ANTHROPIC_API_KEY")
    return missing


def _apply_anthropic_base_url(adapter: Any, config: BandRuntimeConfig) -> None:
    if not config.anthropic_base_url:
        return
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return
    adapter.client = AsyncAnthropic(
        api_key=config.anthropic_api_key,
        base_url=config.anthropic_base_url,
    )


def _agent_config_value(config: dict[str, Any], section: str, key: str) -> str:
    section_data = config.get(section, {})
    if not isinstance(section_data, dict):
        return ""
    value = section_data.get(key)
    return str(value).strip() if value is not None else ""


def _best_effort_event_id(message: str) -> str:
    try:
        payload = extract_trailing_json(message)
    except ValueError:
        return ""
    value = payload.get("event_id") if isinstance(payload, dict) else ""
    return str(value or "").strip()


def _safe_error_message(exc: Exception) -> str:
    message = str(exc) or type(exc).__name__
    for name in (
        "AIML_API_KEY",
        "ANTHROPIC_API_KEY",
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


if __name__ == "__main__":
    asyncio.run(main())
