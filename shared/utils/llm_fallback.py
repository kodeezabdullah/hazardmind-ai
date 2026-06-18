"""Shared LLM routing for all HazardMind agents.

Intelligence work walks a four-link fallback chain, each link tried only when
the one before it cannot answer:

    1. Featherless  — OpenAI-compatible host, the cost-effective primary.
    2. Gemini 3.5 Flash — Google GenAI SDK (GEMINI_API_KEY).
    3. Claude Sonnet — via the AIML Anthropic ``/v1/messages`` endpoint.
    4. GPT-5.5      — via the AIML OpenAI ``/chat/completions`` endpoint (last resort).

Claude and GPT both live on AIML but on *different* protocols: AIML serves its
GPT models only on the OpenAI ``/chat/completions`` path and its Claude models
only on the Anthropic ``/v1/messages`` path, so each needs its own client.
(The Band per-turn LLM is unrelated — it runs through a Band adapter.)

    Featherless: https://api.featherless.ai/v1   (FEATHERLESS_API_KEY)
    Gemini:      Google GenAI SDK                (GEMINI_API_KEY)
    AIML/Claude: https://api.aimlapi.com          (AIML_API_KEY, /v1/messages)
    AIML/GPT:    https://api.aimlapi.com/v1        (AIML_API_KEY, /chat/completions)

Routing by criticality:
    low          -> Featherless chain only (no paid fallbacks)
    normal/high  -> Featherless -> Gemini -> Claude -> GPT
    critical     -> GPT directly (skip the cheaper links)

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

# Gemini (fallback 2). Google GenAI SDK; the model id is overridable.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

# Claude Sonnet via the AIML Anthropic endpoint (fallback 3). AIML serves Claude
# only on the Anthropic /v1/messages protocol (NOT /chat/completions), so it
# needs its own client + base. ANTHROPIC_BASE_URL is the AsyncAnthropic SDK var.
CLAUDE_MODEL = os.getenv("CLAUDE_FALLBACK_MODEL", "claude-sonnet-4-6")
CLAUDE_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.aimlapi.com")

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
GEMINI_TIMEOUT = 30.0
CLAUDE_TIMEOUT = 60.0
GPT_TIMEOUT = 60.0


def _client(api_key_env: str, base_url: str) -> Optional[AsyncOpenAI]:
    """Build an AsyncOpenAI client for ``base_url``, or ``None`` if the key is unset."""
    key = os.getenv(api_key_env)
    if not key:
        logger.warning("%s not set — that provider is disabled.", api_key_env)
        return None
    return AsyncOpenAI(api_key=key, base_url=base_url, max_retries=0)


def _claude_client():
    """Build an AsyncAnthropic client pointed at AIML, or ``None`` if unconfigured."""
    key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("AIML_API_KEY")
    if not key:
        logger.warning("ANTHROPIC_API_KEY/AIML_API_KEY not set — Claude disabled.")
        return None
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        logger.warning("anthropic SDK not installed — Claude fallback disabled.")
        return None
    return AsyncAnthropic(api_key=key, base_url=CLAUDE_BASE_URL, max_retries=0)


def _gemini_client():
    """Build a Google GenAI client, or ``None`` if the key/SDK is unavailable."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        logger.warning("GEMINI_API_KEY not set — Gemini fallback disabled.")
        return None
    try:
        from google import genai
    except ImportError:
        logger.warning("google-genai not installed — Gemini fallback disabled.")
        return None
    return genai.Client(api_key=key)


# Built once and reused. Any may be None if its key/SDK is missing.
featherless_client = _client("FEATHERLESS_API_KEY", FEATHERLESS_BASE_URL)
gemini_client = _gemini_client()
claude_client = _claude_client()
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


async def _call_gemini(prompt: str, system: str, max_tokens: int) -> Optional[str]:
    """Call Gemini via the Google GenAI SDK. Returns text, or ``None`` on failure."""
    if gemini_client is None:
        return None
    try:
        from google.genai import types

        config = types.GenerateContentConfig(max_output_tokens=max_tokens)
        if system:
            config.system_instruction = system
        response = await gemini_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=config,
        )
        content = (getattr(response, "text", None) or "").strip()
        if content:
            logger.info("[LLM] Served by: %s", GEMINI_MODEL)
            return content
        logger.warning("[LLM] %s returned empty content.", GEMINI_MODEL)
        return None
    except Exception as exc:  # noqa: BLE001 — move on to the next link in the chain
        logger.warning("[LLM] Gemini (%s) failed: %s", GEMINI_MODEL, exc)
        return None


async def _call_claude(prompt: str, system: str, max_tokens: int) -> Optional[str]:
    """Call Claude via AIML's Anthropic endpoint. Returns text, or ``None`` on failure."""
    if claude_client is None:
        return None
    try:
        response = await claude_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system or None,
            messages=[{"role": "user", "content": prompt}],
            timeout=CLAUDE_TIMEOUT,
        )
        parts = [
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        ]
        content = "".join(parts).strip()
        if content:
            logger.info("[LLM] Served by: %s", CLAUDE_MODEL)
            return content
        logger.warning("[LLM] %s returned empty content.", CLAUDE_MODEL)
        return None
    except Exception as exc:  # noqa: BLE001 — move on to the last-resort GPT link
        logger.warning("[LLM] Claude (%s) failed: %s", CLAUDE_MODEL, exc)
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
    """Smart LLM routing: Featherless -> Gemini -> Claude -> GPT.

    criticality:
        ``"critical"``     -> GPT directly (skip the cheaper links).
        ``"high"``         -> full chain (Featherless -> Gemini -> Claude -> GPT).
        ``"normal"``/other -> full chain.
        ``"low"``          -> Featherless chain only (no paid fallbacks).

    Returns the response text, or ``None`` if every eligible model failed.
    """
    if criticality == "critical":
        return await _call_gpt(prompt, system, max_tokens)

    content = await _call_featherless(prompt, system, max_tokens)
    if content is not None:
        return content

    if criticality == "low":
        logger.error("[LLM] Featherless exhausted and criticality=low — no fallback.")
        return None

    logger.info("[LLM] Featherless exhausted → Gemini (%s)", GEMINI_MODEL)
    content = await _call_gemini(prompt, system, max_tokens)
    if content is not None:
        return content

    logger.info("[LLM] Gemini exhausted → Claude (%s)", CLAUDE_MODEL)
    content = await _call_claude(prompt, system, max_tokens)
    if content is not None:
        return content

    logger.info("[LLM] Claude exhausted → GPT (%s)", GPT_MODEL)
    return await _call_gpt(prompt, system, max_tokens)
