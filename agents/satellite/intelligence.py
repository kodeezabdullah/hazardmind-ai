"""HazardMind satellite agent — LLM intelligence layer.

This module turns the deterministic GIS pipeline into a reasoning agent. Every
decision point that previously relied on hard-coded rules can now consult an
LLM: parsing the raw Band message, choosing a satellite strategy, recovering
from anomalies, interpreting the raw index numbers, and writing the natural
hand-off message to the next agent.

All LLM access goes through Featherless (an OpenAI-compatible inference host)
with a fallback chain of models. If every Featherless model fails, we fall back
to Claude Opus via the AIML API (also OpenAI-compatible). If that also fails,
the calling method returns ``None`` and the caller drops back to its
deterministic default — intelligence is always additive, never a hard
dependency.

    base_url (Featherless): https://api.featherless.ai/v1   (FEATHERLESS_API_KEY)
    base_url (AIML/Opus):   https://api.aimlapi.com/v1      (AIML_API_KEY)

Model fallback chain (in order):
    1. google/gemma-4-31B-it        (primary)
    2. moonshotai/Kimi-K2.6         (fallback 1)
    3. Qwen/Qwen3.6-35B-A3B         (fallback 2)
    4. deepseek-ai/DeepSeek-V4-Pro  (fallback 3)
    5. claude-opus-4-8 via AIML     (last resort)
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

from openai import OpenAI

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"
AIML_BASE_URL = "https://api.aimlapi.com/v1"

# Per-model timeout, in seconds. If a model does not answer in time we move on
# to the next link in the chain — lives depend on speed.
MODEL_TIMEOUT_SECONDS = 30.0

# The Featherless fallback chain, tried in order. A method may pin a preferred
# primary model; if so it is moved to the front of this chain for that call.
FEATHERLESS_CHAIN = [
    "google/gemma-4-31B-it",        # primary
    "moonshotai/Kimi-K2.6",         # fallback 1
    "Qwen/Qwen3.6-35B-A3B",         # fallback 2
    "deepseek-ai/DeepSeek-V4-Pro",  # fallback 3
]

# Last-resort model, served by the AIML API (separate key / base_url).
AIML_OPUS_MODEL = "claude-opus-4-8"


# --------------------------------------------------------------------------- #
# JSON extraction helper
# --------------------------------------------------------------------------- #
def _extract_json(text: str) -> Optional[dict]:
    """Best-effort parse of a JSON object out of an LLM response.

    Models occasionally wrap JSON in markdown fences or add a sentence of
    preamble despite being told not to. We try a strict parse first, then fall
    back to extracting the outermost ``{...}`` block.
    """
    if not text:
        return None

    text = text.strip()

    # Strip a ```json ... ``` (or plain ``` ... ```) fence if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Fall back to the first balanced-looking {...} span.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            pass

    # Last resort: the response was likely truncated mid-object (a reasoning
    # model that hit finish_reason=length). Try to repair an unterminated JSON
    # object from the first '{' by closing any open string and balancing the
    # open brackets, then re-parse. This salvages a usable dict from a cut-off
    # response instead of dropping the whole call to the deterministic default.
    if start != -1:
        repaired = _repair_truncated_json(text[start:])
        if repaired is not None:
            return repaired
    return None


def _repair_truncated_json(fragment: str) -> Optional[dict]:
    """Best-effort repair of a JSON object truncated mid-stream.

    Walks the fragment tracking string state and the bracket stack, drops a
    trailing incomplete token, closes an open string, and appends the missing
    closing brackets. Returns the parsed dict, or ``None`` if it still won't
    parse. This is intentionally conservative — it only rescues an object that
    was well-formed up to the truncation point.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    # Index just after the last point where the object is structurally complete:
    # a closed value (`}`/`]`), or a separator (`,`) / container open (`{`/`[`).
    # A colon is deliberately NOT a safe point — it promises a value that may be
    # truncated, so we cut back *before* the key it follows.
    last_safe = -1

    for i, ch in enumerate(fragment):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
            last_safe = i + 1
        elif ch in "}]":
            if stack:
                stack.pop()
            last_safe = i + 1
        elif ch == ",":
            last_safe = i + 1

    if not stack:
        return None  # nothing open -> not the truncation case we handle

    # Cut back to the last structurally safe point (drops a half-written value
    # or key), strip a dangling comma, then close every still-open bracket.
    body = fragment[: last_safe if last_safe > 0 else len(fragment)].rstrip()
    body = body.rstrip(",")
    body += "".join(reversed(stack))

    try:
        result = json.loads(body)
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Intelligence
# --------------------------------------------------------------------------- #
class SatelliteIntelligence:
    """LLM-backed reasoning for the satellite agent.

    Construct once and reuse. Each public ``*`` method issues one LLM call
    (with the full fallback chain) and returns either a parsed result or
    ``None`` — never raises — so callers can always fall back to a
    deterministic default.
    """

    def __init__(
        self,
        featherless_api_key: Optional[str] = None,
        aiml_api_key: Optional[str] = None,
    ) -> None:
        self._featherless_key = featherless_api_key or os.getenv(
            "FEATHERLESS_API_KEY"
        )
        self._aiml_key = aiml_api_key or os.getenv("AIML_API_KEY")

        self._featherless: Optional[OpenAI] = None
        if self._featherless_key:
            self._featherless = OpenAI(
                base_url=FEATHERLESS_BASE_URL,
                api_key=self._featherless_key,
                timeout=MODEL_TIMEOUT_SECONDS,
                max_retries=0,  # we run our own fallback chain
            )
        else:
            logger.warning(
                "FEATHERLESS_API_KEY not set — Featherless chain disabled, "
                "will rely on AIML/Opus last resort only."
            )

        self._aiml: Optional[OpenAI] = None
        if self._aiml_key:
            self._aiml = OpenAI(
                base_url=AIML_BASE_URL,
                api_key=self._aiml_key,
                timeout=MODEL_TIMEOUT_SECONDS,
                max_retries=0,
            )
        else:
            logger.warning("AIML_API_KEY not set — Opus last resort disabled.")

    # ------------------------------------------------------------------ #
    # Low-level: run a prompt through the fallback chain
    # ------------------------------------------------------------------ #
    def _build_chain(self, primary_model: Optional[str]) -> list[tuple[str, str]]:
        """Return the ordered list of (provider, model) to try.

        ``provider`` is ``"featherless"`` or ``"aiml"``. If ``primary_model`` is
        given and lives in the Featherless chain, it is moved to the front.
        """
        chain = list(FEATHERLESS_CHAIN)
        if primary_model and primary_model in chain:
            chain.remove(primary_model)
            chain.insert(0, primary_model)
        elif primary_model and primary_model not in chain:
            # An explicitly requested model that isn't in the default chain:
            # try it first anyway (still via Featherless).
            chain.insert(0, primary_model)

        attempts: list[tuple[str, str]] = [("featherless", m) for m in chain]
        attempts.append(("aiml", AIML_OPUS_MODEL))  # last resort
        return attempts

    def _complete(
        self,
        prompt: str,
        *,
        primary_model: Optional[str] = None,
        system: Optional[str] = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> Optional[str]:
        """Run ``prompt`` through the model fallback chain.

        Returns the raw text content of the first model that answers, logging
        which model was used. Returns ``None`` if every model in the chain
        (including the Opus last resort) fails.

        Note on ``max_tokens``: several models in the Featherless chain
        (Kimi-K2.6, Qwen3.6) are *reasoning* models that spend tokens on
        internal thinking before emitting the answer. With too small a budget
        they return ``finish_reason=length`` with empty or truncated visible
        content. The default is therefore generous (2048) so a reasoning model
        has room to both think and finish its JSON; callers needing more (e.g.
        ``interpret_results``) raise it further.
        """
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        for provider, model in self._build_chain(primary_model):
            client = self._featherless if provider == "featherless" else self._aiml
            if client is None:
                continue
            try:
                kwargs = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "timeout": MODEL_TIMEOUT_SECONDS,
                }
                # The AIML-hosted Opus model rejects `temperature` ("deprecated
                # for this model" -> HTTP 400). Only the Featherless models take
                # it; omit it for the Opus last resort so the fallback actually
                # works when the whole Featherless chain is down.
                if provider != "aiml":
                    kwargs["temperature"] = temperature
                resp = client.chat.completions.create(**kwargs)
                content = (resp.choices[0].message.content or "").strip()
                if not content:
                    logger.warning(
                        "Model %s/%s returned empty content, trying next.",
                        provider,
                        model,
                    )
                    continue
                logger.info("LLM call served by %s/%s", provider, model)
                return content
            except Exception as exc:  # noqa: BLE001 - move to next model
                logger.warning(
                    "Model %s/%s failed (%s), trying next in chain.",
                    provider,
                    model,
                    exc,
                )
                continue

        logger.error("All LLM models in the fallback chain failed.")
        return None

    def _complete_json(
        self,
        prompt: str,
        *,
        primary_model: Optional[str] = None,
        system: Optional[str] = None,
        max_tokens: int = 2048,
    ) -> Optional[dict]:
        """Run ``prompt`` and parse the response as a JSON object, or ``None``."""
        raw = self._complete(
            prompt,
            primary_model=primary_model,
            system=system,
            max_tokens=max_tokens,
            temperature=0.1,  # structured output: keep it deterministic
        )
        if raw is None:
            return None
        parsed = _extract_json(raw)
        if parsed is None:
            logger.error("LLM returned non-JSON response: %.300s", raw)
        return parsed

    # ------------------------------------------------------------------ #
    # METHOD 1: parse the raw disaster message
    # ------------------------------------------------------------------ #
    def parse_disaster_input(self, raw_message: str) -> Optional[dict]:
        """Convert a raw Band message into a structured disaster profile.

        Model: google/gemma-4-31B-it. Returns the parsed dict or ``None``.
        """
        prompt = f"""\
Parse this disaster alert message and extract structured information.
Message: {raw_message}

Return ONLY valid JSON:
{{
  "location": "city, country",
  "region": "province/state",
  "disaster_type": "flood/earthquake/landslide/cyclone",
  "magnitude": null or float,
  "secondary_risks": ["list of secondary risks"],
  "urgency": "LOW/MEDIUM/HIGH/CRITICAL",
  "ambiguous": true/false,
  "missing_info": ["what info is missing"],
  "confidence": 0.0-1.0
}}"""
        return self._complete_json(
            prompt, primary_model="google/gemma-4-31B-it"
        )

    # ------------------------------------------------------------------ #
    # METHOD 2: devise the satellite strategy
    # ------------------------------------------------------------------ #
    def devise_satellite_strategy(
        self,
        disaster_profile: Any,
        cloud_cover: Any,
        available_scenes_count: int,
        attempt_number: int,
    ) -> Optional[dict]:
        """Decide the optimal satellite + analysis approach with reasoning.

        Model: moonshotai/Kimi-K2.6. Returns the parsed strategy dict or
        ``None``.
        """
        prompt = f"""\
You are a satellite imagery expert for disaster response.

Disaster profile: {json.dumps(disaster_profile, default=str)}
Current cloud cover: {cloud_cover}%
Available scenes in 7-day window: {available_scenes_count}
This is attempt number: {attempt_number}

Decide the optimal satellite strategy.

Rules:
- flood/cyclone + cloud>30% -> Sentinel-1 SAR
- earthquake/landslide + cloud<30% -> Sentinel-2
- No scenes in 7 days -> expand to 14-30 days
- Large area (>10000km^2) -> triage by priority
- Multiple disasters -> address most lethal first

Return ONLY valid JSON:
{{
  "satellite": "sentinel-1 or sentinel-2 or landsat",
  "reason": "detailed explanation",
  "date_range_days": 7,
  "bands_priority": ["B03", "B08"],
  "analysis_type": "flood/earthquake/landslide",
  "triage_needed": false,
  "triage_zones": [],
  "confidence": 0.0-1.0,
  "fallback_strategy": "what to do if this fails"
}}"""
        return self._complete_json(
            prompt, primary_model="moonshotai/Kimi-K2.6"
        )

    # ------------------------------------------------------------------ #
    # METHOD 3: anomaly recovery
    # ------------------------------------------------------------------ #
    def handle_anomaly(
        self,
        anomaly_type: str,
        context: Any,
        attempt_number: int,
    ) -> Optional[dict]:
        """Generate a recovery strategy when a pipeline step goes wrong.

        Handles: no_sentinel_scenes, high_cloud_cover, low_data_quality,
        download_failed, coverage_insufficient, extreme_index_values,
        r2_upload_failed, copernicus_auth_failed, mosaic_failed,
        landsat_fallback_needed (and any other label passed in).

        Model: Qwen/Qwen3.6-35B-A3B. Returns the parsed strategy dict or
        ``None``.
        """
        prompt = f"""\
You are managing a satellite imagery pipeline for disaster response.
Time is CRITICAL — people's lives depend on speed.

Anomaly detected: {anomaly_type}
Context: {json.dumps(context, default=str)}
Attempt number: {attempt_number}

Generate a recovery strategy.
Be specific and actionable.

Return ONLY valid JSON:
{{
  "action": "retry/fallback/skip/alert_human",
  "specific_steps": ["step 1", "step 2"],
  "use_landsat": true/false,
  "expand_date_range": null or int (days),
  "alert_human": true/false,
  "alert_message": "message if alerting human",
  "confidence_in_recovery": 0.0-1.0,
  "estimated_delay_seconds": 0,
  "reasoning": "why this strategy"
}}"""
        return self._complete_json(
            prompt, primary_model="Qwen/Qwen3.6-35B-A3B", max_tokens=2560
        )

    # ------------------------------------------------------------------ #
    # METHOD 4: interpret the raw GIS results
    # ------------------------------------------------------------------ #
    def interpret_results(
        self,
        index_type: str,
        index_stats: Any,
        disaster_type: str,
        location: str,
        total_zones: int,
        area_km2: float,
        satellite_used: str,
    ) -> Optional[dict]:
        """Convert raw GIS numbers into an expert disaster assessment.

        Model: google/gemma-4-31B-it. Returns the parsed interpretation dict or
        ``None``.
        """
        prompt = f"""\
You are a senior disaster response analyst.
Interpret these satellite analysis results.

Index type: {index_type} (NDWI/NDVI/SAR)
Statistics: {json.dumps(index_stats, default=str)}
Disaster type: {disaster_type}
Location: {location}
Hazard zones detected: {total_zones}
Affected area: {area_km2} km^2
Satellite used: {satellite_used}

Provide expert interpretation.
Be specific — mention actual numbers.
Compare to typical disaster benchmarks.

Return ONLY valid JSON:
{{
  "severity": "LOW/MEDIUM/HIGH/CRITICAL",
  "summary": "2-3 sentence expert summary",
  "key_findings": ["finding 1", "finding 2"],
  "anomalies": ["any unusual patterns"],
  "comparison": "how this compares to typical events",
  "immediate_concerns": ["urgent issues"],
  "confidence": 0.0-1.0,
  "data_quality": "POOR/FAIR/GOOD/EXCELLENT",
  "recommendations": ["rec 1", "rec 2"]
}}"""
        return self._complete_json(
            prompt, primary_model="google/gemma-4-31B-it", max_tokens=2560
        )

    # ------------------------------------------------------------------ #
    # METHOD 5: write the natural Band hand-off message
    # ------------------------------------------------------------------ #
    def generate_band_message(
        self,
        results: Any,
        interpretation: Any,
        anomalies: Any,
        confidence: Any,
        next_agent_handle: str,
    ) -> Optional[str]:
        """Write a natural agent-discussion message for the Band room.

        Unlike the other methods this returns free text (not JSON), or ``None``.
        Model: moonshotai/Kimi-K2.6.
        """
        handle = next_agent_handle.lstrip("@")
        prompt = f"""\
You are HazardMind Satellite Agent — first responder in a disaster response \
pipeline.

You just completed satellite analysis.
Now write a message to @{handle}.

Your results: {json.dumps(results, default=str)}
Expert interpretation: {json.dumps(interpretation, default=str)}
Anomalies found: {json.dumps(anomalies, default=str)}
Overall confidence: {confidence}

Rules:
- Start with @{handle}
- Be specific — use actual numbers
- Mention what you found AND what concerns you
- Flag anomalies clearly with the warning sign emoji
- Suggest what the hazard agent should focus on
- Sound like an expert agent, not a data dump
- Include confidence level
- Max 200 words
- End with the event_id

Do NOT return JSON — return a natural text message."""
        return self._complete(
            prompt,
            primary_model="moonshotai/Kimi-K2.6",
            # Kimi is a reasoning model — it needs headroom to think *and* write
            # the ~200-word message, or it returns empty (finish_reason=length).
            max_tokens=1536,
            temperature=0.5,  # a little warmth for natural prose
        )

    # ------------------------------------------------------------------ #
    # METHOD 6: decide whether Landsat is worth trying
    # ------------------------------------------------------------------ #
    def decide_landsat_fallback(
        self,
        sentinel_failure_reason: str,
        disaster_type: str,
        location: str,
        days_since_disaster: Any,
    ) -> Optional[dict]:
        """Decide whether Landsat 8/9 is a worthwhile fallback.

        Model: google/gemma-4-31B-it. Returns the parsed decision dict or
        ``None``.
        """
        prompt = f"""\
Sentinel satellite data is unavailable.
Reason: {sentinel_failure_reason}
Disaster type: {disaster_type}
Location: {location}
Days since disaster: {days_since_disaster}

Should we use Landsat 8/9 as fallback?
Note: Landsat = 30m resolution, 16-day revisit, always available.

Return ONLY valid JSON:
{{
  "use_landsat": true/false,
  "reason": "why or why not",
  "expected_quality": "LOW/MEDIUM/HIGH",
  "bands_to_use": ["B3", "B5"],
  "confidence": 0.0-1.0
}}"""
        return self._complete_json(
            prompt, primary_model="google/gemma-4-31B-it"
        )


# --------------------------------------------------------------------------- #
# Offline smoke test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from dotenv import load_dotenv

    load_dotenv()

    intel = SatelliteIntelligence()

    print("\n=== METHOD 1: parse_disaster_input ===")
    profile = intel.parse_disaster_input("flood in Peshawar magnitude 6.2")
    print(json.dumps(profile, indent=2))

    print("\n=== METHOD 2: devise_satellite_strategy ===")
    strat = intel.devise_satellite_strategy(
        profile or {"disaster_type": "flood", "location": "Peshawar"},
        cloud_cover=45,
        available_scenes_count=3,
        attempt_number=1,
    )
    print(json.dumps(strat, indent=2))

    print("\n=== METHOD 3: handle_anomaly ===")
    recovery = intel.handle_anomaly(
        "copernicus_auth_failed",
        {"event_id": "test", "location": "Peshawar"},
        attempt_number=1,
    )
    print(json.dumps(recovery, indent=2))
