"""Recent price-move lookup for the 'priced-in' check (Feature 1).

Answers one question for a symbol: "how much has it moved recently, and in
which direction?" — so the impact stage can downgrade news whose move the
market has already made.

Data path (the analysis service is DB-less by design — it reaches data through
the KB REST service, never asyncpg):
  GET /company/{symbol}/price-returns → the KB endpoint returns `recent_move_pct`,
  which combines the LIVE NSE-equity price (Angel One cash LTP, reusing the
  ingestion session via Redis — see kb/live_price.py) with the past few days'
  movement from daily `price_points`. When no live session is published it falls
  back to the shortest daily trailing return. Any error / no data → None
  (caller applies NO downgrade — fail-open).

We use the EQUITY cash price on purpose: the priced-in judgment needs the stock's
own price, not a derivatives feed.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

KNOWLEDGE_BASE_URL = os.getenv("KNOWLEDGE_BASE_URL", "http://kb-service:8001")
# Shortest-first: a 1-day move is the most recent signal of "already moved".
_MOVE_WINDOWS = (1, 3, 5)
_HTTP_TIMEOUT_S = float(os.getenv("PRICED_IN_HTTP_TIMEOUT_S", "2.0"))

# symbol -> (recent_move_pct_or_None, cached_at_epoch). Mirrors signal_gates._resolve_cache.
_move_cache: Dict[str, Tuple[Optional[float], float]] = {}
_MOVE_CACHE_TTL = float(os.getenv("PRICED_IN_CACHE_TTL_S", "60"))


async def get_recent_move(symbol: str) -> Optional[float]:
    """Return the symbol's most-recent trailing return in PERCENT (signed), or
    None if unknown. Fail-open: any HTTP/parse error returns None so the pipeline
    is never blocked by a price lookup.
    """
    sym = (symbol or "").upper().strip()
    if not sym:
        return None

    now = time.time()
    hit = _move_cache.get(sym)
    if hit and now - hit[1] < _MOVE_CACHE_TTL:
        return hit[0]

    move = await _fetch_recent_move(sym)
    _move_cache[sym] = (move, now)
    return move


async def _fetch_recent_move(sym: str) -> Optional[float]:
    url = f"{KNOWLEDGE_BASE_URL}/company/{sym}/price-returns"
    params = {"windows": ",".join(str(w) for w in _MOVE_WINDOWS)}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception as e:  # network/timeout/JSON — fail-open
        logger.debug(f"[priced_in] recent-move lookup failed for {sym}: {e}")
        return None

    if not isinstance(data, dict) or data.get("error"):
        return None
    # The KB endpoint computes `recent_move_pct` = live current price vs the
    # close ~3 trading days ago (combining the current price with the past few
    # days' movement); it falls back to the shortest daily trailing return when
    # no live price is available. Prefer it; else read trailing returns here.
    rm = data.get("recent_move_pct")
    if rm is not None:
        try:
            return float(rm)
        except (TypeError, ValueError):
            pass
    trailing = data.get("trailing_returns_pct") or {}
    for w in _MOVE_WINDOWS:
        val = trailing.get(f"{w}d")
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None
