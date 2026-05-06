"""
Polymarket prediction-market adapter.

Streams probability observations on India-relevant binary markets
(RBI/Fed rate decisions, oil, geopolitics, tariffs, major elections, crypto)
to the `raw.sentiment.in` Kafka topic as a crowd-sentiment signal for the
analysis pipeline.

Implementation notes
--------------------
• Transport: REST polling against Gamma API (https://gamma-api.polymarket.com)
  — free, no auth, returns all active markets with prices. CLOB WebSocket can
  be added in a future pass for sub-second updates.
• Market discovery: poll every 60s (market hours) / 300s (off-hours),
  filter to the curated keyword list, drop markets with volume24hr < $1K.
• Delta tracking: two Redis keys per market
      poly:prob:last:{id}   — most recent observation (for prev_probability)
      poly:prob:1h:{id}     — 1h rolling baseline (SET NX + 1h TTL)
  The NX+TTL trick gives a self-refreshing 1h window without a ZSET scan.
• Noise filter: emit a SentimentItem only if |p - prev_p| ≥ 0.01 (or first
  observation), so idle markets don't flood the topic.
• This adapter does NOT inherit from BaseAdapter — its semantics differ
  (market discovery + per-market delta tracking), but it reuses the same
  redis_client / publish_callback injection pattern.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, time as dtime, timezone, timedelta
from functools import lru_cache
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiohttp

try:
    import redis.asyncio as aioredis  # noqa: F401 (type hint only)
except ImportError:  # pragma: no cover
    aioredis = None  # type: ignore

from bharat_terminal.types import SentimentItem

logger = logging.getLogger(__name__)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

# Strict universe: only Fed monetary policy and commodity prices.
#
# Indian equity intelligence is highly sensitive to two macro drivers Polymarket
# actually has liquid markets on:
#   • Fed rate decisions  → FII flow direction
#   • Commodity prices    → input costs (oil = 85% imported), gold (consumer
#                          demand + jewellery stocks), industrial metals
#                          (steel/copper/aluminium for capex-cycle stocks).
#
# Everything else (geopolitics, US elections, crypto, generic recession markets)
# is too noisy and the signal-to-action mapping is too fuzzy. We keep the
# adapter ruthlessly narrow on purpose.
#
# Matched at WORD BOUNDARIES against question + slug + tags (see find_theme).
RELEVANT_KEYWORDS = (
    # ── US monetary policy (FII flow driver) ────────────────────────────────
    "fed", "fomc", "powell", "federal reserve",
    "rate cut", "rate hike", "rate decision", "interest rate",
    "basis points", "bps cut", "bps hike",

    # ── Commodities — oil ───────────────────────────────────────────────────
    "oil", "crude", "brent", "wti", "opec",

    # ── Commodities — precious metals ───────────────────────────────────────
    "gold", "silver",

    # ── Commodities — industrial metals ─────────────────────────────────────
    "copper", "aluminium", "aluminum", "steel", "iron ore",

    # ── Commodities — energy (ex-oil) ───────────────────────────────────────
    "natural gas", "nat gas",
)

# Redis keys
REDIS_LAST_KEY = "poly:prob:last:"
REDIS_BASELINE_KEY = "poly:prob:1h:"
LAST_TTL_SECONDS = 24 * 3600
BASELINE_TTL_SECONDS = 3600  # 1h rolling baseline

# Adaptive polling — same IST market window as news adapters
IST_OFFSET = timedelta(hours=5, minutes=30)
MARKET_OPEN_IST = dtime(8, 0)
MARKET_CLOSE_IST = dtime(16, 0)


def _is_market_hours() -> bool:
    now_ist = datetime.now(timezone.utc) + IST_OFFSET
    if now_ist.weekday() >= 5:
        return False
    return MARKET_OPEN_IST <= now_ist.time() <= MARKET_CLOSE_IST


class PolymarketAdapter:
    """Async adapter for Polymarket Gamma API → raw.sentiment.in."""

    source_name = "POLYMARKET"

    def __init__(
        self,
        publish_callback: Callable[[SentimentItem], Awaitable[bool]],
        redis_client: Optional["aioredis.Redis"] = None,
        poll_interval_fast: float = 60.0,
        poll_interval_slow: float = 300.0,
        min_volume_24h: float = 1_000_000.0,           # event-level $1M floor
        min_child_volume_24h: float = 25_000.0,        # per-child secondary floor
        min_probability_delta: float = 0.01,
        max_events_per_cycle: int = 50,
        max_markets_per_cycle: int = 200,
    ):
        self._publish = publish_callback
        self._redis = redis_client
        self._poll_fast = poll_interval_fast
        self._poll_slow = poll_interval_slow
        self._min_volume = min_volume_24h
        self._min_child_volume = min_child_volume_24h
        self._min_delta = min_probability_delta
        self._max_events = max_events_per_cycle
        self._max_markets = max_markets_per_cycle
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20, connect=10),
            headers={"User-Agent": "BharatTerminal/1.0 (sentiment-layer)"},
        )
        self._running = True
        self._task = asyncio.create_task(self._run(), name="polymarket-run")
        logger.info(
            f"[POLYMARKET] Adapter started "
            f"(fast={self._poll_fast}s, slow={self._poll_slow}s, "
            f"event_min_vol=${self._min_volume:,.0f}, "
            f"child_min_vol=${self._min_child_volume:,.0f}, "
            f"min_delta={self._min_delta})"
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("[POLYMARKET] Adapter stopped")

    # ── Main loop ────────────────────────────────────────────────────────

    async def _run(self) -> None:
        while self._running:
            cycle_start = time.time()
            try:
                events = await self._fetch_relevant_events()
                emitted = 0
                children_seen = 0
                for ev in events[: self._max_events]:
                    children = self._extract_event_markets(ev)
                    children_seen += len(children)
                    for m in children[: self._max_markets]:
                        try:
                            item = await self._process_market(m, ev, cycle_start)
                            if item:
                                await self._publish(item)
                                emitted += 1
                        except Exception as e:
                            logger.debug(
                                f"[POLYMARKET] child {m.get('id')} of event "
                                f"{ev.get('id')} error: {e}"
                            )
                logger.info(
                    f"[POLYMARKET] Cycle: {len(events)} events / "
                    f"{children_seen} children scanned, "
                    f"{emitted} emitted to sentiment topic"
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"[POLYMARKET] Cycle error: {e}", exc_info=True
                )

            interval = self._poll_fast if _is_market_hours() else self._poll_slow
            elapsed = time.time() - cycle_start
            await asyncio.sleep(max(interval - elapsed, 5.0))

    # ── Fetch + filter (event-level) ─────────────────────────────────────

    async def _fetch_relevant_events(self) -> List[Dict[str, Any]]:
        """
        Query Gamma /events, filter to in-universe events with aggregate
        volume24hr ≥ self._min_volume.

        Polymarket organises price-ladder questions (e.g. "What price will
        WTI hit in May?") as a single EVENT with many child markets ("hit
        $80?", "hit $100?", …). Each child has only a fraction of the
        liquidity. Event-level volume is the right unit for "is this market
        worth tracking?".
        """
        assert self._session is not None
        params = {
            "active": "true",
            "closed": "false",
            "limit": "200",
            "order": "volume24hr",
            "ascending": "false",
        }
        async with self._session.get(GAMMA_EVENTS_URL, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()

        if not isinstance(data, list):
            logger.warning(
                f"[POLYMARKET] Gamma /events returned non-list: {type(data).__name__}"
            )
            return []

        relevant: List[Dict[str, Any]] = []
        for ev in data:
            if not isinstance(ev, dict):
                continue
            if ev.get("closed") or not ev.get("active"):
                continue

            title = (ev.get("title") or "").lower()
            description = (ev.get("description") or "").lower()
            slug = (ev.get("slug") or "").lower()
            ticker = (ev.get("ticker") or "").lower()

            tag_texts: List[str] = []
            for t in ev.get("tags") or []:
                if isinstance(t, str):
                    tag_texts.append(t.lower())
                elif isinstance(t, dict):
                    lbl = t.get("label") or t.get("slug") or ""
                    if lbl:
                        tag_texts.append(str(lbl).lower())

            haystack = f"{title} {description} {slug} {ticker} {' '.join(tag_texts)}"
            if not _haystack_matches(haystack):
                continue

            try:
                vol = float(ev.get("volume24hr") or 0)
            except (TypeError, ValueError):
                vol = 0.0
            if vol < self._min_volume:
                continue

            relevant.append(ev)

        return relevant

    @staticmethod
    def _extract_event_markets(ev: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Get the list of child markets from an event.

        Gamma sometimes returns `markets` as a JSON-encoded string, sometimes
        as a real list. Be defensive.
        """
        raw = ev.get("markets")
        if isinstance(raw, list):
            return [m for m in raw if isinstance(m, dict)]
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return [m for m in parsed if isinstance(m, dict)]
            except Exception:
                return []
        return []

    # ── Per-market processing ────────────────────────────────────────────

    async def _process_market(
        self,
        m: Dict[str, Any],
        ev: Dict[str, Any],
        cycle_start: float,
    ) -> Optional[SentimentItem]:
        market_id = str(m.get("id") or m.get("conditionId") or "").strip()
        if not market_id:
            return None

        question = (m.get("question") or "").strip()
        if not question:
            return None

        # Skip closed children inside an otherwise-active event
        if m.get("closed"):
            return None

        # Per-child volume floor — many price-ladder children are dead branches
        # ($1-2K of volume on a far-OTM tail). Skip those.
        child_vol = _safe_float(m.get("volume24hr")) or 0.0
        if child_vol < self._min_child_volume:
            return None

        probability = self._extract_probability(m)
        if probability is None:
            return None

        volume_24h = child_vol if child_vol else None
        liquidity = _safe_float(m.get("liquidity"))

        end_dt: Optional[datetime] = None
        end_raw = m.get("endDate") or m.get("endDateIso") or ev.get("endDate")
        if isinstance(end_raw, str) and end_raw:
            try:
                end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            except Exception:
                end_dt = None

        # Combine event tags + child tags; dedupe preserving order
        tags: List[str] = []
        for source in (ev.get("tags"), m.get("tags")):
            for t in source or []:
                if isinstance(t, str):
                    tags.append(t)
                elif isinstance(t, dict):
                    lbl = t.get("label") or t.get("slug")
                    if lbl:
                        tags.append(str(lbl))
        seen: set[str] = set()
        tags = [t for t in tags if not (t in seen or seen.add(t))]

        # Prefer event slug for the deep-link — that's the umbrella URL
        # (e.g. /event/what-price-will-wti-hit-in-may-2026), which surfaces
        # all the child markets together. Fall back to child slug.
        ev_slug = ev.get("slug") or m.get("slug") or market_id
        market_url = f"https://polymarket.com/event/{ev_slug}"

        # Prefix the question with the event title so the user gets context
        # ("WTI May 2026 / Will WTI hit $80?")
        ev_title = (ev.get("title") or "").strip()
        full_question = f"{ev_title} / {question}" if ev_title and ev_title not in question else question

        prev_prob, delta_1h = await self._track_delta(market_id, probability)

        # Noise filter: if we already have a recent observation and it hasn't
        # moved meaningfully, skip. Always emit first observation per market.
        if prev_prob is not None and abs(probability - prev_prob) < self._min_delta:
            return None

        return SentimentItem(
            source=self.source_name,
            timestamp_utc=datetime.now(timezone.utc),
            market_id=market_id,
            question=full_question,
            probability=probability,
            prev_probability=prev_prob,
            delta_1h=delta_1h,
            volume_24h=volume_24h,
            liquidity=liquidity,
            end_date=end_dt,
            tags=tags,
            market_url=market_url,
            ingest_latency_ms=(time.time() - cycle_start) * 1000,
        )

    @staticmethod
    def _extract_probability(m: Dict[str, Any]) -> Optional[float]:
        """
        Extract the YES probability from a Gamma market payload.

        Gamma returns outcomes and outcomePrices as JSON-encoded strings:
          outcomes      = '["Yes", "No"]'
          outcomePrices = '["0.72", "0.28"]'

        We find the "Yes" index and return its price, falling back to
        outcomePrices[0] or lastTradePrice.
        """
        prices = _decode_json_list(m.get("outcomePrices"))
        outcomes = _decode_json_list(m.get("outcomes"))

        if prices and outcomes and len(prices) == len(outcomes):
            # Find "Yes" outcome
            for i, o in enumerate(outcomes):
                if isinstance(o, str) and o.strip().lower() == "yes":
                    p = _safe_float(prices[i])
                    if p is not None:
                        return _clamp01(p)
            # Fallback: first outcome price
            p = _safe_float(prices[0])
            if p is not None:
                return _clamp01(p)

        # Last resort: lastTradePrice
        p = _safe_float(m.get("lastTradePrice"))
        if p is not None:
            return _clamp01(p)

        return None

    # ── Redis delta tracking ─────────────────────────────────────────────

    async def _track_delta(
        self, market_id: str, prob: float
    ) -> tuple[Optional[float], Optional[float]]:
        """
        Returns (prev_probability, delta_1h).

        Keys:
          poly:prob:last:{id}  — most recent observation (24h TTL)
          poly:prob:1h:{id}    — baseline from ≤1h ago (NX-set, 1h TTL)

        Re-setting the baseline with NX means it only gets refreshed after the
        TTL expires — so it's always "probability as of when the current 1h
        window started".
        """
        if self._redis is None:
            return None, None

        try:
            last_key = f"{REDIS_LAST_KEY}{market_id}"
            base_key = f"{REDIS_BASELINE_KEY}{market_id}"

            last_raw = await self._redis.get(last_key)
            base_raw = await self._redis.get(base_key)

            prev_prob = _safe_float(last_raw)
            base_prob = _safe_float(base_raw)

            delta_1h = (prob - base_prob) if base_prob is not None else None

            # Write-through current observation
            await self._redis.set(last_key, f"{prob:.6f}", ex=LAST_TTL_SECONDS)
            # Only set baseline if not already present → rolling 1h window
            await self._redis.set(
                base_key, f"{prob:.6f}", ex=BASELINE_TTL_SECONDS, nx=True
            )

            return prev_prob, delta_1h
        except Exception as e:
            logger.debug(f"[POLYMARKET] Redis delta-track error: {e}")
            return None, None


# ── Helpers ──────────────────────────────────────────────────────────────


@lru_cache(maxsize=128)
def _kw_pattern(kw: str) -> re.Pattern:
    """Word-boundary regex for a keyword — prevents 'eth' matching 'something'."""
    return re.compile(rf"\b{re.escape(kw.strip().lower())}\b", re.IGNORECASE)


def _haystack_matches(haystack: str) -> bool:
    """True if any RELEVANT_KEYWORDS hits the haystack at a word boundary."""
    h = haystack.lower()
    return any(_kw_pattern(kw).search(h) for kw in RELEVANT_KEYWORDS)


def _decode_json_list(raw: Any) -> Optional[list]:
    """Decode a value that may be a JSON-encoded string or already a list."""
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, list) else None
        except Exception:
            return None
    return None


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        if isinstance(v, bytes):
            v = v.decode("utf-8")
        return float(v)
    except (TypeError, ValueError):
        return None


def _clamp01(p: float) -> float:
    return max(0.0, min(1.0, p))
