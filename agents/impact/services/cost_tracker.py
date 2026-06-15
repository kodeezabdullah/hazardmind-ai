"""Singleton cost tracker — reset once per request in main.py."""

import logging

logger = logging.getLogger(__name__)

_COST_PER_CALL = {
    "featherless": 0.001,
    "opus": 0.015,
    "gpt": 0.01,
}


class CostTracker:
    def __init__(self) -> None:
        self.featherless_calls = 0
        self.opus_calls = 0
        self.gpt_calls = 0

    def track(self, model_type: str) -> None:
        if model_type == "featherless":
            self.featherless_calls += 1
        elif model_type == "opus":
            self.opus_calls += 1
        elif model_type == "gpt":
            self.gpt_calls += 1
        else:
            logger.warning("CostTracker: unknown model_type '%s'", model_type)
        logger.info(
            "[cost] %s call — running totals: featherless=%d opus=%d gpt=%d",
            model_type, self.featherless_calls, self.opus_calls, self.gpt_calls,
        )

    def get_summary(self) -> dict:
        estimated = (
            self.featherless_calls * _COST_PER_CALL["featherless"]
            + self.opus_calls * _COST_PER_CALL["opus"]
            + self.gpt_calls * _COST_PER_CALL["gpt"]
        )
        return {
            "featherless_calls": self.featherless_calls,
            "opus_calls": self.opus_calls,
            "gpt_calls": self.gpt_calls,
            "estimated_cost_usd": round(estimated, 4),
        }

    def reset(self) -> None:
        self.featherless_calls = 0
        self.opus_calls = 0
        self.gpt_calls = 0


# Module-level singleton — reset at the start of each request
cost_tracker = CostTracker()
