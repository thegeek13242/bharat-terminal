"""Materiality calibrator v1 — P(the market treats this event as material).

Direction-agnostic by design: the July-04 signal audit proved direction is
coin-flip, but reaction SIZE is conditioned by persistent things (event type,
volatility regime, liquidity). This trains a small, inspectable model on our
own history:

    label = |next-session close-to-close move| >= max(K_SIGMA * trailing
            60-session vol, MIN_ABS_MOVE)

using the proven public-day convention (IST date, post-15:30 rolls forward).
Every feature is knowable AT EVENT TIME — no look-ahead; trailing vol windows
end strictly before the base close.

Evaluation is the founder's no-improvement rule made executable: a strict
TIME-based split, scored on Brier/AUC/reliability against the honest
baseline — the empirical materiality rate per LLM-magnitude bucket (what the
existing 1-5 score already gives you). The calibrator earns product wiring
only by beating that out-of-time.

Read-only against the DB (temp tables only). Deterministic: seed 42.

Run (research image, entrypoint overridden):
  docker compose run --rm --entrypoint python research-runner \
      -m bharat_terminal.research.materiality_calibrator [--days 400]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SEED = 42
K_SIGMA = float(os.getenv("MATERIALITY_K_SIGMA", "2.0"))
MIN_ABS_MOVE = float(os.getenv("MATERIALITY_MIN_ABS_MOVE", "0.015"))
MIN_VOL_OBS = 40
EXPORT_DIR = os.getenv("MATERIALITY_EXPORT_DIR", "exports")

_EVENTS_SQL = """
CREATE TEMP TABLE _cal_events AS
SELECT ir.id AS impact_id, ir.created_at, ni.source,
       coalesce(ni.category, '') AS category,
       coalesce(ni.headline, '') AS headline,
       upper(ci->>'symbol') AS symbol,
       coalesce((ci->>'magnitude')::int, 0) AS magnitude,
       coalesce(ci->>'sentiment', 'neutral') AS sentiment,
       coalesce((ci->>'hop_distance')::int, 0) AS hop,
       coalesce(ci->>'time_horizon', '') AS horizon,
       jsonb_array_length(ir.company_impacts) AS n_companies,
       coalesce(ir.confidence, 0) AS confidence,
       CASE WHEN (ir.created_at AT TIME ZONE 'Asia/Kolkata')::time >= time '15:30'
            THEN ((ir.created_at AT TIME ZONE 'Asia/Kolkata')::date + 1)
            ELSE (ir.created_at AT TIME ZONE 'Asia/Kolkata')::date END AS public_day,
       (((ir.created_at AT TIME ZONE 'Asia/Kolkata')::time
           BETWEEN time '09:15' AND time '15:30')
        AND extract(dow FROM (ir.created_at AT TIME ZONE 'Asia/Kolkata'))
            BETWEEN 1 AND 5) AS market_hours
FROM impact_reports ir
JOIN news_items ni ON ni.id = ir.news_id
CROSS JOIN LATERAL jsonb_array_elements(ir.company_impacts) ci
WHERE ir.relevant
  AND ir.created_at >= now() - make_interval(days => :days)
  AND ci->>'symbol' IS NOT NULL
  {source_clause}
"""

_PX_SQL = """
CREATE TEMP TABLE _cal_px AS
SELECT symbol, date, close, ret5_pre,
       stddev_samp(ret) OVER w AS vol60,
       count(ret) OVER w AS nret,
       avg(traded_cr) OVER w AS adv_cr
FROM (
    SELECT symbol, date, close,
           close * coalesce(volume, 0) / 1e7 AS traded_cr,
           close / nullif(lag(close) OVER (PARTITION BY symbol ORDER BY date), 0) - 1 AS ret,
           close / nullif(lag(close, 5) OVER (PARTITION BY symbol ORDER BY date), 0) - 1 AS ret5_pre
    FROM price_points
    WHERE symbol IN (SELECT DISTINCT symbol FROM _cal_events)
      AND date >= (SELECT min(public_day) FROM _cal_events) - 120
) t
WINDOW w AS (PARTITION BY symbol ORDER BY date
             ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING)
"""

_JOIN_SQL = """
SELECT e.*, b.close AS base_close, b.vol60, b.adv_cr, b.ret5_pre,
       r.close AS react_close, r.date AS react_date,
       c.mcap_cr,
       es.sue_abs, es.sue_found
FROM _cal_events e
JOIN LATERAL (
    SELECT close, vol60, adv_cr, nret, ret5_pre FROM _cal_px p
    WHERE p.symbol = e.symbol AND p.date < e.public_day
    ORDER BY date DESC LIMIT 1
) b ON b.nret >= :min_vol_obs AND b.vol60 IS NOT NULL AND b.vol60 > 0
JOIN LATERAL (
    SELECT close, date FROM _cal_px p
    WHERE p.symbol = e.symbol AND p.date >= e.public_day
    ORDER BY date ASC LIMIT 1
) r ON r.date <= e.public_day + 7
LEFT JOIN companies c ON c.symbol = e.symbol
LEFT JOIN LATERAL (
    SELECT abs(s.sue_composite) AS sue_abs, 1 AS sue_found
    FROM earnings_surprises s
    WHERE s.symbol = e.symbol AND s.sue_composite IS NOT NULL
      -- PIT (fixed 2026-07-29): only surprise rows announced AT OR BEFORE
      -- analysis time. The old "+ interval '1 day'" upper bound leaked
      -- next-day XBRL into training (~0.05 AUC inflation, S0_GATE) while
      -- live serving saw has_sue=0 — the press release precedes the XBRL.
      AND s.announced_at BETWEEN e.created_at - interval '3 days'
                             AND e.created_at
    ORDER BY abs(extract(epoch FROM s.announced_at - e.created_at)) ASC
    LIMIT 1
) es ON true
WHERE e.magnitude > 0
"""


async def build_dataset(days: int, filings_only: bool = False):
    """Events × outcomes as a DataFrame. Temp tables only — read-only DB."""
    import pandas as pd
    from sqlalchemy import text
    from bharat_terminal.kb.sync.utils import create_async_session

    SF = create_async_session()
    async with SF() as db:
        events_sql = _EVENTS_SQL.format(
            source_clause="AND ni.source IN ('NSE_FILINGS','BSE_FILINGS')"
            if filings_only else "")
        await db.execute(text(events_sql), {"days": days})
        await db.execute(text(_PX_SQL))
        rows = (await db.execute(
            text(_JOIN_SQL), {"min_vol_obs": MIN_VOL_OBS})).mappings().all()
        await db.rollback()  # discard temp tables; nothing persisted
    df = pd.DataFrame([dict(r) for r in rows])
    logger.info(f"dataset: {len(df)} examples, "
                f"{df['symbol'].nunique() if len(df) else 0} symbols")
    return df


_EVENT_TYPES = [
    # (bucket, keywords) — first match wins; NSE headline template puts the
    # event after "informed the Exchange about/regarding".
    ("results", ("financial result", "unaudited", "audited result", "results for")),
    ("transcript", ("transcript",)),
    ("concall_invite", ("earnings call", "conference call", "call invite",
                        "link of recording", "audio recording")),
    ("investor_meet", ("investor meet", "analyst", "institutional investor")),
    ("order_win", ("order", "contract", "loa", "letter of award", "work order",
                   "bagging", "awarded")),
    ("acquisition_stake", ("acquisition", "acquire", "stake", "amalgamation",
                           "merger", "demerger", "scheme of arrangement",
                           "joint venture", "subsidiary")),
    ("fund_raise", ("fund raise", "fund raising", "qip", "preferential",
                    "rights issue", "allotment", "issue of", "warrant",
                    "capital raise", "ncd", "debenture", "bond")),
    ("rating", ("rating", "crisil", "icra", "care ratings", "india ratings")),
    ("pledge_sast", ("pledge", "sast", "insider trading", "encumbrance")),
    ("corp_action", ("dividend", "bonus", "split", "buyback", "buy-back",
                     "record date")),
    ("board_meeting", ("board meeting", "meeting of the board")),
    ("agm_egm", ("agm", "egm", "annual general", "extraordinary general",
                 "postal ballot", "voting result", "scrutinizer")),
    ("mgmt_change", ("resignation", "appointment", "cessation",
                     "change in manage", "change in director", "kmp")),
    ("newspaper_noise", ("newspaper", "clipping", "advertisement",
                         "press release published")),
    ("regulation_generic", ("regulation 30", "regulation 74", "compliance certificate",
                            "trading window", "closure of trading")),
]


def event_type(headline: str) -> str:
    """Deterministic event-type bucket from the filing headline."""
    h = (headline or "").lower()
    for bucket, keys in _EVENT_TYPES:
        if any(k in h for k in keys):
            return bucket
    return "other"


def featurize(df):
    """Label + feature matrix. Everything here is event-time knowable."""
    import numpy as np
    import pandas as pd

    df = df.copy()
    df["abs_ret"] = (df["react_close"] / df["base_close"] - 1).abs()
    df["threshold"] = np.maximum(K_SIGMA * df["vol60"], MIN_ABS_MOVE)
    df["material"] = (df["abs_ret"] >= df["threshold"]).astype(int)

    X = pd.DataFrame(index=df.index)
    X["magnitude"] = df["magnitude"].clip(1, 5)
    X["hop"] = df["hop"].clip(0, 3)
    X["confidence"] = df["confidence"].fillna(0)
    X["n_companies_log"] = np.log1p(df["n_companies"])
    X["vol60_log"] = np.log(df["vol60"])
    X["adv_cr_log"] = np.log1p(df["adv_cr"].clip(lower=0))
    X["market_hours"] = df["market_hours"].astype(int)
    # v2: event taxonomy, surprise magnitude, priced-in run-up, size.
    df["event_type"] = df["headline"].map(event_type)
    X["sue_abs"] = df["sue_abs"].fillna(0).clip(0, 10)
    X["has_sue"] = df["sue_found"].fillna(0).astype(int)
    X["runup_sigma"] = (df["ret5_pre"].abs()
                        / (df["vol60"] * np.sqrt(5))).clip(0, 10).fillna(0)
    X["mcap_log"] = np.log1p(df["mcap_cr"].fillna(0))
    X["mcap_missing"] = df["mcap_cr"].isna().astype(int)
    for col, prefix in (("sentiment", "sent"), ("horizon", "hz"),
                        ("source", "src"), ("category", "cat"),
                        ("event_type", "ev")):
        d = pd.get_dummies(df[col].fillna("").astype(str).str.lower(),
                           prefix=prefix, dtype=int)
        # cap cardinality defensively (event_type has 16 buckets)
        X = pd.concat([X, d.iloc[:, :24]], axis=1)
    y = df["material"]

    # v3 (feature ablation, 2026-07-19): the signal lives in EVENT CONTENT
    # (taxonomy + |SUE| + run-up: alone 0.593 AUC) with a small assist from
    # the LLM outputs; stock-context (vol/liquidity/mcap: alone 0.512) and
    # misc (category/source/hours/hop: alone 0.483) were dead weight —
    # removing them RAISED out-of-time AUC 0.627 -> 0.638. Vol stays in the
    # LABEL (it defines materiality) but out of the FEATURES, which also
    # closes the labeling-geometry concern (model can't learn the threshold).
    dropped = (["vol60_log", "adv_cr_log", "mcap_log", "mcap_missing",
                "market_hours", "n_companies_log", "hop"]
               + [c for c in X.columns if c.startswith(("src_", "cat_"))])
    X = X.drop(columns=[c for c in dropped if c in X.columns])
    return X, y, df


async def embed_headlines(df):
    """768-dim headline embeddings via the existing Vertex helper, deduped.
    Returns an (n, 768) numpy matrix aligned to df rows."""
    import numpy as np
    from bharat_terminal.llm.embeddings import embed_texts

    uniq = df["headline"].fillna("").unique().tolist()
    logger.info(f"embedding {len(uniq)} unique headlines")
    vecs = await embed_texts(uniq, task_type="RETRIEVAL_DOCUMENT")
    lookup = {h: v for h, v in zip(uniq, vecs)}
    return np.array([lookup[h] for h in df["headline"].fillna("")])


def train_and_evaluate(df, test_days: int, split_frac: float | None = None,
                       emb=None):
    """Time-split train/eval. Returns metrics dict + fitted best model."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import make_pipeline
    from sklearn.metrics import brier_score_loss, roc_auc_score

    np.random.seed(SEED)
    X, y, df = featurize(df)
    if split_frac:
        cutoff = df["created_at"].quantile(1 - split_frac)
    else:
        cutoff = df["created_at"].max() - np.timedelta64(test_days, "D")
    tr, te = df["created_at"] <= cutoff, df["created_at"] > cutoff
    Xtr, ytr, Xte, yte = X[tr], y[tr], X[te], y[te]
    assert len(Xte) > 200, f"test window too small ({len(Xte)})"

    metrics = {
        "n_train": int(tr.sum()), "n_test": int(te.sum()),
        "base_rate_train": round(float(ytr.mean()), 4),
        "base_rate_test": round(float(yte.mean()), 4),
        "label": f"|ret_1d| >= max({K_SIGMA}*vol60, {MIN_ABS_MOVE})",
        "split_cutoff": str(cutoff), "seed": SEED,
    }

    # Baseline 1: constant base rate. Baseline 2 (the one that matters):
    # empirical P(material | LLM magnitude bucket) from TRAIN applied to TEST.
    p_const = np.full(len(yte), ytr.mean())
    mag_p = df[tr].groupby("magnitude")["material"].mean()
    p_mag = df[te]["magnitude"].map(mag_p).fillna(ytr.mean()).values
    metrics["baseline_const_brier"] = round(brier_score_loss(yte, p_const), 4)
    metrics["baseline_magnitude_brier"] = round(brier_score_loss(yte, p_mag), 4)
    try:
        metrics["baseline_magnitude_auc"] = round(roc_auc_score(yte, p_mag), 4)
    except ValueError:
        metrics["baseline_magnitude_auc"] = None

    variants = {
        "logistic": (make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=2000, random_state=SEED)),
            Xtr.values, Xte.values),
        "hgb": (HistGradientBoostingClassifier(random_state=SEED),
                Xtr.values, Xte.values),
    }
    if emb is not None:
        # Headline embeddings compressed to 64 dims; SVD fit on TRAIN ONLY.
        from sklearn.decomposition import TruncatedSVD
        svd = TruncatedSVD(n_components=64, random_state=SEED)
        etr = svd.fit_transform(emb[tr.values])
        ete = svd.transform(emb[te.values])
        metrics["svd_explained_var"] = round(
            float(svd.explained_variance_ratio_.sum()), 3)
        htr = np.hstack([Xtr.values, etr])
        hte = np.hstack([Xte.values, ete])
        variants["emb_logistic"] = (make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=3000, random_state=SEED)), htr, hte)
        variants["emb_hgb"] = (
            HistGradientBoostingClassifier(random_state=SEED), htr, hte)

    best_name, best_brier, best_model, best_p = None, 1e9, None, None
    for name, (m, xtr_v, xte_v) in variants.items():
        m.fit(xtr_v, ytr)
        p = m.predict_proba(xte_v)[:, 1]
        b = brier_score_loss(yte, p)
        metrics[f"{name}_brier"] = round(b, 4)
        metrics[f"{name}_auc"] = round(roc_auc_score(yte, p), 4)
        if b < best_brier:
            best_name, best_brier, best_model, best_p = name, b, m, p

    metrics["best_model"] = best_name
    metrics["beats_magnitude_baseline"] = bool(
        best_brier < metrics["baseline_magnitude_brier"])

    # Reliability by predicted-probability decile (calibration evidence).
    import pandas as pd
    rel = (pd.DataFrame({"p": best_p, "y": yte.values})
           .assign(bucket=lambda d: pd.qcut(d["p"], 10, duplicates="drop"))
           .groupby("bucket", observed=True)
           .agg(pred=("p", "mean"), obs=("y", "mean"), n=("y", "size")))
    metrics["reliability"] = [
        {"pred": round(float(r.pred), 3), "obs": round(float(r.obs), 3),
         "n": int(r.n)} for r in rel.itertuples()]
    return metrics, best_model


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--test-days", type=int, default=30)
    ap.add_argument("--filings-only", action="store_true")
    ap.add_argument("--no-embeddings", action="store_true")
    ap.add_argument("--split-frac", type=float, default=None,
                    help="Proportional time split, e.g. 0.3 = last 30%% as test.")
    args = ap.parse_args()

    df = asyncio.run(build_dataset(args.days, filings_only=args.filings_only))
    if df.empty:
        raise SystemExit("no dataset rows")
    emb = None if args.no_embeddings else asyncio.run(embed_headlines(df))
    metrics, model = train_and_evaluate(df, args.test_days, args.split_frac,
                                        emb=emb)
    metrics["filings_only"] = args.filings_only
    metrics["feature_version"] = 3

    os.makedirs(EXPORT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).date().isoformat()
    with open(f"{EXPORT_DIR}/materiality_calibrator_v1_{stamp}.json", "w") as f:
        json.dump(metrics, f, indent=2, default=str)
    try:
        import joblib
        joblib.dump(model, f"{EXPORT_DIR}/materiality_calibrator_v1_{stamp}.joblib")
    except Exception as e:
        logger.warning(f"model artifact save failed: {e}")

    print(json.dumps(metrics, indent=2, default=str))
    verdict = ("BEATS the LLM-magnitude baseline out-of-time"
               if metrics["beats_magnitude_baseline"]
               else "does NOT beat the LLM-magnitude baseline — no wiring")
    print(f"\nVERDICT: {metrics['best_model']} {verdict}")


if __name__ == "__main__":
    main()
