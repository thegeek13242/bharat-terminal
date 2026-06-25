"""
Data-backed calibration of dossier weights. Deterministic and re-runnable —
designed to scale to the full universe (every number recomputable per symbol).

Replaces LLM-guessed continuous parameters with values derived from our own
price panel and NSE financials (the audit showed LLM topology is sound but the
numeric weights are arbitrary: 0.05/0.1-grid values, ~zero correlation with
realized co-movement, prompt-anchored lags):

  A. Linkage strengths  := market-adjusted (partial) return correlation of the
     pair, shrunk toward the linkage-kind mean (shrink weight n/(n+120)),
     clipped to [0.05, 0.95]. Pairs without >=120 common trading days get the
     kind-mean prior. Applied to dossier_linkages.strength and to
     company_relationships.weight for source_of_truth='dossier_agent' rows.
     NSE-stake rows (source_of_truth='nse_shp') are NEVER touched — those are
     filing facts.

  B. Market-sensitivity betas := realized beta vs an equal-weight market factor
     built from our own panel (days with >500 names), for trigger_id in
     (FII_FLOW, NIFTY50, NIFTY_BANK). Clipped to [-2, 2]; confidence -> high.

  C. INPUT_COST channel betas := blend of the LLM beta with a fundamentals
     prior from NSE filings:
        beta_fund = -min(1, PERSISTENCE_K * (1 - pass_through) * cost_share / ebitda_margin)
     where cost_share = cost_of_materials/revenue (latest 4 quarters, avg) and
     pass_through is MEASURED per company from our own quarterly history:
     the regression slope of d(EBITDA margin) on d(cost share) — a margin that
     compresses 1:1 with cost share means zero pass-through; an insensitive
     margin means full pass-through. Estimates are clipped to [0,1] and shrunk
     toward the cross-company mean with weight n/(n+24); companies without
     >=12 usable quarters get the mean. PERSISTENCE_K = 0.5 remains the one
     documented assumption (earnings-shock -> price persistence discount).
     Blend = mean(llm, fund) when LLM agrees in sign, else fundamentals alone.
     Idempotent: a previously blended row records 'prior beta X' in rationale;
     re-runs recover X as the LLM beta instead of double-blending.

Usage:
  python -m bharat_terminal.research.calibrate_weights            # apply
  python -m bharat_terminal.research.calibrate_weights --dry-run  # report only
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import re
from typing import Dict, Optional, Tuple

from sqlalchemy import text

from bharat_terminal.research.db import session_scope

logger = logging.getLogger(__name__)

MIN_COMMON_DAYS = 120     # min overlapping return days for a pair estimate
SHRINK_K = 120            # shrinkage half-weight (w = n / (n + SHRINK_K))
STRENGTH_CLIP = (0.05, 0.95)
BETA_CLIP = (-2.0, 2.0)
MARKET_MIN_NAMES = 500    # min names per day for the equal-weight market factor
MIN_BETA_DAYS = 150
PERSISTENCE_K = 0.5       # documented assumption: earnings shock -> price persistence
PT_SHRINK_K = 24          # pass-through shrinkage half-weight (quarters)
PT_MIN_QUARTERS = 12      # min usable quarter-diffs for a company estimate
MARKET_TRIGGERS = ("FII_FLOW", "NIFTY50", "NIFTY_BANK")

# Shared SQL: daily returns and the equal-weight market factor.
_RETS_CTE = f"""
WITH rets AS (
  SELECT symbol, date,
         close/NULLIF(lag(close) OVER (PARTITION BY symbol ORDER BY date),0) - 1 AS r
  FROM price_points
),
mkt AS (
  SELECT date, avg(r) rm FROM rets WHERE r IS NOT NULL
  GROUP BY date HAVING count(*) > {MARKET_MIN_NAMES}
)
"""


async def _market_stats(s) -> Dict[str, Tuple[float, int]]:
    """symbol -> (corr_with_market, n_days)."""
    rows = (await s.execute(text(_RETS_CTE + f"""
        SELECT x.symbol, corr(x.r, m.rm), count(*)
        FROM rets x JOIN mkt m USING(date)
        WHERE x.r IS NOT NULL
        GROUP BY x.symbol HAVING count(*) >= {MIN_BETA_DAYS}
    """))).all()
    return {r[0]: (float(r[1]), int(r[2])) for r in rows if r[1] is not None}


async def _realized_betas(s) -> Dict[str, float]:
    rows = (await s.execute(text(_RETS_CTE + f"""
        SELECT x.symbol, covar_samp(x.r, m.rm)/NULLIF(var_samp(m.rm),0)
        FROM rets x JOIN mkt m USING(date)
        WHERE x.r IS NOT NULL
        GROUP BY x.symbol HAVING count(*) >= {MIN_BETA_DAYS}
    """))).all()
    return {r[0]: float(r[1]) for r in rows if r[1] is not None}


async def _pair_corrs(s) -> Dict[Tuple[str, str], Tuple[float, int]]:
    """(source,target) -> (raw_corr, n_common_days) for all linkage pairs."""
    rows = (await s.execute(text(_RETS_CTE + f"""
        , pairs AS (SELECT DISTINCT source_symbol s, target_symbol t FROM dossier_linkages)
        SELECT p.s, p.t, corr(a.r, b.r), count(*)
        FROM pairs p
        JOIN rets a ON a.symbol = p.s
        JOIN rets b ON b.symbol = p.t AND b.date = a.date
        WHERE a.r IS NOT NULL AND b.r IS NOT NULL
        GROUP BY p.s, p.t HAVING count(*) >= {MIN_COMMON_DAYS}
    """))).all()
    return {(r[0], r[1]): (float(r[2]), int(r[3])) for r in rows if r[2] is not None}


async def _pass_through(s) -> Tuple[Dict[str, Tuple[float, int]], float]:
    """Measured pass-through per company from our quarterly cost history.

    pass_through = clip(1 + slope, 0, 1) where slope = regr_slope of
    d(ebitda_margin) on d(cost_share) across consolidated quarters
    (|d cost_share| < 0.3 outlier guard). Returns ({symbol: (pt, n)}, mean_pt).
    """
    rows = (await s.execute(text(f"""
        WITH q AS (
          SELECT symbol, period_end,
                 cost_of_materials_cr/NULLIF(revenue_cr,0) AS cs,
                 ebitda_margin_pct/100.0 AS m
          FROM financial_results
          WHERE consolidated AND cost_of_materials_cr IS NOT NULL
                AND revenue_cr > 0 AND ebitda_margin_pct IS NOT NULL
        ), d AS (
          SELECT symbol,
                 cs - lag(cs) OVER (PARTITION BY symbol ORDER BY period_end) AS dcs,
                 m  - lag(m)  OVER (PARTITION BY symbol ORDER BY period_end) AS dm
          FROM q
        )
        SELECT symbol, regr_slope(dm, dcs), count(*)
        FROM d WHERE dcs IS NOT NULL AND dm IS NOT NULL AND abs(dcs) < 0.3
        GROUP BY symbol HAVING count(*) >= {PT_MIN_QUARTERS}
    """))).all()
    pt: Dict[str, Tuple[float, int]] = {}
    for sym, slope, n in rows:
        if slope is None:
            continue
        pt[sym] = (_clip(1.0 + float(slope), 0.0, 1.0), int(n))
    mean_pt = sum(v[0] for v in pt.values()) / max(1, len(pt))
    return pt, mean_pt


# Recover the original LLM beta from a previously blended row (idempotency).
_PRIOR_BETA_RE = re.compile(r"prior beta (-?[0-9.]+)\.")
_FUND_PREFIX_RE = re.compile(
    r"^Fundamentals-blended:[^|]*?prior beta -?[0-9.]+\.\s*")


def _partial_corr(r_ij: float, r_im: float, r_jm: float) -> float:
    denom = math.sqrt(max(1e-9, (1 - r_im**2) * (1 - r_jm**2)))
    return (r_ij - r_im * r_jm) / denom


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


async def calibrate(dry_run: bool) -> None:
    async with session_scope() as s:
        # ── shared market statistics ─────────────────────────────────────
        mkt_corr = await _market_stats(s)
        pair_corr = await _pair_corrs(s)
        logger.info(f"market stats for {len(mkt_corr)} symbols; "
                    f"{len(pair_corr)} linkage pairs with >= {MIN_COMMON_DAYS} days")

        # ── A. linkage strengths ─────────────────────────────────────────
        links = (await s.execute(text(
            "SELECT id, source_symbol, target_symbol, kind, strength FROM dossier_linkages"
        ))).all()

        # partial correlation per pair where data allows
        partials: Dict[int, Tuple[float, int]] = {}
        for lid, src, tgt, kind, _old in links:
            pc = pair_corr.get((src, tgt))
            if not pc or src not in mkt_corr or tgt not in mkt_corr:
                continue
            partials[lid] = (_partial_corr(pc[0], mkt_corr[src][0], mkt_corr[tgt][0]), pc[1])

        # kind-level means of the measured partials (the shrinkage prior)
        kind_acc: Dict[str, list] = {}
        for lid, src, tgt, kind, _old in links:
            if lid in partials:
                kind_acc.setdefault(kind, []).append(partials[lid][0])
        kind_mean = {k: sum(v) / len(v) for k, v in kind_acc.items()}
        overall_mean = (sum(x for v in kind_acc.values() for x in v)
                        / max(1, sum(len(v) for v in kind_acc.values())))
        logger.info("kind-mean partial corr: " +
                    ", ".join(f"{k}={m:.3f}" for k, m in sorted(kind_mean.items())))

        n_meas, n_prior = 0, 0
        for lid, src, tgt, kind, old in links:
            prior = kind_mean.get(kind, overall_mean)
            if lid in partials:
                pc, n = partials[lid]
                w = n / (n + SHRINK_K)
                strength = w * pc + (1 - w) * prior
                n_meas += 1
            else:
                strength = prior   # no price history for the pair (e.g. unlisted)
                n_prior += 1
            strength = round(_clip(strength, *STRENGTH_CLIP), 3)
            if not dry_run:
                await s.execute(text(
                    "UPDATE dossier_linkages SET strength=:st WHERE id=:id"
                ), {"st": strength, "id": lid})
                await s.execute(text("""
                    UPDATE company_relationships
                    SET weight=:st, source_of_truth='price_calibrated'
                    WHERE source_symbol=:src AND target_symbol=:tgt
                      AND relationship_type=:kind AND source_of_truth IN
                          ('dossier_agent','price_calibrated')
                """), {"st": strength, "src": src, "tgt": tgt, "kind": kind})
        logger.info(f"[A] linkage strengths: {n_meas} measured+shrunk, "
                    f"{n_prior} prior-only (no price overlap)")

        # ── B. market-sensitivity betas ──────────────────────────────────
        betas = await _realized_betas(s)
        rows = (await s.execute(text(
            "SELECT id, symbol, trigger_id, beta, rationale FROM dossier_sensitivities "
            "WHERE trigger_id = ANY(:trigs)"), {"trigs": list(MARKET_TRIGGERS)})).all()
        n_b = 0
        for rid, sym, trig, old, rationale in rows:
            rb = betas.get(sym)
            if rb is None:
                continue
            new = round(_clip(rb, *BETA_CLIP), 2)
            already = (rationale or "").startswith("Realized beta")
            if not dry_run:
                if already:  # idempotent re-run: refresh the beta only
                    await s.execute(text(
                        "UPDATE dossier_sensitivities SET beta=:b WHERE id=:id"
                    ), {"b": new, "id": rid})
                else:
                    await s.execute(text(
                        "UPDATE dossier_sensitivities SET beta=:b, confidence='high', "
                        "rationale = 'Realized beta vs equal-weight market factor "
                        "(our price panel, >=150 trading days). Was LLM estimate ' "
                        "|| :old || '. ' || rationale WHERE id=:id"
                    ), {"b": new, "old": str(old), "id": rid})
            n_b += 1
        logger.info(f"[B] market betas updated: {n_b}/{len(rows)}")

        # ── C. INPUT_COST fundamentals blend ─────────────────────────────
        fin = (await s.execute(text("""
            SELECT symbol,
                   avg(cost_of_materials_cr/NULLIF(revenue_cr,0)) cost_share,
                   avg(ebitda_margin_pct)/100.0 margin
            FROM (
              SELECT symbol, cost_of_materials_cr, revenue_cr, ebitda_margin_pct,
                     row_number() OVER (PARTITION BY symbol ORDER BY period_end DESC) rn
              FROM financial_results
              WHERE cost_of_materials_cr IS NOT NULL AND revenue_cr > 0
            ) q WHERE rn <= 4 GROUP BY symbol
        """))).all()
        fund = {r[0]: (float(r[1]), float(r[2] or 0)) for r in fin}

        pt_map, pt_mean = await _pass_through(s)
        logger.info(f"[C] pass-through measured for {len(pt_map)} companies "
                    f"(mean={pt_mean:.2f})")

        rows = (await s.execute(text(
            "SELECT id, symbol, trigger_id, beta, rationale FROM dossier_sensitivities "
            "WHERE channel='INPUT_COST'"))).all()
        n_c = 0
        for rid, sym, trig, old, rationale in rows:
            f = fund.get(sym)
            if not f or f[0] <= 0:
                continue
            cost_share, margin = f[0], max(f[1], 0.05)

            # Measured pass-through, shrunk toward the cross-company mean.
            est = pt_map.get(sym)
            if est:
                w = est[1] / (est[1] + PT_SHRINK_K)
                pt = w * est[0] + (1 - w) * pt_mean
            else:
                pt = pt_mean
            beta_fund = -min(1.0, PERSISTENCE_K * (1 - pt) * cost_share / margin)

            # Idempotency: if a previous run blended this row, recover the
            # original LLM beta from the rationale marker instead of the
            # current (already blended) value, and strip the old prefix.
            rationale = rationale or ""
            m = _PRIOR_BETA_RE.search(rationale)
            llm_beta = float(m.group(1)) if m else float(old)
            base_rationale = _FUND_PREFIX_RE.sub("", rationale)

            new = (llm_beta + beta_fund) / 2 if llm_beta < 0 else beta_fund
            new = round(_clip(new, *BETA_CLIP), 2)
            if not dry_run:
                await s.execute(text(
                    "UPDATE dossier_sensitivities SET beta=:b, rationale = "
                    "'Fundamentals-blended: cost_share=' || :cs || "
                    "', ebitda_margin=' || :mg || ', pass_through=' || :pt || "
                    "' (' || :pts || '), prior beta ' || :old || '. ' || :base "
                    "WHERE id=:id"
                ), {"b": new, "cs": f"{cost_share:.3f}", "mg": f"{margin:.3f}",
                    "pt": f"{pt:.2f}",
                    "pts": "measured" if est else "universe-mean",
                    "old": f"{llm_beta:g}", "base": base_rationale, "id": rid})
            n_c += 1
        logger.info(f"[C] INPUT_COST betas blended: {n_c}/{len(rows)} "
                    f"(measured pt: {sum(1 for _,sym,_,_,_ in rows if sym in pt_map)})")

        if dry_run:
            logger.info("DRY RUN — no writes performed")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(prog="calibrate_weights")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(calibrate(args.dry_run))


if __name__ == "__main__":
    main()
