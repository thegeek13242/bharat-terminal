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
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, List, Optional

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

from bharat_terminal.types import CommodityTick, DerivativeTick

logger = logging.getLogger(__name__)

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/"
    "OpenAPIScripMaster.json"
)

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
EXCHANGE_TYPE_NSE = 1
EXCHANGE_TYPE_NFO = 2
EXCHANGE_TYPE_MCX = 5

SUBSCRIPTION_MODE_LTP        = 1   # last-traded-price only
SUBSCRIPTION_MODE_QUOTE      = 2   # OHLC + LTP + volume + buy/sell qty
SUBSCRIPTION_MODE_SNAP_QUOTE = 3   # QUOTE + open_interest + IV (NFO only). Required for F&O.

DEFAULT_PRICE_DIVISOR = 100.0  # MCX + NFO prices come in paise

# ── NFO universe (F&O contracts we subscribe to) ────────────────────────
# Stock futures — NIFTY 50 names. The list lives here as a stable snapshot
# of NSE-listed F&O names rather than importing research.universe to avoid
# the ingestion service depending on research code.
NFO_STOCK_FUTURES: tuple[str, ...] = (
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
    "BHARTIARTL", "ITC", "LT", "AXISBANK", "KOTAKBANK",
    "SBIN", "HINDUNILVR", "BAJFINANCE", "ASIANPAINT", "MARUTI",
    "M&M", "SUNPHARMA", "TITAN", "ULTRACEMCO", "ONGC",
    "WIPRO", "NTPC", "POWERGRID", "TATAMOTORS", "HCLTECH",
    "JSWSTEEL", "COALINDIA", "BAJAJFINSV", "ADANIENT", "ADANIPORTS",
    "TATASTEEL", "GRASIM", "DRREDDY", "HINDALCO", "BPCL",
    "TECHM", "BRITANNIA", "CIPLA", "EICHERMOT", "NESTLEIND",
    "INDUSINDBK", "DIVISLAB", "TATACONSUM", "HEROMOTOCO", "APOLLOHOSP",
    "BAJAJ-AUTO", "UPL", "SBILIFE", "HDFCLIFE", "LTIM",
)

# Index futures
NFO_INDEX_FUTURES: tuple[str, ...] = ("NIFTY", "BANKNIFTY", "FINNIFTY")


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
        # ── NFO additions ──────────────────────────────────────────────
        # If publish_derivative_callback is set, the adapter ALSO resolves NFO
        # tokens for the configured stock + index futures universe and emits
        # DerivativeTick events alongside MCX ticks (single WS subscription).
        publish_derivative_callback: Optional[Callable[[DerivativeTick], Awaitable[bool]]] = None,
        nfo_stock_universe: Optional[tuple] = None,
        nfo_index_universe: Optional[tuple] = None,
    ):
        self._publish = publish_callback
        self._publish_derivative = publish_derivative_callback
        self._api_key = api_key or os.getenv("ANGEL_ONE_API_KEY", "")
        self._client_code = client_code or os.getenv("ANGEL_ONE_CLIENT_CODE", "")
        self._password = password or os.getenv("ANGEL_ONE_PASSWORD", "")
        self._totp_secret = totp_secret or os.getenv("ANGEL_ONE_TOTP_SECRET", "")
        self._universe = universe or dict(MCX_UNIVERSE)
        self._nfo_stock_universe = tuple(nfo_stock_universe) if nfo_stock_universe is not None else NFO_STOCK_FUTURES
        self._nfo_index_universe = tuple(nfo_index_universe) if nfo_index_universe is not None else NFO_INDEX_FUTURES
        self._scrip_refresh_hours = scrip_refresh_hours
        self._reconnect_delay = reconnect_delay_seconds
        self._max_reconnect_delay = max_reconnect_delay_seconds

        # Resolved on login / token-refresh
        self._jwt_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._feed_token: Optional[str] = None
        # token (str) → metadata dict. Now polymorphic across exchanges:
        #   MCX: {"exch": "MCX", "commodity": str, "symbol": str, "expiry": str, "divisor": float}
        #   NFO: {"exch": "NFO", "underlying": str, "instrument_type": "FUT"|"CE"|"PE",
        #         "contract_symbol": str, "expiry": str, "strike": float|None, "divisor": float}
        self._token_metadata: Dict[str, Dict] = {}

        # WS thread + bridge
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Optional[SmartWebSocketV2] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._main_task: Optional[asyncio.Task] = None
        self._running = False
        self._connected = asyncio.Event()
        self._stats = {"ticks": 0, "publish_ok": 0, "publish_fail": 0}

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
        logger.info(
            f"[{self.source_name}] Adapter started "
            f"(universe={list(self._universe.values())})"
        )

    async def stop(self) -> None:
        self._running = False
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

                # 4) Wait until disconnected (or shutdown)
                while self._running and self._ws_thread and self._ws_thread.is_alive():
                    await asyncio.sleep(1)

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
        if not all([self._jwt_token, self._feed_token]):
            raise RuntimeError(f"SmartAPI login returned incomplete tokens: {data}")
        logger.info(f"[{self.source_name}] SmartAPI login OK ({self._client_code})")

    async def _publish_session(self) -> None:
        """Publish the current session token to Redis (`angelone:session`) so the
        KB service can reuse it for stateless equity LTP lookups. Fail-open."""
        try:
            import json as _json
            import redis.asyncio as _aioredis
            if getattr(self, "_redis", None) is None:
                self._redis = _aioredis.from_url(
                    os.getenv("REDIS_URL", "redis://redis:6379/0"),
                    encoding="utf-8", decode_responses=True,
                )
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
          NFO: nearest-non-expired FUTSTK for each NIFTY-50 name AND
               nearest-non-expired FUTIDX for NIFTY/BANKNIFTY/FINNIFTY.

        Both populate the same self._token_metadata dict, tagged by `exch`
        so `_parse_tick` can route each incoming tick to the correct
        downstream model (CommodityTick vs DerivativeTick).
        """
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        ) as session:
            async with session.get(SCRIP_MASTER_URL) as resp:
                resp.raise_for_status()
                raw = await resp.read()
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

        # ── 2) NFO FUTSTK + FUTIDX (only if a derivative callback is set) ──
        nfo_resolved: List[str] = []
        nfo_missing: List[str] = []
        if self._publish_derivative is not None:
            stock_set = set(self._nfo_stock_universe)
            index_set = set(self._nfo_index_universe)
            stock_candidates: Dict[str, List] = {n: [] for n in stock_set}
            index_candidates: Dict[str, List] = {n: [] for n in index_set}

            for row in scrip_list:
                if not isinstance(row, dict):
                    continue
                if row.get("exch_seg") != "NFO":
                    continue
                instr = row.get("instrumenttype")
                if instr not in ("FUTSTK", "FUTIDX"):
                    continue
                name = row.get("name") or ""
                exp = _parse_expiry(row.get("expiry") or "")
                if exp is None or exp < now_utc:
                    continue
                if instr == "FUTSTK" and name in stock_set:
                    stock_candidates[name].append((exp, row))
                elif instr == "FUTIDX" and name in index_set:
                    index_candidates[name].append((exp, row))

            for name in stock_set:
                rows = stock_candidates.get(name) or []
                if not rows:
                    nfo_missing.append(f"FUTSTK:{name}")
                    continue
                rows.sort(key=lambda r: r[0])
                _exp_dt, near = rows[0]
                token = str(near.get("token") or "").strip()
                if not token:
                    continue
                new_metadata[token] = {
                    "exch": "NFO",
                    "underlying": name,
                    "instrument_type": "FUT",
                    "contract_symbol": near.get("symbol") or "",
                    "expiry": near.get("expiry") or "",
                    "strike": None,
                    "divisor": DEFAULT_PRICE_DIVISOR,
                }
                nfo_resolved.append(f"{name}=FUTSTK")

            for name in index_set:
                rows = index_candidates.get(name) or []
                if not rows:
                    nfo_missing.append(f"FUTIDX:{name}")
                    continue
                rows.sort(key=lambda r: r[0])
                _exp_dt, near = rows[0]
                token = str(near.get("token") or "").strip()
                if not token:
                    continue
                new_metadata[token] = {
                    "exch": "NFO",
                    "underlying": name,
                    "instrument_type": "FUT",
                    "contract_symbol": near.get("symbol") or "",
                    "expiry": near.get("expiry") or "",
                    "strike": None,
                    "divisor": DEFAULT_PRICE_DIVISOR,
                }
                nfo_resolved.append(f"{name}=FUTIDX")

            logger.info(
                f"[{self.source_name}] Resolved {len(nfo_resolved)} NFO futures "
                f"({sum(1 for r in nfo_resolved if 'FUTSTK' in r)} stock + "
                f"{sum(1 for r in nfo_resolved if 'FUTIDX' in r)} index)"
                + (f" | missing: {nfo_missing[:10]}" + ("…" if len(nfo_missing) > 10 else "")
                   if nfo_missing else "")
            )

        self._token_metadata = new_metadata

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

        # Group tokens by exchange — single subscribe() call, multiple
        # exchange entries in the token_list as per SmartAPI docs.
        mcx_tokens = [t for t, m in self._token_metadata.items() if m.get("exch") == "MCX"]
        nfo_tokens = [t for t, m in self._token_metadata.items() if m.get("exch") == "NFO"]

        token_list: List[Dict] = []
        if mcx_tokens:
            token_list.append({"exchangeType": EXCHANGE_TYPE_MCX, "tokens": mcx_tokens})
        if nfo_tokens:
            token_list.append({"exchangeType": EXCHANGE_TYPE_NFO, "tokens": nfo_tokens})

        try:
            assert self._ws is not None, "ws not constructed before on_open"
            # Mode 3 (SnapQuote) is a superset of mode 2 (Quote) — it adds the
            # OI fields we need for F&O. MCX has no real OI but the extra fields
            # are nulled so it's harmless to use SnapQuote across the board.
            self._ws.subscribe(
                "bharat-derivs-1", SUBSCRIPTION_MODE_SNAP_QUOTE, token_list
            )
            logger.info(
                f"[{self.source_name}] Subscribed mode=SNAP_QUOTE to "
                f"{len(mcx_tokens)} MCX + {len(nfo_tokens)} NFO tokens"
            )
            if self._loop:
                self._loop.call_soon_threadsafe(self._connected.set)
        except Exception as e:
            logger.error(f"[{self.source_name}] subscribe failed: {e}", exc_info=True)

    def _on_data(self, wsapp, message) -> None:  # noqa: ARG002
        """
        Bridge SmartWebSocketV2 callback (worker thread) into the asyncio
        loop. We route the parsed model (CommodityTick or DerivativeTick) to
        the correct publish callback based on the token's exchange metadata.
        """
        try:
            parsed = self._parse_tick(message)
            if parsed is None:
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
        Translate a SmartWebSocketV2 message into the correct model:
          • MCX token → CommodityTick (existing path)
          • NFO token → DerivativeTick (new path, with OI fields)
        Returns None for unknown tokens or malformed messages.
        """
        if not isinstance(message, dict):
            return None
        token = str(message.get("token") or "").strip()
        if not token:
            return None
        meta = self._token_metadata.get(token)
        if not meta:
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

        if meta.get("exch") == "NFO":
            # SnapQuote-mode message contains the OI fields. SmartAPI WS docs
            # describe them as integers; the python SDK normalises field names.
            return DerivativeTick(
                timestamp_utc=now_utc,
                exchange_timestamp_ms=exchange_ts,
                symbol=meta["underlying"],
                instrument_type=meta["instrument_type"],
                contract_symbol=meta.get("contract_symbol") or "",
                token=token,
                expiry=meta.get("expiry") or None,
                strike=meta.get("strike"),
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
                open_interest=_intish("open_interest"),
                change_in_oi=_intish("open_interest_change_in_percentage"),
                ingest_latency_ms=latency_ms,
            )

        return None  # unknown exchange tag

    async def _dispatch(self, tick) -> None:
        """Route the parsed tick to the right publish callback."""
        try:
            if isinstance(tick, DerivativeTick):
                if self._publish_derivative is None:
                    return
                ok = await self._publish_derivative(tick)
            else:
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
