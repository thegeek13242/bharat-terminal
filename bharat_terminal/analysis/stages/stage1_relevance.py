"""
Stage 1: Relevance classification via Claude Haiku.

For each incoming NewsItem we ask Haiku a single yes/no question — "is this
meaningful for trading or investing in NSE/BSE listed companies?" — with a
tightly-scoped prompt that enumerates what counts as market news vs. what
doesn't (personal-finance how-tos, lifestyle, sports, AI-prompt explainers,
listicles, etc.).

The classifier returns a `RelevanceResult` with the same dict shape as the
previous embedding-based version, so callers in `pipeline.py` and
`batch_processor.py` don't need to change.

Design notes
------------
• Sync wrapper around the Anthropic SDK's sync client — both call sites
  (pipeline stage1_node, batch_processor) are sync, and the latency budget
  (~400-800ms per call) fits inside the per-news-item processing window.
• NSE_FILINGS / BSE_FILINGS short-circuit: always relevant, no LLM call.
• Redis cache keyed by sha256(headline + body[:200]) with 7-day TTL — the
  same headline appearing on multiple sources (cross-posting) only costs
  one LLM call. Cache hits return in ~5ms.
• Fail-open on Anthropic errors (401, 429, timeout) — better to surface a
  potentially-irrelevant item than silently drop genuine news during an
  API hiccup.
• Cost: ~150 input + 30 output tokens per call. At Haiku 4.5 list price
  that's a fraction of a cent per call — ~$0.02/day at typical ingestion
  rates after dedup, with the Redis cache reducing it further.
• Prompt caching: the system prompt is marked `cache_control=ephemeral`
  so Anthropic caches its 1500-token expansion across calls within a
  ~5-minute window, dropping per-call input cost ~10x.
"""
import os
import time
import logging
import hashlib
import json
from typing import TypedDict

import anthropic

from bharat_terminal.types import NewsItem

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
RELEVANCE_MODEL = os.getenv("RELEVANCE_MODEL", "claude-haiku-4-5-20251001")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
CACHE_TTL_SEC = int(os.getenv("RELEVANCE_CACHE_TTL_SEC", str(7 * 24 * 3600)))
ANTHROPIC_TIMEOUT_SEC = float(os.getenv("RELEVANCE_TIMEOUT_SEC", "8"))


# Tight system prompt with explicit YES/NO criteria. We mark it
# cache_control=ephemeral so Anthropic prompt-caches its tokenisation,
# dropping per-call input cost roughly 10x for back-to-back calls.
_SYSTEM_PROMPT = """You are a strict relevance filter for an Indian equity market intelligence terminal.

Decide whether a news item is meaningful for trading or investing in NSE/BSE listed companies.

RELEVANT (output relevant=true):
- Corporate actions on listed firms: earnings, results, guidance, M&A,
  partnerships, deals/contracts, capacity expansion, regulatory approvals,
  management changes, fund-raises, stake sales, buybacks, IPOs/FPOs, OFS
- Market or sector activity: index moves, sector rotation, FII/DII flows,
  bulk/block deals, insider trading, promoter pledge changes
- Macro events that move Indian markets: Fed/RBI rate decisions,
  oil/commodity prices, USD/INR, GDP, CPI/WPI inflation, fiscal policy,
  trade balance, current account, foreign reserves
- Regulator/exchange actions: SEBI rules and enforcement, NSE/BSE
  circulars, government policy changes (taxation, FDI, sector reforms)
- Geopolitical events with credible India-equity transmission channel
  (oil supply, China decoupling, sanctions, war)

IRRELEVANT (output relevant=false):
- Personal-finance how-to guides: PPF, EPF, NPS, SIP, ULIP, tax-saving,
  credit card tips, EMI calculators, retirement planning, education fund
- Lifestyle, entertainment, sports, food, travel, horoscope, celebrity,
  fashion, gadget reviews, opinion columns unrelated to listed firms
- AI-prompt articles ("I asked ChatGPT", "Gemini predicted", "ChatGPT says")
- Listicles and pure explainers without a specific market event:
  "5 ways to save tax", "Top 10 stocks for 2030", "What happens if you...",
  "Rules explained", "Things to know before"
- Generic education content, glossary entries, definitions, FAQs
- Human-interest stories, profiles, anniversary pieces

Respond with EXACTLY this JSON, no other text:
{"relevant": true|false, "reason": "<5-12 word justification>"}"""


class RelevanceResult(TypedDict):
    relevant: bool
    confidence: float
    reason: str
    latency_ms: float


# ── Lazy singletons ──────────────────────────────────────────────────────

_redis_client = None
_redis_probed = False


def _get_redis():
    """Return a connected sync Redis client or None if unavailable."""
    global _redis_client, _redis_probed
    if _redis_probed:
        return _redis_client
    _redis_probed = True
    try:
        import redis  # local import — keeps module load cheap if redis missing
        client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
        client.ping()
        _redis_client = client
        logger.info(f"[stage1] Redis cache available at {REDIS_URL}")
    except Exception as e:
        logger.warning(f"[stage1] Redis cache unavailable ({e}); proceeding without")
        _redis_client = None
    return _redis_client


_anthropic_client = None


def _get_anthropic():
    """Return a sync Anthropic client or None if no API key configured."""
    global _anthropic_client
    if _anthropic_client is None and ANTHROPIC_API_KEY:
        _anthropic_client = anthropic.Anthropic(
            api_key=ANTHROPIC_API_KEY,
            timeout=ANTHROPIC_TIMEOUT_SEC,
        )
    return _anthropic_client


def _cache_key(headline: str, body: str) -> str:
    digest = hashlib.sha256(
        (headline + "|" + body[:200]).encode("utf-8")
    ).hexdigest()[:16]
    return f"relevance:cache:{digest}"


def _parse_llm_response(raw: str) -> dict:
    """Parse {'relevant': bool, 'reason': str} from Haiku's reply."""
    text = raw.strip()
    # Strip markdown fences if Haiku decided to be helpful
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    # Trim anything before the first '{' or after the last '}'
    if "{" in text and "}" in text:
        text = text[text.index("{") : text.rindex("}") + 1]
    parsed = json.loads(text)
    return {
        "relevant": bool(parsed.get("relevant", False)),
        "reason": str(parsed.get("reason", ""))[:140],
    }


# ── Main entry ───────────────────────────────────────────────────────────


def classify_relevance(news_item: NewsItem) -> RelevanceResult:
    """
    Decide whether `news_item` is market-relevant. Returns a RelevanceResult.

    Behaviour:
      1. NSE/BSE filings → always relevant (no LLM call).
      2. Redis cache hit → return cached verdict (~5ms).
      3. Haiku call with strict YES/NO JSON output.
      4. On any error → fail-open (relevant=True, confidence=0.5).
    """
    start = time.time()

    # ── Short-circuit: exchange filings are always relevant ───────────
    if news_item.source in ("NSE_FILINGS", "BSE_FILINGS"):
        return RelevanceResult(
            relevant=True,
            confidence=1.0,
            reason=f"Corporate filing from {news_item.source} — always relevant",
            latency_ms=(time.time() - start) * 1000,
        )

    headline = (news_item.headline or "").strip()
    body = (news_item.body or "")[:200].strip()

    if not headline:
        # No headline → can't classify → drop. Better than passing junk.
        return RelevanceResult(
            relevant=False,
            confidence=1.0,
            reason="empty headline",
            latency_ms=(time.time() - start) * 1000,
        )

    # ── Cache lookup ──────────────────────────────────────────────────
    redis_client = _get_redis()
    cache_key = _cache_key(headline, body)
    if redis_client:
        try:
            cached = redis_client.get(cache_key)
            if cached is not None:
                payload = json.loads(cached)
                return RelevanceResult(
                    relevant=bool(payload["relevant"]),
                    confidence=float(payload.get("confidence", 0.9)),
                    reason=f"[cache] {payload.get('reason', '')}",
                    latency_ms=(time.time() - start) * 1000,
                )
        except Exception as e:
            logger.debug(f"[stage1] cache get failed: {e}")

    # ── LLM call ──────────────────────────────────────────────────────
    client = _get_anthropic()
    if client is None:
        # No key configured: fail-open. Logged once at module load via warning.
        return RelevanceResult(
            relevant=True,
            confidence=0.5,
            reason="No ANTHROPIC_API_KEY — defaulting to relevant",
            latency_ms=(time.time() - start) * 1000,
        )

    user_msg = (
        f"Headline: {headline}\nBody: {body}" if body
        else f"Headline: {headline}"
    )

    try:
        resp = client.messages.create(
            model=RELEVANCE_MODEL,
            max_tokens=80,
            temperature=0.0,
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text if resp.content else ""
        parsed = _parse_llm_response(raw)
        relevant = parsed["relevant"]
        reason = parsed["reason"]

        # Persist to cache. Confidence is fixed at 0.95 — the LLM doesn't
        # give us a probability and the prompt is deliberately strict.
        if redis_client:
            try:
                redis_client.setex(
                    cache_key,
                    CACHE_TTL_SEC,
                    json.dumps({
                        "relevant": relevant,
                        "confidence": 0.95,
                        "reason": reason,
                    }),
                )
            except Exception as e:
                logger.debug(f"[stage1] cache set failed: {e}")

        latency_ms = (time.time() - start) * 1000
        if latency_ms > 1500:
            logger.warning(
                f"[stage1] SLA breach: {latency_ms:.0f}ms > 1500ms for {news_item.id}"
            )

        return RelevanceResult(
            relevant=relevant,
            confidence=0.95,
            reason=reason or ("market-relevant" if relevant else "off-topic"),
            latency_ms=latency_ms,
        )

    except json.JSONDecodeError as e:
        logger.error(
            f"[stage1] Haiku returned non-JSON for {news_item.id}: {e}; raw: {raw[:120]!r}"
        )
        # Malformed response — fail-open
        return RelevanceResult(
            relevant=True,
            confidence=0.5,
            reason="Classifier malformed output, defaulting to relevant",
            latency_ms=(time.time() - start) * 1000,
        )
    except Exception as e:
        logger.error(f"[stage1] Haiku call failed for {news_item.id}: {e}")
        # Network / auth / quota error — fail-open
        return RelevanceResult(
            relevant=True,
            confidence=0.5,
            reason=f"Classifier error, defaulting to relevant: {type(e).__name__}",
            latency_ms=(time.time() - start) * 1000,
        )
