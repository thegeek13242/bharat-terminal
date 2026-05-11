"""
Stage 1: Relevance classification.

Two backends, selected by RELEVANCE_BACKEND env (default: local):

  local      — HuggingFace zero-shot classifier
                (MoritzLaurer/deberta-v3-base-zeroshot-v2.0).
                Free, runs on the VM, ~300-500ms/item on 2vCPU.
                Pre-baked into the analysis-worker image at build time.

  anthropic  — Claude Haiku API call.
                ~400-800ms/item, ~$0.02/day at our volume after caching.
                Higher accuracy on edge cases; opt-in via env.

Both share:
  • Same RelevanceResult dict shape (pipeline.py + batch_processor.py
    callers untouched).
  • NSE_FILINGS / BSE_FILINGS short-circuit — always relevant, no
    classifier call.
  • Redis-backed cache keyed by sha256(headline+body[:200]) with
    7-day TTL. Cross-source duplicates and Kafka replays return in ~5ms.
  • Fail-open on any error so a transient hiccup doesn't silently drop
    genuine news.
"""
import os
import time
import logging
import hashlib
import json
from typing import TypedDict

from bharat_terminal.types import NewsItem

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────

RELEVANCE_BACKEND = os.getenv("RELEVANCE_BACKEND", "local").lower()
RELEVANCE_MODEL = os.getenv(
    "RELEVANCE_MODEL", "MoritzLaurer/deberta-v3-base-zeroshot-v2.0"
)
# Score the top-predicted label must exceed for "relevant" to be true.
# Higher = stricter. The classifier returns softmax probabilities over the
# candidate labels, so 0.50 means "must be the plurality winner with >50%
# confidence" — comfortable default given our highly-distinct label set.
RELEVANCE_LOCAL_THRESHOLD = float(os.getenv("RELEVANCE_LOCAL_THRESHOLD", "0.50"))

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
CACHE_TTL_SEC = int(os.getenv("RELEVANCE_CACHE_TTL_SEC", str(7 * 24 * 3600)))

# Anthropic fallback path config
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("RELEVANCE_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
ANTHROPIC_TIMEOUT_SEC = float(os.getenv("RELEVANCE_TIMEOUT_SEC", "8"))


# ── Candidate labels for the zero-shot classifier ────────────────────────
# Order matters: the FIRST label is the "relevant" one. Everything else
# is a noise bucket. The classifier picks whichever has highest entailment
# probability; we mark relevant=True iff the first label wins and clears
# the threshold.
RELEVANT_LABEL = "news about Indian stock markets, NSE BSE listed companies, RBI policy, or commodities"

NOISE_LABELS = [
    "personal finance how-to guide about PPF EPF tax savings or retirement planning",
    "lifestyle entertainment sports horoscope celebrity food or travel article",
    "article using ChatGPT Gemini or AI assistant to give advice",
    "generic explainer listicle 5 ways or what happens if guide",
    "opinion column or human interest story unrelated to listed companies",
]

CANDIDATE_LABELS = [RELEVANT_LABEL] + NOISE_LABELS


class RelevanceResult(TypedDict):
    relevant: bool
    confidence: float
    reason: str
    latency_ms: float


# ── Redis cache (shared by both backends) ────────────────────────────────

_redis_client = None
_redis_probed = False


def _get_redis():
    global _redis_client, _redis_probed
    if _redis_probed:
        return _redis_client
    _redis_probed = True
    try:
        import redis
        client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
        client.ping()
        _redis_client = client
        logger.info(f"[stage1] Redis cache available at {REDIS_URL}")
    except Exception as e:
        logger.warning(f"[stage1] Redis cache unavailable ({e}); proceeding without")
        _redis_client = None
    return _redis_client


def _cache_key(headline: str, body: str) -> str:
    digest = hashlib.sha256(
        (headline + "|" + body[:200]).encode("utf-8")
    ).hexdigest()[:16]
    return f"relevance:cache:{RELEVANCE_BACKEND}:{digest}"


# ── Local backend (zero-shot classifier) ─────────────────────────────────

_zeroshot_pipeline = None


def _get_zeroshot():
    """Lazy-load the zero-shot classifier."""
    global _zeroshot_pipeline
    if _zeroshot_pipeline is None:
        from transformers import pipeline  # type: ignore
        logger.info(f"[stage1] Loading zero-shot classifier: {RELEVANCE_MODEL}")
        # device=-1 forces CPU; explicit so it doesn't try to find a GPU.
        _zeroshot_pipeline = pipeline(
            "zero-shot-classification",
            model=RELEVANCE_MODEL,
            device=-1,
        )
        logger.info("[stage1] Zero-shot classifier ready")
    return _zeroshot_pipeline


def _classify_local(headline: str, body: str) -> tuple[bool, float, str]:
    """
    Run the zero-shot classifier. Returns (relevant, confidence, reason).
    """
    pipe = _get_zeroshot()
    text = f"{headline}. {body}".strip() if body else headline

    # multi_label=False → softmax over labels (probabilities sum to 1)
    result = pipe(text, candidate_labels=CANDIDATE_LABELS, multi_label=False)

    top_label = result["labels"][0]
    top_score = float(result["scores"][0])

    relevant = (top_label == RELEVANT_LABEL) and (top_score >= RELEVANCE_LOCAL_THRESHOLD)

    if relevant:
        reason = f"market label scored {top_score:.2f}"
    elif top_label == RELEVANT_LABEL:
        # Won but below threshold — borderline ambiguous
        reason = f"market label scored {top_score:.2f} < {RELEVANCE_LOCAL_THRESHOLD}"
    else:
        # Lost to a noise label entirely
        # Use short noise tag from the label
        short = top_label.split(" ", 4)[0:3]
        reason = f"top label: {' '.join(short)}… ({top_score:.2f})"

    return relevant, top_score, reason


# ── Anthropic backend (Claude Haiku) ─────────────────────────────────────

_ANTHROPIC_SYSTEM_PROMPT = """You are a strict relevance filter for an Indian equity market intelligence terminal.

Decide whether a news item is meaningful for trading or investing in NSE/BSE listed companies.

RELEVANT (output relevant=true):
- Corporate actions: earnings, M&A, deals, partnerships, capacity, regulatory approvals, management changes, fund-raises, stake sales, IPOs
- Market/sector activity: index moves, FII/DII flows, bulk/block deals, insider trading
- Macro events: Fed/RBI rates, oil/commodity prices, USD/INR, GDP, inflation, fiscal policy
- Regulator/exchange actions: SEBI rules, NSE/BSE circulars, government policy
- Geopolitical events with India-equity transmission

IRRELEVANT (output relevant=false):
- Personal-finance how-to: PPF, EPF, tax saving, calculators, retirement planning
- Lifestyle, entertainment, sports, horoscope, celebrity, food, travel
- AI-prompt articles ("I asked ChatGPT", "Gemini said")
- Listicles and pure explainers: "5 ways to", "What happens if", "Rules explained"
- Human-interest stories unrelated to listed companies

Respond with EXACTLY this JSON, no other text:
{"relevant": true|false, "reason": "<5-12 word justification>"}"""


_anthropic_client = None


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None and ANTHROPIC_API_KEY:
        import anthropic
        _anthropic_client = anthropic.Anthropic(
            api_key=ANTHROPIC_API_KEY,
            timeout=ANTHROPIC_TIMEOUT_SEC,
        )
    return _anthropic_client


def _parse_anthropic_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    if "{" in text and "}" in text:
        text = text[text.index("{") : text.rindex("}") + 1]
    return json.loads(text)


def _classify_anthropic(headline: str, body: str) -> tuple[bool, float, str]:
    client = _get_anthropic()
    if client is None:
        raise RuntimeError("No ANTHROPIC_API_KEY configured")
    user_msg = f"Headline: {headline}\nBody: {body}" if body else f"Headline: {headline}"
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=80,
        temperature=0.0,
        system=[{
            "type": "text",
            "text": _ANTHROPIC_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = resp.content[0].text if resp.content else ""
    parsed = _parse_anthropic_json(raw)
    return bool(parsed.get("relevant", False)), 0.95, str(parsed.get("reason", ""))[:140]


# ── Main entry ───────────────────────────────────────────────────────────


def classify_relevance(news_item: NewsItem) -> RelevanceResult:
    """
    Classify whether `news_item` is market-relevant.

    Flow:
      1. NSE/BSE filings → always relevant (no classifier call).
      2. Redis cache hit → return cached verdict (~5ms).
      3. Run configured backend (local zero-shot OR Anthropic Haiku).
      4. Cache + return.
      5. On any classifier error → fail-open (relevant=True, confidence=0.5).
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

    # ── Backend dispatch ──────────────────────────────────────────────
    try:
        if RELEVANCE_BACKEND == "local":
            relevant, score, reason = _classify_local(headline, body)
        elif RELEVANCE_BACKEND == "anthropic":
            relevant, score, reason = _classify_anthropic(headline, body)
        else:
            raise RuntimeError(
                f"Unknown RELEVANCE_BACKEND={RELEVANCE_BACKEND!r}; "
                "expected 'local' or 'anthropic'"
            )
    except Exception as e:
        logger.error(
            f"[stage1] {RELEVANCE_BACKEND} backend failed for {news_item.id}: {e}"
        )
        return RelevanceResult(
            relevant=True,
            confidence=0.5,
            reason=f"Classifier error, defaulting to relevant: {type(e).__name__}",
            latency_ms=(time.time() - start) * 1000,
        )

    # ── Cache write ───────────────────────────────────────────────────
    if redis_client:
        try:
            redis_client.setex(
                cache_key,
                CACHE_TTL_SEC,
                json.dumps({
                    "relevant": relevant,
                    "confidence": score,
                    "reason": reason,
                }),
            )
        except Exception as e:
            logger.debug(f"[stage1] cache set failed: {e}")

    latency_ms = (time.time() - start) * 1000
    sla = 1500 if RELEVANCE_BACKEND == "anthropic" else 800
    if latency_ms > sla:
        logger.warning(
            f"[stage1] SLA breach: {latency_ms:.0f}ms > {sla}ms "
            f"({RELEVANCE_BACKEND}) for {news_item.id}"
        )

    return RelevanceResult(
        relevant=relevant,
        confidence=score,
        reason=reason or ("market-relevant" if relevant else "off-topic"),
        latency_ms=latency_ms,
    )
