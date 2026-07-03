"""Admin: re-run the analysis pipeline on a stored news event and update the
stored report in place.

Use case: a misclassified event (wrong sentiment, wrong scope, missed
mechanism) needs correction with the CURRENT prompts/code — e.g. the
HINDZINC record-output event scored "negative" by a pre-overhaul prompt.

Behaviour:
  * Re-runs the full per-item pipeline on the ORIGINAL input text — the
    full_body snapshot the original run saw is read back from analysis_audit
    (news_items only persists the short body).
  * UPDATES the existing impact_reports row in place (same id + created_at):
    the feed position is preserved and no duplicate feed entry appears.
  * INSERTS a new analysis_audit row (pipeline_mode='reanalyze') — the
    ORIGINAL audit row is never touched, so the trail shows both the original
    analysis and the correction.
  * Invalidates the Redis report cache (the API serves Redis first).
  * signal_outcomes rows from the original signals are kept: eval data must
    reflect what the system actually did at the time.

Dry-run by default; --apply writes. Run inside the analysis container:

  python -m bharat_terminal.analysis.reanalyze "<news_id>"           # dry run
  python -m bharat_terminal.analysis.reanalyze "<news_id>" --apply
"""
import argparse
import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from bharat_terminal.types import NewsItem, ImpactReport

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reanalyze")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise SystemExit("DATABASE_URL not set")
    # sqlalchemy-style URLs carry a +driver suffix asyncpg doesn't accept.
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _audit_content_hash(news_item: dict, report: dict) -> str:
    """Mirror of kafka_relay._audit_content_hash — same canonical payload so a
    compliance reviewer can verify re-analysis rows the same way."""
    payload = {
        "news_id": report.get("news_id"),
        "source": news_item.get("source"),
        "headline": news_item.get("headline"),
        "url": news_item.get("url"),
        "full_body": news_item.get("full_body"),
        "relevant": report.get("relevant"),
        "confidence": report.get("confidence"),
        "macro_theme": report.get("macro_theme"),
        "affected_sectors": report.get("affected_sectors"),
        "company_impacts": report.get("company_impacts"),
        "trade_signals": report.get("trade_signals"),
    }
    canonical = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _impacts_line(impacts) -> str:
    if not impacts:
        return "  (no company impacts)"
    return "\n".join(
        f"  {i.get('symbol'):12} {i.get('sentiment'):8} mag {i.get('magnitude')} "
        f"hop {i.get('hop_distance', 0)} | {str(i.get('explanation'))[:90]}"
        for i in impacts
    )


async def run(news_id: str, apply: bool, allow_irrelevant: bool) -> None:
    import asyncpg

    conn = await asyncpg.connect(_dsn())
    try:
        news = await conn.fetchrow(
            "SELECT id, source, timestamp_utc, headline, body, url, category, "
            "symbols_mentioned FROM news_items WHERE id = $1", news_id)
        if not news:
            raise SystemExit(f"No news_items row for id {news_id!r}")

        audit = await conn.fetchrow(
            "SELECT full_body, body_word_count, body_fetch_status, "
            "body_extraction_method FROM analysis_audit WHERE news_id = $1 "
            "ORDER BY analysis_created_at DESC NULLS LAST LIMIT 1", news_id)

        old = await conn.fetchrow(
            "SELECT id, created_at, relevant, confidence, macro_theme, "
            "company_impacts, trade_signals FROM impact_reports "
            "WHERE news_id = $1 ORDER BY created_at DESC LIMIT 1", news_id)
        if not old:
            raise SystemExit(f"No impact_reports row for news_id {news_id!r} — nothing to update")

        symbols = news["symbols_mentioned"]
        if isinstance(symbols, str):
            symbols = json.loads(symbols or "[]")
        full_body = (audit["full_body"] if audit else None) or news["body"]

        item = NewsItem(
            id=news["id"],
            source=news["source"],
            timestamp_utc=news["timestamp_utc"],
            headline=news["headline"],
            body=news["body"],
            url=news["url"] or "",
            ingest_latency_ms=0.0,
            category=news["category"],
            symbols_mentioned=symbols or [],
            full_body=full_body,
        )
        if not full_body or len(full_body) < 200:
            logger.warning(
                f"Input text is short ({len(full_body or '')} chars) — the re-analysis "
                f"sees no more than the original run stored."
            )

        old_impacts = old["company_impacts"]
        if isinstance(old_impacts, str):
            old_impacts = json.loads(old_impacts or "[]")
        print(f"\n=== OLD analysis ({old['created_at']:%Y-%m-%d %H:%M} UTC, report {old['id'][:8]}…) ===")
        print(f"  relevant={old['relevant']} confidence={old['confidence']} theme={old['macro_theme']}")
        print(_impacts_line(old_impacts))

        # ── Re-run the pipeline with current prompts/code ──────────────────
        from bharat_terminal.analysis.pipeline import process_news_item
        report: ImpactReport = await process_news_item(item)

        new_impacts = [i.model_dump() for i in report.company_impacts]
        new_signals = [s.model_dump() for s in report.trade_signals]
        print(f"\n=== NEW analysis (scope={report.event_scope}) ===")
        print(f"  relevant={report.relevant} confidence={report.confidence} theme={report.macro_theme}")
        print(_impacts_line(new_impacts))

        if not apply:
            print("\nDRY RUN — nothing written. Re-run with --apply to update the stored report.")
            return
        if not report.relevant and not allow_irrelevant:
            raise SystemExit(
                "\nNew analysis says relevant=False — refusing to overwrite a relevant "
                "report with an empty one. Re-run with --allow-irrelevant to force."
            )

        # ── 1. Update the existing report row in place ─────────────────────
        await conn.execute(
            """
            UPDATE impact_reports SET
                relevant = $2, confidence = $3, macro_theme = $4,
                affected_sectors = $5::jsonb, company_impacts = $6::jsonb,
                trade_signals = $7::jsonb, processing_latency_ms = $8
            WHERE id = $1
            """,
            old["id"], report.relevant, report.confidence, report.macro_theme,
            json.dumps(report.affected_sectors), json.dumps(new_impacts, default=str),
            json.dumps(new_signals, default=str), report.processing_latency_ms,
        )

        # ── 2. New audit row (original audit row stays untouched) ──────────
        news_dict = {"source": item.source, "headline": item.headline,
                     "url": item.url, "full_body": item.full_body}
        report_dict = {"news_id": news_id, "relevant": report.relevant,
                       "confidence": report.confidence, "macro_theme": report.macro_theme,
                       "affected_sectors": report.affected_sectors,
                       "company_impacts": new_impacts, "trade_signals": new_signals}
        await conn.execute(
            """
            INSERT INTO analysis_audit
                (id, news_id, source, headline, url, published_at, full_body,
                 body_word_count, body_fetch_status, body_extraction_method,
                 relevant, confidence, macro_theme, affected_sectors,
                 company_impacts, trade_signals, model_id, provider,
                 prompt_version, input_tokens, output_tokens, pipeline_mode,
                 processing_latency_ms, content_hash, analysis_created_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,
                    $15::jsonb,$16::jsonb,$17,$18,$19,$20,$21,$22,$23,$24,$25)
            """,
            str(uuid.uuid4()), news_id, item.source, item.headline, item.url,
            item.timestamp_utc, item.full_body,
            (audit["body_word_count"] if audit else None),
            (audit["body_fetch_status"] if audit else None),
            (audit["body_extraction_method"] if audit else None),
            report.relevant, report.confidence, report.macro_theme,
            json.dumps(report.affected_sectors), json.dumps(new_impacts, default=str),
            json.dumps(new_signals, default=str),
            os.getenv("IMPACT_MODEL", "gemini-2.5-flash"), "vertex",
            "reanalyze-cli", None, None, "reanalyze",
            report.processing_latency_ms,
            _audit_content_hash(news_dict, report_dict),
            datetime.now(timezone.utc),
        )

        # ── 3. Invalidate the Redis report cache (API serves Redis first) ──
        try:
            import redis as _redis
            r = _redis.from_url(REDIS_URL, socket_timeout=3)
            r.delete(f"impact:{news_id}")
            print("Redis cache invalidated.")
        except Exception as e:
            logger.warning(f"Redis invalidation failed (cache expires in <=24h): {e}")

        print(f"\nAPPLIED: report {old['id'][:8]}… updated in place; "
              f"re-analysis recorded in analysis_audit (pipeline_mode=reanalyze).")
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="reanalyze")
    ap.add_argument("news_id", help="news_items.id of the event to re-analyse")
    ap.add_argument("--apply", action="store_true", help="write the update (default: dry run)")
    ap.add_argument("--allow-irrelevant", action="store_true",
                    help="allow overwriting with a relevant=False result")
    args = ap.parse_args()
    asyncio.run(run(args.news_id, args.apply, args.allow_irrelevant))


if __name__ == "__main__":
    main()
