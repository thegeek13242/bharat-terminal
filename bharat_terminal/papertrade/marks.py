"""LTP marks + F&O universe lookup for the paper trader.

WHY this module exists separately: the paper engine must never own a broker
session. Live equity LTPs come from kb-service's /ltp endpoint, which reuses
the single licensed Angel One session (see kb/api.py — internal-only data,
do not widen exposure). The F&O universe decides CASH vs FUT for shorts:
symbols with an NFO stock future can stay short overnight; everything else
is intraday-only cash.

The scrip-master download carries a documented landmine: the file is ~35MB
and Angel One's CDN has been measured at ~250KB/s from this VM (137s on
2026-07-23). A tight total timeout made every refresh fail after a restart
(see ingestion/adapters/angelone_mcx.py). Hence total=600s / sock_read=60s
here, plus a 24h Redis cache so the download is at most daily.
"""
from __future__ import annotations

import json
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

KB_SERVICE_URL = os.getenv("KB_SERVICE_URL", "http://kb-service:8001")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# kb-service /ltp truncates a batch to 16 symbols per request (kb/api.py).
_LTP_BATCH = 16
_LTP_TIMEOUT_S = 10.0

# Same constant as ingestion/adapters/angelone_mcx.py — duplicated on purpose:
# importing that module drags in the whole ingestion/websocket adapter stack.
SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/"
    "OpenAPIScripMaster.json"
)
# 35MB / slow-CDN lesson (2026-07-23): total caps a slow-but-progressing
# download, sock_read bounds a truly stalled stream.
SCRIP_MASTER_TIMEOUT_TOTAL_S = float(os.getenv("SCRIP_MASTER_TIMEOUT_S", "600"))
SCRIP_MASTER_TIMEOUT_READ_S = 60

FNO_REDIS_KEY = "papertrade:fno_universe"
FNO_REDIS_TTL_S = 24 * 3600
# In-process memo so per-event evaluations don't hit Redis every time.
_FNO_MEMO_TTL_S = 6 * 3600

_client: httpx.AsyncClient | None = None
_redis = None
_fno_memo: tuple[float, set[str]] | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_LTP_TIMEOUT_S)
    return _client


def _get_redis():
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(
            REDIS_URL,
            encoding="utf-8", decode_responses=True,
            # Wedged connections must raise, not park (2026-07-20 postmortem).
            socket_timeout=10, socket_connect_timeout=5,
        )
    return _redis


async def close() -> None:
    """Release the shared HTTP client and Redis connection (shutdown path)."""
    global _client, _redis
    if _client is not None:
        await _client.aclose()
        _client = None
    if _redis is not None:
        await _redis.close()
        _redis = None


# ── Live LTPs ────────────────────────────────────────────────────────────────

async def get_ltps(symbols) -> dict[str, float]:
    """Batch live LTPs via kb-service. Returns {symbol: ltp}; symbols with no
    live quote are simply ABSENT (no EOD fallback here — a stale close is a
    wrong fill price, and wrong-but-confident is this project's failure mode).
    """
    syms = sorted({s.strip().upper() for s in symbols if s and s.strip()})
    out: dict[str, float] = {}
    if not syms:
        return out
    client = _get_client()
    for i in range(0, len(syms), _LTP_BATCH):
        chunk = syms[i:i + _LTP_BATCH]
        try:
            resp = await client.get(
                f"{KB_SERVICE_URL}/ltp", params={"symbols": ",".join(chunk)})
            resp.raise_for_status()
            quotes = resp.json().get("quotes", {})
            for sym, q in quotes.items():
                ltp = q.get("ltp")
                if ltp:
                    out[sym.upper()] = float(ltp)
        except Exception as e:
            logger.warning(f"[LTP] batch fetch failed for {chunk}: {e}")
    return out


# ── F&O universe ─────────────────────────────────────────────────────────────

async def _fetch_fno_from_scrip_master() -> set[str]:
    """Download the Angel One scrip master and extract NFO single-stock-future
    underlyings. `name` on an NFO FUTSTK row is the NSE underlying symbol."""
    import aiohttp
    timeout = aiohttp.ClientTimeout(
        total=SCRIP_MASTER_TIMEOUT_TOTAL_S, sock_read=SCRIP_MASTER_TIMEOUT_READ_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(SCRIP_MASTER_URL) as resp:
            resp.raise_for_status()
            raw = await resp.read()
    rows = json.loads(raw)
    if not isinstance(rows, list):
        raise RuntimeError(f"scrip master returned non-list: {type(rows).__name__}")
    names = {
        str(r["name"]).upper() for r in rows
        if r.get("exch_seg") == "NFO"
        and r.get("instrumenttype") == "FUTSTK"
        and r.get("name")
    }
    if not names:
        raise RuntimeError("scrip master parsed but yielded 0 NFO FUTSTK names")
    return names


async def _redis_cached_fno() -> set[str] | None:
    try:
        cached = await _get_redis().get(FNO_REDIS_KEY)
        if cached:
            return set(json.loads(cached))
    except Exception as e:
        logger.warning(f"[FNO] redis read failed: {e}")
    return None


async def get_fno_universe() -> set[str]:
    """Symbols with an NFO stock future. Resolution order: in-process memo →
    Redis (24h TTL) → fresh scrip-master download → stale-Redis fallback.
    Empty set on total failure: callers then treat every symbol as non-F&O
    (cash intraday-only) — the conservative direction for shorts.
    """
    global _fno_memo
    now = time.monotonic()
    if _fno_memo is not None and (now - _fno_memo[0]) < _FNO_MEMO_TTL_S:
        return _fno_memo[1]

    universe = await _redis_cached_fno()
    if universe:
        logger.info(f"[FNO] universe from redis cache: {len(universe)} symbols")
    else:
        try:
            universe = await _fetch_fno_from_scrip_master()
            logger.info(f"[FNO] universe from scrip master: {len(universe)} symbols")
            try:
                await _get_redis().setex(
                    FNO_REDIS_KEY, FNO_REDIS_TTL_S, json.dumps(sorted(universe)))
            except Exception as e:
                logger.warning(f"[FNO] redis cache write failed: {e}")
        except Exception as e:
            logger.warning(f"[FNO] scrip master fetch failed: {e}")
            universe = await _redis_cached_fno()  # possibly stale but better
            if universe is None:
                logger.warning(
                    "[FNO] no universe available — all symbols treated as "
                    "non-F&O (cash intraday-only)")
                return set()   # do NOT memoize failure; retry next call

    _fno_memo = (now, universe)
    return universe
