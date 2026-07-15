import asyncio
import os
import time
import logging
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import AsyncIterator, Awaitable, Callable, Optional

from bharat_terminal.types import NewsItem
from bharat_terminal.ingestion.pdf_fetch import fetch_pdf_text
from .base import BaseAdapter

IST = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger(__name__)

# Bounded-concurrent PDF fetch within a poll cycle. Sequential fetch made a
# burst of N filings take N x (download+extract) before anything reached
# Kafka. Kept modest — NSE has 403'd this VM before; don't look like a scraper
# hammering attachments.
PDF_FETCH_CONCURRENCY = int(os.getenv("NSE_PDF_FETCH_CONCURRENCY", "4"))


async def gather_pdf_texts(
    attachments: list[Optional[str]],
    fetch: Callable[[str], Awaitable[Optional[str]]],
    limit: int = PDF_FETCH_CONCURRENCY,
) -> list[Optional[str]]:
    """Fetch attachment texts concurrently, at most `limit` in flight.
    Order-preserving; any failure/exception yields None for that slot."""
    sem = asyncio.Semaphore(max(1, limit))

    async def one(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        async with sem:
            return await fetch(url)

    results = await asyncio.gather(*(one(a) for a in attachments), return_exceptions=True)
    return [r if isinstance(r, str) else None for r in results]


class NSEFilingsAdapter(BaseAdapter):
    source_name = "NSE_FILINGS"
    poll_interval_seconds = 60.0
    poll_interval_fast = 15.0   # Tier 1 — near-real-time during market hours
    poll_interval_slow = 120.0
    rate_limit_per_minute = 10

    # ?index=equities is required — without it the API returns an error string, not JSON
    ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/home-corporate-announcements?index=equities"
    BOARD_MEETINGS_URL = "https://www.nseindia.com/api/home-board-meetings?index=equities"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    }

    async def fetch(self) -> AsyncIterator[NewsItem]:
        session = await self.get_session()

        for url, category, sym_field, ts_field, ts_fmt in [
            (
                self.ANNOUNCEMENTS_URL,
                "CORPORATE_ANNOUNCEMENT",
                "symbol",
                "an_dt",
                "%d-%b-%Y %H:%M:%S",
            ),
            (
                self.BOARD_MEETINGS_URL,
                "BOARD_MEETING",
                "bm_symbol",
                "bm_date",
                "%d-%b-%Y",
            ),
        ]:
            ingest_start = time.time()
            try:
                async with session.get(url, headers=self.HEADERS) as resp:
                    resp.raise_for_status()
                    raw = await resp.text()
                    if not raw or not raw.strip().startswith(("{", "[")):
                        logger.warning(f"[NSE] Non-JSON response from {url}: {raw[:100]}")
                        self._record_failure()
                        continue

                    data = json.loads(raw)
                    items = data if isinstance(data, list) else data.get("data", [])

                    # Pass 1: parse + dedupe quickly, collecting the new items.
                    pending: list[dict] = []
                    for item in (items or [])[:20]:
                        symbol = (
                            item.get(sym_field)
                            or item.get("symbol")
                            or item.get("sm_symbol")
                            or ""
                        )
                        uid = f"NSE_{category}_{symbol}_{item.get(ts_field, '')}"
                        if self.seen_contains(uid):
                            continue
                        await self.seen_add(uid)

                        headline = (
                            item.get("attchmntText")
                            or item.get("bm_purpose")
                            or item.get("desc")
                            or item.get("subject")
                        )
                        if not headline or not headline.strip():
                            continue

                        ts_str = item.get(ts_field, "")
                        now = datetime.now(timezone.utc)
                        try:
                            # NSE publishes timestamps in IST. Stamping them as
                            # UTC made every filing look 5.5h in the future and
                            # the clamp below then erased the real publication
                            # time. Parse as IST, convert to UTC.
                            ts = datetime.strptime(ts_str.strip(), ts_fmt).replace(
                                tzinfo=IST
                            ).astimezone(timezone.utc)
                        except Exception:
                            ts = now

                        # BOARD_MEETING uses bm_date — the *scheduled* meeting date,
                        # which is in the future — as the only date field. A news
                        # item's timestamp_utc must be when it was published, never
                        # the future, so clamp forward-dated timestamps to ingestion
                        # time (the announcement was seen now).
                        if ts > now:
                            logger.debug(
                                f"[NSE] Clamping future timestamp {ts.isoformat()} "
                                f"-> now for {category} {symbol}"
                            )
                            ts = now

                        pending.append({
                            "uid": uid, "symbol": symbol, "headline": headline,
                            "ts": ts, "item": item,
                            "attachment": item.get("attchmntFile"),
                        })

                    if not pending:
                        continue

                    # Pass 2: the filing's real content lives in the PDF
                    # attachment (attchmntFile); the JSON only carries truncated
                    # attchmntText. Fetch attachments CONCURRENTLY (bounded) —
                    # sequential fetch serialized a burst of filings for
                    # minutes. Fail-soft per item: fall back to the truncated
                    # text / structured fields.
                    pdf_texts = await gather_pdf_texts(
                        [p["attachment"] for p in pending],
                        lambda url: fetch_pdf_text(url, session),
                    )

                    # Pass 3: yield in original order.
                    for p, pdf_text in zip(pending, pdf_texts):
                        item = p["item"]
                        body = (
                            pdf_text
                            or item.get("bm_desc")
                            or item.get("details")
                            or item.get("attchmntText")
                        )
                        ingest_latency = (time.time() - ingest_start) * 1000

                        yield NewsItem(
                            id=p["uid"],
                            source=self.source_name,
                            timestamp_utc=p["ts"],
                            headline=p["headline"],
                            body=body,
                            # Cite the actual filing PDF (attchmntFile). The old
                            # /company-info/corporate-announcements?symbol= path
                            # 404s (broken). Fall back to the company page (valid,
                            # HTTP 200) only when a filing carries no attachment.
                            url=(
                                (p["attachment"] or "").strip()
                                or f"https://www.nseindia.com/get-quotes/equity?symbol={p['symbol']}"
                            ),
                            raw_html=None,
                            ingest_latency_ms=ingest_latency,
                            category=category,
                            symbols_mentioned=[p["symbol"]] if p["symbol"] else [],
                            tier="tier1",
                        )

            except Exception as e:
                logger.error(f"[NSE] Error fetching {url}: {e}")
                self._record_failure()
