"""
Angel One SmartAPI → MCX live tick adapter.

Streams last-traded-price ticks for a curated list of MCX commodity futures
(crude oil, gold, silver, copper, aluminium, natural gas) via Angel One's
SmartWebSocketV2 onto the `raw.commodity.in` Kafka topic.

Design notes
------------
• Authentication is TOTP-based (clientCode + PIN + TOTP). All four pieces
  are env-injected; the adapter does NOT prompt or persist them.
• The SmartAPI Python SDK runs the WebSocket on its own thread with
  callback-style events (on_open / on_data / on_close / on_error). We
  bridge that into asyncio via `asyncio.run_coroutine_threadsafe` so the
  rest of the ingestion service stays single-threaded async.
• Token resolution: MCX futures are monthly-expiring contracts. On startup
  (and on a daily refresh) we pull the OpenAPI scrip master, filter to
  FUTCOM rows for our universe of commodity names, and pick the nearest
  non-expired contract per name. This auto-rolls the subscription on
  expiry day.
• Price scaling: SmartWebSocketV2 returns prices as integers in the
  exchange's minor unit. For MCX commodities the divisor is 100
  (₹.paise). We rescale once on parse so downstream sees ₹ in the
  contract's quote unit.
• This adapter does NOT inherit BaseAdapter — its lifecycle is push-based
  (WS callbacks) rather than pull-based (poll loop).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, time as dtime, timezone
from typing import Awaitable, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import aiohttp

try:
    import pyotp
except ImportError:  # pragma: no cover
    pyotp = None  # type: ignore

try:
    from SmartApi import SmartConnect
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    _SMARTAPI_IMPORT_ERROR: Optional[BaseException] = None
except ImportError as _e:  # pragma: no cover
    SmartConnect = None  # type: ignore
    SmartWebSocketV2 = None  # type: ignore
    # Capture the actual ImportError so start() can show why — usually it's
    # a missing transitive dep (logzero, requests) rather than the SDK
    # itself. The previous code swallowed this and reported a misleading
    # "smartapi-python is not installed".
    _SMARTAPI_IMPORT_ERROR = _e

from bharat_terminal.types import CommodityTick

logger = logging.getLogger(__name__)

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/"
    "OpenAPIScripMaster.json"
)
# The scrip master is ~35MB and the CDN can serve it as slow as ~250KB/s from
# this VM (measured 137s on 2026-07-23, when the old total=60s deadline made
# every refresh time out after a restart: login OK, refresh forever retrying,
# websocket never opened, MCX ticks silently absent). The generous total only
# caps slow-but-progressing downloads; sock_read bounds a truly stalled stream.
SCRIP_MASTER_TIMEOUT_S = float(os.getenv("SCRIP_MASTER_TIMEOUT_S", "600"))

# Daily pre-open re-login (2026-07-29 incident): Angel One force-expires the
# REST JWT overnight while the WEBSOCKET keeps streaming on the old session —
# so the supervisor never re-logged in, kept republishing yesterday's dead
# token, and equity LTP served AG8001 "Invalid Token" all morning (blocked
# the paper trader's at-open fills). Refresh the session every day at this
# IST time regardless of websocket health.
_IST = ZoneInfo("Asia/Kolkata")
_RELOGIN_HHMM = os.getenv("ANGELONE_RELOGIN_IST", "08:40")
RELOGIN_IST = dtime(*(int(x) for x in _RELOGIN_HHMM.split(":")))

# Universe of MCX commodities we care about. Keys are the `name` field in the
# scrip master; values are the canonical name we publish to Kafka.
MCX_UNIVERSE: Dict[str, str] = {
    "CRUDEOIL":   "CRUDEOIL",
    "GOLD":       "GOLD",
    "SILVER":     "SILVER",
    "COPPER":     "COPPER",
    "ALUMINIUM":  "ALUMINIUM",
    "NATURALGAS": "NATURALGAS",
    # Mini contracts skipped intentionally — they shadow the main ones with
    # lower liquidity. Add later if we want finer-grained subscriptions.
}

# SmartWebSocketV2 constants (https://smartapi.angelone.in/docs/MarketData)
# SDK docstring: 1=nse_cm, 2=nse_fo, 3=bse_cm, 4=bse_fo, 5=mcx_fo. (The SDK's
# "only exchangeType 1" validation applies to mode 4/depth only — QUOTE mode
# subscriptions accept any exchange.)
EXCHANGE_TYPE_NSE = 1
EXCHANGE_TYPE_BSE = 3
EXCHANGE_TYPE_MCX = 5

SUBSCRIPTION_MODE_LTP        = 1   # last-traded-price only
SUBSCRIPTION_MODE_QUOTE      = 2   # OHLC + LTP + volume + buy/sell qty
SUBSCRIPTION_MODE_SNAP_QUOTE = 3   # QUOTE + volume + buy/sell qty

DEFAULT_PRICE_DIVISOR = 100.0  # MCX prices come in paise

# ── Dynamic NSE equity LTP (internal-only terminal feature) ────────────────
# The api-gateway records demand in a Redis ZSET (symbol → last-request epoch);
# this adapter subscribes/unsubscribes those symbols on the SAME WebSocket the
# MCX stream uses (Angel One is single-session — never open a second login),
# and pushes each tick to Redis: SET ltp:{SYMBOL} + PUBLISH ltp.updates.
# LICENSING: personal-use data — consumed only by the internal-gated LTP
# endpoints/WS; never widen to customer surfaces.
LTP_WANT_KEY = "ltp:want"
LTP_CHANNEL = "ltp.updates"
LTP_KEY_TTL_S = 60
# A symbol stays subscribed while its demand entry is younger than this.
LTP_DEMAND_WINDOW_S = int(os.getenv("LTP_DEMAND_WINDOW_S", "90"))
LTP_SUB_POLL_S = float(os.getenv("LTP_SUB_POLL_S", "3"))
EQUITY_CORRELATION_ID = "bharat-eq-ltp"


def _parse_expiry(expiry_str: str) -> Optional[datetime]:
    """Parse Angel One scrip-master expiry, e.g. '21NOV2025' → datetime."""
    if not expiry_str:
        return None
    try:
        return datetime.strptime(expiry_str, "%d%b%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class AngelOneMCXAdapter:
    """Angel One SmartAPI → raw.commodity.in adapter."""

    source_name = "ANGELONE_MCX"

    def __init__(
        self,
        publish_callback: Callable[[CommodityTick], Awaitable[bool]],
        api_key: Optional[str] = None,
        client_code: Optional[str] = None,
        password: Optional[str] = None,
        totp_secret: Optional[str] = None,
        universe: Optional[Dict[str, str]] = None,
        scrip_refresh_hours: int = 12,
        reconnect_delay_seconds: float = 5.0,
        max_reconnect_delay_seconds: float = 60.0,
    ):
        self._publish = publish_callback
        self._api_key = api_key or os.getenv("ANGEL_ONE_API_KEY", "")
        self._client_code = client_code or os.getenv("ANGEL_ONE_CLIENT_CODE", "")
        self._password = password or os.getenv("ANGEL_ONE_PASSWORD", "")
        self._totp_secret = totp_secret or os.getenv("ANGEL_ONE_TOTP_SECRET", "")
        self._universe = universe or dict(MCX_UNIVERSE)
        self._scrip_refresh_hours = scrip_refresh_hours
        self._reconnect_delay = reconnect_delay_seconds
        self._max_reconnect_delay = max_reconnect_delay_seconds
        # Re-publish the Redis session token this often while connected, so the
        # KB live-price (priced-in) check never falls back to stale EOD data
        # mid-session. Must stay well under the 13h TTL set in _publish_session.
        self._session_refresh_s = float(os.getenv("ANGELONE_SESSION_REFRESH_S", str(4 * 3600)))

        # Resolved on login / token-refresh
        self._jwt_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._feed_token: Optional[str] = None
        self._last_login_ist_date = None    # set on each _smartapi_login
        # token (str) → metadata dict.
        #   MCX: {"exch": "MCX", "commodity": str, "symbol": str, "expiry": str, "divisor": float}
        self._token_metadata: Dict[str, Dict] = {}

        # WS thread + bridge
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Optional[SmartWebSocketV2] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._main_task: Optional[asyncio.Task] = None
        self._running = False
        self._connected = asyncio.Event()
        self._stats = {"ticks": 0, "publish_ok": 0, "publish_fail": 0, "eq_ticks": 0}

        # NSE equity LTP state (demand-driven; see module constants above).
        self._nse_token_by_symbol: Dict[str, str] = {}
        self._nse_symbol_by_token: Dict[str, str] = {}
        self._bse_token_by_symbol: Dict[str, str] = {}
        self._bse_symbol_by_token: Dict[str, str] = {}
        # (exchangeType, token) pairs currently subscribed — NSE and BSE token
        # spaces are not guaranteed disjoint, so bare tokens are ambiguous.
        self._equity_subscribed: set = set()
        self._eq_task: Optional[asyncio.Task] = None
        self._redis = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        if SmartConnect is None or SmartWebSocketV2 is None:
            cause = (
                f" (caused by: {type(_SMARTAPI_IMPORT_ERROR).__name__}: "
                f"{_SMARTAPI_IMPORT_ERROR})"
                if _SMARTAPI_IMPORT_ERROR is not None
                else ""
            )
            raise RuntimeError(
                "SmartApi import failed — typically a missing transitive "
                "dep (logzero / requests). Check requirements/ingestion.txt."
                + cause
            )
        if pyotp is None:
            raise RuntimeError("pyotp is not installed — required for SmartAPI TOTP login")
        if not all([self._api_key, self._client_code, self._password, self._totp_secret]):
            raise RuntimeError(
                "Angel One credentials missing. Set ANGEL_ONE_API_KEY, "
                "ANGEL_ONE_CLIENT_CODE, ANGEL_ONE_PASSWORD, ANGEL_ONE_TOTP_SECRET."
            )

        self._loop = asyncio.get_running_loop()
        self._running = True
        self._main_task = asyncio.create_task(self._run(), name="angelone-mcx-run")
        self._eq_task = asyncio.create_task(
            self._equity_subscription_loop(), name="angelone-eq-ltp"
        )
        logger.info(
            f"[{self.source_name}] Adapter started "
            f"(universe={list(self._universe.values())})"
        )

    async def stop(self) -> None:
        self._running = False
        if self._eq_task:
            self._eq_task.cancel()
            try:
                await self._eq_task
            except asyncio.CancelledError:
                pass
        if self._ws is not None:
            try:
                self._ws.close_connection()
            except Exception:
                pass
        if self._main_task:
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5)
        logger.info(
            f"[{self.source_name}] Adapter stopped "
            f"(ticks={self._stats['ticks']}, "
            f"published={self._stats['publish_ok']}, "
            f"failed={self._stats['publish_fail']})"
        )

    # ── Main supervisor loop ────────────────────────────────────────────

    async def _run(self) -> None:
        backoff = self._reconnect_delay
        last_scrip_refresh = 0.0

        while self._running:
            try:
                # 1) Login (gets jwt + feed token)
                await asyncio.to_thread(self._smartapi_login)

                # 1b) Publish the session token to Redis so other services (the
                # KB price endpoint) can REUSE it for on-demand equity LTP without
                # a second login (Angel One is single-session). Fail-open.
                await self._publish_session()
                last_session_publish = time.time()

                # 2) Refresh scrip master if stale (or first time)
                now = time.time()
                if now - last_scrip_refresh > self._scrip_refresh_hours * 3600:
                    await self._refresh_scrip_universe()
                    last_scrip_refresh = now

                if not self._token_metadata:
                    logger.warning(
                        f"[{self.source_name}] No MCX tokens resolved — sleeping 60s"
                    )
                    await asyncio.sleep(60)
                    continue

                # 3) Open WebSocket and subscribe
                self._connected.clear()
                self._open_websocket()

                # 4) Wait until disconnected (or shutdown), refreshing the Redis
                #    session token periodically so it never expires mid-session.
                while self._running and self._ws_thread and self._ws_thread.is_alive():
                    await asyncio.sleep(1)
                    if time.time() - last_session_publish > self._session_refresh_s:
                        await self._publish_session()
                        last_session_publish = time.time()
                    # Daily pre-open REST-token refresh (see RELOGIN_IST note
                    # at top). If the fresh session invalidates the running
                    # websocket, the thread exits and the supervisor loop
                    # rebuilds it — either way the JWT in Redis is valid at
                    # the equity open.
                    now_ist = datetime.now(_IST)
                    if (now_ist.date() != self._last_login_ist_date
                            and now_ist.time() >= RELOGIN_IST):
                        logger.info(f"[{self.source_name}] daily pre-open "
                                    f"re-login (REST JWT refresh)")
                        await asyncio.to_thread(self._smartapi_login)
                        await self._publish_session()
                        last_session_publish = time.time()

                logger.warning(f"[{self.source_name}] WebSocket loop exited")
                # Reset backoff after a clean run >5min
                backoff = self._reconnect_delay

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"[{self.source_name}] Supervisor error: {e}", exc_info=True
                )

            if self._running:
                logger.info(
                    f"[{self.source_name}] Reconnecting in {backoff:.1f}s"
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_reconnect_delay)

    # ── SmartAPI auth ───────────────────────────────────────────────────

    def _smartapi_login(self) -> None:
        """Synchronous SmartAPI login — runs in worker thread."""
        sc = SmartConnect(api_key=self._api_key)
        totp = pyotp.TOTP(self._totp_secret).now()
        result = sc.generateSession(self._client_code, self._password, totp)
        if not result or not result.get("status"):
            raise RuntimeError(f"SmartAPI login failed: {result}")
        data = result.get("data") or {}
        self._jwt_token = data.get("jwtToken")
        self._refresh_token = data.get("refreshToken")
        self._feed_token = sc.getfeedToken()
        self._last_login_ist_date = datetime.now(_IST).date()
        if not all([self._jwt_token, self._feed_token]):
            raise RuntimeError(f"SmartAPI login returned incomplete tokens: {data}")
        logger.info(f"[{self.source_name}] SmartAPI login OK ({self._client_code})")

    async def _get_redis(self):
        if self._redis is None:
            import redis.asyncio as _aioredis
            self._redis = _aioredis.from_url(
                os.getenv("REDIS_URL", "redis://redis:6379/0"),
                encoding="utf-8", decode_responses=True,
                # 2026-07-20 postmortem: wedged connections must raise, not park.
                socket_timeout=10, socket_connect_timeout=5,
            )
        return self._redis

    async def _publish_session(self) -> None:
        """Publish the current session token to Redis (`angelone:session`) so the
        KB service can reuse it for stateless equity LTP lookups. Fail-open."""
        try:
            import json as _json
            await self._get_redis()
            payload = _json.dumps({
                "api_key": self._api_key,
                "access_token": self._jwt_token,
                "feed_token": self._feed_token,
            })
            # TTL slightly longer than the login cadence so it never goes stale
            # mid-session; refreshed on every (re)login.
            await self._redis.setex("angelone:session", 13 * 3600, payload)
            logger.info(f"[{self.source_name}] published session token to Redis for equity LTP reuse")
        except Exception as e:
            logger.warning(f"[{self.source_name}] session publish failed (fail-open): {e}")

    # ── Scrip master → near-month token resolution ───────────────────

    async def _refresh_scrip_universe(self) -> None:
        """
        Fetch the OpenAPI scrip master once and resolve tokens for:

          MCX: nearest-non-expired FUTCOM for each commodity in MCX_UNIVERSE.

        Populates self._token_metadata (tagged by `exch`) so `_parse_tick`
        can build a CommodityTick for each incoming tick.
        """
        # Resumable download (2026-08-21): the CDN closes connections
        # mid-body at random offsets (ClientPayloadError "payload is not
        # completed") but honours HTTP Range, so truncated attempts
        # accumulate instead of restarting from zero — the 2026-07-28
        # timeout fix alone could not survive today's CDN behaviour.
        # Partial progress persists across supervisor reconnects (measured
        # 2026-08-21: the ELB cut every ~1-3MB; 12 attempts from zero could
        # not cover 37MB, but resuming across cycles can).
        if not hasattr(self, "_scrip_dl"):
            self._scrip_dl = {"buf": bytearray(), "total": None}
        buf = self._scrip_dl["buf"]
        total = self._scrip_dl["total"]
        last_err = "incomplete"
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=SCRIP_MASTER_TIMEOUT_S, sock_read=60)
        ) as session:
            for attempt in range(1, 41):
                headers = {"Range": f"bytes={len(buf)}-"} if buf else {}
                try:
                    async with session.get(SCRIP_MASTER_URL, headers=headers) as resp:
                        if resp.status == 200:
                            if buf:
                                del buf[:]             # Range ignored: restart (in place)
                            total = resp.content_length or total
                        elif resp.status == 206:
                            cr = resp.headers.get("Content-Range", "")
                            if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
                                total = int(cr.rsplit("/", 1)[1])
                        else:
                            resp.raise_for_status()
                        async for chunk in resp.content.iter_chunked(1 << 16):
                            buf.extend(chunk)
                    self._scrip_dl["total"] = total
                    if total is None or len(buf) >= total:
                        break
                    last_err = f"short body ({len(buf)}/{total})"
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    last_err = f"{type(e).__name__} at {len(buf)} bytes"
                logger.info(f"[{self.source_name}] scrip-master attempt "
                            f"{attempt}/40: {last_err} — resuming")
                await asyncio.sleep(min(15.0, 1.0 * attempt))
            else:
                # progress stays in self._scrip_dl for the next reconnect
                raise RuntimeError(f"scrip master incomplete after 40 attempts "
                                   f"({len(buf)}/{total} bytes kept): {last_err}")
        raw = bytes(buf)
        del buf[:]
        self._scrip_dl["total"] = None
        try:
            scrip_list = json.loads(raw)
        except Exception as e:
            raise RuntimeError(f"Scrip master parse failed: {e}")

        if not isinstance(scrip_list, list):
            raise RuntimeError(
                f"Scrip master returned non-list: {type(scrip_list).__name__}"
            )

        now_utc = datetime.now(timezone.utc)
        new_metadata: Dict[str, Dict] = {}

        # ── 1) MCX FUTCOM ─────────────────────────────────────────────
        mcx_candidates: Dict[str, List] = {n: [] for n in self._universe}
        for row in scrip_list:
            if not isinstance(row, dict):
                continue
            if row.get("exch_seg") != "MCX":
                continue
            if row.get("instrumenttype") != "FUTCOM":
                continue
            name = row.get("name") or ""
            if name not in self._universe:
                continue
            exp = _parse_expiry(row.get("expiry") or "")
            if exp is None or exp < now_utc:
                continue
            mcx_candidates[name].append((exp, row))

        mcx_resolved: List[str] = []
        mcx_missing: List[str] = []
        for name, canonical in self._universe.items():
            rows = mcx_candidates.get(name) or []
            if not rows:
                mcx_missing.append(canonical)
                continue
            rows.sort(key=lambda r: r[0])
            expiry_dt, near = rows[0]
            token = str(near.get("token") or "").strip()
            if not token:
                mcx_missing.append(canonical)
                continue
            new_metadata[token] = {
                "exch": "MCX",
                "commodity": canonical,
                "symbol": near.get("symbol") or "",
                "expiry": near.get("expiry") or "",
                "divisor": DEFAULT_PRICE_DIVISOR,
            }
            mcx_resolved.append(f"{canonical}={near.get('symbol')}")

        logger.info(
            f"[{self.source_name}] Resolved {len(mcx_resolved)} MCX contracts: "
            f"{', '.join(mcx_resolved)}"
            + (f" | missing: {mcx_missing}" if mcx_missing else "")
        )

        self._token_metadata = new_metadata

        # ── 2) NSE + BSE cash equities (for demand-driven LTP) ─────────
        nse_by_symbol: Dict[str, str] = {}
        nse_by_token: Dict[str, str] = {}
        bse_by_symbol: Dict[str, str] = {}
        bse_by_token: Dict[str, str] = {}
        for row in scrip_list:
            if not isinstance(row, dict):
                continue
            seg = row.get("exch_seg")
            sym = row.get("symbol") or ""
            token = str(row.get("token") or "").strip()
            if not token:
                continue
            if seg == "NSE":
                # NSE cash equities are listed as "<NAME>-EQ".
                if sym.endswith("-EQ"):
                    base = sym[:-3].upper()
                    nse_by_symbol[base] = token
                    nse_by_token[token] = base
            elif seg == "BSE":
                # BSE cash equities have instrumenttype "" (indices are
                # AMXIDX); symbol == BSE scrip_id == our companies.symbol for
                # BSE-only rows, token == the 6-digit BSE scrip code.
                if sym and not (row.get("instrumenttype") or ""):
                    bse_by_symbol[sym.upper()] = token
                    bse_by_token[token] = sym.upper()
        self._nse_token_by_symbol = nse_by_symbol
        self._nse_symbol_by_token = nse_by_token
        self._bse_token_by_symbol = bse_by_symbol
        self._bse_symbol_by_token = bse_by_token
        logger.info(
            f"[{self.source_name}] equity token map loaded "
            f"(NSE: {len(nse_by_symbol)}, BSE: {len(bse_by_symbol)} symbols)"
        )

    # ── WebSocket lifecycle ─────────────────────────────────────────────

    def _open_websocket(self) -> None:
        """Spin up SmartWebSocketV2 in a background thread."""
        assert self._jwt_token and self._feed_token

        ws = SmartWebSocketV2(
            self._jwt_token,
            self._api_key,
            self._client_code,
            self._feed_token,
            max_retry_attempt=1,
        )

        ws.on_open = self._on_open                  # type: ignore[attr-defined]
        ws.on_data = self._on_data                  # type: ignore[attr-defined]
        ws.on_error = self._on_error                # type: ignore[attr-defined]
        ws.on_close = self._on_close                # type: ignore[attr-defined]

        self._ws = ws
        self._ws_thread = threading.Thread(
            target=ws.connect,
            name="angelone-ws",
            daemon=True,
        )
        self._ws_thread.start()
        logger.info(f"[{self.source_name}] WebSocket thread started")

    # ── SmartAPI WS callbacks (run in WS thread!) ────────────────────────

    def _on_open(self, wsapp) -> None:  # noqa: ARG002
        # `wsapp` is the underlying websocket-client WebSocketApp passed by
        # SmartAPI's callback shim; it does NOT have .subscribe / .close_
        # connection — those live on the SmartWebSocketV2 wrapper that we
        # constructed and hold as self._ws.
        if not self._token_metadata:
            logger.warning(f"[{self.source_name}] on_open with no tokens — closing")
            try:
                if self._ws is not None:
                    self._ws.close_connection()
            except Exception:
                pass
            return

        mcx_tokens = [t for t, m in self._token_metadata.items() if m.get("exch") == "MCX"]

        token_list: List[Dict] = []
        if mcx_tokens:
            token_list.append({"exchangeType": EXCHANGE_TYPE_MCX, "tokens": mcx_tokens})

        try:
            assert self._ws is not None, "ws not constructed before on_open"
            # Mode 3 (SnapQuote) is a superset of mode 2 (Quote); MCX has no OI so
            # the extra fields are nulled — harmless.
            self._ws.subscribe(
                "bharat-derivs-1", SUBSCRIPTION_MODE_SNAP_QUOTE, token_list
            )
            logger.info(
                f"[{self.source_name}] Subscribed mode=SNAP_QUOTE to "
                f"{len(mcx_tokens)} MCX tokens"
            )
            # Fresh socket: equity subscriptions must be re-established by the
            # demand loop (set mutation is GIL-atomic; safe from this thread).
            self._equity_subscribed = set()
            if self._loop:
                self._loop.call_soon_threadsafe(self._connected.set)
        except Exception as e:
            logger.error(f"[{self.source_name}] subscribe failed: {e}", exc_info=True)

    def _on_data(self, wsapp, message) -> None:  # noqa: ARG002
        """
        Bridge SmartWebSocketV2 callback (worker thread) into the asyncio
        loop, publishing the parsed CommodityTick (MCX) or equity LTP payload.
        """
        try:
            parsed = self._parse_tick(message)
            if parsed is None:
                return
            if isinstance(parsed, tuple) and parsed[0] == "EQ":
                self._stats["eq_ticks"] += 1
                if self._loop and self._running:
                    asyncio.run_coroutine_threadsafe(
                        self._dispatch_equity(parsed[1]), self._loop
                    )
                return
            self._stats["ticks"] += 1
            if self._loop and self._running:
                asyncio.run_coroutine_threadsafe(
                    self._dispatch(parsed), self._loop
                )
        except Exception as e:
            logger.debug(f"[{self.source_name}] tick parse error: {e}")

    def _on_error(self, wsapp, error) -> None:  # noqa: ARG002
        logger.error(f"[{self.source_name}] WS error: {error}")

    def _on_close(self, wsapp, close_status_code=None, close_msg=None) -> None:  # noqa: ARG002
        # websocket-client invokes on_close with (wsapp, close_status_code,
        # close_msg); accept the extra positional args to avoid a TypeError
        # ("_on_close() takes 2 positional arguments but 4 were given").
        logger.warning(
            f"[{self.source_name}] WS closed (code={close_status_code}, msg={close_msg})"
        )

    # ── Tick parsing + publish ───────────────────────────────────────────

    def _parse_tick(self, message):
        """
        Translate a SmartWebSocketV2 message into a CommodityTick (MCX token).
        Returns None for unknown tokens or malformed messages.
        """
        if not isinstance(message, dict):
            return None
        token = str(message.get("token") or "").strip()
        if not token:
            return None
        meta = self._token_metadata.get(token)
        if not meta:
            # NSE/BSE equity tick (demand-driven LTP path)? The wire message
            # carries exchange_type — use it to disambiguate tokens that exist
            # in both segments; fall back NSE-first when it is absent.
            exch_type = message.get("exchange_type")
            if exch_type == EXCHANGE_TYPE_BSE:
                eq_symbol = self._bse_symbol_by_token.get(token)
            elif exch_type == EXCHANGE_TYPE_NSE:
                eq_symbol = self._nse_symbol_by_token.get(token)
            else:
                eq_symbol = (
                    self._nse_symbol_by_token.get(token)
                    or self._bse_symbol_by_token.get(token)
                )
            if eq_symbol:
                ltp_raw = message.get("last_traded_price")
                if ltp_raw is None:
                    return None
                try:
                    payload = {
                        "symbol": eq_symbol,
                        "ltp": float(ltp_raw) / DEFAULT_PRICE_DIVISOR,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                except (TypeError, ValueError):
                    return None
                pc = message.get("closed_price")
                if pc is not None:
                    try:
                        payload["prev_close"] = float(pc) / DEFAULT_PRICE_DIVISOR
                    except (TypeError, ValueError):
                        pass
                return ("EQ", payload)
            return None  # subscription drift — ignore

        divisor = float(meta.get("divisor") or DEFAULT_PRICE_DIVISOR)
        ltp_raw = message.get("last_traded_price")
        if ltp_raw is None:
            return None
        try:
            last_price = float(ltp_raw) / divisor
        except (TypeError, ValueError):
            return None

        def _scaled(key: str) -> Optional[float]:
            v = message.get(key)
            if v is None:
                return None
            try:
                return float(v) / divisor
            except (TypeError, ValueError):
                return None

        def _intish(key: str) -> Optional[int]:
            v = message.get(key)
            if v is None:
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        def _floatish(key: str) -> Optional[float]:
            v = message.get(key)
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        exchange_ts = _intish("exchange_timestamp")
        latency_ms = (time.time() * 1000) - exchange_ts if exchange_ts else 0.0
        now_utc = datetime.now(timezone.utc)

        if meta.get("exch") == "MCX":
            return CommodityTick(
                timestamp_utc=now_utc,
                exchange_timestamp_ms=exchange_ts,
                commodity=meta["commodity"],
                contract_symbol=meta["symbol"],
                token=token,
                expiry=meta.get("expiry") or None,
                last_price=last_price,
                open_price=_scaled("open_price_of_the_day"),
                high_price=_scaled("high_price_of_the_day"),
                low_price=_scaled("low_price_of_the_day"),
                prev_close=_scaled("closed_price"),
                last_quantity=_intish("last_traded_quantity"),
                avg_price=_scaled("average_traded_price"),
                volume_day=_intish("volume_trade_for_the_day"),
                total_buy_qty=_floatish("total_buy_quantity"),
                total_sell_qty=_floatish("total_sell_quantity"),
                ingest_latency_ms=latency_ms,
            )

        return None  # unknown exchange tag

    async def _dispatch_equity(self, payload: Dict) -> None:
        """Push an equity LTP tick to Redis: keyed value for REST/snapshot
        reads + pub/sub for the api-gateway's WS fanout. Fail-open."""
        try:
            r = await self._get_redis()
            data = json.dumps(payload)
            pipe = r.pipeline()
            pipe.setex(f"ltp:{payload['symbol']}", LTP_KEY_TTL_S, data)
            pipe.publish(LTP_CHANNEL, data)
            await pipe.execute()
        except Exception as e:
            logger.debug(f"[{self.source_name}] equity ltp dispatch error: {e}")

    # ── Demand-driven equity subscription management ─────────────────────

    async def _equity_subscription_loop(self) -> None:
        """Every few seconds, align WS equity subscriptions with demand
        recorded by the api-gateway in the `ltp:want` ZSET. Fail-open: any
        error skips the cycle; the MCX stream is never affected."""
        while self._running:
            try:
                await asyncio.sleep(LTP_SUB_POLL_S)
                if not (self._ws and self._connected.is_set()):
                    continue
                if not self._nse_token_by_symbol:
                    continue

                r = await self._get_redis()
                cutoff = time.time() - LTP_DEMAND_WINDOW_S
                wanted_syms = await r.zrangebyscore(LTP_WANT_KEY, cutoff, "+inf")
                # Housekeeping: drop expired demand entries.
                await r.zremrangebyscore(LTP_WANT_KEY, "-inf", cutoff)

                # NSE-first: dual-listed symbols keep their NSE stream; the
                # BSE map serves the BSE-only universe.
                desired = set()
                for s in wanted_syms:
                    sym = (s or "").upper()
                    if not sym:
                        continue
                    if sym in self._nse_token_by_symbol:
                        desired.add((EXCHANGE_TYPE_NSE, self._nse_token_by_symbol[sym]))
                    elif sym in self._bse_token_by_symbol:
                        desired.add((EXCHANGE_TYPE_BSE, self._bse_token_by_symbol[sym]))
                to_add = desired - self._equity_subscribed
                to_remove = self._equity_subscribed - desired

                def _grouped(pairs) -> list:
                    by_exch: Dict[int, list] = {}
                    for exch, tok in pairs:
                        by_exch.setdefault(exch, []).append(tok)
                    return [
                        {"exchangeType": exch, "tokens": sorted(toks)}
                        for exch, toks in sorted(by_exch.items())
                    ]

                if to_add:
                    await asyncio.to_thread(
                        self._ws.subscribe,
                        EQUITY_CORRELATION_ID,
                        SUBSCRIPTION_MODE_QUOTE,
                        _grouped(to_add),
                    )
                    self._equity_subscribed |= to_add
                    logger.info(
                        f"[{self.source_name}] equity LTP subscribed +{len(to_add)} "
                        f"(total {len(self._equity_subscribed)})"
                    )
                if to_remove:
                    await asyncio.to_thread(
                        self._ws.unsubscribe,
                        EQUITY_CORRELATION_ID,
                        SUBSCRIPTION_MODE_QUOTE,
                        _grouped(to_remove),
                    )
                    self._equity_subscribed -= to_remove
                    logger.info(
                        f"[{self.source_name}] equity LTP unsubscribed -{len(to_remove)} "
                        f"(total {len(self._equity_subscribed)})"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"[{self.source_name}] equity subscription loop error: {e}")

    async def _dispatch(self, tick) -> None:
        """Publish the parsed CommodityTick."""
        try:
            ok = await self._publish(tick)
            if ok:
                self._stats["publish_ok"] += 1
            else:
                self._stats["publish_fail"] += 1
        except Exception as e:
            self._stats["publish_fail"] += 1
            logger.debug(f"[{self.source_name}] publish error: {e}")

        # Heartbeat log every 500 ticks
        if self._stats["ticks"] % 500 == 0:
            logger.info(
                f"[{self.source_name}] ticks={self._stats['ticks']} "
                f"published={self._stats['publish_ok']} "
                f"failed={self._stats['publish_fail']}"
            )
