import time
import logging
import json
import os
from dataclasses import dataclass, asdict
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

@dataclass
class LLMCallRecord:
    call_id: str
    model: str
    stage: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    cost_usd: float
    success: bool
    error: Optional[str] = None
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

# Pricing per 1M tokens (as of early 2025)
MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 5.00, "output": 15.00},
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
}

def compute_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pricing = MODEL_PRICING.get(model, {"input": 1.0, "output": 3.0})
    return (prompt_tokens / 1_000_000 * pricing["input"]) + \
           (completion_tokens / 1_000_000 * pricing["output"])


class LLMCallLogger:
    """Logs every LLM call with tokens, latency, cost."""

    def __init__(self, log_file: Optional[str] = None):
        self.log_file = log_file or os.getenv("LLM_LOG_FILE", "/tmp/llm_calls.jsonl")
        self._total_cost_usd = 0.0
        self._call_count = 0

    def log(self, record: LLMCallRecord):
        self._total_cost_usd += record.cost_usd
        self._call_count += 1

        logger.info(
            f"LLM[{record.stage}] model={record.model} "
            f"tokens={record.prompt_tokens}+{record.completion_tokens} "
            f"latency={record.latency_ms:.0f}ms cost=${record.cost_usd:.6f}"
        )

        try:
            with open(self.log_file, "a") as f:
                f.write(json.dumps(asdict(record)) + "\n")
        except Exception as e:
            logger.warning(f"Failed to write LLM log: {e}")

    @property
    def total_cost_usd(self) -> float:
        return self._total_cost_usd

    @property
    def call_count(self) -> int:
        return self._call_count

# Singleton
_logger_instance: Optional[LLMCallLogger] = None

def get_llm_logger() -> LLMCallLogger:
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = LLMCallLogger()
    return _logger_instance
