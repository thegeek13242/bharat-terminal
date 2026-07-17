"""Daily accuracy check: results-filing sentiment vs same-day price move.

Motivation (SWSOLAR postmortem 2026-07-16): quarterly-results filings were
classified by headline-metric cherry-picking, so "positive" calls landed on
quarters the market sold hard. Frozen pre-fix baseline (2026-07-02 →
2026-07-16 09:12 UTC, filings ≤14:00 IST, corrected events excluded):

    positive n=21  avg +0.38%  hard-wrong (fell >1.5%) 8/21 = 38.1%
    neutral  n=36  avg -0.23%
    negative n=2   avg -3.90%

The scenario-rubric fix (analysis/scenario_rubrics.py) deployed 2026-07-16
~09:12 UTC targets exactly this. This module re-measures the same confusion
matrix daily over the post-fix window so drift is visible, and records each
run in sync_log (sync_type='results_sentiment_eval', metadata = full stats).

Methodology — identical to the baseline study, deliberately:
  - Results filings only, detected with the SAME detect_scenario() the rubric
    uses (headline + body P&L gate), so we measure what the fix targets.
  - Filings ≤14:00 IST only, guaranteeing ≥90 minutes of post-filing tape.
    Late-day filings get the full day's move attributed to them, which
    misattributes pre-filing intraday moves (NAVKARCORP 2026-07-17 fell 10.7%
    BEFORE its 14:55 filing and rose after — the old 15:00 window scored it
    hard-wrong). After-hours filings excluded entirely (reaction is next-day).
  - Move = close(D) vs previous trading close, from price_points (NSE+BSE).
  - hop-0 impacts only. hard-wrong: positive & move < -1.5%, or
    negative & move > +1.5%.

Read-only against news/impact/price tables; writes ONLY a sync_log row
(skipped when dry_run=True). Deterministic — no sampling, no LLM.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy import text

from bharat_terminal.analysis.scenario_rubrics import detect_scenario
from bharat_terminal.kb.sync.utils import (
    complete_sync_log, create_async_session, start_sync_log,
)

logger = logging.getLogger(__name__)

# The rubric deploy boundary — post-fix window starts here. (datetime, not
# str: asyncpg rejects string params against timestamptz columns.)
FIX_DEPLOYED_AT = datetime(2026, 7, 16, 9, 12, tzinfo=timezone.utc)

# Frozen pre-fix baseline — recomputed 2026-07-17 under the current
# methodology (filings ≤14:00 IST so ≥90min of post-filing tape; the 18
# manually-corrected 2026-07-16 events excluded, since their stored rows no
# longer reflect pre-fix pipeline output). The original 15:00-window baseline
# was 23.8% hard-wrong; the cleaner window shows pre-fix positives were worse
# (38.1%) once late-day misattribution is removed.
BASELINE = {
    "window": "2026-07-02..2026-07-16T09:12 (pre-fix, <=14:00 IST, corrections excluded)",
    "positive": {"n": 21, "avg_day_pct": 0.38, "hard_wrong": 8, "hard_wrong_pct": 38.1},
    "neutral": {"n": 36, "avg_day_pct": -0.23},
    "negative": {"n": 2, "avg_day_pct": -3.90, "hard_wrong": 0},
}

HARD_WRONG_PCT = 1.5  # |same-day move| beyond which a wrong-sign call is "hard wrong"


def _has_fingerprint(explanation: str) -> bool:
    """Did the explanation follow the rubric's core-line discipline?

    Standard companies: revenue AND PBT stated. Banks/NBFC-lenders legitimately
    report NII + net profit instead of revenue/PBT (FEDERALBNK 2026-07-17 was a
    false negative under the revenue-only check), so accept that vocabulary too.
    """
    e = (explanation or "").lower()
    standard = "revenue" in e and ("pbt" in e or "profit before" in e)
    lender = (("net interest income" in e or "nii" in e or "interest income" in e)
              and ("net profit" in e or "profit after tax" in e or "pat" in e
                   or "pbt" in e))
    return standard or lender


async def evaluate(dry_run: bool = True) -> Dict[str, Any]:
    """Compute the post-fix confusion matrix (cumulative since FIX_DEPLOYED_AT).

    Returns the stats dict; persists it to sync_log unless dry_run.
    """
    SessionFactory = create_async_session()
    async with SessionFactory() as s:
        rows = (await s.execute(text("""
            WITH ev AS (
              SELECT n.id, n.headline, n.body,
                     (n.timestamp_utc AT TIME ZONE 'Asia/Kolkata')::date AS ist_date,
                     imp->>'symbol' AS symbol,
                     imp->>'sentiment' AS sentiment,
                     (imp->>'magnitude')::int AS magnitude,
                     imp->>'explanation' AS explanation
              FROM news_items n
              JOIN impact_reports ir ON ir.news_id = n.id
              CROSS JOIN LATERAL jsonb_array_elements(ir.company_impacts) imp
              WHERE ir.created_at > :fix_at
                AND (imp->>'hop_distance')::int = 0
                AND (n.timestamp_utc AT TIME ZONE 'Asia/Kolkata')::time
                    BETWEEN '09:15' AND '14:00'
            )
            SELECT ev.*, p1.close AS close_d,
                   (SELECT p0.close FROM price_points p0
                    WHERE p0.symbol = ev.symbol AND p0.date < ev.ist_date
                    ORDER BY p0.date DESC LIMIT 1) AS close_prev
            FROM ev
            JOIN price_points p1 ON p1.symbol = ev.symbol AND p1.date = ev.ist_date
        """), {"fix_at": FIX_DEPLOYED_AT})).mappings().all()

    # Results-filing filter: same detector the rubric uses.
    events = []
    for r in rows:
        if not r["close_prev"] or float(r["close_prev"]) <= 0:
            continue
        if detect_scenario(r["headline"] or "", r["body"] or "") != "financial_results":
            continue
        move = 100.0 * (float(r["close_d"]) / float(r["close_prev"]) - 1)
        events.append({
            "symbol": r["symbol"], "date": str(r["ist_date"]),
            "sentiment": r["sentiment"], "magnitude": r["magnitude"],
            "move_pct": round(move, 2),
            "fingerprint": _has_fingerprint(r["explanation"]),
        })

    stats: Dict[str, Any] = {
        "window": f"{FIX_DEPLOYED_AT.isoformat()}..now (post-fix, cumulative)",
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_events": len(events),
        "fingerprint_rate_pct": round(
            100.0 * sum(1 for e in events if e["fingerprint"]) / len(events), 1)
            if events else None,
        "hard_wrong_examples": [],
        "baseline": BASELINE,
    }
    for sent in ("positive", "neutral", "negative"):
        sub = [e for e in events if e["sentiment"] == sent]
        if not sub:
            stats[sent] = {"n": 0}
            continue
        moves = [e["move_pct"] for e in sub]
        if sent == "positive":
            wrong = [e for e in sub if e["move_pct"] < -HARD_WRONG_PCT]
        elif sent == "negative":
            wrong = [e for e in sub if e["move_pct"] > HARD_WRONG_PCT]
        else:
            wrong = []
        stats[sent] = {
            "n": len(sub),
            "avg_day_pct": round(sum(moves) / len(moves), 2),
            "hard_wrong": len(wrong),
            "hard_wrong_pct": round(100.0 * len(wrong) / len(sub), 1),
        }
        stats["hard_wrong_examples"] += [
            f"{e['date']} {e['symbol']} {sent}/{e['magnitude']} moved {e['move_pct']:+.1f}%"
            for e in wrong[:5]]

    pos, base_pos = stats["positive"], BASELINE["positive"]
    logger.info(
        f"[RESULTS-EVAL] post-fix n={stats['n_events']} | "
        f"positive: n={pos.get('n', 0)} hard-wrong={pos.get('hard_wrong', 0)} "
        f"({pos.get('hard_wrong_pct', 0)}%) vs baseline {base_pos['hard_wrong_pct']}% | "
        f"rubric-fingerprint {stats['fingerprint_rate_pct']}%")
    for ex in stats["hard_wrong_examples"]:
        logger.info(f"[RESULTS-EVAL]   hard-wrong: {ex}")

    if not dry_run:
        async with SessionFactory() as s:
            log_id = await start_sync_log(s, "results_sentiment_eval", "IMPACT_QA")
            await complete_sync_log(
                s, log_id, "completed",
                records_processed=stats["n_events"], metadata=stats)
            await s.commit()
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(prog="results_sentiment_eval")
    ap.add_argument("--execute", action="store_true",
                    help="persist the run to sync_log (default: dry-run, log only)")
    args = ap.parse_args()
    asyncio.run(evaluate(dry_run=not args.execute))


if __name__ == "__main__":
    main()
