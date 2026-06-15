"""Intelligent LLM routing based on criticality level.

Routing table:
  low      → Featherless only (no Opus fallback)
  normal   → Featherless; if confidence < 0.6 → Opus
  high     → Opus 4.8 directly; if Opus fails → GPT-4.5
  critical → Opus 4.8 + Featherless verification; results combined

Featherless chain (in order):
  1. google/gemma-4-31B-it
  2. moonshotai/Kimi-K2.6
  3. Qwen/Qwen3.6-35B-A3B

Opus 4.8 and GPT-4.5 both use the AIML API (same base_url, different model names).
GPT-4.5 is only reached when Opus throws an exception or times out.
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

FEATHERLESS_PRIMARY = "google/gemma-4-31B-it"
FEATHERLESS_SECONDARY = "moonshotai/Kimi-K2.6"
FEATHERLESS_TERTIARY = "Qwen/Qwen3.6-35B-A3B"
FEATHERLESS_CHAIN = [FEATHERLESS_PRIMARY, FEATHERLESS_SECONDARY, FEATHERLESS_TERTIARY]

OPUS_MODEL = "claude-opus-4-8"
GPT_MODEL = "gpt-4.5"


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
    kwargs: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2048,
    }
    # Claude and GPT via AIML don't accept temperature
    if model_type == "featherless":
        kwargs["temperature"] = 0.2

    last_exc: Exception | None = None
    for attempt in range(3):
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
            if "429" in str(exc) and attempt < 2:
                wait = 5 * (attempt + 1)
                logger.warning("[router] %s 429 — retry in %ds (attempt %d/3)", model, wait, attempt + 1)
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
    """Call claude-opus-4-8 via AIML API. Use for high/critical tasks only."""
    logger.info("[router] Opus 4.8 call via AIML API")
    return await _call_model(_aiml_client(), OPUS_MODEL, prompt, "opus")


async def gpt_call(prompt: str) -> dict | None:
    """Call GPT-4.5 via AIML API. LAST RESORT — only when Opus throws an exception."""
    logger.warning("[router] GPT-4.5 LAST RESORT call via AIML API")
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
    GPT-4.5 only reached when Opus throws an exception.
    """
    logger.info("[router:%s] smart_llm_call criticality=%s", task_name, criticality)

    if criticality == "low":
        reasoning = "low criticality → Featherless only, no Opus"
        result = await featherless_call(prompt)
        model = FEATHERLESS_PRIMARY
        return result, model, reasoning

    elif criticality == "normal":
        result = await featherless_call(prompt)
        if result is not None:
            confidence = result.get("confidence", 1.0)
            if confidence >= 0.6:
                reasoning = f"normal → Featherless sufficient (confidence={confidence:.2f})"
                return result, FEATHERLESS_PRIMARY, reasoning
            logger.info("[router:%s] Featherless confidence %.2f < 0.6 — escalating to Opus", task_name, confidence)

        reasoning = "normal → Featherless confidence low, escalated to Opus 4.8"
        result = await opus_call(prompt)
        return result, OPUS_MODEL, reasoning

    elif criticality == "high":
        reasoning = "high criticality → Opus 4.8 directly"
        result = await opus_call(prompt)
        if result is not None:
            return result, OPUS_MODEL, reasoning

        logger.warning("[router:%s] Opus failed in high-criticality path — GPT-4.5 last resort", task_name)
        result = await gpt_call(prompt)
        return result, GPT_MODEL, "high → Opus failed → GPT-4.5 last resort"

    elif criticality == "critical":
        reasoning = "critical → Opus 4.8 primary + Featherless verification"
        opus_result = await opus_call(prompt)
        if opus_result is None:
            logger.error("[router:%s] Opus failed in critical path — GPT-4.5 last resort", task_name)
            opus_result = await gpt_call(prompt)
            if opus_result is None:
                logger.error("[router:%s] GPT-4.5 also failed — returning None", task_name)
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
