"""Intelligent LLM routing based on criticality level.

Routing table:
  low      → Featherless only (no Opus fallback)
  normal   → Featherless; if confidence < 0.6 → Opus
  high     → Opus 4.8 directly; if Opus fails → GPT-5.5
  critical → Opus 4.8 + Featherless verification; results combined

Featherless chain (in order):
  1. google/gemma-4-31B-it
  2. moonshotai/Kimi-K2.6
  3. Qwen/Qwen3.6-35B-A3B

Opus 4.8 and GPT-5.5 both use the AIML API (same base_url, different model names).
GPT-5.5 is only reached when Opus throws an exception or times out.
"""

import asyncio
import json
import logging
import os
import re

from openai import AsyncOpenAI

from services.cost_tracker import cost_tracker

logger = logging.getLogger(__name__)

FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"
AIML_BASE_URL = "https://api.aimlapi.com/v1"
# Gemini via its OpenAI-compatible endpoint. Used as the escalation model now
# that the AIML account (Opus/GPT) is out of funds — Gemini has a huge context
# window and a separate quota, so high/critical reasoning still gets a strong
# model instead of falling to None and emitting null impact numbers.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

FEATHERLESS_PRIMARY = "google/gemma-4-31B-it"
FEATHERLESS_SECONDARY = "moonshotai/Kimi-K2.6"
FEATHERLESS_TERTIARY = "Qwen/Qwen3.6-35B-A3B"
FEATHERLESS_CHAIN = [FEATHERLESS_PRIMARY, FEATHERLESS_SECONDARY, FEATHERLESS_TERTIARY]

OPUS_MODEL = "claude-opus-4-8"
# Last-resort GPT via AIML. GPT-5.5 is served under its dated id
# `gpt-5.5-2026-04-23` (bare `gpt-5.5` 404s). Override with GPT_FALLBACK_MODEL.
GPT_MODEL = os.getenv("GPT_FALLBACK_MODEL", "gpt-5.5-2026-04-23")
GEMINI_MODEL = os.getenv("GEMINI_ESCALATION_MODEL", "gemini-2.5-flash")


# ── Clients ─────────────────────────────────────────────────────────────────

def _featherless_client() -> AsyncOpenAI:
    key = os.environ.get("FEATHERLESS_API_KEY", "")
    if not key:
        logger.error("[router] FEATHERLESS_API_KEY not set — Featherless calls will fail")
    return AsyncOpenAI(api_key=key, base_url=FEATHERLESS_BASE_URL)


def _aiml_client() -> AsyncOpenAI:
    key = os.environ.get("AIML_API_KEY", "")
    if not key:
        logger.error("[router] AIML_API_KEY not set — Opus/GPT calls will fail")
    return AsyncOpenAI(api_key=key, base_url=AIML_BASE_URL)


def _gemini_client() -> AsyncOpenAI | None:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return None
    return AsyncOpenAI(api_key=key, base_url=GEMINI_BASE_URL)


async def gemini_call(prompt: str) -> dict | None:
    """Call Gemini via its OpenAI-compatible endpoint. Escalation model used when
    AIML (Opus/GPT) is unavailable/out of funds."""
    client = _gemini_client()
    if client is None:
        logger.error("[router] GEMINI_API_KEY not set — Gemini escalation unavailable")
        return None
    logger.info("[router] Gemini escalation call (%s)", GEMINI_MODEL)
    return await _call_model(client, GEMINI_MODEL, prompt, "gemini")


# ── JSON extraction ──────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = re.sub(r"```\s*", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


# ── Low-level model caller ───────────────────────────────────────────────────

async def _call_model(
    client: AsyncOpenAI,
    model: str,
    prompt: str,
    model_type: str,  # "featherless" | "opus" | "gpt"
) -> dict | None:
    """Call one model with 429 retry. Returns parsed dict or None."""
    # Gemini emits fenced JSON and can be verbose; give it more room so the JSON
    # object isn't truncated mid-array (which made _extract_json fail and wrongly
    # fall through to the dead GPT/AIML path, returning 0 population).
    max_tokens = 8192 if model_type == "gemini" else 2048
    kwargs: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    # Claude and GPT via AIML don't accept temperature
    if model_type in ("featherless", "gemini"):
        kwargs["temperature"] = 0.2

    # Featherless's 4-unit concurrency cap is shared across ALL pipeline agents,
    # so under parallel load a call 429s until a slot frees — give it many
    # patient retries (a slot WILL open). Quota/funds errors fail fast (below).
    max_attempts = 8 if model_type == "featherless" else 3
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            resp = await client.chat.completions.create(**kwargs)
            text = resp.choices[0].message.content or ""
            cost_tracker.track(model_type)
            result = _extract_json(text)
            if result is None:
                logger.warning("[router] %s returned non-JSON: %.200s", model, text)
            else:
                logger.info("[router] %s → success", model)
            return result
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            # A daily-quota / out-of-funds error won't clear for hours — don't
            # waste backoff time, fail fast so the next provider is tried now.
            if "RESOURCE_EXHAUSTED" in msg or "free_tier" in msg or "out of funds" in msg.lower():
                logger.warning("[router] %s hard-limited (quota/funds) — failing fast to next provider", model)
                break
            # A transient concurrency 429 (Featherless) clears once a slot frees —
            # keep retrying with a capped backoff so the call eventually lands.
            if "429" in msg and attempt < max_attempts - 1:
                wait = min(5 + 3 * attempt, 20)
                logger.warning("[router] %s 429 — retry in %ds (attempt %d/%d)", model, wait, attempt + 1, max_attempts)
                await asyncio.sleep(wait)
            else:
                break

    logger.warning("[router] %s failed: %s", model, last_exc)
    return None


# ── Public call helpers ──────────────────────────────────────────────────────

async def featherless_call(prompt: str, model: str = FEATHERLESS_PRIMARY) -> dict | None:
    """
    Try the specified Featherless model, then fall through the remainder of the
    priority chain [PRIMARY → SECONDARY → TERTIARY] on failure.
    Never falls through to Opus — that's smart_llm_call's job.
    """
    client = _featherless_client()

    start = FEATHERLESS_CHAIN.index(model) if model in FEATHERLESS_CHAIN else 0
    chain = FEATHERLESS_CHAIN[start:]

    for m in chain:
        result = await _call_model(client, m, prompt, "featherless")
        if result is not None:
            if m != model:
                logger.info("[router] Featherless fell back: %s → %s", model, m)
            return result
        logger.warning("[router] %s returned None, trying next Featherless model", m)

    logger.error("[router] All Featherless models failed from %s", model)
    return None


async def opus_call(prompt: str) -> dict | None:
    """High-criticality escalation model.

    Prefers Gemini (huge context, live quota) because the AIML account that
    hosts Opus is out of funds; set PREFER_GEMINI_ESCALATION=false to try AIML
    Opus first once the account is topped up.
    """
    prefer_gemini = os.getenv("PREFER_GEMINI_ESCALATION", "true").lower() != "false"
    if prefer_gemini and os.getenv("GEMINI_API_KEY"):
        result = await gemini_call(prompt)
        if result is not None:
            return result
        logger.warning("[router] Gemini escalation failed — trying AIML Opus")
    logger.info("[router] Opus 4.8 call via AIML API")
    return await _call_model(_aiml_client(), OPUS_MODEL, prompt, "opus")


async def gpt_call(prompt: str) -> dict | None:
    """Call GPT-5.5 via AIML API. LAST RESORT — only when Opus throws an exception."""
    logger.warning("[router] GPT-5.5 LAST RESORT call via AIML API")
    return await _call_model(_aiml_client(), GPT_MODEL, prompt, "gpt")


# ── Result combiner ──────────────────────────────────────────────────────────

def combine_results(primary: dict, verify: dict | None) -> dict:
    """Merge Opus primary analysis with Featherless verification (critical path)."""
    if verify is None:
        return {**primary, "verified": False, "verification_notes": "Featherless verification unavailable"}

    combined = dict(primary)
    combined["verified"] = verify.get("verified", True)
    combined["verification_notes"] = verify.get("notes", verify.get("methodology", ""))

    if not verify.get("verified", True):
        combined["verification_concerns"] = verify.get("concerns", "")
        combined["verification_confidence"] = verify.get("confidence", 1.0)
        logger.warning("[router] Featherless verification flagged concerns: %s", combined["verification_concerns"])

    return combined


# ── Smart router ─────────────────────────────────────────────────────────────

async def smart_llm_call(
    prompt: str,
    criticality: str,
    context: dict | None = None,
    task_name: str = "unknown",
) -> tuple[dict | None, str, str]:
    """
    Route LLM call based on criticality level.

    Returns:
        (result_dict | None, model_used: str, reasoning: str)

    Never use Opus for routine (low/normal when Featherless confidence ≥ 0.6).
    GPT-5.5 only reached when Opus throws an exception.
    """
    logger.info("[router:%s] smart_llm_call criticality=%s", task_name, criticality)

    if criticality == "low":
        reasoning = "low criticality → Featherless only, no Opus"
        result = await featherless_call(prompt)
        model = FEATHERLESS_PRIMARY
        return result, model, reasoning

    elif criticality == "normal":
        featherless_result = await featherless_call(prompt)
        if featherless_result is not None:
            confidence = featherless_result.get("confidence", 1.0)
            if confidence >= 0.6:
                reasoning = f"normal → Featherless sufficient (confidence={confidence:.2f})"
                return featherless_result, FEATHERLESS_PRIMARY, reasoning
            logger.info("[router:%s] Featherless confidence %.2f < 0.6 — escalating", task_name, confidence)

        # Escalate (Opus is internally Gemini-first); if every escalation tier is
        # exhausted, keep the original low-confidence Featherless result rather
        # than returning None (which would emit null impact numbers downstream).
        escalated = await opus_call(prompt)
        if escalated is None:
            escalated = await gemini_call(prompt)
        if escalated is not None:
            return escalated, "escalated", "normal → Featherless low confidence, escalated"
        if featherless_result is not None:
            logger.warning("[router:%s] escalation exhausted — keeping Featherless result", task_name)
            return featherless_result, FEATHERLESS_PRIMARY, "normal → escalation down, Featherless kept"
        return None, "none", "normal → all models failed"

    elif criticality == "high":
        reasoning = "high criticality → Gemini/Opus escalation"
        # opus_call tries Gemini first (huge context, own quota), then AIML Opus.
        result = await opus_call(prompt)
        if result is not None:
            return result, OPUS_MODEL, reasoning

        logger.warning("[router:%s] Gemini+Opus down — GPT-5.5 last resort", task_name)
        result = await gpt_call(prompt)
        if result is not None:
            return result, GPT_MODEL, "high → escalation down → GPT-5.5 last resort"
        # All escalation tiers exhausted (Gemini quota hit, AIML out of funds) —
        # fall back to the Featherless chain so we still return real numbers
        # instead of None (which would emit null impact data downstream).
        logger.warning("[router:%s] all escalation failed — Featherless safety net", task_name)
        result = await featherless_call(prompt)
        return result, FEATHERLESS_PRIMARY, "high → escalation exhausted → Featherless safety net"

    elif criticality == "critical":
        reasoning = "critical → Gemini/Opus primary + Featherless verification"
        # opus_call tries Gemini first, then AIML Opus.
        opus_result = await opus_call(prompt)
        if opus_result is None:
            logger.error("[router:%s] Gemini+Opus down — GPT-5.5 last resort", task_name)
            opus_result = await gpt_call(prompt)
            if opus_result is None:
                # All escalation tiers exhausted — fall back to the Featherless
                # chain so critical tasks still return data, not None.
                logger.warning("[router:%s] escalation exhausted — Featherless safety net", task_name)
                opus_result = await featherless_call(prompt)
            if opus_result is None:
                logger.error("[router:%s] all models failed — returning None", task_name)
                return None, "none", "critical → all models failed"

        verify_prompt = (
            "Review this disaster impact analysis. Flag errors or inconsistencies.\n"
            "Return JSON only: {\"verified\": true|false, \"confidence\": 0.0-1.0, "
            "\"concerns\": \"...\", \"notes\": \"...\"}\n\n"
            f"Analysis:\n{json.dumps(opus_result, indent=2)}"
        )
        verify = await featherless_call(verify_prompt)
        combined = combine_results(opus_result, verify)
        model_used = f"{OPUS_MODEL}+{FEATHERLESS_PRIMARY}(verify)"
        return combined, model_used, reasoning

    else:
        logger.warning("[router:%s] Unknown criticality '%s' → Featherless default", task_name, criticality)
        result = await featherless_call(prompt)
        return result, FEATHERLESS_PRIMARY, f"unknown criticality '{criticality}' → Featherless default"
