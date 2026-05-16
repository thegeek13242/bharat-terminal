"""
Daily FII/DII/Pro/Client participant-wise OI download.

NSE publishes the previous trading day's F&O participant positioning as a
CSV at:
    https://archives.nseindia.com/content/nsccl/fao_participant_oi_DDMMYYYY.csv

CSV header (one of two formats — we handle both):
    Client Type, Future Index Long, Future Index Short,
    Future Stock Long, Future Stock Short,
    Option Index Call Long, Option Index Put Long,
    Option Index Call Short, Option Index Put Short,
    Option Stock Call Long, Option Stock Put Long,
    Option Stock Call Short, Option Stock Put Short,
    Total Long Contracts, Total Short Contracts

We pivot this wide format into the long-form `participant_oi` table:
    (date, client_type, instrument_type, long_contracts, short_contracts)

Why long-form: makes time-series queries trivial — "FII net index-fut
positioning over last 30 days" is a single GROUP BY, not a sum over 4
wide columns.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, timedelta
from typing import List, Tuple

import httpx
from sqlalchemy import text

from bharat_terminal.kb.sync.utils import create_async_session

logger = logging.getLogger(__name__)

NSE_URL_TPL = "https://archives.nseindia.com/content/nsccl/fao_participant_oi_{ddmmyyyy}.csv"

# NSE serves these CSVs without authentication, but the request needs a
# browser-like User-Agent + Referer or it returns 403.
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/csv,application/csv,*/*;q=0.8",
    "Referer": "https://www.nseindia.com/all-reports-derivatives",
}

# Map between wide CSV columns and (instrument_type, side) pairs in our schema.
# Keys are normalised lower-case names; we strip spaces.
CSV_COLUMN_MAP = {
    "futureindexlong":       ("FUTIDX",    "long"),
    "futureindexshort":      ("FUTIDX",    "short"),
    "futurestocklong":       ("FUTSTK",    "long"),
    "futurestockshort":      ("FUTSTK",    "short"),
    "optionindexcalllong":   ("OPTIDX_CE", "long"),
    "optionindexcallshort":  ("OPTIDX_CE", "short"),
    "optionindexputlong":    ("OPTIDX_PE", "long"),
    "optionindexputshort":   ("OPTIDX_PE", "short"),
    "optionstockcalllong":   ("OPTSTK_CE", "long"),
    "optionstockcallshort":  ("OPTSTK_CE", "short"),
    "optionstockputlong":    ("OPTSTK_PE", "long"),
    "optionstockputshort":   ("OPTSTK_PE", "short"),
}


async def fetch_participant_oi(for_date: date) -> List[Tuple[str, str, int, int]]:
    """
    Download + parse one day's participant OI CSV.

    Returns a list of (client_type, instrument_type, long_contracts,
    short_contracts) tuples ready for upsert.
    """
    url = NSE_URL_TPL.format(ddmmyyyy=for_date.strftime("%d%m%Y"))
    async with httpx.AsyncClient(timeout=30, headers=NSE_HEADERS, follow_redirects=True) as client:
        # Warm-up to set session cookie — NSE archives are cookie-protected
        try:
            await client.get("https://www.nseindia.com/", timeout=10)
        except Exception:
            pass
        resp = await client.get(url)
        if resp.status_code == 404:
            logger.info(f"[PARTICIPANT-OI] no CSV for {for_date} (404) — likely non-trading day")
            return []
        resp.raise_for_status()
        text_data = resp.text

    # Skip preamble lines (NSE includes a title row above the header)
    reader = csv.reader(io.StringIO(text_data))
    rows: List[Tuple[str, str, int, int]] = []
    header: List[str] = []
    norm_header: List[str] = []

    for raw_row in reader:
        if not raw_row:
            continue
        if not header:
            # Detect header by presence of "Client Type" column
            normalized = [c.strip().lower().replace(" ", "") for c in raw_row]
            if "clienttype" in normalized:
                header = raw_row
                norm_header = normalized
            continue

        # We've parsed the header; everything below is data
        try:
            client_idx = norm_header.index("clienttype")
        except ValueError:
            continue
        if len(raw_row) <= client_idx:
            continue
        client = raw_row[client_idx].strip().upper()
        if client not in ("CLIENT", "DII", "FII", "PRO"):
            continue

        # Find each instrument-type column we know about, pair up long+short.
        # Long and short are separate columns in the wide CSV.
        per_instr: dict[str, dict[str, int]] = {}
        for col_name_norm, (instr, side) in CSV_COLUMN_MAP.items():
            if col_name_norm not in norm_header:
                continue
            i = norm_header.index(col_name_norm)
            if i >= len(raw_row):
                continue
            cell = raw_row[i].strip().replace(",", "")
            if not cell:
                continue
            try:
                val = int(float(cell))
            except ValueError:
                continue
            bucket = per_instr.setdefault(instr, {})
            bucket[side] = val

        for instr, sides in per_instr.items():
            rows.append((client, instr, sides.get("long", 0), sides.get("short", 0)))

    logger.info(f"[PARTICIPANT-OI] parsed {len(rows)} (client × instrument) rows for {for_date}")
    return rows


async def sync_participant_oi(target_date: date | None = None) -> dict:
    """
    Sync one trading day's participant_oi. Defaults to yesterday — NSE
    publishes the CSV around 18:00 IST the same trading day, but using
    yesterday as default makes the daily 19:00 cron run safe and
    idempotent even if the source is delayed.
    """
    if target_date is None:
        target_date = date.today() - timedelta(days=1)

    rows = await fetch_participant_oi(target_date)
    if not rows:
        return {"date": str(target_date), "rows": 0, "status": "no_data"}

    SessionFactory = create_async_session()
    upserted = 0
    async with SessionFactory() as session:
        for client_type, instrument_type, long_c, short_c in rows:
            await session.execute(
                text("""
                    INSERT INTO participant_oi
                        (date, client_type, instrument_type, long_contracts, short_contracts, source_csv)
                    VALUES (:d, :ct, :it, :l, :s, :src)
                    ON CONFLICT (date, client_type, instrument_type) DO UPDATE
                        SET long_contracts = EXCLUDED.long_contracts,
                            short_contracts = EXCLUDED.short_contracts
                """),
                {
                    "d": target_date,
                    "ct": client_type,
                    "it": instrument_type,
                    "l": long_c,
                    "s": short_c,
                    "src": f"fao_participant_oi_{target_date.strftime('%d%m%Y')}.csv",
                },
            )
            upserted += 1
        await session.commit()

    logger.info(f"[PARTICIPANT-OI] upserted {upserted} rows for {target_date}")
    return {"date": str(target_date), "rows": upserted, "status": "success"}
