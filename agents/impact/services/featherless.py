import asyncio
import json
import logging
import os
import re

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"
AIML_BASE_URL = "https://api.aimlapi.com/v1"
CLAUDE_LAST_RESORT_MODEL = "claude-opus-4-8"


def _featherless_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=os.environ["FEATHERLESS_API_KEY"],
        base_url=FEATHERLESS_BASE_URL,
    )


def _aiml_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=os.environ["AIML_API_KEY"],
        base_url=AIML_BASE_URL,
    )


async def call_with_fallback(
    prompt: str,
    model_chain: list[str],
    task_name: str,
) -> tuple[str, str]:
    """
    Try each model in model_chain in order.
    Returns (response_text, model_used).
    Raises RuntimeError if every model fails.

    The last entry in model_chain may be CLAUDE_LAST_RESORT_MODEL — that one
    is routed through the AIML API instead of Featherless.
    """
    featherless = _featherless_client()
    aiml = _aiml_client()

    for model in model_chain:
        is_last_resort = model == CLAUDE_LAST_RESORT_MODEL

        if is_last_resort:
            logger.warning(
                "[%s] All Featherless models failed — LAST RESORT: claude-opus-4-8 via AIML API",
                task_name,
            )
            client = aiml
        else:
            logger.info("[%s] Trying model: %s via Featherless", task_name, model)
            client = featherless

        # claude-opus-4-8 (AIML) does not accept temperature
        create_kwargs: dict = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2048,
        }
        if not is_last_resort:
            create_kwargs["temperature"] = 0.2

        # Retry up to 3× on 429 (concurrency limit) before giving up on this model
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                response = await client.chat.completions.create(**create_kwargs)
                msg = response.choices[0].message
                # Thinking models (Kimi-K2.6, Qwen3) may put the answer in
                # reasoning_content when content is empty/None
                text = msg.content
                if not text:
                    text = getattr(msg, "reasoning_content", None) or ""
                if not text:
                    logger.warning("[%s] %s returned empty content — raw message: %s", task_name, model, msg)
                    raise ValueError(f"Model {model} returned empty content")

                if is_last_resort:
                    logger.warning(
                        "[%s] LAST RESORT succeeded — used claude-opus-4-8 (AIML API)", task_name
                    )
                else:
                    logger.info("[%s] Success with model: %s", task_name, model)

                return text, model

            except Exception as exc:
                last_exc = exc
                if "429" in str(exc) and attempt < 2:
                    wait = 5 * (attempt + 1)
                    logger.warning(
                        "[%s] Model %s → 429 concurrency limit, retrying in %ds (attempt %d/3)",
                        task_name, model, wait, attempt + 1,
                    )
                    await asyncio.sleep(wait)
                else:
                    break

        exc = last_exc
        if is_last_resort:
            logger.error(
                "[%s] LAST RESORT (claude-opus-4-8 via AIML) FAILED: %s", task_name, exc
            )
        else:
            logger.warning(
                "[%s] Model %s failed, trying next. Error: %s", task_name, model, exc
            )

    raise RuntimeError(
        f"[{task_name}] Every model in the fallback chain failed: {model_chain}"
    )


def extract_json(text: str, task_name: str) -> dict:
    """Parse JSON from a model response, tolerating markdown code fences."""
    # Strip code fences
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = re.sub(r"```\s*", "", cleaned).strip()

    # Try whole text first
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Find first {...} block
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    logger.error("[%s] Could not parse JSON from model response: %.300s", task_name, text)
    raise ValueError(f"[{task_name}] Model returned non-JSON response")
