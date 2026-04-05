import time
import logging
from datetime import datetime, timezone
from typing import AsyncIterator
from bharat_terminal.types import NewsItem
from .base import BaseAdapter

logger = logging.getLogger(__name__)


class BSEFilingsAdapter(BaseAdapter):
    source_name = "BSE_FILINGS"
    poll_interval_seconds = 30.0
    rate_limit_per_minute = 10

    BASE_URL = "https://api.bseindia.com/BseIndiaAPI/api"
    ANNOUNCEMENTS_URL = (
        f"{BASE_URL}/AnnGetData/w"
        "?strCat=-1&strPrevDate=&strScrip=&strSearch=P&strToDate=&strType=C&subcategory=-1"
    )

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.bseindia.com",
        "Referer": "https://www.bseindia.com/",
    }

    def __init__(self):
        super().__init__()
        self._seen_ids: set = set()

    async def fetch(self) -> AsyncIterator[NewsItem]:
        session = await self.get_session()
        ingest_start = time.time()

        try:
            async with session.get(self.ANNOUNCEMENTS_URL, headers=self.HEADERS) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)

                items = data.get("Table", data) if isinstance(data, dict) else data

                for item in (items or [])[:20]:
                    item_id = str(
                        item.get("DT_TM")
                        or item.get("NEWSID")
                        or item.get("FILENO")
                        or ""
                    )
                    scrip = item.get("SCRIP_CD") or item.get("scrip_cd") or ""
                    uid = f"BSE_{scrip}_{item_id}"

                    if uid in self._seen_ids:
                        continue
                    self._seen_ids.add(uid)

                    headline = (
                        item.get("HEADLINE")
                        or item.get("SUBCATNAME")
                        or item.get("CATEGORYNAME")
                        or "BSE Corporate Filing"
                    )

                    ts_str = item.get("DT_TM") or item.get("NEWS_DT") or ""
                    try:
                        if ts_str:
                            # BSE format: "20240101T120000+0530"
                            ts = datetime.fromisoformat(ts_str.replace("+0530", "+05:30"))
                            ts = ts.astimezone(timezone.utc)
                        else:
                            ts = datetime.now(timezone.utc)
                    except Exception:
                        ts = datetime.now(timezone.utc)

                    ingest_latency = (time.time() - ingest_start) * 1000
                    url = (
                        f"https://www.bseindia.com/stock-share-price/"
                        f"announcements/?scripcd={scrip}"
                    )

                    yield NewsItem(
                        id=uid,
                        source=self.source_name,
                        timestamp_utc=ts,
                        headline=headline,
                        body=item.get("ATTACHMENTNAME"),
                        url=url,
                        raw_html=None,
                        ingest_latency_ms=ingest_latency,
                        category="CORPORATE_FILING",
                        symbols_mentioned=[scrip] if scrip else [],
                    )

        except Exception as e:
            logger.error(f"[BSE] Error fetching announcements: {e}")
            self._record_failure()
