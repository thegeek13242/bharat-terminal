"""Window recovery: concall transcripts announced in a past date range.

The AWS-migration gap (2026-08-09..11) left news_items empty for those days,
and every transcript discovery path (the watcher and backfill_nse) reads from
news_items — so transcripts ANNOUNCED in the hole are invisible to them. This
driver discovers directly from the NSE per-symbol dated announcements API
(transcript_sync._nse_announcements) for a symbol cohort, then fetches/stores
through the module's own primitives.

Rows get a synthesized news_id ('NSE_RECOVERY_<symbol>_<epoch>') since no
news_items row exists; ON CONFLICT (news_id) DO NOTHING plus a pdf_url
pre-check make re-runs idempotent. Structuring lands as pending
(availability-first) — the watcher's cycle annotates later.

Run (kb image; cohort = symbols with a result_event in --cohort-days before
the window end, the population that posts concall transcripts):
  docker compose run --rm --no-deps -v $PWD/scripts:/app/scripts kb-migrate \
    python -m scripts.backfill_transcripts_window 2026-08-09 2026-08-11
"""
import argparse
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import text

from bharat_terminal.kb.sync.transcript_sync import (
    _nse_announcements, _fetch_pdf, extract_full_text, derive_period,
    MIN_CHARS_PER_PAGE, NSE_API_DELAY_S, POLITENESS_DELAY_S,
)
from bharat_terminal.kb.sync.utils import create_async_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_transcripts_window")

IST = timezone(timedelta(hours=5, minutes=30))


def _announced_at(a: dict):
    for key, fmt in (("sort_date", "%Y-%m-%d %H:%M:%S"),
                     ("an_dt", "%d-%b-%Y %H:%M:%S")):
        raw = (a.get(key) or "").strip()
        if raw:
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=IST)
            except ValueError:
                continue
    return None


def _transcript_pdf(a: dict):
    blob = f"{a.get('attchmntText') or ''} {a.get('desc') or ''}".lower()
    f = (a.get("attchmntFile") or "").strip()
    if "transcript" in blob and ".pdf" in f.lower():
        return f
    return None


async def main(d_from: date, d_to: date, cohort_days: int) -> None:
    SessionFactory = create_async_session()
    async with SessionFactory() as db:
        symbols = [r[0] for r in (await db.execute(text("""
            SELECT DISTINCT symbol FROM result_events
            WHERE result_date BETWEEN :a AND :b ORDER BY symbol
        """), {"a": d_from - timedelta(days=cohort_days), "b": d_to})).all()]
        before = (await db.execute(text(
            "SELECT count(*) FROM concall_transcripts"))).scalar_one()
    logger.info("cohort: %d symbols; concall_transcripts before: %d", len(symbols), before)

    api_from, api_to = d_from.strftime("%d-%m-%Y"), d_to.strftime("%d-%m-%Y")
    stats = {"found": 0, "stored_ok": 0, "needs_ocr": 0, "fetch_failed": 0,
             "extract_failed": 0, "already_present": 0, "api_failed": 0}

    async with httpx.AsyncClient() as http:
        for i, symbol in enumerate(symbols):
            anns = await _nse_announcements(http, symbol, api_from, api_to)
            await asyncio.sleep(NSE_API_DELAY_S)
            if anns is None:
                stats["api_failed"] += 1
                continue
            for a in anns:
                pdf_url = _transcript_pdf(a)
                if not pdf_url:
                    continue
                stats["found"] += 1
                ann_at = _announced_at(a) or datetime.combine(
                    d_to, datetime.min.time(), tzinfo=IST)

                async with SessionFactory() as db:
                    dup = (await db.execute(text(
                        "SELECT 1 FROM concall_transcripts WHERE pdf_url = :u LIMIT 1"),
                        {"u": pdf_url})).first()
                if dup:
                    stats["already_present"] += 1
                    continue

                data, status = await _fetch_pdf(http, pdf_url)
                full_text = None
                pages = chars = None
                if data is not None:
                    try:
                        full_text, pages, _offsets = await asyncio.to_thread(
                            extract_full_text, data)
                    except Exception as e:
                        logger.warning("extract error %s: %s", symbol, e)
                    status = "ok" if full_text else (status or "extract_failed")
                    if (status == "ok" and pages
                            and len(full_text) / pages < MIN_CHARS_PER_PAGE):
                        status = "needs_ocr"
                    chars = len(full_text) if full_text else None

                news_id = f"NSE_RECOVERY_{symbol}_{int(ann_at.timestamp())}"
                headline = " ".join((a.get("attchmntText") or a.get("desc") or
                                     "Transcript").split())[:500]
                async with SessionFactory() as db:
                    await db.execute(text("""
                        INSERT INTO concall_transcripts
                            (symbol, news_id, source, headline, announced_at,
                             pdf_url, pdf_bytes, page_count, char_count,
                             full_text, parse_status, period_label)
                        VALUES (:sym, :nid, 'NSE_FILINGS', :head, :ann, :url,
                                :bytes, :pages, :chars, :text, :status, :period)
                        ON CONFLICT (news_id) DO NOTHING
                    """), {"sym": symbol, "nid": news_id, "head": headline,
                           "ann": ann_at, "url": pdf_url,
                           "bytes": len(data) if data else None,
                           "pages": pages, "chars": chars, "text": full_text,
                           "status": status,
                           "period": derive_period(full_text, ann_at)})
                    await db.commit()

                key = "stored_ok" if status == "ok" else (
                    status if status in stats else "fetch_failed")
                stats[key] = stats.get(key, 0) + 1
                if status == "ok":
                    logger.info("%s: %sp / %s chars", symbol, pages,
                                f"{chars:,}" if chars else "?")
                await asyncio.sleep(POLITENESS_DELAY_S)
            if (i + 1) % 50 == 0:
                logger.info("progress: %d/%d symbols, stats=%s", i + 1, len(symbols), stats)

    async with SessionFactory() as db:
        after = (await db.execute(text(
            "SELECT count(*) FROM concall_transcripts"))).scalar_one()
    logger.info("DONE %s..%s: %s; concall_transcripts %d -> %d",
                d_from, d_to, stats, before, after)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("d_from", type=date.fromisoformat)
    ap.add_argument("d_to", type=date.fromisoformat)
    ap.add_argument("--cohort-days", type=int, default=8,
                    help="result_events lookback before d_from for the symbol cohort")
    args = ap.parse_args()
    asyncio.run(main(args.d_from, args.d_to, args.cohort_days))
