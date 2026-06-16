"""Shared LLM routing for all HazardMind agents.

Intelligence work routes through **Featherless** (an OpenAI-compatible inference
host) first — it is the cost-effective primary and serves every routine call.
**GPT (via the AIML API, also OpenAI-compatible) is the last resort**, used only
when Featherless cannot answer or when a caller marks the work as critical.

This is deliberately the *only* place GPT is reached over the OpenAI
`/chat/completions` protocol. The Band per-turn LLM still runs through the Band
``AnthropicAdapter`` (Anthropic ``/v1/messages`` protocol via the AIML proxy);
AIML does not expose its GPT models on that protocol, so GPT cannot be the Band
adapter model and lives here instead.

    Featherless: https://api.featherless.ai/v1   (FEATHERLESS_API_KEY)
    AIML / GPT:  https://api.aimlapi.com/v1       (AIML_API_KEY)

Routing by criticality:
    low / normal -> Featherless chain only
    high         -> Featherless chain -> GPT
    critical     -> GPT directly (skip Featherless)

Every path returns ``None`` rather than raising, so a caller can always fall
back to its deterministic default — intelligence is additive, never required.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Featherless models (primary), tried in order until one answers.
FEATHERLESS_MODELS = [
    "google/gemma-4-31B-it",
    "moonshotai/Kimi-K2.6",
    "Qwen/Qwen3.6-35B-A3B",
    "deepseek-ai/DeepSeek-V4-Pro",
]

# GPT via the AIML API (last resort). AIML lists GPT-5.5 only under its dated id
# `gpt-5.5-2026-04-23` (bare `gpt-5.5` 404s; the `-pro` variant 404s too). The
# dated id resolves (verified: 403 OutOfFunds, i.e. it exists, just needs a
# funded account). Override with GPT_FALLBACK_MODEL if the catalogue changes.
GPT_MODEL = os.getenv("GPT_FALLBACK_MODEL", "gpt-5.5-2026-04-23")

FEATHERLESS_BASE_URL = os.getenv(
    "FEATHERLESS_BASE_URL", "https://api.featherless.ai/v1"
)
# The OpenAI-protocol base for AIML. OPENAI_BASE_URL is the canonical var; we
# fall back to the legacy AIML names so existing .env files keep working.
GPT_BASE_URL = (
    os.getenv("OPENAI_BASE_URL")
    or os.getenv("AIML_BASE_URL")
    or "https://api.aimlapi.com/v1"
)

# Per-call timeouts (seconds). Lives depend on speed, so a slow model is dropped.
FEATHERLESS_TIMEOUT = 30.0
GPT_TIMEOUT = 60.0


def _client(api_key_env: str, base_url: str) -> Optional[AsyncOpenAI]:
    """Build an AsyncOpenAI client for ``base_url``, or ``None`` if the key is unset."""
    key = os.getenv(api_key_env)
    if not key:
        logger.warning("%s not set — that provider is disabled.", api_key_env)
        return None
    return AsyncOpenAI(api_key=key, base_url=base_url, max_retries=0)


# Built once and reused. Either may be None if its key is missing.
featherless_client = _client("FEATHERLESS_API_KEY", FEATHERLESS_BASE_URL)
gpt_client = _client("AIML_API_KEY", GPT_BASE_URL)


async def _call_gpt(prompt: str, system: str, max_tokens: int) -> Optional[str]:
    """Call GPT via AIML (OpenAI protocol). Returns text, or ``None`` on failure."""
    if gpt_client is None:
        logger.error("[LLM] GPT requested but AIML_API_KEY is not set.")
        return None
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        response = await gpt_client.chat.completions.create(
            model=GPT_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            timeout=GPT_TIMEOUT,
        )
        content = (response.choices[0].message.content or "").strip()
        if content:
            logger.info("[LLM] Served by: %s", GPT_MODEL)
            return content
        logger.warning("[LLM] %s returned empty content.", GPT_MODEL)
        return None
    except Exception as exc:  # noqa: BLE001 — last resort never raises to caller
        logger.warning("[LLM] GPT (%s) failed: %s", GPT_MODEL, exc)
        return None


async def _call_featherless(prompt: str, system: str, max_tokens: int) -> Optional[str]:
    """Walk the Featherless chain; return the first model's text, or ``None``."""
    if featherless_client is None:
        return None
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    for model in FEATHERLESS_MODELS:
        try:
            response = await featherless_client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.2,
                timeout=FEATHERLESS_TIMEOUT,
            )
            content = (response.choices[0].message.content or "").strip()
            if content and len(content) > 10:
                logger.info("[LLM] Served by: %s", model)
                return content
            logger.warning("[LLM] %s returned empty/short content, trying next.", model)
        except Exception as exc:  # noqa: BLE001 — move to next model in the chain
            logger.warning("[LLM] %s failed: %s", model, exc)
            continue
    return None


async def llm_call(
    prompt: str,
    system: str = "",
    max_tokens: int = 1000,
    criticality: str = "normal",
) -> Optional[str]:
    """Smart LLM routing across Featherless (primary) and GPT (last resort).

    criticality:
        ``"critical"``     -> GPT directly (skip Featherless).
        ``"high"``         -> Featherless chain, then GPT if it is exhausted.
        ``"normal"``/other -> Featherless chain, then GPT as a final fallback.
        ``"low"``          -> Featherless chain only (no GPT).

    Returns the response text, or ``None`` if every eligible model failed.
    """
    if criticality == "critical":
        return await _call_gpt(prompt, system, max_tokens)

    content = await _call_featherless(prompt, system, max_tokens)
    if content is not None:
        return content

    if criticality == "low":
        logger.error("[LLM] Featherless exhausted and criticality=low — no GPT fallback.")
        return None

    logger.info("[LLM] Featherless exhausted → GPT (%s)", GPT_MODEL)
    return await _call_gpt(prompt, system, max_tokens)
