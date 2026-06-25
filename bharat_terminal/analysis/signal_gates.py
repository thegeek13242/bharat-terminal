"""
Signal quality gates — the noise filter between "the LLM said long" and
"we publish a trade signal to professionals".

Three gates, applied in stage 5 (so both the real-time path and any batch
escalation pass through them):

1. Derivative-headline filter: market recaps, gainers/losers lists, third-party
   stock recommendations and multibagger stories are derivative content, not
   tradeable events. No signal should ever be generated from them.
2. Symbol resolution: a signal must carry a real, tradeable NSE symbol from our
   companies table (resolved via kb-service, which also maps company names to
   canonical symbols). Unresolvable symbols (US tickers, free-text names) are
   dropped — a signal that can't be routed to an order is not a signal.
3. Dedup: one (symbol, direction) signal per window (default 6h) across all
   paths, via Redis SETNX. Repeated coverage of the same move must not emit
   repeated signals.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

KB_SERVICE_URL = os.getenv("KB_SERVICE_URL", "http://kb-service:8001")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
# 48h: follow-up coverage of the same event (block deals, demergers) keeps
# re-surfacing across outlets for 1-2 days — 6h let the same event re-signal.
SIGNAL_DEDUP_TTL_SEC = int(os.getenv("SIGNAL_DEDUP_TTL_SEC", str(48 * 3600)))
# An article older than this at processing time is history, not news — never
# signal from it (covers slow feeds and re-published syndicated copies).
MAX_ARTICLE_AGE_H = float(os.getenv("SIGNAL_MAX_ARTICLE_AGE_H", "12"))

# ── Gate 1: derivative / recap content ───────────────────────────────────

# Headlines that *describe* market action or recycle third-party calls.
_DERIVATIVE_PATTERNS = re.compile(
    r"top gainers|top losers|gainers\s*&(amp;)?\s*losers|"
    r"stocks? to (buy|watch|sell)|stock recommendations?|trading guide|"
    r"buy or sell|brokerage|target price|"
    r"multibagger|penny stock|"
    r"52[- ]week (high|low)s?|hit.*record high|"
    r"sensex (today|ends|falls|rises|closes)|nifty (above|below|ends|closes)|"
    r"market (live|wrap|close|closing bell|opening bell)|"
    r"stocks? in (focus|news|action)|shares? in focus|hot stocks|"
    r"share price live|live updates|"
    r"what lies ahead|what (are |do )?experts|experts? (say|view)|"
    r"rally up to|rall(y|ies|ied) \d+%|surged? \d+%|"
    r"(jump|plunge|soar|crash|tumble)\w* (nearly |over |about )?\d+%|"
    r"(rise|rose|fell|gain|drop)s? (up to )?\d+(\.\d+)?% (today|this week)|"
    r"zoomed|skyrocket",
    re.IGNORECASE,
)


def is_derivative_headline(headline: str) -> bool:
    """True if the headline is market-recap / recommendation content."""
    return bool(_DERIVATIVE_PATTERNS.search(headline or ""))


def is_stale_article(news_item) -> bool:
    """True if the article's publication time is too old to trade on.

    Uses timestamp_utc (publication). Future timestamps (legacy IST-as-UTC
    rows) are treated as fresh — staleness only ever blocks, never un-blocks.
    """
    from datetime import datetime, timezone
    ts = getattr(news_item, "timestamp_utc", None)
    if ts is None:
        return False
    age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    return age_h > MAX_ARTICLE_AGE_H


# ── Gate 2: symbol resolution via kb-service ─────────────────────────────

# (symbol_upper) -> (canonical_symbol_or_None, cached_at_epoch)
_resolve_cache: Dict[str, Tuple[Optional[str], float]] = {}
_RESOLVE_CACHE_TTL = 6 * 3600


def resolve_symbol(symbol: str) -> Optional[str]:
    """Resolve a raw LLM-emitted symbol/name to a canonical NSE symbol.

    Uses kb-service /company/{symbol} (exact-symbol lookup with a company-name
    fallback), so e.g. 'ALLIED BLENDERS' maps to its listed symbol while 'ORCL'
    (not listed on NSE) returns None. Fail-closed: if the KB is unreachable we
    return None — better to drop a signal than publish an unroutable one.
    """
    sym = (symbol or "").upper().strip()
    if not sym:
        return None
    now = time.time()
    hit = _resolve_cache.get(sym)
    if hit and now - hit[1] < _RESOLVE_CACHE_TTL:
        return hit[0]

    canonical: Optional[str] = None
    try:
        import httpx
        r = httpx.get(f"{KB_SERVICE_URL}/company/{sym}", timeout=5)
        if r.status_code == 200:
            data = r.json()
            # KB nests the canonical symbol under "identity".
            raw = (data.get("identity") or {}).get("symbol") or data.get("symbol") or ""
            canonical = raw.upper().strip() or None
    except Exception as e:
        logger.warning(f"[gates] symbol resolution unavailable for {sym}: {e}")
        return None  # fail closed

    _resolve_cache[sym] = (canonical, now)
    return canonical


# ── Gate 3: dedup via Redis ──────────────────────────────────────────────

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
    except Exception as e:
        logger.warning(f"[gates] Redis unavailable ({e}); dedup disabled")
        _redis_client = None
    return _redis_client


def dedup_passes(symbol: str, direction: str) -> bool:
    """True if no (symbol, direction) signal was emitted in the dedup window.
    Marks the pair as emitted on success. Fail-open if Redis is down (a dup is
    less harmful than silently suppressing all signals)."""
    client = _get_redis()
    if client is None:
        return True
    try:
        key = f"signal:emitted:{symbol}:{direction}"
        return bool(client.set(key, "1", nx=True, ex=SIGNAL_DEDUP_TTL_SEC))
    except Exception as e:
        logger.warning(f"[gates] dedup check failed ({e}); allowing")
        return True


# ── Combined post-LLM filter ─────────────────────────────────────────────

def filter_signals(signals: List, headline: str) -> List:
    """Apply symbol-resolution + dedup gates to LLM-generated TradeSignals.

    Canonicalizes each signal's symbol in place. Drops: neutral/avoid signals,
    unresolvable symbols, and dups. (The derivative-headline gate should be
    applied BEFORE the LLM call — see stage5 — so it never reaches here.)
    """
    kept = []
    for sig in signals:
        if sig.direction not in ("long", "short"):
            continue
        if getattr(sig, "instrument_type", "equity") == "avoid":
            continue
        canonical = resolve_symbol(sig.symbol)
        if not canonical:
            logger.info(f"[gates] dropped signal: unresolvable symbol {sig.symbol!r} "
                        f"(headline: {headline[:60]!r})")
            continue
        sig.symbol = canonical
        if not dedup_passes(canonical, sig.direction):
            logger.info(f"[gates] dropped signal: dup {canonical}/{sig.direction} within window")
            continue
        kept.append(sig)
    return kept
