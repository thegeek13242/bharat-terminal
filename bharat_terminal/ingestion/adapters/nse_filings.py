import time
import logging
from datetime import datetime, timezone
from typing import AsyncIterator
from bharat_terminal.types import NewsItem
from .base import BaseAdapter

logger = logging.getLogger(__name__)


class NSEFilingsAdapter(BaseAdapter):
    source_name = "NSE_FILINGS"
    poll_interval_seconds = 30.0
    rate_limit_per_minute = 10

    # NSE API endpoints (official, no auth needed for public data)
    BASE_URL = "https://www.nseindia.com"
    ANNOUNCEMENTS_URL = f"{BASE_URL}/api/home-corporate-announcements"
    BOARD_MEETINGS_URL = f"{BASE_URL}/api/home-board-meetings"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    }

    def __init__(self):
        super().__init__()
        self._seen_ids: set = set()
        self._cookies: dict = {}

    async def _refresh_cookies(self):
        """NSE requires session cookies from homepage before API calls."""
        session = await self.get_session()
        try:
            async with session.get(self.BASE_URL, headers=self.HEADERS) as resp:
                self._cookies = {k: v.value for k, v in resp.cookies.items()}
                logger.debug(f"[NSE] Refreshed cookies: {list(self._cookies.keys())}")
        except Exception as e:
            logger.warning(f"[NSE] Cookie refresh failed: {e}")

    async def fetch(self) -> AsyncIterator[NewsItem]:
        if not self._cookies:
            await self._refresh_cookies()

        session = await self.get_session()

        for url, category in [
            (self.ANNOUNCEMENTS_URL, "CORPORATE_ANNOUNCEMENT"),
            (self.BOARD_MEETINGS_URL, "BOARD_MEETING"),
        ]:
            ingest_start = time.time()
            try:
                async with session.get(
                    url, headers=self.HEADERS, cookies=self._cookies
                ) as resp:
                    if resp.status == 403:
                        logger.warning(f"[NSE] 403 on {url}, refreshing cookies")
                        await self._refresh_cookies()
                        continue
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)

                    items = data if isinstance(data, list) else data.get("data", [])

                    for item in (items or [])[:20]:  # Process latest 20
                        item_id = item.get("id") or item.get("seqno") or ""
                        uid = f"NSE_{item_id}"
                        if uid in self._seen_ids:
                            continue
                        self._seen_ids.add(uid)

                        # NSE announcement fields vary; try common field names
                        headline = (
                            item.get("attchmntText")
                            or item.get("desc")
                            or item.get("subject")
                            or item.get("purpose")
                            or "NSE Corporate Announcement"
                        )
                        symbol = item.get("symbol") or item.get("sm_symbol") or ""

                        ts_str = item.get("exchdisstime") or item.get("bm_date") or ""
                        try:
                            if ts_str:
                                ts = datetime.strptime(ts_str[:19], "%d-%b-%Y %H:%M:%S")
                                ts = ts.replace(tzinfo=timezone.utc)
                            else:
                                ts = datetime.now(timezone.utc)
                        except Exception:
                            ts = datetime.now(timezone.utc)

                        ingest_latency = (time.time() - ingest_start) * 1000

                        yield NewsItem(
                            id=uid,
                            source=self.source_name,
                            timestamp_utc=ts,
                            headline=headline,
                            body=item.get("attchmntFile") or item.get("details"),
                            url=(
                                f"https://www.nseindia.com/company-info/"
                                f"corporate-announcements?symbol={symbol}"
                            ),
                            raw_html=None,
                            ingest_latency_ms=ingest_latency,
                            category=category,
                            symbols_mentioned=[symbol] if symbol else [],
                        )

            except Exception as e:
                logger.error(f"[NSE] Error fetching {url}: {e}")
                self._record_failure()
                return
