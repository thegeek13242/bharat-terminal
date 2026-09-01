"""E1 composite (E-series Stage 4) — expectations from a stacked estimator set.

v1 estimators (scope frozen 2026-09-01):
  #1 guidance          T1, ~624 cos with labelled outcomes   [pending]
  #3 statistical GBM   T4, universe-wide                     [Stage 3, done]
  #4 peer read-across  55 industry groups / 3,327 cos        [THIS MODULE]
  #6 pre-event run-up  universe                              [E3 feature]
(#2 business updates optional/flag; #5 options out; #7 news consensus deferred.)

This module starts with the estimator that was un-cut last: PEER READ-ACROSS.
When a company reports, most of its industry has usually already reported the
same quarter (measured 2026-09-01: 72.3% of events have 10+ peers already out,
only 3.5% have none), so peer results carry information about the quarter's
industry-wide conditions that a company's own history cannot.

PIT rule, absolutely load-bearing: a peer counts only if
`peers.announced_at < this event's announced_at`. Using same-quarter peers
without that filter would let the future leak in wholesale — the single
easiest way to fake a good expectations model.

Validation before construction (the house pattern): peer features are added
to the Stage 3 GBM and the out-of-sample skill delta is measured. If they do
not earn their place they do not enter the composite.

  python -m bharat_terminal.research.e1_composite --validate-peers
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Optional

import re

import numpy as np
import pandas as pd

from bharat_terminal.research.seasonal_baseline import (FEATURES, GBM_KW,
                                                        METRICS, WINSOR,
                                                        load_panel,
                                                        run_backtest)

logger = logging.getLogger(__name__)
np.random.seed(42)

PEER_FEATURES = ["p_n", "p_median_dev", "p_disp", "p_beat_rate", "p_frac_out"]
MIN_PEERS = 3          # below this the median is noise; features go NaN

# ── Estimator #1: management guidance (T1) ───────────────────────────────
# The only estimator that can plausibly capture WHAT THE MARKET EXPECTED
# rather than extrapolate fundamentals — which, after the Stage 4A reframe,
# is E1's actual target. Guidance is overwhelmingly ANNUAL (FY) and stated
# as growth or margin percentages, so converting it to a quarterly level
# expectation is most of the work.
#
# PIT rule: only guidance with stated_at < the target's announced_at, and
# whose period actually covers the target quarter.
_GUIDANCE_SQL = """
SELECT g.symbol, g.metric_key, g.metric_raw, g.period_key, g.period_end AS g_period_end,
       g.value_low, g.value_high, g.unit_key, g.stated_at
FROM guidance_ledger g
WHERE g.scoreable AND g.stated_at IS NOT NULL
  AND g.metric_key IN ('revenue','revenue_growth','ebitda_margin','ebitda',
                       'pat_margin','pat','pat_growth','ebitda_growth')
"""


def fy_label(period_end) -> str:
    """Indian fiscal year ending 31 March: Jun-2026 quarter is in FY27."""
    d = pd.Timestamp(period_end)
    y = d.year if d.month <= 3 else d.year + 1
    return f"FY{str(y)[-2:]}"


# The ledger's metric_key='revenue' bucket CONFLATES four different things
# (verified 2026-09-01 by reading metric_raw): total-company revenue,
# SEGMENT revenue ("EPC business revenue", "paint business revenue"),
# INCREMENTAL revenue ("Total incremental revenue from capacity expansions")
# and multi-year ASPIRATIONS (VSTTILLERS guiding FY29). Segment/incremental
# figures are naturally a fraction of company revenue, which is why absolute
# guidance scored a median guided/actual ratio of 0.15 before this filter.
# Percentage-growth guidance is unaffected (ratio 1.02) because "revenue
# growth of 15%" is stated at company level.
#
# This is a GAP IN THE LEDGER, not just here: guidance needs a `scope`
# dimension (total | segment | incremental) that C0's extraction does not
# capture. It matters for Paper A too — a fulfillment label on segment
# guidance is not the same claim as one on company guidance.
# HARD rejects describe a DELTA, not a level — no "total" qualifier can
# rescue them. Caught by test 2026-09-01: "Total incremental revenue from
# capacity expansions" (GOKEX) was being accepted as company revenue because
# "Total" matched the override, when "total" there modifies *incremental*.
_DELTA_RE = re.compile(
    r"\b(incremental|addition(al)?|accretion|from\s+capacity|"
    r"capacity\s+expansion|new\s+capacity)\b", re.I)
# Soft rejects name a PART of the company — overridable by an explicit
# company-level qualifier ("total company business").
_SEGMENT_RE = re.compile(
    r"\b(segment|business|division|vertical|subsidiary|export|domestic|"
    r"new\s+business|product|brand|international|overseas)\b", re.I)
_TOTAL_RE = re.compile(r"\b(total|consolidated|company|overall|group)\b", re.I)


def is_company_level(metric_raw: str) -> bool:
    """True when the guidance plainly refers to the WHOLE company. Ambiguity
    resolves to False — a segment figure used as a company expectation is a
    silent 6x error, so refusing is far cheaper than guessing."""
    t = (metric_raw or "").strip()
    if not t:
        return True                     # bare "revenue" == company level
    if _DELTA_RE.search(t):
        return False                    # a delta is never a level
    if _SEGMENT_RE.search(t) and not _TOTAL_RE.search(t):
        return False
    return True


def horizon_ok(period_key: str, target_fy: str) -> bool:
    """Reject aspirational targets more than one fiscal year beyond the
    quarter being forecast (VSTTILLERS guiding FY29 says nothing about a
    FY27 quarter)."""
    try:
        if not (period_key or "").startswith("FY") or not target_fy.startswith("FY"):
            return True
        return int(period_key[2:]) - int(target_fy[2:]) <= 1
    except (ValueError, IndexError):
        return True


def _mid(lo, hi):
    vals = [v for v in (lo, hi) if v is not None and np.isfinite(v)]
    return float(np.mean(vals)) if vals else None


def guidance_expectation(g_rows: pd.DataFrame, y4: float, trailing_year: float,
                         metric: str) -> Optional[tuple]:
    """(expected_value, band_low, band_high, basis) from the most recent
    applicable guidance, or None when nothing converts.

    Conversions, all PIT-safe (they use only y[t-4] and the trailing year
    ending there):
      revenue_growth %  -> y4 * (1 + g)
      revenue cr ANNUAL -> annual * (y4 / trailing_year)   [seasonal share]
      ebitda_margin %   -> expected_revenue * margin
    Refuses rather than guesses when the seasonal share is unavailable —
    a fabricated expectation is worse than no tier.
    """
    if g_rows.empty:
        return None
    latest = g_rows.sort_values("stated_at").groupby("metric_key").tail(1)
    by = {r.metric_key: r for r in latest.itertuples()}
    share = (y4 / trailing_year) if (trailing_year and trailing_year > 0
                                     and np.isfinite(trailing_year)) else None

    def level_from(key_abs, key_growth):
        r = by.get(key_growth)
        if r is not None and (r.unit_key or "") == "pct":
            lo, hi = r.value_low, r.value_high
            m = _mid(lo, hi)
            if m is not None and y4 is not None:
                return (y4 * (1 + m / 100.0),
                        y4 * (1 + (lo if lo is not None else m) / 100.0),
                        y4 * (1 + (hi if hi is not None else m) / 100.0),
                        f"{key_growth}_pct")
        r = by.get(key_abs)
        if r is not None and (r.unit_key or "") == "cr":
            m = _mid(r.value_low, r.value_high)
            if m is None:
                return None
            annual = str(r.period_key or "").startswith("FY")
            if annual:
                if share is None:
                    return None          # refuse: no seasonal share
                f = share
            else:
                f = 1.0
            lo = (r.value_low if r.value_low is not None else m) * f
            hi = (r.value_high if r.value_high is not None else m) * f
            return (m * f, lo, hi, f"{key_abs}_{'annual' if annual else 'qtr'}")
        return None

    if metric == "revenue":
        return level_from("revenue", "revenue_growth")
    rev = level_from("revenue", "revenue_growth")
    key = "ebitda_margin" if metric == "ebitda" else "pat_margin"
    r = by.get(key)
    if r is not None and (r.unit_key or "") == "pct" and rev is not None:
        m = _mid(r.value_low, r.value_high)
        if m is not None:
            lo = (r.value_low if r.value_low is not None else m)
            hi = (r.value_high if r.value_high is not None else m)
            return (rev[0] * m / 100.0, rev[1] * lo / 100.0,
                    rev[2] * hi / 100.0, f"{key}_x_revenue")
    return level_from(metric, f"{metric}_growth")

_META_SQL = """
SELECT es.symbol, es.period_end, es.announced_at, c.industry, c.basic_industry
FROM earnings_surprises es
JOIN companies c ON c.symbol = es.symbol
WHERE es.announced_at IS NOT NULL AND c.industry IS NOT NULL
"""


async def load_meta() -> pd.DataFrame:
    import asyncpg
    dsn = (os.getenv("DATABASE_URL") or "").replace(
        "postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn=dsn, timeout=30, command_timeout=180)
    try:
        rows = await conn.fetch(_META_SQL)
    finally:
        await conn.close()
    df = pd.DataFrame([dict(r) for r in rows])
    df["period_end"] = pd.to_datetime(df["period_end"])
    df["announced_at"] = pd.to_datetime(df["announced_at"], utc=True)
    return df


def add_peer_features(bt: pd.DataFrame, meta: pd.DataFrame,
                      key: str = "industry") -> pd.DataFrame:
    """Expanding, announcement-ordered peer aggregates within (industry,
    period_end). Every value for row i uses ONLY peers announced strictly
    before row i — enforced by sorting on announced_at and taking a strict
    prefix, never a group-wide statistic."""
    d = bt.merge(meta, on=["symbol", "period_end"], how="inner")
    d["dev"] = ((d["actual"] - d["sn"]) / d["scale"]).clip(-WINSOR, WINSOR)
    out = []
    d = d[d[key].notna()]
    for _, g in d.groupby([key, "period_end"], sort=False):
        g = g.sort_values("announced_at").reset_index(drop=True)
        devs = g["dev"].to_numpy(float)
        ann = g["announced_at"].to_numpy()
        n = len(g)
        p_n = np.zeros(n); p_med = np.full(n, np.nan)
        p_disp = np.full(n, np.nan); p_beat = np.full(n, np.nan)
        for i in range(n):
            # strict prefix: ties on announced_at are EXCLUDED (a peer that
            # printed in the same second is not prior information).
            k = int(np.searchsorted(ann, ann[i], side="left"))
            if k == 0:
                continue
            prior = devs[:k]
            prior = prior[np.isfinite(prior)]
            p_n[i] = prior.size
            if prior.size >= MIN_PEERS:
                p_med[i] = float(np.median(prior))
                p_disp[i] = float(np.percentile(prior, 75) - np.percentile(prior, 25))
                p_beat[i] = float((prior > 0).mean())
        g["p_n"] = p_n
        g["p_median_dev"] = p_med
        g["p_disp"] = p_disp
        g["p_beat_rate"] = p_beat
        # How far through the industry's reporting season this event is —
        # early reporters have little peer info, late ones a lot.
        g["p_frac_out"] = p_n / max(1, n - 1)
        out.append(g)
    return pd.concat(out) if out else d


def gbm_skill_frame(d: pd.DataFrame, features: list):
    """Same expanding-window retrain as gbm_skill, but returns the per-row
    predictions (with announced_at carried through) instead of a summary."""
    from sklearn.ensemble import HistGradientBoostingRegressor
    d = d.copy()
    d["f_fq"] = pd.to_datetime(d["period_end"]).dt.month.map(
        {3: 4, 6: 1, 9: 2, 12: 3}).astype(float)
    d["y"] = ((d["actual"] - d["sn"]) / d["scale"]).clip(-WINSOR, WINSOR)
    preds = []
    for yr in sorted({int(y) for y in pd.to_datetime(d["period_end"]).dt.year
                      if y >= 2023}):
        cut = pd.Timestamp(f"{yr}-01-01")
        tr = d[pd.to_datetime(d["period_end"]) < cut]
        te = d[(pd.to_datetime(d["period_end"]) >= cut)
               & (pd.to_datetime(d["period_end"]) < pd.Timestamp(f"{yr+1}-01-01"))]
        if len(tr) < 2000 or te.empty:
            continue
        m = HistGradientBoostingRegressor(**GBM_KW)
        m.fit(tr[features].to_numpy(float), tr["y"].to_numpy(float))
        t = te.copy()
        t["adj"] = m.predict(te[features].to_numpy(float))
        preds.append(t)
    return pd.concat(preds) if preds else None


def gbm_skill(d: pd.DataFrame, features: list, label: str) -> dict:
    """Expanding-window annual retrain, identical to Stage 3 so the two
    numbers are directly comparable."""
    from sklearn.ensemble import HistGradientBoostingRegressor
    d = d.copy()
    d["f_fq"] = pd.to_datetime(d["period_end"]).dt.month.map(
        {3: 4, 6: 1, 9: 2, 12: 3}).astype(float)
    d["y"] = ((d["actual"] - d["sn"]) / d["scale"]).clip(-WINSOR, WINSOR)
    years = sorted({int(y) for y in pd.to_datetime(d["period_end"]).dt.year
                    if y >= 2023})
    preds = []
    for yr in years:
        cut = pd.Timestamp(f"{yr}-01-01")
        tr = d[pd.to_datetime(d["period_end"]) < cut]
        te = d[(pd.to_datetime(d["period_end"]) >= cut)
               & (pd.to_datetime(d["period_end"]) < pd.Timestamp(f"{yr+1}-01-01"))]
        if len(tr) < 2000 or te.empty:
            continue
        m = HistGradientBoostingRegressor(**GBM_KW)
        m.fit(tr[features].to_numpy(float), tr["y"].to_numpy(float))
        t = te.copy()
        t["adj"] = m.predict(te[features].to_numpy(float))
        preds.append(t)
    if not preds:
        return {}
    g = pd.concat(preds)
    en = g["y"].abs()
    eg = (g["y"] - g["adj"]).clip(-WINSOR, WINSOR).abs()
    truth, pred = np.sign(g["y"]), np.sign(g["adj"])
    mask = (truth != 0) & (pred != 0)
    return {"label": label, "n": int(len(g)),
            "mae": float(eg.mean()), "mae_naive": float(en.mean()),
            "skill": float(1 - eg.mean() / en.mean()),
            "direction": float((truth[mask] == pred[mask]).mean()),
            "floor": float((g["y"] > 0).mean())}


async def validate_peers(metrics: list) -> int:
    panel = await load_panel()
    meta = await load_meta()
    logger.info(f"meta: {len(meta):,} announced events with industry")
    for metric in metrics:
        bt = run_backtest(panel, metric)
        print(f"\n=== E1 estimator #4 — peer read-across — {metric.upper()} ===")
        rows = []
        for key in ("industry", "basic_industry"):
            d = add_peer_features(bt, meta, key=key)
            cov = float((d["p_n"] >= MIN_PEERS).mean())
            if not rows:
                rows.append(gbm_skill(d, FEATURES, "base (Stage 3)"))
            r = gbm_skill(d, FEATURES + PEER_FEATURES, f"+ peers by {key}")
            r["cov"] = cov
            rows.append(r)
        rows = [r for r in rows if r]
        if len(rows) < 2:
            print("insufficient data"); continue
        print(f"{'model':26} {'MAE':>8} {'skill':>8} {'direction':>10} {'peer cov':>9}")
        for r in rows:
            print(f"{r['label']:26} {r['mae']:8.4f} {r['skill']:+7.1%} "
                  f"{r['direction']:>9.1%} "
                  f"{(format(r['cov'],'.1%') if 'cov' in r else '-'):>9}")
        b = rows[0]
        for r in rows[1:]:
            print(f"  {r['label']}: {100*(r['skill']-b['skill']):+.1f}pp skill, "
                  f"{100*(r['direction']-b['direction']):+.1f}pp direction")
    return 0


# v1.1: scope filter corrected — "Total incremental ..." was passing as
# company-level (see _DELTA_RE). v1 rows are kept; consumers filter on
# model_version, which is why the table is append-only.
MODEL_VERSION = "e1-v1.1-2026-09-01"

_INSERT_SQL = """
INSERT INTO expectations
  (symbol, period_end, metric, estimator, expected_value, band_low,
   band_high, tier, basis, inputs_max_ts, as_of, provenance, model_version)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13)
ON CONFLICT ON CONSTRAINT uq_expectation DO NOTHING
"""


async def write_expectations(metrics: list, execute: bool,
                             limit: Optional[int]) -> int:
    """Compute and persist BOTH expectations per (company, quarter, metric).

    inputs_max_ts is the load-bearing field: the latest timestamp of any
    input that fed the row. For `gbm` that is the announcement of the most
    recent quarter in its feature window; for `guidance` it is the
    guidance's own stated_at. The leakage audit asserts it is strictly
    before the target event's announced_at, which is what makes
    "no look-ahead" a checkable property rather than a promise.
    """
    import asyncpg
    panel = await load_panel()
    meta = await load_meta()
    dsn = (os.getenv("DATABASE_URL") or "").replace(
        "postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn=dsn, timeout=30, command_timeout=300)
    try:
        gl = pd.DataFrame([dict(r) for r in await conn.fetch(_GUIDANCE_SQL)])
        gl["stated_at"] = pd.to_datetime(gl["stated_at"], utc=True)
        # Announcement of the PREVIOUS quarter = the newest fact the GBM's
        # feature window can legitimately contain.
        prev = meta.sort_values("announced_at").copy()
        prev["prev_ann"] = prev.groupby("symbol")["announced_at"].shift(1)
        prev = prev[["symbol", "period_end", "prev_ann"]]

        stats = {"gbm": 0, "guidance": 0, "skipped_no_prev": 0}
        for metric in metrics:
            bt = run_backtest(panel, metric)
            d = add_peer_features(bt, meta, key="industry")
            gb = gbm_skill_frame(d, FEATURES + PEER_FEATURES)
            if gb is None or gb.empty:
                continue
            gb = gb.merge(prev, on=["symbol", "period_end"], how="left")
            gb = gb.sort_values(["symbol", "period_end"])
            gb["trailing_year"] = (gb.groupby("symbol")["sn"]
                                   .transform(lambda x: x.rolling(4, min_periods=4).sum()))
            gl_by_sym = {k: v for k, v in gl.groupby("symbol", sort=False)}
            rows = []
            for r in gb.itertuples():
                if limit and len(rows) >= limit:
                    break
                if pd.isna(r.prev_ann):
                    stats["skipped_no_prev"] += 1
                    continue
                pe = pd.Timestamp(r.period_end).date()
                gbm_val = r.sn + r.adj * r.scale
                rows.append((r.symbol, pe, metric, "gbm", float(gbm_val),
                             None, None, "T4", "global_gbm_peer",
                             r.prev_ann.to_pydatetime(),
                             r.announced_at.to_pydatetime(),
                             json.dumps({"scale": float(r.scale),
                                         "seasonal_naive": float(r.sn),
                                         "adj_scaled": float(r.adj),
                                         "peer_n": float(r.p_n)}),
                             MODEL_VERSION))
                stats["gbm"] += 1
                g = gl_by_sym.get(r.symbol)
                if g is None:
                    continue
                fy = fy_label(r.period_end)
                app = g[(g["stated_at"] < r.announced_at)
                        & ((g["period_key"] == fy)
                           | (g["g_period_end"] == pe))]
                if not app.empty:
                    app = app[app["metric_raw"].map(is_company_level)
                              & app["period_key"].map(lambda k: horizon_ok(k, fy))]
                got = guidance_expectation(app, r.sn, r.trailing_year, metric)
                if got is None or not np.isfinite(got[0]):
                    continue
                exp, lo, hi, basis = got
                rows.append((r.symbol, pe, metric, "guidance", float(exp),
                             float(lo) if np.isfinite(lo) else None,
                             float(hi) if np.isfinite(hi) else None,
                             "T1", basis,
                             app["stated_at"].max().to_pydatetime(),
                             r.announced_at.to_pydatetime(),
                             json.dumps({"n_items": int(len(app)),
                                         "metrics": sorted(set(app["metric_key"]))}),
                             MODEL_VERSION))
                stats["guidance"] += 1
            if execute and rows:
                await conn.executemany(_INSERT_SQL, rows)
            logger.info(f"{metric}: {len(rows):,} expectation rows "
                        f"{'WRITTEN' if execute else '(dry-run)'}")
    finally:
        await conn.close()
    logger.info(f"{'EXECUTE' if execute else 'DRY-RUN'} totals: {stats}")
    return 0


async def leakage_audit() -> int:
    """The Stage 4 exit gate. Every stored expectation must have been
    computable strictly BEFORE its event was announced. Three independent
    checks, all run against the persisted table rather than the code that
    wrote it — so a future writer bug is caught by the same gate."""
    import asyncpg
    dsn = (os.getenv("DATABASE_URL") or "").replace(
        "postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn=dsn, timeout=30, command_timeout=300)
    try:
        checks = {
            "inputs after announcement (HARD LEAK)": """
                SELECT count(*) FROM expectations e
                JOIN earnings_surprises s USING (symbol, period_end)
                WHERE e.inputs_max_ts >= s.announced_at""",
            "as_of disagrees with the event clock": """
                SELECT count(*) FROM expectations e
                JOIN earnings_surprises s USING (symbol, period_end)
                WHERE e.as_of <> s.announced_at""",
            "guidance stated at/after announcement": """
                SELECT count(*) FROM expectations e
                JOIN earnings_surprises s USING (symbol, period_end)
                WHERE e.estimator = 'guidance' AND e.inputs_max_ts >= s.announced_at""",
            "inputs_max_ts null/absent": """
                SELECT count(*) FROM expectations WHERE inputs_max_ts IS NULL""",
            "duplicate rows per (sym,period,metric,estimator,version)": """
                SELECT coalesce(sum(c - 1), 0) FROM (
                  SELECT count(*) c FROM expectations
                  GROUP BY symbol, period_end, metric, estimator, model_version
                  HAVING count(*) > 1) t""",
        }
        total = await conn.fetchval("SELECT count(*) FROM expectations")
        print(f"\n=== E1 LEAKAGE AUDIT — {total:,} stored expectations ===")
        failed = 0
        for label, sql in checks.items():
            n = await conn.fetchval(sql)
            status = "PASS" if n == 0 else "FAIL"
            failed += 0 if n == 0 else 1
            print(f"  [{status}] {label}: {n}")
        by = await conn.fetch("""
            SELECT estimator, metric, count(*) n,
                   round(avg(EXTRACT(EPOCH FROM (as_of - inputs_max_ts))/86400.0)::numeric,1) AS avg_lead_days
            FROM expectations GROUP BY 1,2 ORDER BY 1,2""")
        print(f"\n{'estimator':12} {'metric':10} {'rows':>8} {'avg lead (days)':>16}")
        for r in by:
            print(f"{r['estimator']:12} {r['metric']:10} {r['n']:>8,} "
                  f"{r['avg_lead_days']:>16}")
        print(f"\nGATE: {'PASSED' if failed == 0 else f'FAILED ({failed} checks)'}")
        return 0 if failed == 0 else 1
    finally:
        await conn.close()


async def validate_guidance(metrics: list) -> int:
    """WITHIN-COMPANY test (the Stage 5 design): on the SAME rows, does the
    guidance-derived expectation beat the statistical GBM? Comparing T1
    companies against T4 companies would confound estimator quality with
    company mix, which is why the spec insists on this form."""
    import asyncpg
    panel = await load_panel()
    meta = await load_meta()
    dsn = (os.getenv("DATABASE_URL") or "").replace(
        "postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn=dsn, timeout=30, command_timeout=180)
    try:
        gl = pd.DataFrame([dict(r) for r in await conn.fetch(_GUIDANCE_SQL)])
    finally:
        await conn.close()
    gl["stated_at"] = pd.to_datetime(gl["stated_at"], utc=True)
    logger.info(f"guidance ledger: {len(gl):,} scoreable convertible items")

    for metric in metrics:
        bt = run_backtest(panel, metric)
        d = add_peer_features(bt, meta, key="industry")
        gb = gbm_skill_frame(d, FEATURES + PEER_FEATURES)
        if gb is None or gb.empty:
            print(f"{metric}: no GBM predictions"); continue
        # trailing-year level ending at y[t-4], for the seasonal share
        gb = gb.sort_values(["symbol", "period_end"])
        gb["trailing_year"] = (gb.groupby("symbol")["sn"]
                               .transform(lambda s: s.rolling(4, min_periods=4).sum()))
        rows = []
        gl_by_sym = {k: v for k, v in gl.groupby("symbol", sort=False)}
        for r in gb.itertuples():
            g = gl_by_sym.get(r.symbol)
            if g is None:
                continue
            fy = fy_label(r.period_end)
            app = g[(g["stated_at"] < r.announced_at)
                    & ((g["period_key"] == fy)
                       | (g["g_period_end"] == pd.Timestamp(r.period_end).date()))]
            if not app.empty:
                app = app[app["metric_raw"].map(is_company_level)
                          & app["period_key"].map(lambda k: horizon_ok(k, fy))]
            got = guidance_expectation(app, r.sn, r.trailing_year, metric)
            if got is None:
                continue
            exp, lo, hi, basis = got
            if not np.isfinite(exp):
                continue
            rows.append({"symbol": r.symbol, "period_end": r.period_end,
                         "actual": r.actual, "scale": r.scale, "sn": r.sn,
                         "gbm": r.sn + r.adj * r.scale, "guid": exp,
                         "basis": basis})
        if not rows:
            print(f"{metric}: no convertible guidance"); continue
        g = pd.DataFrame(rows)
        e_naive = ((g["actual"] - g["sn"]) / g["scale"]).clip(-WINSOR, WINSOR).abs()
        e_gbm = ((g["actual"] - g["gbm"]) / g["scale"]).clip(-WINSOR, WINSOR).abs()
        e_gui = ((g["actual"] - g["guid"]) / g["scale"]).clip(-WINSOR, WINSOR).abs()
        blend = (g["gbm"] + g["guid"]) / 2
        e_bl = ((g["actual"] - blend) / g["scale"]).clip(-WINSOR, WINSOR).abs()
        print(f"\n=== E1 estimator #1 — GUIDANCE, within-company — {metric.upper()} ===")
        print(f"rows with convertible pre-announcement guidance: {len(g):,} "
              f"({g['symbol'].nunique():,} companies)")
        print(f"{'expectation':22} {'MAE':>8} {'skill vs naive':>15}")
        for label, e in (("seasonal naive", e_naive), ("statistical GBM", e_gbm),
                         ("GUIDANCE", e_gui), ("50/50 blend", e_bl)):
            print(f"{label:22} {e.mean():8.4f} "
                  f"{1 - e.mean()/e_naive.mean():+14.1%}")
        print(f"guidance vs GBM on the same rows: "
              f"{100*(1 - e_gui.mean()/e_gbm.mean()):+.1f}% "
              f"({'guidance wins' if e_gui.mean() < e_gbm.mean() else 'GBM wins'})")
        # Diagnostic: per-basis error + median ratio. A systematic ratio
        # (e.g. ~4x) localises a conversion bug rather than a real finding.
        g["err"] = ((g["actual"] - g["guid"]) / g["scale"]).clip(-WINSOR, WINSOR)
        g["ratio"] = g["guid"] / g["actual"].replace(0, np.nan)
        # Apples-to-apples: guidance vs the GBM ON THE SAME ROWS, per basis.
        # The pooled number is dominated by the unreliable absolute-value
        # conversion, which hides how the clean conversions actually do.
        g["e_gui"] = g["err"].abs()
        g["e_gbm"] = ((g["actual"] - g["gbm"]) / g["scale"]).clip(-WINSOR, WINSOR).abs()
        print(f"{'basis':26} {'n':>6} {'guid MAE':>9} {'GBM MAE':>9} "
              f"{'guid vs GBM':>12} {'med ratio':>10}")
        for b, sl in g.groupby("basis"):
            rel = 1 - sl["e_gui"].mean() / sl["e_gbm"].mean()
            print(f"{b:26} {len(sl):>6} {sl['e_gui'].mean():9.4f} "
                  f"{sl['e_gbm'].mean():9.4f} {rel:+11.1%} {sl['ratio'].median():>10.2f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--validate-peers", action="store_true")
    ap.add_argument("--validate-guidance", action="store_true")
    ap.add_argument("--write", action="store_true", help="dry-run the writer")
    ap.add_argument("--execute", action="store_true", help="persist expectations")
    ap.add_argument("--audit", action="store_true", help="run the leakage audit")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--metric", default="revenue,pat,ebitda")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    metrics = [m.strip() for m in a.metric.split(",") if m.strip() in METRICS]
    if a.audit:
        return asyncio.run(leakage_audit())
    if a.write or a.execute:
        return asyncio.run(write_expectations(metrics, a.execute, a.limit))
    if a.validate_guidance:
        return asyncio.run(validate_guidance(metrics))
    return asyncio.run(validate_peers(metrics))


if __name__ == "__main__":
    sys.exit(main())
