"""PEAD A/B: does a BETTER EXPECTATION sharpen the validated drift signal?

The question (Stage 3 follow-up, 2026-08-26). The live PEAD strategy ranks
events by composite-SUE, whose implied expectation is seasonal-naive PLUS
the company's own mean prior YoY change:

    SUE = (Y[t] - Y[t-4] - mu_prior) / sigma_prior          [validated: D10-D1
                                                             +2.11% @20d, t=4.7]

Stage 3 showed that expectation ("snd") is WEAK — a global cross-learning
GBM beats it by +12.8% (pat) to +20.7% (revenue) out-of-sample. So: swap
only the expectation, hold everything else fixed, and see whether the decile
spread improves.

    SUE_gbm = (Y[t] - gbm_forecast) / sigma_prior

**Both arms share sigma_prior, the winsor cap, the cohort deciling and the
return window.** The ONLY difference is the expectation, so any change in
spread is attributable to it and nothing else. This is a hedge on the
E-series thesis: it pays off whether or not text-derived expectations
(guidance/commentary) later prove out, because it needs no text at all.

Honest limits, stated up front:
  * Cohorts start where the GBM has training data (2021+), so this is ~20
    quarterly cohorts, not the 51 of the original study. Both arms use the
    SAME cohorts, so the paired comparison is valid; the absolute spreads
    are not directly comparable to the validated number.
  * Abnormal return = stock minus NIFTYBEES over the same window. A large-cap
    index is a poor benchmark for microcaps, but it is applied identically to
    both arms.
  * PIT: financial_results rows are revised in place (Stage 0). Affects both
    arms equally; absolute levels are flattered, the comparison is not.
  * Close-to-close from D+1, i.e. it EXCLUDES the announcement gap (the S0
    lesson: gap returns are not capturable).

Read-only; writes nothing.
  python -m bharat_terminal.research.pead_expectations_ab
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
from datetime import date
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
np.random.seed(42)

SUE_WINSOR = 5.0            # matches research/pead_signals.py v2.1
MIN_PRIOR_OBS = 6
HOLD_DAYS = 20              # trading days, the validated horizon
BENCHMARK = "NIFTYBEES"
TEST_FROM = 2021            # first cohort year the GBM can be tested on
FEATURES = ["sd1", "sd2", "sd3", "sd4", "qoq1", "trend", "vol",
            "nhist", "logscale", "fq", "snd_adj"]
GBM_KW = dict(max_iter=300, learning_rate=0.06, max_depth=6,
              min_samples_leaf=200, l2_regularization=1.0,
              early_stopping=True, validation_fraction=0.15, random_state=42)

_FIN_SQL = """
SELECT symbol, period_end, consolidated, eps_basic, revenue_cr
FROM financial_results WHERE period_type = 'quarterly' AND period_end >= '2015-01-01'
"""
_EVT_SQL = """
SELECT symbol, period_end, announced_at, sue_composite
FROM earnings_surprises WHERE announced_at IS NOT NULL
"""
_PX_SQL = "SELECT symbol, date, close FROM price_points WHERE close > 0"


def shift_quarter(d: date, n: int) -> date:
    """Exact port of research/pead_signals.shift_quarter."""
    m = d.month + 3 * n
    y = d.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    last = [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, last)


def _prior_yoy(hist: Dict[date, float], pe: date) -> List[float]:
    out = []
    for back in range(1, 13):
        q = shift_quarter(pe, -back)
        q4 = shift_quarter(q, -4)
        if q in hist and q4 in hist:
            out.append(hist[q] - hist[q4])
    return out


def build_rows(fin: pd.DataFrame, col: str) -> pd.DataFrame:
    """Per (company, quarter): the naive-SUE inputs AND the GBM feature
    vector, all from history strictly before that quarter."""
    recs = []
    for sym, g in fin.groupby("symbol", sort=False):
        hist = {r.period_end: float(getattr(r, col)) for r in g.itertuples()
                if getattr(r, col) is not None and np.isfinite(getattr(r, col))}
        for pe in sorted(hist):
            p4 = shift_quarter(pe, -4)
            if p4 not in hist:
                continue
            prior = _prior_yoy(hist, pe)
            if len(prior) < MIN_PRIOR_OBS:
                continue
            mu = float(np.mean(prior))
            sd = float(np.std(prior, ddof=1))
            if not np.isfinite(sd) or sd <= 1e-9:
                continue
            lag = {k: hist.get(shift_quarter(pe, -k)) for k in range(1, 9)}
            lvl = [v for k, v in lag.items() if k <= 4 and v is not None]
            if len(lvl) < 3:
                continue
            scale = float(np.mean(np.abs(lvl)))
            if not np.isfinite(scale) or scale <= 1e-6:
                continue

            def sdiff(k):
                a, b = lag.get(k), lag.get(k + 4)
                return (a - b) / scale if a is not None and b is not None else np.nan

            prior4 = np.mean([v for k, v in lag.items() if k <= 4 and v is not None])
            p58 = [v for k, v in lag.items() if 5 <= k <= 8 and v is not None]
            recs.append({
                "symbol": sym, "period_end": pe,
                "actual": hist[pe], "y4": hist[p4], "scale": scale,
                "mu_prior": mu, "sigma_prior": sd,
                "sd1": sdiff(1), "sd2": sdiff(2), "sd3": sdiff(3), "sd4": sdiff(4),
                "qoq1": ((lag[1] - lag[2]) / scale
                         if lag.get(1) is not None and lag.get(2) is not None else np.nan),
                "trend": ((prior4 - np.mean(p58)) / scale if p58 else np.nan),
                "vol": float(np.std(prior[:8]) / scale) if len(prior) >= 3 else np.nan,
                "nhist": float(len(hist)), "logscale": float(np.log10(scale)),
                "fq": {3: 4, 6: 1, 9: 2, 12: 3}.get(pe.month, np.nan),
                "snd_adj": mu / scale,
            })
    df = pd.DataFrame(recs)
    if not df.empty:
        # target: scaled deviation from the seasonal-naive level, so the
        # GBM learns the ADJUSTMENT and naive == predicting zero.
        df["y"] = ((df["actual"] - df["y4"]) / df["scale"]).clip(-3, 3)
    return df


def add_gbm(df: pd.DataFrame) -> pd.DataFrame:
    """Expanding-window annual retrain; predicts the scaled deviation from
    Y[t-4], from which the GBM expectation follows."""
    from sklearn.ensemble import HistGradientBoostingRegressor
    df = df.copy()
    df["year"] = pd.to_datetime(df["period_end"]).dt.year
    out = []
    for yr in sorted(y for y in df["year"].unique() if y >= TEST_FROM):
        tr, te = df[df["year"] < yr], df[df["year"] == yr]
        if len(tr) < 2000 or te.empty:
            continue
        m = HistGradientBoostingRegressor(**GBM_KW)
        m.fit(tr[FEATURES].to_numpy(float), tr["y"].to_numpy(float))
        t = te.copy()
        t["gbm_fc"] = t["y4"] + m.predict(te[FEATURES].to_numpy(float)) * t["scale"]
        out.append(t)
    return pd.concat(out) if out else pd.DataFrame()


def sue_arms(df: pd.DataFrame) -> pd.DataFrame:
    w = SUE_WINSOR
    df = df.copy()
    df["sue_naive"] = ((df["actual"] - df["y4"] - df["mu_prior"])
                       / df["sigma_prior"]).clip(-w, w)
    df["sue_gbm"] = ((df["actual"] - df["gbm_fc"])
                     / df["sigma_prior"]).clip(-w, w)
    return df


def forward_abnormal(evt: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    """Close-to-close abnormal return over HOLD_DAYS trading days, entered
    the first trading day AFTER the announcement (the gap is excluded)."""
    px = px.sort_values(["symbol", "date"])
    series = {s: (g["date"].to_numpy(), g["close"].to_numpy(float))
              for s, g in px.groupby("symbol", sort=False)}
    if BENCHMARK not in series:
        raise RuntimeError(f"benchmark {BENCHMARK} missing from price_points")
    b_dates, b_close = series[BENCHMARK]
    rows = []
    for r in evt.itertuples():
        s = series.get(r.symbol)
        if s is None:
            continue
        d, c = s
        i = int(np.searchsorted(d, np.datetime64(r.ann_date), side="right"))
        j = i + HOLD_DAYS
        if j >= len(d):
            continue
        stock = c[j] / c[i] - 1.0
        bi = int(np.searchsorted(b_dates, d[i], side="left"))
        bj = int(np.searchsorted(b_dates, d[j], side="left"))
        if bi >= len(b_close) or bj >= len(b_close):
            continue
        bench = b_close[bj] / b_close[bi] - 1.0
        rows.append({"symbol": r.symbol, "period_end": r.period_end,
                     "abn": stock - bench})
    return pd.DataFrame(rows)


def decile_spread(df: pd.DataFrame, col: str) -> dict:
    """Per-cohort D10-D1 on `col`, then the t-stat across cohorts — the
    validated study's design."""
    spreads, ns = [], []
    for _, g in df.groupby("cohort"):
        if len(g) < 100:
            continue
        try:
            dec = pd.qcut(g[col].rank(method="first"), 10, labels=False)
        except ValueError:
            continue
        top = g.loc[dec == 9, "abn"].mean()
        bot = g.loc[dec == 0, "abn"].mean()
        if np.isfinite(top) and np.isfinite(bot):
            spreads.append(top - bot)
            ns.append(len(g))
    if len(spreads) < 4:
        return {"cohorts": len(spreads)}
    a = np.array(spreads)
    t = float(a.mean() / (a.std(ddof=1) / math.sqrt(len(a))))
    return {"cohorts": len(a), "mean_spread_pct": float(a.mean() * 100),
            "t_stat": t, "hit_cohorts": int((a > 0).sum()),
            "median_n_per_cohort": int(np.median(ns))}


async def load() -> tuple:
    import asyncpg
    dsn = (os.getenv("DATABASE_URL") or "").replace(
        "postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn=dsn, timeout=30, command_timeout=300)
    try:
        fin = pd.DataFrame([dict(r) for r in await conn.fetch(_FIN_SQL)])
        evt = pd.DataFrame([dict(r) for r in await conn.fetch(_EVT_SQL)])
        px = pd.DataFrame([dict(r) for r in await conn.fetch(_PX_SQL)])
    finally:
        await conn.close()
    px["date"] = pd.to_datetime(px["date"])
    return fin, evt, px


async def main_async() -> int:
    fin, evt, px = await load()
    logger.info(f"loaded fin={len(fin):,} evt={len(evt):,} px={len(px):,}")
    # Pin one basis per company (consolidated preferred by observation count)
    cnt = fin.dropna(subset=["revenue_cr"]).groupby(
        ["symbol", "consolidated"]).size().rename("n").reset_index()
    cnt["pref"] = cnt["consolidated"].astype(int)
    best = (cnt.sort_values(["symbol", "n", "pref"], ascending=[True, False, False])
               .drop_duplicates("symbol")[["symbol", "consolidated"]])
    fin = fin.merge(best, on=["symbol", "consolidated"])

    evt = evt.copy()
    evt["ann_date"] = pd.to_datetime(evt["announced_at"]).dt.tz_convert(
        "Asia/Kolkata").dt.date
    abn = forward_abnormal(evt, px)
    logger.info(f"forward returns computed for {len(abn):,} events")

    arms = {}
    for metric, col in (("eps", "eps_basic"), ("revenue", "revenue_cr")):
        rows = build_rows(fin, col)
        g = add_gbm(rows)
        if g.empty:
            continue
        arms[metric] = sue_arms(g)[["symbol", "period_end", "sue_naive", "sue_gbm"]] \
            .rename(columns={"sue_naive": f"n_{metric}", "sue_gbm": f"g_{metric}"})
        logger.info(f"{metric}: {len(g):,} scored quarters")

    d = arms["eps"].merge(arms["revenue"], on=["symbol", "period_end"], how="inner")
    d["composite_naive"] = d[["n_eps", "n_revenue"]].mean(axis=1)
    d["composite_gbm"] = d[["g_eps", "g_revenue"]].mean(axis=1)
    # Hybrid: the components behave OPPOSITELY (see the report) — naive
    # expectation wins for EPS, GBM wins for revenue. POST-HOC construction:
    # it must be pre-registered and re-validated out-of-sample before it is
    # trusted; with 18 cohorts and 7 arms, selection on noise is a live risk.
    d["composite_hybrid"] = d[["n_eps", "g_revenue"]].mean(axis=1)
    d = d.merge(abn, on=["symbol", "period_end"], how="inner")
    d["cohort"] = pd.to_datetime(d["period_end"]).dt.to_period("Q").astype(str)

    print(f"\n=== PEAD A/B: expectation swap (n={len(d):,} events, "
          f"{d['cohort'].nunique()} cohorts, {HOLD_DAYS}d abnormal vs {BENCHMARK}) ===\n")
    print(f"{'arm':22} {'cohorts':>8} {'D10-D1':>9} {'t-stat':>8} {'cohorts>0':>10} {'med n':>7}")
    res = {}
    for label, col in (("composite (naive)", "composite_naive"),
                       ("composite (GBM)", "composite_gbm"),
                       ("eps only (naive)", "n_eps"), ("eps only (GBM)", "g_eps"),
                       ("revenue only (naive)", "n_revenue"),
                       ("revenue only (GBM)", "g_revenue"),
                       ("HYBRID naive-eps+gbm-rev", "composite_hybrid")):
        r = decile_spread(d, col)
        res[col] = r
        if "t_stat" in r:
            print(f"{label:22} {r['cohorts']:>8} {r['mean_spread_pct']:>8.2f}% "
                  f"{r['t_stat']:>8.2f} {r['hit_cohorts']:>10} {r['median_n_per_cohort']:>7}")
    a, b = res.get("composite_naive", {}), res.get("composite_gbm", {})
    if "mean_spread_pct" in a and "mean_spread_pct" in b:
        print(f"\nexpectation swap effect: {b['mean_spread_pct'] - a['mean_spread_pct']:+.2f}pp "
              f"on the D10-D1 spread ({a['mean_spread_pct']:.2f}% -> {b['mean_spread_pct']:.2f}%)")
    print(json.dumps({k: v for k, v in res.items()}, indent=2, default=str)[:0])
    return 0


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
