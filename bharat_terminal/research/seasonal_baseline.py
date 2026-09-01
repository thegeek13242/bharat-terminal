"""Seasonal earnings baseline (E-series Stage 3) — the universe-wide
expectations floor every richer E1 estimator must beat.

WHY this exists: E1's estimator stack is ordered by coverage. Guidance
(T1, 509 cos) and business updates (T2, ~100/qtr) cover a minority of the
universe; this statistical model covers ALL ~3,600 reporters and carries
the tail where the direction-signals thesis says the edge lives. Stage 5's
go/no-go is a WITHIN-COMPANY test — do the text-derived estimators beat
THIS model for the same firms — so the floor must be honest and strong,
not a straw man.

Models (all walk-forward, no look-ahead):
  sn      Seasonal naive: F = Y[t-4]. What composite-SUE uses today; the
          number to beat.
  snd     Seasonal naive + drift: F = Y[t-4] + mean(seasonal diffs in the
          trailing window) — captures a company's growth trend.
  foster  Foster (1977), the accounting literature's standard quarterly
          earnings expectation: seasonal differences follow an AR(1),
          F = Y[t-4] + delta + phi*(Y[t-1] - Y[t-5]), with (delta, phi)
          fitted per company on trailing data only. Falls back to `snd`
          when history is too short to fit.

Scoring: forecast error scaled by the company's own trailing mean absolute
level (percentage errors explode on near-zero PAT), winsorized, then
compared to `sn` as skill = 1 - MAE_model/MAE_sn. Basis (consolidated vs
standalone) is held CONSTANT per company series — mixing them mid-series
manufactures fake surprises (the E2 basis-matching rule, applied upstream).

PIT caveat (Stage 0 finding): financial_results rows are revised in place,
so a backtest reads today's values, not first-published ones. Revisions are
rare outside the 2026-06 scale-fix, but the honest framing is that this
measures MODEL skill, not what a live system would have seen. The E-series
event clock stays earnings_surprises.announced_at.

Run (read-only; writes nothing):
  python -m bharat_terminal.research.seasonal_baseline --metric revenue
  python -m bharat_terminal.research.seasonal_baseline --metric pat --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

np.random.seed(42)          # house rule: any sampling carries a visible seed

METRICS = {"revenue": "revenue_cr", "ebitda": "ebitda_cr", "pat": "pat_cr"}

# Foster needs Y[t-1], Y[t-4], Y[t-5]; fitting (delta, phi) needs a few
# seasonal differences on top. 12 quarters keeps the AR(1) fit honest.
MIN_HIST_FOSTER = 12
MIN_HIST_ANY = 5            # the lag structure alone
DRIFT_WINDOW = 8            # quarters of seasonal diffs for `snd`
SCALE_WINDOW = 4            # trailing quarters for the error scale
WINSOR = 3.0                # cap |scaled error| — one bad row must not
                            # decide the model comparison
BACKTEST_FROM = "2019-03-31"

_PANEL_SQL = """
SELECT symbol, period_end, consolidated, revenue_cr, ebitda_cr, pat_cr
FROM financial_results
WHERE period_type = 'quarterly' AND period_end >= '2016-01-01'
ORDER BY symbol, period_end
"""


async def load_panel() -> pd.DataFrame:
    import asyncpg
    dsn = (os.getenv("DATABASE_URL") or "").replace(
        "postgresql+asyncpg://", "postgresql://")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set")
    conn = await asyncpg.connect(dsn=dsn, timeout=30, command_timeout=180)
    try:
        rows = await conn.fetch(_PANEL_SQL)
    finally:
        await conn.close()
    df = pd.DataFrame([dict(r) for r in rows])
    df["period_end"] = pd.to_datetime(df["period_end"])
    return df


def pick_basis(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """One series per company on a CONSTANT basis: whichever of
    consolidated/standalone has more non-null observations for that metric
    (ties -> consolidated, the reporting default for groups)."""
    counts = (df.dropna(subset=[col])
                .groupby(["symbol", "consolidated"]).size()
                .rename("n").reset_index())
    counts["pref"] = counts["consolidated"].astype(int)   # tiebreak
    best = (counts.sort_values(["symbol", "n", "pref"],
                               ascending=[True, False, False])
                  .drop_duplicates("symbol")[["symbol", "consolidated"]])
    return df.merge(best, on=["symbol", "consolidated"], how="inner")


def _fit_ar1(x: np.ndarray) -> tuple:
    """OLS of x[1:] on x[:-1] -> (delta, phi). Guarded for degenerate
    windows; phi clipped to (-0.95, 0.95) so a near-unit-root fit cannot
    explode a forecast."""
    if x.size < 4:
        return float(np.mean(x)) if x.size else 0.0, 0.0
    y, lag = x[1:], x[:-1]
    var = float(np.var(lag))
    if not np.isfinite(var) or var <= 1e-12:
        return float(np.mean(y)), 0.0
    phi = float(np.cov(lag, y, bias=True)[0, 1] / var)
    phi = max(-0.95, min(0.95, phi))
    delta = float(np.mean(y) - phi * np.mean(lag))
    return delta, phi


def forecast_company(vals: np.ndarray) -> List[dict]:
    """Walk-forward forecasts for one company's evenly-ordered quarterly
    series. Index i is forecast using ONLY vals[:i]."""
    out: List[dict] = []
    n = vals.size
    for i in range(MIN_HIST_ANY, n):
        hist = vals[:i]
        if not np.isfinite(hist[-5:]).all() or not np.isfinite(vals[i]):
            continue
        y_1, y_4, y_5 = hist[-1], hist[-4], hist[-5]

        f_sn = y_4
        # seasonal diffs available inside the trailing drift window
        sd = hist[4:] - hist[:-4]
        sd = sd[np.isfinite(sd)][-DRIFT_WINDOW:]
        f_snd = y_4 + (float(np.mean(sd)) if sd.size else 0.0)

        if i >= MIN_HIST_FOSTER and sd.size >= 4:
            sd_full = hist[4:] - hist[:-4]
            sd_full = sd_full[np.isfinite(sd_full)]
            delta, phi = _fit_ar1(sd_full)
            f_foster = y_4 + delta + phi * (y_1 - y_5)
            foster_fitted = True
        else:
            f_foster, foster_fitted = f_snd, False

        scale_src = np.abs(hist[-SCALE_WINDOW:])
        scale = float(np.mean(scale_src[np.isfinite(scale_src)])) \
            if np.isfinite(scale_src).any() else 0.0
        if not np.isfinite(scale) or scale <= 1e-6:
            continue                     # dormant/zero-revenue series

        # Features for the GLOBAL model (all scale-normalised so they are
        # comparable across companies; NaN where history is too short —
        # HistGradientBoosting consumes NaN natively, no imputation).
        def _sd(k):                       # seasonal diff ending k quarters back
            a, b = i - k, i - k - 4
            return (hist[a] - hist[b]) / scale if b >= 0 and np.isfinite(
                hist[a]) and np.isfinite(hist[b]) else np.nan

        def _lvl(a, b):
            w = hist[max(0, i - a):i - b]
            w = w[np.isfinite(w)]
            return float(np.mean(w)) if w.size else np.nan

        prior4, prior8 = _lvl(4, 0), _lvl(8, 4)
        past_sd = hist[4:] - hist[:-4]
        past_sd = past_sd[np.isfinite(past_sd)] / scale
        feats = {
            "f_sd1": _sd(1), "f_sd2": _sd(2), "f_sd3": _sd(3), "f_sd4": _sd(4),
            "f_qoq1": (hist[-1] - hist[-2]) / scale if i >= 2 else np.nan,
            "f_trend": ((prior4 - prior8) / scale
                        if np.isfinite(prior4) and np.isfinite(prior8) else np.nan),
            "f_vol": float(np.std(past_sd[-8:])) if past_sd.size >= 3 else np.nan,
            "f_nhist": float(i),
            "f_logscale": float(np.log10(scale)),
            "f_foster": (f_foster - f_sn) / scale,
        }
        out.append({**feats, "i": i, "actual": float(vals[i]), "scale": scale,
                    "sn": float(f_sn), "snd": float(f_snd),
                    "foster": float(f_foster), "n_hist": i,
                    "adj_foster": float(f_foster - f_sn),
                    "adj_snd": float(f_snd - f_sn),
                    "foster_fitted": foster_fitted})
    return out


def run_backtest(df: pd.DataFrame, metric: str) -> pd.DataFrame:
    col = METRICS[metric]
    df = pick_basis(df, col)
    recs: List[dict] = []
    for sym, g in df.sort_values("period_end").groupby("symbol", sort=False):
        g = g.drop_duplicates("period_end", keep="last")
        vals = g[col].to_numpy(dtype=float)
        dates = g["period_end"].to_numpy()
        for r in forecast_company(vals):
            recs.append({"symbol": sym, "period_end": dates[r["i"]], **r})
    out = pd.DataFrame(recs)
    if out.empty:
        return out
    return out[out["period_end"] >= pd.Timestamp(BACKTEST_FROM)].copy()


def evaluate(bt: pd.DataFrame) -> dict:
    """Scaled-error metrics per model, plus skill vs the seasonal-naive
    floor. Errors are winsorized at +/-WINSOR before averaging."""
    res: Dict[str, dict] = {}
    for m in ("sn", "snd", "foster"):
        err = (bt["actual"] - bt[m]) / bt["scale"]
        err = err.clip(-WINSOR, WINSOR)
        res[m] = {"mae": float(err.abs().mean()),
                  "median_ae": float(err.abs().median()),
                  "bias": float(err.mean()),
                  "rmse": float(np.sqrt((err ** 2).mean()))}
    base = res["sn"]["mae"]
    for m, d in res.items():
        d["skill_vs_sn"] = float(1.0 - d["mae"] / base) if base > 0 else 0.0
    # Does the model call the direction of the YoY change better than "no
    # change"? (sign of actual-vs-seasonal-naive, predicted by the model.)
    hits = {}
    truth = np.sign(bt["actual"] - bt["sn"])
    for m in ("snd", "foster"):
        pred = np.sign(bt[m] - bt["sn"])
        mask = (truth != 0) & (pred != 0)
        hits[m] = float((truth[mask] == pred[mask]).mean()) if mask.any() else float("nan")
    return {"n_forecasts": int(len(bt)),
            "n_companies": int(bt["symbol"].nunique()),
            "period": [str(bt["period_end"].min().date()),
                       str(bt["period_end"].max().date())],
            "foster_fitted_pct": round(100.0 * bt["foster_fitted"].mean(), 1),
            "models": res, "direction_hit_rate": hits}


# Shrinkage grid (2026-08-26): raw Foster beats seasonal-naive on MEDIAN
# error and direction but loses on MEAN — a minority of AR extrapolations
# blow up. Shrinking the adjustment toward zero and capping it at a
# multiple of the company's own scale keeps the signal and cuts the tail.
LAMBDAS = (0.0, 0.15, 0.25, 0.4, 0.6, 0.8, 1.0)
CAPS = (0.15, 0.25, 0.5, 1.0, 99.0)
TRAIN_END = "2022-12-31"      # tune here, report on 2023-01-01 onward


def _shrunk_mae(sl: pd.DataFrame, adj_col: str, lam: float, cap: float) -> float:
    adj = (sl[adj_col] / sl["scale"]).clip(-cap, cap) * lam
    err = ((sl["actual"] - sl["sn"]) / sl["scale"]) - adj
    return float(err.clip(-WINSOR, WINSOR).abs().mean())


def tune_shrinkage(bt: pd.DataFrame, adj_col: str = "adj_foster") -> dict:
    """Grid-search (lambda, cap) on the TRAIN period only, then score the
    winner out-of-sample. Reports the naive floor on the same test rows so
    the comparison is like-for-like."""
    train = bt[bt["period_end"] <= pd.Timestamp(TRAIN_END)]
    test = bt[bt["period_end"] > pd.Timestamp(TRAIN_END)]
    if len(train) < 500 or len(test) < 500:
        return {}
    best, best_mae = None, float("inf")
    for lam in LAMBDAS:
        for cap in CAPS:
            m = _shrunk_mae(train, adj_col, lam, cap)
            if m < best_mae:
                best, best_mae = (lam, cap), m
    lam, cap = best
    sn_test = ((test["actual"] - test["sn"]) / test["scale"]).clip(
        -WINSOR, WINSOR).abs().mean()
    mae_test = _shrunk_mae(test, adj_col, lam, cap)
    # direction hit-rate of the shrunk adjustment on the test rows
    truth = np.sign(test["actual"] - test["sn"])
    pred = np.sign(test[adj_col])
    mask = (truth != 0) & (pred != 0)
    # Honesty check: revenue mostly grows, so "always up" already scores
    # well. The direction hit-rate is only meaningful against THIS floor.
    base_up = float((test["actual"] > test["sn"]).mean())
    return {"lambda": lam, "cap": cap, "base_rate_up": base_up,
            "train_n": int(len(train)), "test_n": int(len(test)),
            "test_mae": float(mae_test), "test_mae_sn": float(sn_test),
            "test_skill": float(1.0 - mae_test / sn_test) if sn_test > 0 else 0.0,
            "test_direction_hit": float((truth[mask] == pred[mask]).mean())
            if mask.any() else float("nan")}


# The 2020s answer to Foster (1977): CROSS-LEARNING. Foster fits (delta,
# phi) per company on ~25 quarterly points — data starvation is exactly why
# its tails blew up and why shrinkage was needed. M4/M5 showed a single
# model trained across ALL series beats per-series fitting on short
# histories. Same histogram-GBM algorithm as LightGBM (sklearn 1.9), the
# family already in production here (materiality v4.1 `hgb`).
FEATURES = ["f_sd1", "f_sd2", "f_sd3", "f_sd4", "f_qoq1", "f_trend",
            "f_vol", "f_nhist", "f_logscale", "f_foster", "f_fq"]
GBM_KW = dict(max_iter=300, learning_rate=0.06, max_depth=6,
              min_samples_leaf=200, l2_regularization=1.0,
              early_stopping=True, validation_fraction=0.15,
              random_state=42)          # visible seed, house rule


def global_gbm_backtest(bt: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Expanding-window annual retraining: for each test year Y, fit on
    every company-quarter that had already been REPORTED before Y starts,
    then predict Y. Target is the scaled deviation from seasonal-naive —
    i.e. the model learns the ADJUSTMENT, so naive == predicting zero and
    the comparison with shrunk Foster is like-for-like."""
    from sklearn.ensemble import HistGradientBoostingRegressor
    d = bt.copy()
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
               & (pd.to_datetime(d["period_end"]) < pd.Timestamp(f"{yr + 1}-01-01"))]
        if len(tr) < 2000 or te.empty:
            continue
        m = HistGradientBoostingRegressor(**GBM_KW)
        m.fit(tr[FEATURES].to_numpy(dtype=float), tr["y"].to_numpy(dtype=float))
        out = te.copy()
        out["gbm_adj"] = m.predict(te[FEATURES].to_numpy(dtype=float))
        # Control: a CONSTANT adjustment (the train-period median). If the
        # GBM merely learned "things grow", this scores the same. It also
        # scores exactly the always-up floor on direction by construction.
        out["const_adj"] = float(tr["y"].median())
        out["train_n"] = len(tr)
        preds.append(out)
    return pd.concat(preds) if preds else None


def evaluate_gbm(g: pd.DataFrame) -> dict:
    err_naive = ((g["actual"] - g["sn"]) / g["scale"]).clip(-WINSOR, WINSOR)
    err_gbm = (err_naive - g["gbm_adj"]).clip(-WINSOR, WINSOR)
    truth, pred = np.sign(err_naive), np.sign(g["gbm_adj"])
    mask = (truth != 0) & (pred != 0)
    per_year = {}
    for yr, sl in g.groupby(pd.to_datetime(g["period_end"]).dt.year):
        en = ((sl["actual"] - sl["sn"]) / sl["scale"]).clip(-WINSOR, WINSOR).abs().mean()
        eg = (((sl["actual"] - sl["sn"]) / sl["scale"]).clip(-WINSOR, WINSOR)
              - sl["gbm_adj"]).clip(-WINSOR, WINSOR).abs().mean()
        per_year[int(yr)] = {"n": int(len(sl)),
                             "skill": float(1 - eg / en) if en > 0 else 0.0}
    err_const = (err_naive - g["const_adj"]).clip(-WINSOR, WINSOR)
    return {"n": int(len(g)),
            "mae_const": float(err_const.abs().mean()),
            "skill_const": float(1 - err_const.abs().mean() / err_naive.abs().mean()),
            "mae": float(err_gbm.abs().mean()),
            "mae_naive": float(err_naive.abs().mean()),
            "skill": float(1 - err_gbm.abs().mean() / err_naive.abs().mean()),
            "direction_hit": float((truth[mask] == pred[mask]).mean())
            if mask.any() else float("nan"),
            "base_rate_up": float((g["actual"] > g["sn"]).mean()),
            "per_year": per_year}


def report(metric: str, ev: dict, bt: pd.DataFrame) -> str:
    L = [f"=== Stage 3 seasonal baseline — {metric.upper()} ===",
         f"forecasts: {ev['n_forecasts']:,} over {ev['n_companies']:,} companies "
         f"({ev['period'][0]} -> {ev['period'][1]}); "
         f"Foster fitted on {ev['foster_fitted_pct']}% of rows",
         "",
         f"{'model':8} {'MAE(scaled)':>12} {'median':>9} {'bias':>8} {'skill vs sn':>12}"]
    for m in ("sn", "snd", "foster"):
        d = ev["models"][m]
        L.append(f"{m:8} {d['mae']:12.4f} {d['median_ae']:9.4f} "
                 f"{d['bias']:+8.4f} {d['skill_vs_sn']:+11.1%}")
    L.append("")
    L.append("direction hit-rate vs seasonal-naive: " + ", ".join(
        f"{m}={v:.1%}" for m, v in ev["direction_hit_rate"].items()))
    # history-depth slice: does the model earn its keep where data is thin?
    L.append("")
    L.append("by history depth (skill vs sn):")
    bins = [(5, 11, "5-11q"), (12, 19, "12-19q"), (20, 99, "20q+")]
    for lo, hi, label in bins:
        sl = bt[(bt["n_hist"] >= lo) & (bt["n_hist"] <= hi)]
        if len(sl) < 50:
            continue
        base = ((sl["actual"] - sl["sn"]) / sl["scale"]).clip(-WINSOR, WINSOR).abs().mean()
        row = [f"  {label:8} n={len(sl):>6,}"]
        for m in ("snd", "foster"):
            e = ((sl["actual"] - sl[m]) / sl["scale"]).clip(-WINSOR, WINSOR).abs().mean()
            row.append(f"{m}={1 - e / base:+.1%}")
        L.append("  ".join(row))
    tu = ev.get("shrinkage") or {}
    if tu:
        L.append("")
        L.append(f"shrinkage tuned on <= {TRAIN_END}, scored after "
                 f"(train n={tu['train_n']:,}, test n={tu['test_n']:,}):")
        L.append(f"  lambda={tu['lambda']} cap={tu['cap']}x scale  ->  "
                 f"test MAE {tu['test_mae']:.4f} vs naive {tu['test_mae_sn']:.4f}"
                 f"  skill {tu['test_skill']:+.1%}"
                 f"  direction {tu['test_direction_hit']:.1%}"
                 f" (always-up floor {tu['base_rate_up']:.1%})")
    gb = ev.get("gbm") or {}
    if gb:
        L.append("")
        L.append(f"GLOBAL GBM (cross-learning, expanding-window annual "
                 f"retrain, n={gb['n']:,}):")
        L.append(f"  test MAE {gb['mae']:.4f} vs naive {gb['mae_naive']:.4f}"
                 f"  skill {gb['skill']:+.1%}"
                 f"  direction {gb['direction_hit']:.1%}"
                 f" (floor {gb['base_rate_up']:.1%})")
        L.append(f"  control — constant drift: MAE {gb['mae_const']:.4f}"
                 f"  skill {gb['skill_const']:+.1%}"
                 f"  (GBM edge over constant: "
                 f"{100 * (gb['skill'] - gb['skill_const']):+.1f}pp)")
        L.append("  by year: " + ", ".join(
            f"{y}: {d['skill']:+.1%} (n={d['n']:,})"
            for y, d in sorted(gb["per_year"].items())))
    return "\n".join(L)


async def main_async(metrics: List[str], json_path: Optional[str]) -> int:
    df = await load_panel()
    logger.info(f"panel: {len(df):,} rows, {df['symbol'].nunique():,} companies")
    all_ev = {}
    for metric in metrics:
        bt = run_backtest(df, metric)
        if bt.empty:
            print(f"{metric}: no forecasts produced")
            continue
        ev = evaluate(bt)
        ev["shrinkage"] = tune_shrinkage(bt)
        g = global_gbm_backtest(bt)
        if g is not None:
            ev["gbm"] = evaluate_gbm(g)
        all_ev[metric] = ev
        print(report(metric, ev, bt))
        print()
    if json_path:
        with open(json_path, "w") as f:
            json.dump(all_ev, f, indent=2, default=str)
        logger.info(f"wrote {json_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--metric", default="revenue,pat,ebitda",
                    help="comma-separated: revenue,pat,ebitda")
    ap.add_argument("--json", default=None, help="write metrics JSON here")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    metrics = [m.strip() for m in args.metric.split(",") if m.strip() in METRICS]
    return asyncio.run(main_async(metrics, args.json))


if __name__ == "__main__":
    sys.exit(main())
