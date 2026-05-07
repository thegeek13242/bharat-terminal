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
except ImportError:  # pragma: no cover
    SmartConnect = None  # type: ignore
    SmartWebSocketV2 = None  # type: ignore

from bharat_terminal.types import CommodityTick

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

# SmartWebSocketV2 constants
EXCHANGE_TYPE_MCX = 5
SUBSCRIPTION_MODE_QUOTE = 2   # gives OHLC + LTP + volume + buy/sell qty
DEFAULT_PRICE_DIVISOR = 100.0  # MCX prices come in paise


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

        # Resolved on login / token-refresh
        self._jwt_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._feed_token: Optional[str] = None
        # token (str) → {"commodity": str, "symbol": str, "expiry": str, "divisor": float}
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
            raise RuntimeError(
                "smartapi-python is not installed — add it to requirements/ingestion.txt"
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

    # ── Scrip master → near-month token resolution ───────────────────

    async def _refresh_scrip_universe(self) -> None:
        """
        Fetch the OpenAPI scrip master and pick the nearest non-expired FUTCOM
        contract for each commodity in our universe.
        """
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        ) as session:
            async with session.get(SCRIP_MASTER_URL) as resp:
                resp.raise_for_status()
                # The file is large (~10MB) — stream-parse via bytes
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
        # name → (expiry_dt, scrip_row)
        candidates: Dict[str, List] = {n: [] for n in self._universe}

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
            candidates[name].append((exp, row))

        new_metadata: Dict[str, Dict] = {}
        resolved: List[str] = []
        missing: List[str] = []
        for name, canonical in self._universe.items():
            rows = candidates.get(name) or []
            if not rows:
                missing.append(canonical)
                continue
            rows.sort(key=lambda r: r[0])  # earliest expiry first
            expiry_dt, near = rows[0]
            token = str(near.get("token") or "").strip()
            if not token:
                missing.append(canonical)
                continue
            new_metadata[token] = {
                "commodity": canonical,
                "symbol": near.get("symbol") or "",
                "expiry": near.get("expiry") or "",
                "divisor": DEFAULT_PRICE_DIVISOR,
            }
            resolved.append(f"{canonical}={near.get('symbol')}")

        self._token_metadata = new_metadata
        logger.info(
            f"[{self.source_name}] Resolved {len(resolved)} MCX contracts: "
            f"{', '.join(resolved)}"
            + (f" | missing: {missing}" if missing else "")
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
        if not self._token_metadata:
            logger.warning(f"[{self.source_name}] on_open with no tokens — closing")
            try:
                wsapp.close_connection()
            except Exception:
                pass
            return
        token_list = [
            {
                "exchangeType": EXCHANGE_TYPE_MCX,
                "tokens": list(self._token_metadata.keys()),
            }
        ]
        try:
            wsapp.subscribe(
                "bharat-mcx-1", SUBSCRIPTION_MODE_QUOTE, token_list
            )
            logger.info(
                f"[{self.source_name}] Subscribed mode=QUOTE to "
                f"{len(self._token_metadata)} MCX tokens"
            )
            if self._loop:
                self._loop.call_soon_threadsafe(self._connected.set)
        except Exception as e:
            logger.error(f"[{self.source_name}] subscribe failed: {e}")

    def _on_data(self, wsapp, message) -> None:  # noqa: ARG002
        """
        Bridge SmartWebSocketV2 callback (worker thread) into the asyncio
        loop. The library hands us a dict with parsed binary fields.
        """
        try:
            tick = self._parse_tick(message)
            if tick is None:
                return
            self._stats["ticks"] += 1
            if self._loop and self._running:
                asyncio.run_coroutine_threadsafe(
                    self._dispatch(tick), self._loop
                )
        except Exception as e:
            logger.debug(f"[{self.source_name}] tick parse error: {e}")

    def _on_error(self, wsapp, error) -> None:  # noqa: ARG002
        logger.error(f"[{self.source_name}] WS error: {error}")

    def _on_close(self, wsapp) -> None:  # noqa: ARG002
        logger.warning(f"[{self.source_name}] WS closed")

    # ── Tick parsing + publish ───────────────────────────────────────────

    def _parse_tick(self, message) -> Optional[CommodityTick]:
        """Translate SmartWebSocketV2 message into our CommodityTick model."""
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
        return CommodityTick(
            timestamp_utc=datetime.now(timezone.utc),
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
            ingest_latency_ms=(
                (time.time() * 1000) - exchange_ts if exchange_ts else 0.0
            ),
        )

    async def _dispatch(self, tick: CommodityTick) -> None:
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
