"""HazardMind satellite agent — evidence-based stance system.

The satellite agent respects the orchestrator's authority but is a domain
expert: when an instruction contradicts what its own evidence shows, it speaks
up, reasons about the disagreement, and defends its position — while still
deferring if the orchestrator insists. ``StanceEngine`` turns the agent's
current evidence + confidence into a position on an incoming instruction, and
renders that position as a natural Band message.

All reasoning is delegated to the Featherless model chain (via the shared
``SatelliteIntelligence``); the engine itself is deterministic glue with a
conservative fallback (default to *comply* with low self-confidence) whenever
the LLM is unavailable, so a missing model never makes the agent insubordinate.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class StanceEngine:
    """Form and defend evidence-based positions on orchestrator instructions."""

    def __init__(self, intelligence: Optional[Any] = None) -> None:
        self._intelligence = intelligence

    def _intel(self) -> Any:
        if self._intelligence is None:
            from intelligence import SatelliteIntelligence

            self._intelligence = SatelliteIntelligence()
        return self._intelligence

    # ------------------------------------------------------------------ #
    # Form a stance
    # ------------------------------------------------------------------ #
    def evaluate_orchestrator_instruction(
        self,
        instruction: str,
        current_evidence: Any,
        tracker: Any,
    ) -> dict:
        """Decide whether to agree with an orchestrator instruction.

        Returns a stance dict::

            {
              "agree": bool,
              "confidence_in_own_position": 0.0-1.0,
              "reasoning": str,
              "recommendation": str,
              "response_to_orchestrator": str,
              "will_comply_if_insisted": bool
            }

        On total LLM failure, returns a conservative *comply* stance (the agent
        defers rather than silently ignoring the orchestrator).
        """
        intel = self._intel()
        report = tracker.get_report() if hasattr(tracker, "get_report") else tracker
        prompt = f"""\
You are HazardMind Satellite Agent, an expert in remote sensing and disaster \
analysis.

Orchestrator instruction: {instruction}

Your current evidence:
{json.dumps(current_evidence, indent=2, default=str)}

Your confidence report:
{json.dumps(report, indent=2, default=str)}

Evaluate:
1. Does this instruction make scientific sense given your evidence?
2. What does your evidence suggest instead (if anything)?
3. Should you comply, push back, or negotiate?
4. What is your professional recommendation?

Be a skilled expert who respects authority but speaks up when the data says \
otherwise. Do not be contrarian for its own sake — agree when the instruction \
is sound.

Return ONLY valid JSON:
{{
  "agree": true,
  "confidence_in_own_position": 0.0,
  "reasoning": "...",
  "recommendation": "...",
  "response_to_orchestrator": "natural message",
  "will_comply_if_insisted": true
}}"""
        stance = intel._complete_json(
            prompt, primary_model="Qwen/Qwen3.6-35B-A3B", max_tokens=2560
        )
        if not stance:
            logger.warning("StanceEngine: LLM unavailable; defaulting to comply.")
            return {
                "agree": True,
                "confidence_in_own_position": 0.3,
                "reasoning": "Could not reason about the instruction (LLM unavailable); "
                "deferring to the orchestrator.",
                "recommendation": "Proceed as instructed.",
                "response_to_orchestrator": "Acknowledged — proceeding as instructed.",
                "will_comply_if_insisted": True,
            }
        # Normalise the fields the renderer relies on so a partial LLM reply
        # never crashes form_band_message.
        stance.setdefault("agree", True)
        stance.setdefault("confidence_in_own_position", 0.5)
        stance.setdefault("reasoning", "")
        stance.setdefault("recommendation", "")
        stance.setdefault("response_to_orchestrator", "")
        stance.setdefault("will_comply_if_insisted", True)
        return stance

    # ------------------------------------------------------------------ #
    # Render the stance as a Band message
    # ------------------------------------------------------------------ #
    def form_band_message(self, stance: dict, orchestrator_handle: str) -> str:
        """Convert a stance dict into a natural Band message string."""
        handle = (orchestrator_handle or "").lstrip("@")
        if stance.get("agree"):
            rec = stance.get("recommendation") or "Agreed."
            return f"@{handle}\n{rec}\nProceeding as suggested."

        try:
            conf_pct = float(stance.get("confidence_in_own_position") or 0.0)
        except (TypeError, ValueError):
            conf_pct = 0.0
        closing = (
            "I will switch if you insist — awaiting your call."
            if stance.get("will_comply_if_insisted")
            else "Strongly recommend reconsidering."
        )
        response = stance.get("response_to_orchestrator") or "I have a concern with this instruction."
        reasoning = stance.get("reasoning") or "see evidence above"
        return (
            f"@{handle}\n{response}\n\n"
            f"My evidence suggests: {reasoning}\n\n"
            f"Confidence in current approach: {conf_pct:.0%}\n\n"
            f"{closing}"
        )


if __name__ == "__main__":
    # Offline structural smoke test with a stubbed intelligence layer.
    logging.basicConfig(level=logging.INFO)

    class _StubIntel:
        def _complete_json(self, prompt, **kw):
            return {
                "agree": False,
                "confidence_in_own_position": 0.85,
                "reasoning": "Cloud cover is only 15% — optical Sentinel-2 is reliable; "
                "switching to SAR would lose spectral detail.",
                "recommendation": "Stay on Sentinel-2.",
                "response_to_orchestrator": "I'd hold off on SAR here.",
                "will_comply_if_insisted": True,
            }

    eng = StanceEngine(intelligence=_StubIntel())
    s = eng.evaluate_orchestrator_instruction("switch to SAR", {"cloud_cover": 15}, {"overall": 0.8})
    print(json.dumps(s, indent=2))
    print("---")
    print(eng.form_band_message(s, "@hazardmind-orchestrator"))
