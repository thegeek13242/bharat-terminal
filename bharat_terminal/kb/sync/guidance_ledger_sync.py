"""Guidance ledger sync — C2 (MANAGEMENT_COMMENTARY_2026-07-31.md §C2).

Projects transcript_guidance (free-text metrics, as-stated units) into
guidance_ledger with CANONICAL keys (guidance_normalize), maintains the
statement lifecycle, and scores resolved guidance against financial_results
actuals into guidance_outcomes:

1. NORMALIZE every transcript_guidance row → ledger row (upsert keyed on
   source_guidance_id — idempotent, re-runs converge).
2. LIFECYCLE per (symbol, metric_key, period_key): latest statement ACTIVE,
   earlier ones SUPERSEDED; change_verdict recomputed on canonical keys
   (raised/lowered/maintained by midpoint, 'changed' on unit change) — this
   fixes the raw table's spelling-noise degradation (GRAVITA) and cross-call
   renames (UJJIVANSFB advances→asset growth both → loan_growth).
3. OUTCOMES: active + scoreable + period elapsed + actual present →
   delivered-vs-guided verdict (above/in/below + met per better_direction),
   basis recorded (consolidated preferred), ambiguity flags kept. Status →
   RESOLVED. The scorer REFUSES unit mismatches, non-INR bases, H-periods
   and missing growth bases rather than guess.

Credibility: derived at read time (kb endpoint), NEVER stored — as of
2026-07-31 zero companies have >= 2 resolved pairs (corpus is one season
old, 67% of guidance is FY27+); any multiplier stays OFF until that changes.

CLI (dry-run is the DEFAULT, writes nothing, works even before migration 034):
  python -m bharat_terminal.kb.sync.guidance_ledger_sync            # report
  python -m bharat_terminal.kb.sync.guidance_ledger_sync --execute  # write
Cron (host, after the evening results eval): nightly 20:15 IST weekdays.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import date

from sqlalchemy import text

from bharat_terminal.kb.sync.guidance_normalize import (
    normalize_row, to_crore,
)

logger = logging.getLogger("guidance_ledger_sync")

_MAINTAIN_REL_TOL = 0.02   # midpoints within 2% relative → 'maintained'

Q_GUIDANCE = text("""
    SELECT id, transcript_id, symbol, announced_at, metric, period,
           value_low, value_high, unit
    FROM transcript_guidance
    ORDER BY symbol, announced_at, id
""")

Q_LEDGER_TABLES_EXIST = text("""
    SELECT count(*) FROM information_schema.tables
    WHERE table_name IN ('guidance_ledger', 'guidance_outcomes')
""")

Q_EXISTING_OUTCOMES = text("SELECT ledger_id FROM guidance_outcomes")

Q_ACTUAL_QUARTER = text("""
    SELECT revenue_cr, ebitda_cr, ebitda_margin_pct, pat_cr, pbt_cr, tax_cr,
           eps_basic, ytd_revenue_cr, ytd_pat_cr, ytd_eps_basic,
           consolidated, announced_at
    FROM financial_results
    WHERE symbol = :symbol AND period_end = :period_end
      AND period_type = 'quarterly'
    ORDER BY consolidated DESC
    LIMIT 1
""")

Q_FY_QUARTERS = text("""
    SELECT period_end, revenue_cr, ebitda_cr, pat_cr, pbt_cr, tax_cr,
           consolidated, announced_at
    FROM financial_results
    WHERE symbol = :symbol AND period_end > :fy_start
      AND period_end <= :fy_end AND period_type = 'quarterly'
      AND consolidated = :consolidated
    ORDER BY period_end
""")

UPSERT_LEDGER = text("""
    INSERT INTO guidance_ledger
        (symbol, metric_key, metric_raw, period_key, period_end,
         value_low, value_high, unit_key, unit_raw, better_direction,
         source_guidance_id, transcript_id, stated_at, change_verdict,
         restatements, status, scoreable, updated_at)
    VALUES
        (:symbol, :metric_key, :metric_raw, :period_key, :period_end,
         :value_low, :value_high, :unit_key, :unit_raw, :better_direction,
         :source_guidance_id, :transcript_id, :stated_at, :change_verdict,
         :restatements, :status, :scoreable, now())
    ON CONFLICT (source_guidance_id) DO UPDATE SET
        metric_key = EXCLUDED.metric_key,
        period_key = EXCLUDED.period_key,
        period_end = EXCLUDED.period_end,
        unit_key = EXCLUDED.unit_key,
        better_direction = EXCLUDED.better_direction,
        change_verdict = EXCLUDED.change_verdict,
        restatements = EXCLUDED.restatements,
        status = CASE WHEN guidance_ledger.status = 'resolved'
                      THEN 'resolved' ELSE EXCLUDED.status END,
        scoreable = EXCLUDED.scoreable,
        updated_at = now()
""")

INSERT_OUTCOME = text("""
    INSERT INTO guidance_outcomes
        (ledger_id, symbol, metric_key, period_key, guided_low, guided_high,
         delivered, delivered_verdict, met, basis_consolidated, formula,
         actual_period_end, actual_announced_at, ambiguity_flags)
    SELECT gl.id, :symbol, :metric_key, :period_key, :guided_low,
           :guided_high, :delivered, :delivered_verdict, :met,
           :basis_consolidated, :formula, :actual_period_end,
           :actual_announced_at, CAST(:ambiguity_flags AS jsonb)
    FROM guidance_ledger gl
    WHERE gl.source_guidance_id = :source_guidance_id
    ON CONFLICT (ledger_id) DO NOTHING
""")

MARK_RESOLVED = text("""
    UPDATE guidance_ledger SET status = 'resolved', updated_at = now()
    WHERE source_guidance_id = :source_guidance_id
""")


def _mid(lo, hi) -> float | None:
    vals = [v for v in (lo, hi) if v is not None]
    return sum(vals) / len(vals) if vals else None


def compute_lifecycle(rows: list[dict]) -> None:
    """In place: status/change_verdict/restatements per canonical group."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["symbol"], r["metric_key"], r["period_key"])].append(r)
    for group in groups.values():
        group.sort(key=lambda r: (r["stated_at"], r["source_guidance_id"]))
        for i, r in enumerate(group):
            r["status"] = "active" if i == len(group) - 1 else "superseded"
            r["restatements"] = len(group) - 1
            if i == 0:
                r["change_verdict"] = "new"
                continue
            prev = group[i - 1]
            mid, pmid = _mid(r["value_low"], r["value_high"]), \
                _mid(prev["value_low"], prev["value_high"])
            if mid is None or pmid is None or r["unit_key"] != prev["unit_key"]:
                r["change_verdict"] = "changed"
            elif abs(mid - pmid) <= _MAINTAIN_REL_TOL * max(1e-9, abs(pmid)):
                r["change_verdict"] = "maintained"
            else:
                r["change_verdict"] = "raised" if mid > pmid else "lowered"


async def _fetch_actual(session, symbol: str, formula: str, period_key: str,
                        period_end: date):
    """(delivered, basis_consolidated, announced_at, flags) or None."""
    flags: list[str] = []
    is_fy = period_key.startswith("FY")
    is_q = period_key.startswith("Q")
    if not (is_fy or is_q):
        return None                                # H1/H2/month: v1 refuses

    async def q_row(pe):
        return (await session.execute(
            Q_ACTUAL_QUARTER, {"symbol": symbol, "period_end": pe})
        ).mappings().first()

    async def fy_rows(consolidated):
        return (await session.execute(Q_FY_QUARTERS, {
            "symbol": symbol, "fy_start": date(period_end.year - 1, 3, 31),
            "fy_end": period_end, "consolidated": consolidated,
        })).mappings().all()

    def yoy(cur, base):
        if cur is None or base in (None, 0):
            return None
        return (cur / base - 1.0) * 100.0

    row = await q_row(period_end)
    if row is None:
        return None
    consolidated, announced = row["consolidated"], row["announced_at"]
    if not consolidated:
        flags.append("standalone_basis")

    if is_q:
        base_row = None
        if formula.endswith("_growth_pct"):
            base_row = await q_row(date(period_end.year - 1, period_end.month,
                                        period_end.day))
            if base_row is None:
                return None
        val = {
            "revenue_cr": row["revenue_cr"],
            "pat_cr": row["pat_cr"],
            "ebitda_cr": row["ebitda_cr"],
            "eps": row["eps_basic"],
            "ebitda_margin_pct": row["ebitda_margin_pct"],
            "pat_margin_pct": (row["pat_cr"] / row["revenue_cr"] * 100.0
                               if row["pat_cr"] is not None
                               and row["revenue_cr"] else None),
            "tax_rate_pct": (row["tax_cr"] / row["pbt_cr"] * 100.0
                             if row["tax_cr"] is not None
                             and row["pbt_cr"] else None),
            "revenue_growth_pct": yoy(row["revenue_cr"],
                                      base_row and base_row["revenue_cr"]),
            "pat_growth_pct": yoy(row["pat_cr"],
                                  base_row and base_row["pat_cr"]),
            "ebitda_growth_pct": yoy(row["ebitda_cr"],
                                     base_row and base_row["ebitda_cr"]),
        }.get(formula)
    else:
        # FY: levels from the Q4 row's ytd columns; EBITDA/tax need all 4
        # quarters summed on one basis; growth compares to prior-FY same way.
        def need4(rows_, col):
            vals = [r[col] for r in rows_]
            return sum(vals) if len(vals) == 4 and all(
                v is not None for v in vals) else None

        cur4 = await fy_rows(consolidated)
        val = None
        if formula in ("revenue_cr", "pat_cr", "eps", "pat_margin_pct",
                       "revenue_growth_pct", "pat_growth_pct"):
            ytd = {"revenue_cr": row["ytd_revenue_cr"],
                   "pat_cr": row["ytd_pat_cr"], "eps": row["ytd_eps_basic"]}
            if formula in ytd:
                val = ytd[formula]
            elif formula == "pat_margin_pct":
                val = (row["ytd_pat_cr"] / row["ytd_revenue_cr"] * 100.0
                       if row["ytd_pat_cr"] is not None
                       and row["ytd_revenue_cr"] else None)
            else:
                base_q4 = await q_row(date(period_end.year - 1, 3, 31))
                if base_q4 is None:
                    return None
                col = ("ytd_revenue_cr" if formula == "revenue_growth_pct"
                       else "ytd_pat_cr")
                val = yoy(row[col], base_q4[col])
        elif formula in ("ebitda_cr", "ebitda_margin_pct",
                         "ebitda_growth_pct", "tax_rate_pct"):
            flags.append("fy_sum_of_quarters")
            if formula == "ebitda_cr":
                val = need4(cur4, "ebitda_cr")
            elif formula == "ebitda_margin_pct":
                e, r_ = need4(cur4, "ebitda_cr"), need4(cur4, "revenue_cr")
                val = e / r_ * 100.0 if e is not None and r_ else None
            elif formula == "tax_rate_pct":
                t, p = need4(cur4, "tax_cr"), need4(cur4, "pbt_cr")
                val = t / p * 100.0 if t is not None and p else None
            else:
                prev4 = (await session.execute(Q_FY_QUARTERS, {
                    "symbol": symbol,
                    "fy_start": date(period_end.year - 2, 3, 31),
                    "fy_end": date(period_end.year - 1, 3, 31),
                    "consolidated": consolidated,
                })).mappings().all()
                cur_e, prev_e = need4(cur4, "ebitda_cr"), need4(prev4,
                                                                "ebitda_cr")
                val = yoy(cur_e, prev_e)
    if val is None:
        return None
    if formula.startswith("ebitda"):
        flags.append("derived_ebitda")
    return float(val), consolidated, announced, flags


def _verdict(delivered: float, lo, hi, better: str | None):
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    if lo is None:
        return None, None
    verdict = ("below" if delivered < lo
               else "above" if delivered > hi else "in")
    met = None
    if better == "higher":
        met = delivered >= lo
    elif better == "lower":
        met = delivered <= hi
    elif verdict == "in":
        met = True
    return verdict, met


async def run(execute: bool) -> int:
    from bharat_terminal.kb.sync.utils import create_async_session
    session_factory = create_async_session()
    async with session_factory() as session:
        tables = (await session.execute(Q_LEDGER_TABLES_EXIST)).scalar()
        raw = (await session.execute(Q_GUIDANCE)).mappings().all()
        logger.info(f"transcript_guidance rows: {len(raw)} | ledger tables "
                    f"present: {tables}/2")

        rows = []
        for g in raw:
            n = normalize_row(g["metric"], g["period"], g["unit"],
                              g["announced_at"])
            lo, hi = g["value_low"], g["value_high"]
            if n["unit_key"] in ("lakh", "mn", "bn") and n["scoreable"]:
                lo = to_crore(lo, n["unit_key"]) if lo is not None else None
                hi = to_crore(hi, n["unit_key"]) if hi is not None else None
                n["unit_key"] = "cr"
            rows.append({
                "symbol": g["symbol"], "metric_key": n["metric_key"],
                "metric_raw": g["metric"], "period_key": n["period_key"],
                "period_end": n["period_end"], "value_low": lo,
                "value_high": hi, "unit_key": n["unit_key"],
                "unit_raw": g["unit"],
                "better_direction": n["better_direction"],
                "source_guidance_id": g["id"],
                "transcript_id": g["transcript_id"],
                "stated_at": g["announced_at"], "scoreable": n["scoreable"],
                "norm_flags": n["flags"],
            })
        compute_lifecycle(rows)

        # Candidates for outcome scoring: active, scoreable, period elapsed.
        today = date.today()
        candidates = [r for r in rows if r["status"] == "active"
                      and r["scoreable"] and r["period_end"]
                      and r["period_end"] < today]
        outcomes = []
        for r in candidates:
            got = await _fetch_actual(session, r["symbol"], _formula_of(r),
                                      r["period_key"], r["period_end"])
            if got is None:
                continue
            delivered, consolidated, announced, flags = got
            verdict, met = _verdict(delivered, r["value_low"],
                                    r["value_high"], r["better_direction"])
            if verdict is None:
                continue
            outcomes.append({**r, "delivered": delivered,
                             "delivered_verdict": verdict, "met": met,
                             "basis_consolidated": consolidated,
                             "actual_announced_at": announced,
                             "ambiguity_flags": r["norm_flags"] + flags})

        _report(rows, candidates, outcomes)

        if not execute:
            logger.info("DRY-RUN: nothing written")
            return 0
        if tables != 2:
            logger.error("guidance_ledger/guidance_outcomes missing — apply "
                         "migration 034 first")
            return 1

        from bharat_terminal.kb.sync.utils import (
            start_sync_log, complete_sync_log,
        )
        log_id = await start_sync_log(session, "guidance_ledger_sync",
                                      "transcript_guidance")
        # Commit the log row in its own transaction (market_state_sync
        # convention) so a failed upsert's rollback can't erase it.
        await session.commit()
        try:
            for r in rows:
                await session.execute(UPSERT_LEDGER, {
                    k: r[k] for k in (
                        "symbol", "metric_key", "metric_raw", "period_key",
                        "period_end", "value_low", "value_high", "unit_key",
                        "unit_raw", "better_direction", "source_guidance_id",
                        "transcript_id", "stated_at", "change_verdict",
                        "restatements", "status", "scoreable")})
            existing = {row[0] for row in
                        (await session.execute(Q_EXISTING_OUTCOMES)).all()}
            n_out = 0
            for o in outcomes:
                await session.execute(INSERT_OUTCOME, {
                    "symbol": o["symbol"], "metric_key": o["metric_key"],
                    "period_key": o["period_key"],
                    "guided_low": o["value_low"],
                    "guided_high": o["value_high"],
                    "delivered": o["delivered"],
                    "delivered_verdict": o["delivered_verdict"],
                    "met": o["met"],
                    "basis_consolidated": o["basis_consolidated"],
                    "formula": _formula_of(o),
                    "actual_period_end": o["period_end"],
                    "actual_announced_at": o["actual_announced_at"],
                    "ambiguity_flags": json.dumps(o["ambiguity_flags"]),
                    "source_guidance_id": o["source_guidance_id"]})
                await session.execute(
                    MARK_RESOLVED,
                    {"source_guidance_id": o["source_guidance_id"]})
                n_out += 1
            await complete_sync_log(
                session, log_id, "completed", records_processed=len(rows),
                metadata={"outcomes_written": n_out,
                          "outcomes_preexisting": len(existing)})
            await session.commit()
            logger.info(f"EXECUTE: upserted {len(rows)} ledger rows, "
                        f"{n_out} outcomes")
        except Exception as e:
            await session.rollback()
            await complete_sync_log(session, log_id, "failed",
                                    error_message=str(e))
            await session.commit()
            raise
    return 0


def _formula_of(r: dict) -> str:
    from bharat_terminal.kb.sync.guidance_normalize import canonical_metric
    return canonical_metric(r["metric_raw"])[2] or "none"


def _report(rows, candidates, outcomes) -> None:
    named = [r for r in rows if r["better_direction"] is not None
             or r["scoreable"]]
    logger.info(
        f"normalized: {len(rows)} rows | named-family rows: {len(named)} "
        f"({100 * len(named) / max(1, len(rows)):.1f}%) | scoreable: "
        f"{sum(r['scoreable'] for r in rows)}")
    logger.info("top metric_keys: " + ", ".join(
        f"{k}={c}" for k, c in Counter(
            r["metric_key"] for r in rows).most_common(12)))
    logger.info("lifecycle: " + json.dumps(
        Counter(r["status"] for r in rows)))
    logger.info("change_verdicts (recomputed): " + json.dumps(
        Counter(r["change_verdict"] for r in rows)))
    logger.info(f"outcome candidates (active+scoreable+elapsed): "
                f"{len(candidates)} | scored: {len(outcomes)}")
    for o in outcomes:
        logger.info(
            f"  OUTCOME {o['symbol']} {o['metric_key']} {o['period_key']}: "
            f"guided [{o['value_low']}, {o['value_high']}] {o['unit_key']} "
            f"→ delivered {o['delivered']:.2f} = {o['delivered_verdict']} "
            f"(met={o['met']}, flags={o['ambiguity_flags']})")
    per_co = Counter(o["symbol"] for o in outcomes)
    multi = {s: c for s, c in per_co.items() if c >= 2}
    logger.info(f"credibility-eligible companies (>=2 resolved): "
                f"{len(multi)} {multi or ''} — multiplier stays OFF")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="compute + report, write nothing (default)")
    mode.add_argument("--execute", action="store_true",
                      help="upsert ledger + outcomes")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(run(execute=args.execute))


if __name__ == "__main__":
    sys.exit(main())
