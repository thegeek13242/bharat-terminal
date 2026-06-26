"""Feature 2 — signal accuracy tracking + failure analysis (measure + flag).

Links each emitted trade signal to its realized forward price movement and
records the outcome in `signal_outcomes`, then (separately) runs an LLM
post-mortem categorising why wrong signals failed. Measurement only — nothing
here adjusts gates, prompts, or conviction; a human reviews the metrics.

Mirrors `calibrate_weights.py`: deterministic, idempotent, re-runnable, batch.

Subcommands:
  ingest      explode impact_reports.trade_signals -> upsert signal_outcomes
  evaluate    fill realized forward returns for elapsed horizons (no look-ahead)
  postmortem  LLM categorisation of wrong signals (costed; run sparingly)
  report      print hit-rate slices
  all         ingest + evaluate (no LLM)

Usage:
  python -m bharat_terminal.research.score_signals all --since-days 120
  python -m bharat_terminal.research.score_signals report --horizon 5d
  python -m bharat_terminal.research.score_signals postmortem --limit 50
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta, date

import numpy as np
from sqlalchemy import text

from bharat_terminal.research.db import session_scope
from bharat_terminal.grounding.queries import get_forward_returns

# Determinism rule: declared even though this job does no sampling.
np.random.seed(42)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("score_signals")

HORIZONS = (1, 5, 10, 20)
# Move smaller than this (percent) over a horizon is "no move" → inconclusive.
DEAD_BAND_PCT = float(os.getenv("OUTCOME_DEAD_BAND_PCT", "1.0"))
# Proxy mapping CompanyImpact.magnitude → expected move %, for magnitude_error.
MAGNITUDE_TO_PCT = {1: 0.5, 2: 1.0, 3: 2.0, 4: 4.0, 5: 8.0}
POSTMORTEM_MODEL = os.getenv("SIGNALS_MODEL", "gemini-3.1-pro-preview")
FAILURE_CATEGORIES = ("already_priced_in", "wrong_thesis", "market_regime", "data_error", "too_slow")


# ── ingest ────────────────────────────────────────────────────────────────
# Explode the anonymous trade_signals JSONB into signal_outcomes with a stable
# synthetic signal_id. ON CONFLICT updates only denormalized descriptors — it
# NEVER overwrites eval_status, realized returns, or the failure post-mortem.
_INGEST_SQL = text("""
INSERT INTO signal_outcomes (
    signal_id, impact_report_id, news_id, symbol, direction, conviction,
    instrument_type, macro_theme, sector, source, signal_created_at,
    implied_move_pct, eval_status
)
SELECT DISTINCT ON (signal_id)
    signal_id, impact_report_id, news_id, symbol, direction, conviction,
    instrument_type, macro_theme, sector, source, signal_created_at,
    implied_move_pct, eval_status
FROM (
    SELECT
        substr(encode(sha256((ir.id || '|' || upper(sig->>'symbol') || '|' || (sig->>'direction'))::bytea), 'hex'), 1, 32) AS signal_id,
        ir.id AS impact_report_id, ir.news_id AS news_id, upper(sig->>'symbol') AS symbol,
        sig->>'direction' AS direction, sig->>'conviction' AS conviction,
        sig->>'instrument_type' AS instrument_type, ir.macro_theme AS macro_theme,
        COALESCE(c.sector_nse, 'UNKNOWN') AS sector, n.source AS source, ir.created_at AS signal_created_at,
        CASE ci.magnitude WHEN 1 THEN 0.5 WHEN 2 THEN 1.0 WHEN 3 THEN 2.0
                          WHEN 4 THEN 4.0 WHEN 5 THEN 8.0 ELSE NULL END AS implied_move_pct,
        'pending' AS eval_status
    FROM impact_reports ir
    CROSS JOIN LATERAL jsonb_array_elements(ir.trade_signals) sig
    LEFT JOIN news_items n ON n.id = ir.news_id
    LEFT JOIN companies c ON c.symbol = upper(sig->>'symbol')
    LEFT JOIN LATERAL (
        SELECT (impact->>'magnitude')::int AS magnitude
        FROM jsonb_array_elements(ir.company_impacts) impact
        WHERE upper(impact->>'symbol') = upper(sig->>'symbol')
        LIMIT 1
    ) ci ON true
    WHERE ir.relevant = true
      AND ir.trade_signals IS NOT NULL
      AND jsonb_array_length(ir.trade_signals) > 0
      AND ir.created_at >= :since
      AND (sig->>'direction') IN ('long', 'short')
) sub
ORDER BY signal_id
ON CONFLICT (signal_id) DO UPDATE SET
    conviction       = EXCLUDED.conviction,
    instrument_type  = EXCLUDED.instrument_type,
    macro_theme      = EXCLUDED.macro_theme,
    sector           = EXCLUDED.sector,
    source           = EXCLUDED.source,
    implied_move_pct = EXCLUDED.implied_move_pct
""")


async def ingest(since_days: int, dry_run: bool) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    async with session_scope() as s:
        before = (await s.execute(text("SELECT count(*) FROM signal_outcomes"))).scalar_one()
        candidates = (await s.execute(text("""
            SELECT count(*) FROM impact_reports ir
            CROSS JOIN LATERAL jsonb_array_elements(ir.trade_signals) sig
            WHERE ir.relevant AND ir.trade_signals IS NOT NULL
              AND jsonb_array_length(ir.trade_signals) > 0
              AND ir.created_at >= :since AND (sig->>'direction') IN ('long','short')
        """), {"since": since})).scalar_one()
        logger.info(f"ingest: {candidates} long/short signals since {since.date()} "
                    f"({before} outcome rows currently)")
        if dry_run:
            logger.info("dry-run: no writes")
            return
        await s.execute(_INGEST_SQL, {"since": since})
        await s.commit()
        after = (await s.execute(text("SELECT count(*) FROM signal_outcomes"))).scalar_one()
        logger.info(f"ingest: {after - before} new rows, {after} total")


# ── evaluate ────────────────────────────────────────────────────────────────
def _correct(ret, direction: str):
    """Direction-correct with a dead-band. None = not-yet-elapsed or inconclusive."""
    if ret is None:
        return None
    if abs(ret) < DEAD_BAND_PCT:
        return None  # no meaningful move → inconclusive, not 'wrong'
    return (ret > 0) if direction == "long" else (ret < 0)


async def evaluate(dry_run: bool) -> None:
    async with session_scope() as s:
        rows = (await s.execute(text("""
            SELECT signal_id, symbol, direction, signal_created_at
            FROM signal_outcomes WHERE eval_status IN ('pending', 'partial')
            ORDER BY signal_created_at
        """))).all()
    logger.info(f"evaluate: {len(rows)} pending/partial rows")
    if dry_run:
        return

    updated = complete = no_data = 0
    for signal_id, symbol, direction, created_at in rows:
        fr = await get_forward_returns(symbol, created_at.date(), HORIZONS)
        status = fr.get("status")
        if status == "no_price_data":
            async with session_scope() as s:
                await s.execute(text("UPDATE signal_outcomes SET eval_status='no_price_data', "
                                     "evaluated_at=now() WHERE signal_id=:id"), {"id": signal_id})
                await s.commit()
            no_data += 1
            continue
        if status == "no_entry":
            continue  # signal too recent — no tradeable close yet; leave pending

        rets = fr["returns"]
        vals = {f"ret_{h}d": rets.get(f"{h}d") for h in HORIZONS}
        corrects = {f"correct_{h}d": _correct(rets.get(f"{h}d"), direction) for h in HORIZONS}
        all_elapsed = fr.get("all_horizons_elapsed", False)
        new_status = "complete" if all_elapsed else "partial"
        async with session_scope() as s:
            ed_raw = fr.get("entry_date")
            ed = date.fromisoformat(ed_raw) if isinstance(ed_raw, str) else ed_raw
            await s.execute(text("""
                UPDATE signal_outcomes SET
                    entry_price=:ep, entry_date=:ed,
                    ret_1d=:r1, ret_5d=:r5, ret_10d=:r10, ret_20d=:r20,
                    correct_1d=:c1, correct_5d=:c5, correct_10d=:c10, correct_20d=:c20,
                    eval_status=:st, evaluated_at=now()
                WHERE signal_id=:id
            """), {
                "id": signal_id, "ep": fr.get("entry_price"), "ed": ed,
                "r1": vals["ret_1d"], "r5": vals["ret_5d"], "r10": vals["ret_10d"], "r20": vals["ret_20d"],
                "c1": corrects["correct_1d"], "c5": corrects["correct_5d"],
                "c10": corrects["correct_10d"], "c20": corrects["correct_20d"],
                "st": new_status,
            })
            await s.commit()
        updated += 1
        if new_status == "complete":
            complete += 1
    logger.info(f"evaluate: updated={updated} (complete={complete}), no_price_data={no_data}")


# ── report ──────────────────────────────────────────────────────────────────
async def report(horizon: str) -> None:
    col = f"correct_{horizon}"
    if col not in {f"correct_{h}d" for h in HORIZONS}:
        raise SystemExit(f"horizon must be one of {[f'{h}d' for h in HORIZONS]}")
    async with session_scope() as s:
        for dim in ("conviction", "macro_theme", "sector", "source", "trigger_id"):
            rows = (await s.execute(text(f"""
                SELECT {dim} AS k,
                       count(*) FILTER (WHERE {col} IS NOT NULL) AS evaluated,
                       round(100.0 * count(*) FILTER (WHERE {col} IS TRUE)
                             / NULLIF(count(*) FILTER (WHERE {col} IS NOT NULL), 0), 1) AS hit_rate_pct
                FROM signal_outcomes
                GROUP BY {dim} ORDER BY evaluated DESC NULLS LAST
            """))).all()
            print(f"\n== hit-rate ({horizon}) by {dim} ==")
            for k, n, hr in rows:
                if n:
                    print(f"  {str(k):24} n={n:5}  hit={hr}%")


# ── postmortem (LLM; costed — run sparingly / after dossier run) ──────────────
_POSTMORTEM_TOOL = {
    "name": "categorize_failure",
    "description": "Categorize why a trade signal failed to play out.",
    "input_schema": {
        "type": "object",
        "properties": {
            "failure_category": {"type": "string", "enum": list(FAILURE_CATEGORIES)},
            "rationale": {"type": "string", "description": "1-2 sentences, specific to this signal."},
        },
        "required": ["failure_category", "rationale"],
    },
}


async def postmortem(limit: int, horizon: str, dry_run: bool) -> None:
    from bharat_terminal import llm
    from bharat_terminal.analysis.llm_logger import get_llm_logger, LLMCallRecord, compute_cost
    import time as _time

    col = f"correct_{horizon}"
    async with session_scope() as s:
        rows = (await s.execute(text(f"""
            SELECT so.signal_id, so.symbol, so.direction, so.conviction, so.{f"ret_{horizon}"} AS ret,
                   n.headline, n.body, ir.trade_signals
            FROM signal_outcomes so
            LEFT JOIN impact_reports ir ON ir.id = so.impact_report_id
            LEFT JOIN news_items n ON n.id = so.news_id
            WHERE so.eval_status='complete' AND so.{col} IS FALSE
              AND so.failure_category IS NULL
            ORDER BY so.signal_created_at DESC LIMIT :lim
        """), {"lim": limit})).all()
    logger.info(f"postmortem: {len(rows)} wrong+uncategorized signals (horizon {horizon})")
    if dry_run or not rows:
        return

    llm_logger = get_llm_logger()
    done = 0
    for signal_id, symbol, direction, conviction, ret, headline, body, _sigs in rows:
        prompt = (
            f"A '{direction}' ({conviction} conviction) trade signal on {symbol} was generated from this news:\n"
            f"HEADLINE: {headline}\nBODY: {(body or '')[:1500]}\n\n"
            f"Over the {horizon} horizon the stock realized {ret:+.2f}% — i.e. the signal was WRONG "
            f"(it did not move in the predicted direction). Categorize the most likely reason and explain briefly."
        )
        try:
            t0 = _time.time()
            resp = await llm.acomplete(
                model=POSTMORTEM_MODEL, max_tokens=512,
                system="You are a sell-side risk analyst doing blameless signal post-mortems for Indian equities.",
                tools=[_POSTMORTEM_TOOL],
                messages=[{"role": "user", "content": prompt}],
            )
            latency_ms = (_time.time() - t0) * 1000
            cat = rationale = None
            for b in resp.content:
                if getattr(b, "type", None) == "tool_use" and b.name == "categorize_failure":
                    cat = b.input.get("failure_category")
                    rationale = b.input.get("rationale")
                    break
            llm_logger.log(LLMCallRecord(
                call_id=signal_id, model=POSTMORTEM_MODEL, stage="signal_postmortem",
                prompt_tokens=resp.usage.input_tokens, completion_tokens=resp.usage.output_tokens,
                latency_ms=latency_ms,
                cost_usd=compute_cost(POSTMORTEM_MODEL, resp.usage.input_tokens, resp.usage.output_tokens),
                success=bool(cat),
            ))
            if not cat or cat not in FAILURE_CATEGORIES:
                continue
            async with session_scope() as s:
                await s.execute(text("""
                    UPDATE signal_outcomes SET failure_category=:cat, failure_rationale=:r,
                        postmortem_model=:m, postmortem_at=now() WHERE signal_id=:id
                """), {"cat": cat, "r": rationale, "m": POSTMORTEM_MODEL, "id": signal_id})
                await s.commit()
            done += 1
        except Exception as e:
            logger.warning(f"postmortem {symbol} failed: {e}")
    logger.info(f"postmortem: categorized {done}/{len(rows)}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="score_signals")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("ingest", "evaluate", "all"):
        p = sub.add_parser(name)
        p.add_argument("--since-days", type=int, default=120)
        p.add_argument("--dry-run", action="store_true")
    pr = sub.add_parser("report"); pr.add_argument("--horizon", default="5d")
    pm = sub.add_parser("postmortem")
    pm.add_argument("--limit", type=int, default=50)
    pm.add_argument("--horizon", default="5d")
    pm.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.cmd == "ingest":
        asyncio.run(ingest(args.since_days, args.dry_run))
    elif args.cmd == "evaluate":
        asyncio.run(evaluate(args.dry_run))
    elif args.cmd == "all":
        async def _all():
            await ingest(args.since_days, args.dry_run)
            await evaluate(args.dry_run)
        asyncio.run(_all())
    elif args.cmd == "report":
        asyncio.run(report(args.horizon))
    elif args.cmd == "postmortem":
        asyncio.run(postmortem(args.limit, args.horizon, args.dry_run))


if __name__ == "__main__":
    main()
