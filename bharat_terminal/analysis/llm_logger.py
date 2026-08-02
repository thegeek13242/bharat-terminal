import contextvars
import logging
import json
import os
from dataclasses import dataclass, asdict
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Task-local cost bucket. An asyncio task that calls begin_task_cost() gets its
# own accumulator (contextvars are task-scoped), so CONCURRENT pipeline items
# attribute LLM spend correctly. The old pattern — delta of the global total —
# only worked while RT items processed strictly one at a time.
#
# The bucket is a MUTABLE single-element list on purpose: child tasks spawned
# within an item (e.g. the parallel filing-summary task) receive a *copy* of
# the context, but the copy holds a reference to the same list — so their LLM
# spend still lands in the parent item's bucket.
_task_cost: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "llm_task_cost", default=None
)


def begin_task_cost() -> None:
    """Start a fresh cost bucket for the current asyncio task (and children)."""
    _task_cost.set([0.0])


def current_task_cost() -> Optional[float]:
    """Accumulated LLM cost for the current task (None if never started)."""
    bucket = _task_cost.get()
    return bucket[0] if bucket is not None else None

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
    # Optional instrumentation (default 0 → existing callers unaffected via asdict).
    # thinking_tokens: Gemini thoughts billed as output but reported separately.
    # cached_tokens: input tokens served from cache (billed at a discount).
    # web_search_calls: grounding requests, billed PER CALL, not per token.
    # turns: agent tool-loop iterations for this run.
    thinking_tokens: int = 0
    cached_tokens: int = 0
    web_search_calls: int = 0
    turns: int = 0
    # What this call was FOR: news_id for analysis-stage calls, symbol for
    # dossier/grounding calls. Powers per-filing cost rollups in /admin/costs
    # (SUM(cost_usd) GROUP BY context_id). Empty = unattributed.
    context_id: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

# Pricing per 1M tokens (as of early 2025)
MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    # Gemini on Vertex (approx list price per 1M tokens)
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    "gemini-3.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-3.1-pro-preview": {"input": 1.25, "output": 5.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 5.00, "output": 15.00},
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
}

def compute_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pricing = MODEL_PRICING.get(model, {"input": 1.0, "output": 3.0})
    return (prompt_tokens / 1_000_000 * pricing["input"]) + \
           (completion_tokens / 1_000_000 * pricing["output"])


class LLMCallLogger:
    """Logs every LLM call with tokens, latency, cost.

    Sinks, all fail-open:
    1. JSONL file (LLM_LOG_FILE) — legacy/local debugging; EPHEMERAL in
       containers (no volume mount) and wiped on restart.
    2. Redis LPUSH llm:calls — drained by the api-gateway flusher into the
       durable llm_call_log table (migration 035). This is the sink
       /admin/costs reads. Capped by LTRIM so a dead flusher can't grow
       Redis unboundedly.
    """

    REDIS_QUEUE = "llm:calls"
    REDIS_QUEUE_CAP = 50_000

    def __init__(self, log_file: Optional[str] = None):
        self.log_file = log_file or os.getenv("LLM_LOG_FILE", "/tmp/llm_calls.jsonl")
        self._total_cost_usd = 0.0
        self._call_count = 0
        self._redis = None
        self._redis_dead = False
        self._service = os.getenv("SERVICE_NAME") or os.getenv("HOSTNAME", "")

    def _push_redis(self, record: LLMCallRecord) -> None:
        """Queue the record for the durable llm_call_log table. Sync client
        with short timeouts (call sites are latency-tolerant — they just spent
        seconds on an LLM); one hard failure disables further attempts for
        this process so a Redis outage costs at most one timeout."""
        if self._redis_dead:
            return
        try:
            if self._redis is None:
                import redis
                self._redis = redis.Redis.from_url(
                    os.getenv("REDIS_URL", "redis://redis:6379/0"),
                    decode_responses=True,
                    socket_connect_timeout=2, socket_timeout=2,
                )
            payload = asdict(record)
            payload["service"] = self._service
            pipe = self._redis.pipeline()
            pipe.lpush(self.REDIS_QUEUE, json.dumps(payload))
            pipe.ltrim(self.REDIS_QUEUE, 0, self.REDIS_QUEUE_CAP - 1)
            pipe.execute()
        except Exception as e:
            logger.warning("LLM cost redis push failed (disabling for this "
                           "process): %s", str(e)[:200])
            self._redis_dead = True

    def log(self, record: LLMCallRecord):
        self._total_cost_usd += record.cost_usd
        self._call_count += 1

        # Attribute to the current task's bucket when one is active. In-place
        # mutation so spawned child tasks (which share the list) count too.
        bucket = _task_cost.get()
        if bucket is not None:
            bucket[0] += record.cost_usd

        # Feed the rolling-hour cost guardrail (alerts if spend exceeds budget).
        try:
            from bharat_terminal.analysis.guardrails import get_monitor
            get_monitor().record_cost(record.cost_usd)
        except Exception:
            pass  # guardrails must never break logging

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

        self._push_redis(record)

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
