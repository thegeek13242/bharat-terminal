"""On-demand live NSE/BSE *equity* LTP via Angel One — for the priced-in check.

We deliberately use the EQUITY cash price (not derivatives): the priced-in
judgment only needs the stock's current price + its recent multi-day movement.

Single-session safe: Angel One is single-session-per-client and the live session
already lives in the ingestion service (streaming commodities). Rather than log
in a second time (which would invalidate that stream), the ingestion adapter
publishes its current session token to Redis (`angelone:session`), and here we
REUSE that token for stateless REST `getMarketData` calls — no new login.

Everything is fail-open: missing token, expired session, unknown symbol, or any
API error returns nothing and the caller falls back to daily `price_points`.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Dict, List

import httpx

logger = logging.getLogger(__name__)

REDIS_SESSION_KEY = "angelone:session"
# Public scrip master (no auth) — maps tradingsymbol/token across all segments.
SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)
_SCRIP_REFRESH_S = 12 * 3600

# symbol -> equity token, built lazily from the scrip master. Two maps: NSE
# is resolved first so dual-listed symbols keep their existing NSE pricing;
# the BSE map only ever serves BSE-only universe symbols (companies.symbol ==
# BSE scrip_id == Angel One's BSE `symbol` field, so name lookup resolves).
_equity_tokens: Dict[str, str] = {}
_bse_equity_tokens: Dict[str, str] = {}
_scrip_loaded_at: float = 0.0


async def _ensure_equity_tokens() -> None:
    """Lazily fetch + cache the NSE/BSE equity (symbol -> token) maps. Fail-open."""
    global _equity_tokens, _bse_equity_tokens, _scrip_loaded_at
    if _equity_tokens and (time.time() - _scrip_loaded_at) < _SCRIP_REFRESH_S:
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(SCRIP_MASTER_URL)
        rows = resp.json()
        tokens: Dict[str, str] = {}
        bse_tokens: Dict[str, str] = {}
        for r in rows:
            seg = r.get("exch_seg")
            sym = (r.get("symbol") or "")
            if seg == "NSE":
                # NSE cash equities are listed as "<NAME>-EQ".
                if sym.endswith("-EQ"):
                    tokens[sym[:-3].upper()] = str(r.get("token") or "")
            elif seg == "BSE":
                # BSE cash equities have instrumenttype "" (indices are AMXIDX);
                # token == the 6-digit BSE scrip code.
                if sym and not (r.get("instrumenttype") or ""):
                    bse_tokens[sym.upper()] = str(r.get("token") or "")
        if tokens:
            _equity_tokens = tokens
            _bse_equity_tokens = bse_tokens
            _scrip_loaded_at = time.time()
            logger.info(
                f"[live_price] equity token map loaded "
                f"(NSE: {len(tokens)}, BSE: {len(bse_tokens)} symbols)"
            )
    except Exception as e:
        logger.warning(f"[live_price] scrip-master load failed (fail-open): {e}")


def _smartconnect_from_redis(session_json: str):
    """Build a token-reusing SmartConnect (no generateSession) from the
    ingestion-published session. Returns None if the SDK/session is unavailable."""
    try:
        from SmartApi import SmartConnect
    except Exception:
        return None
    try:
        sess = json.loads(session_json)
        sc = SmartConnect(api_key=sess.get("api_key"))
        # generateSession returns the jwt already prefixed with "Bearer "; the SDK
        # re-adds "Bearer " in _request, so strip it here to avoid "Bearer Bearer …"
        # (which Angel One rejects with AG8001 Invalid Token).
        tok = (sess.get("access_token") or "")
        if tok.startswith("Bearer "):
            tok = tok[7:]
        sc.setAccessToken(tok)
        ft = sess.get("feed_token")
        if ft and hasattr(sc, "setFeedToken"):
            sc.setFeedToken(ft)
        return sc
    except Exception as e:
        logger.warning(f"[live_price] could not build SmartConnect from session (fail-open): {e}")
        return None


async def get_current_ltps(symbols: List[str], redis_client) -> Dict[str, Dict[str, float]]:
    """Return {symbol: {"ltp": float, "prev_close": float}} for NSE/BSE equities,
    using the shared Angel One session. NSE resolves first (dual-listed symbols
    keep NSE pricing); BSE covers the BSE-only universe. Fail-open → {} on any
    problem."""
    if not symbols or redis_client is None:
        return {}
    try:
        session_json = await redis_client.get(REDIS_SESSION_KEY)
    except Exception:
        session_json = None
    if not session_json:
        return {}  # ingestion hasn't published a live session — daily fallback

    await _ensure_equity_tokens()
    # NSE and BSE token spaces are not guaranteed disjoint — key the reverse
    # map by (exchange, token); getMarketData rows carry an `exchange` field.
    token_to_symbol: Dict[tuple, str] = {}
    exchange_tokens: Dict[str, List[str]] = {}
    for s in symbols:
        sym = (s or "").upper()
        tok = _equity_tokens.get(sym)
        exch = "NSE"
        if not tok:
            tok = _bse_equity_tokens.get(sym)
            exch = "BSE"
        if tok:
            exchange_tokens.setdefault(exch, []).append(tok)
            token_to_symbol[(exch, tok)] = sym
    if not exchange_tokens:
        return {}

    sc = _smartconnect_from_redis(session_json)
    if sc is None:
        return {}

    try:
        import asyncio
        result = await asyncio.to_thread(sc.getMarketData, "OHLC", exchange_tokens)
    except Exception as e:
        logger.warning(f"[live_price] getMarketData failed (fail-open): {e}")
        return {}

    out: Dict[str, Dict[str, float]] = {}
    try:
        fetched = ((result or {}).get("data") or {}).get("fetched") or []
        for row in fetched:
            tok = str(row.get("symbolToken") or "")
            exch = str(row.get("exchange") or "NSE")
            sym = token_to_symbol.get((exch, tok))
            ltp = row.get("ltp")
            prev_close = row.get("close")  # Angel One OHLC 'close' = previous close
            day_open = row.get("open")     # today's open = pre-open auction price
            if sym and ltp is not None:
                out[sym] = {"ltp": float(ltp),
                            "prev_close": float(prev_close) if prev_close is not None else None,
                            "open": float(day_open) if day_open else None}
    except Exception as e:
        logger.warning(f"[live_price] parse failed (fail-open): {e}")
        return {}
    return out


async def get_full_quotes(symbols: List[str], redis_client) -> Dict[str, Dict]:
    """FULL-mode quotes: ltp + total buy/sell quantities + best-5 depth.

    Built for the pre-open demand probe (papertrade, 09:05-09:07 IST): during
    the auction collection window the exchange disseminates the forming order
    book, and Angel One's FULL quote carries the aggregate totBuyQuan /
    totSellQuan and depth for it. Also valid during continuous trading (then
    it is simply the live book). Fail-open → {}.
    """
    if not symbols or redis_client is None:
        return {}
    try:
        session_json = await redis_client.get(REDIS_SESSION_KEY)
    except Exception:
        session_json = None
    if not session_json:
        return {}

    await _ensure_equity_tokens()
    token_to_symbol: Dict[tuple, str] = {}
    exchange_tokens: Dict[str, List[str]] = {}
    for s in symbols:
        sym = (s or "").upper()
        tok = _equity_tokens.get(sym)
        exch = "NSE"
        if not tok:
            tok = _bse_equity_tokens.get(sym)
            exch = "BSE"
        if tok:
            exchange_tokens.setdefault(exch, []).append(tok)
            token_to_symbol[(exch, tok)] = sym
    if not exchange_tokens:
        return {}
    sc = _smartconnect_from_redis(session_json)
    if sc is None:
        return {}
    try:
        import asyncio
        result = await asyncio.to_thread(sc.getMarketData, "FULL", exchange_tokens)
    except Exception as e:
        logger.warning(f"[live_price] FULL getMarketData failed (fail-open): {e}")
        return {}

    out: Dict[str, Dict] = {}
    try:
        fetched = ((result or {}).get("data") or {}).get("fetched") or []
        for row in fetched:
            sym = token_to_symbol.get(
                (str(row.get("exchange") or "NSE"),
                 str(row.get("symbolToken") or "")))
            if not sym:
                continue
            depth = row.get("depth") or {}
            out[sym] = {
                "ltp": float(row["ltp"]) if row.get("ltp") else None,
                "open": float(row["open"]) if row.get("open") else None,
                "prev_close": (float(row["close"])
                               if row.get("close") else None),
                "tot_buy_qty": (float(row["totBuyQuan"])
                                if row.get("totBuyQuan") is not None else None),
                "tot_sell_qty": (float(row["totSellQuan"])
                                 if row.get("totSellQuan") is not None else None),
                "best_buy": (depth.get("buy") or [{}])[0],
                "best_sell": (depth.get("sell") or [{}])[0],
            }
    except Exception as e:
        logger.warning(f"[live_price] FULL parse failed (fail-open): {e}")
        return {}
    return out
