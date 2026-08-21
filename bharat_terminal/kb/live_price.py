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

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional

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
# NSE cash-equity series we will price, best-market first. EQ = rolling
# settlement, BE/BZ = trade-for-trade (where ESM/GSM surveillance names live),
# SM/ST = SME board. Anything else on exch_seg NSE is not a cash equity.
_NSE_CASH_SERIES = ("EQ", "BE", "BZ", "SM", "ST")
_NSE_SERIES_RANK = {s: i for i, s in enumerate(_NSE_CASH_SERIES)}
_scrip_loaded_at: float = 0.0

# 2026-08-21 outage (second cold start after an EC2 freeze): the 35MB scrip
# master came off Angel One's CDN at 4-11 KB/s with truncated payloads, the
# old 15s timeout failed every attempt, both token maps stayed EMPTY, /ltp
# returned nothing, and papertrade was blind for a whole session (21 open
# positions unmarked, exits blocked). Three defences now:
#   1. the CDN refresh runs in a BACKGROUND task — a request never waits on it;
#   2. successful maps persist in Redis (7d) and seed the next cold start;
#   3. unresolved symbols fall back to SmartAPI searchScrip one at a time
#      (memoized; 1h negative cache) — papertrade tracks ~30 symbols, so this
#      alone restores marks within one MTM cycle.
_TOKEN_CACHE_KEY = "live_price:equity_tokens:v1"
_TOKEN_CACHE_TTL_S = 7 * 24 * 3600
_SCRIP_TIMEOUT = httpx.Timeout(60.0, connect=15.0)   # per-read, not total
_SEARCH_NEG_TTL_S = 3600
_refresh_task: Optional[asyncio.Task] = None
_search_neg: Dict[str, float] = {}


def _parse_scrip_master(rows) -> tuple[Dict[str, str], Dict[str, str]]:
    """Pure: scrip-master rows -> (NSE base->token, BSE symbol->token)."""
    tokens: Dict[str, str] = {}
    bse_tokens: Dict[str, str] = {}
    nse_series: Dict[str, str] = {}
    for r in rows:
        seg = r.get("exch_seg")
        sym = (r.get("symbol") or "")
        if seg == "NSE":
            # NSE cash equities are "<NAME>-<SERIES>". Indexing ONLY "-EQ"
            # (the old behaviour) silently lost every surveillance and SME
            # series — 291 -BE, 414 -SM, 145 -ST names — which then fell
            # through to the BSE map and got priced off the WRONG exchange.
            # Measured 2026-07-31: GLFL (GLFL-BE) quoted 6.20 vs NSE's 5.87
            # open, OILCOUNTUB (OILCOUNTUB-BE) 53.34 vs 56.40 — both were
            # BSE circuit-band prints, up to 5% wrong, feeding live paper
            # marks and fills. Non-equity NSE series (SG/GS/N0/N1/MF/TB =
            # gilts, bonds, MFs, T-bills) stay excluded by the whitelist.
            if "-" in sym:
                base, series = sym.rsplit("-", 1)
                rank = _NSE_SERIES_RANK.get(series.upper())
                if rank is not None and base:
                    base_u = base.upper()
                    held = nse_series.get(base_u)
                    # EQ outranks BE outranks SME: a name listed in two
                    # series keeps its primary market.
                    if held is None or rank < _NSE_SERIES_RANK[held]:
                        tokens[base_u] = str(r.get("token") or "")
                        nse_series[base_u] = series.upper()
        elif seg == "BSE":
            # BSE cash equities have instrumenttype "" (indices are AMXIDX);
            # token == the 6-digit BSE scrip code.
            if sym and not (r.get("instrumenttype") or ""):
                bse_tokens[sym.upper()] = str(r.get("token") or "")
    return tokens, bse_tokens


async def _refresh_from_cdn(redis_client) -> None:
    """Background: download + parse the scrip master; on success swap the
    maps in and persist them to Redis. Fail-open with a typed log line."""
    global _equity_tokens, _bse_equity_tokens, _scrip_loaded_at
    try:
        async with httpx.AsyncClient(timeout=_SCRIP_TIMEOUT) as client:
            resp = await client.get(SCRIP_MASTER_URL)
        tokens, bse_tokens = _parse_scrip_master(resp.json())
        if not tokens:
            raise RuntimeError("scrip master parsed but yielded 0 NSE tokens")
        _equity_tokens, _bse_equity_tokens = tokens, bse_tokens
        _scrip_loaded_at = time.time()
        logger.info(f"[live_price] equity token map loaded from CDN "
                    f"(NSE: {len(tokens)}, BSE: {len(bse_tokens)} symbols)")
        if redis_client is not None:
            try:
                await redis_client.set(
                    _TOKEN_CACHE_KEY,
                    json.dumps({"nse": tokens, "bse": bse_tokens,
                                "ts": time.time()}),
                    ex=_TOKEN_CACHE_TTL_S)
            except Exception as e:
                logger.warning(f"[live_price] token-map cache write failed: {e}")
    except Exception as e:
        # repr(): asyncio/httpx timeouts str() to '' and the old log line
        # read "scrip-master load failed (fail-open): " with no cause.
        logger.warning(f"[live_price] scrip-master refresh failed (fail-open): "
                       f"{type(e).__name__}: {e!r}")


async def _load_cached_tokens(redis_client) -> bool:
    """Seed the maps from the Redis cache (a previous successful CDN load)."""
    global _equity_tokens, _bse_equity_tokens, _scrip_loaded_at
    if redis_client is None:
        return False
    try:
        raw = await redis_client.get(_TOKEN_CACHE_KEY)
    except Exception as e:
        logger.warning(f"[live_price] token-map cache read failed: {e}")
        return False
    if not raw:
        return False
    try:
        d = json.loads(raw)
        _equity_tokens, _bse_equity_tokens = d["nse"], d["bse"]
        # Count as loaded, but schedule a CDN retry in 15 min rather than 12h.
        _scrip_loaded_at = time.time() - _SCRIP_REFRESH_S + 900
        logger.warning(f"[live_price] using CACHED token map from Redis "
                       f"(age {(time.time() - d.get('ts', 0)) / 3600:.1f}h, "
                       f"NSE {len(_equity_tokens)}, BSE {len(_bse_equity_tokens)})"
                       f" — CDN refresh retried in the background")
        return True
    except Exception as e:
        logger.warning(f"[live_price] token-map cache unusable: {e}")
        return False


async def _ensure_equity_tokens(redis_client=None) -> None:
    """Keep the NSE/BSE (symbol -> token) maps warm WITHOUT ever blocking a
    request on the 35MB CDN download. Order: fresh maps -> Redis cache ->
    kick a background CDN refresh (at most one in flight)."""
    global _refresh_task, _scrip_loaded_at
    fresh = _equity_tokens and (time.time() - _scrip_loaded_at) < _SCRIP_REFRESH_S
    if fresh:
        return
    if not _equity_tokens:
        await _load_cached_tokens(redis_client)
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = asyncio.create_task(_refresh_from_cdn(redis_client))
        # A refresh attempt counts as "recently tried" so a dead CDN doesn't
        # spawn a new download on every request; the task swaps maps on success.
        if _equity_tokens:
            _scrip_loaded_at = time.time() - _SCRIP_REFRESH_S + 900


def _pick_search_match(rows, symbol: str, exch: str) -> Optional[str]:
    """Pure: best symboltoken from SmartAPI searchScrip rows. NSE rows are
    '<BASE>-<SERIES>' — exact base match, series ranked EQ>BE>BZ>SM>ST.
    BSE rows match on tradingsymbol == symbol (scrip id)."""
    sym_u = symbol.upper()
    best, best_rank = None, 99
    for r in rows or []:
        ts = str(r.get("tradingsymbol") or "").upper()
        tok = str(r.get("symboltoken") or "")
        if not tok:
            continue
        if exch == "NSE":
            if "-" not in ts:
                continue
            base, series = ts.rsplit("-", 1)
            rank = _NSE_SERIES_RANK.get(series)
            if base == sym_u and rank is not None and rank < best_rank:
                best, best_rank = tok, rank
        elif exch == "BSE" and ts == sym_u:
            return tok
    return best


def _resolve_via_search(sc, symbol: str) -> Optional[tuple]:
    """Sync (run in a thread): (exchange, token) via SmartAPI searchScrip,
    NSE first then BSE. Uses the SDK's raw request so we get the rows, not
    the SDK's pretty-printed summary string."""
    for exch in ("NSE", "BSE"):
        try:
            res = sc._postRequest("api.search.scrip",
                                  {"exchange": exch, "searchscrip": symbol})
        except Exception:
            continue
        tok = _pick_search_match((res or {}).get("data") or [], symbol, exch)
        if tok:
            return exch, tok
    return None


async def _resolve_unmapped(sc, symbols: List[str]) -> None:
    """Fill in-memory maps for symbols the scrip master didn't cover (or
    when the maps are empty), one searchScrip call per symbol, memoized
    with a 1h negative cache so a bad symbol can't hammer the API."""
    now = time.time()
    for s in symbols:
        sym = (s or "").upper()
        if not sym or sym in _equity_tokens or sym in _bse_equity_tokens:
            continue
        if now - _search_neg.get(sym, 0) < _SEARCH_NEG_TTL_S:
            continue
        try:
            hit = await asyncio.to_thread(_resolve_via_search, sc, sym)
        except Exception as e:
            logger.debug(f"[live_price] searchScrip failed for {sym}: {e}")
            hit = None
        if hit:
            exch, tok = hit
            (_equity_tokens if exch == "NSE" else _bse_equity_tokens)[sym] = tok
            logger.info(f"[live_price] {sym}: token resolved via searchScrip ({exch})")
        else:
            _search_neg[sym] = now


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

    await _ensure_equity_tokens(redis_client)
    sc = _smartconnect_from_redis(session_json)
    if sc is None:
        return {}
    await _resolve_unmapped(sc, symbols)
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

    try:
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
                            "open": float(day_open) if day_open else None,
                            # Which exchange this print came from. Stamped so a
                            # wrong-segment quote is visible downstream instead
                            # of silently becoming a fill price (2026-07-31).
                            "exchange": exch}
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

    await _ensure_equity_tokens(redis_client)
    sc = _smartconnect_from_redis(session_json)
    if sc is None:
        return {}
    await _resolve_unmapped(sc, symbols)
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
    try:
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
