"""Materiality lab — experiment harness over the calibrator's dataset.

Companion to materiality_calibrator.py (the champion harness). This module
exists so experiments iterate on a CACHED frame instead of re-running the
temp-table joins against prod Postgres for every idea. Protocol is pinned to
the calibrator's: same label, same time split, seed 42, same baselines.

  docker compose run --rm \
      -v $PWD/bharat_terminal:/app/bharat_terminal:ro -v $PWD/exports:/app/exports \
      --entrypoint python research-runner -m bharat_terminal.research.materiality_lab \
      --build --days 400 --exp all

Ledger: one JSON line per variant in $MATERIALITY_LAB_DIR/ledger.jsonl.
See MATERIALITY_LAB_2026-07-28.md for the problem statement and roadmap.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import bharat_terminal.research.materiality_calibrator as mc

logger = logging.getLogger(__name__)

SEED = 42
LAB_DIR = os.getenv("MATERIALITY_LAB_DIR", "exports/materiality_lab")
SPLIT_FRAC = 0.3
BODY_CHARS = 8000

# Reuse the calibrator's event SQL verbatim, adding only the attachment body
# (capped — TF-IDF saturates well before 8K chars and the cache stays small).
# Monkeypatching the module constant keeps the join/label logic single-sourced.
_BODY_ANCHOR = "coalesce(ni.headline, '') AS headline,"
_EVENTS_SQL_WITH_BODY = mc._EVENTS_SQL.replace(
    _BODY_ANCHOR,
    _BODY_ANCHOR + f"\n       left(coalesce(ni.body, ''), {BODY_CHARS}) AS body,",
    1,
)
assert "AS body" in _EVENTS_SQL_WITH_BODY, "calibrator SQL anchor moved"


def _cache_path(days: int) -> str:
    return f"{LAB_DIR}/dataset_{days}d.pkl.gz"


async def build_cache(days: int) -> "object":
    mc._EVENTS_SQL = _EVENTS_SQL_WITH_BODY
    df = await mc.build_dataset(days, filings_only=True)
    os.makedirs(LAB_DIR, exist_ok=True)
    df.to_pickle(_cache_path(days), compression="gzip")
    logger.info(f"cached {len(df)} rows -> {_cache_path(days)}")
    return df


def load_cache(days: int):
    import pandas as pd
    return pd.read_pickle(_cache_path(days), compression="gzip")


# ── shared evaluation ─────────────────────────────────────────────────────

def split_masks(df):
    cutoff = df["created_at"].quantile(1 - SPLIT_FRAC)
    return df["created_at"] <= cutoff, df["created_at"] > cutoff, cutoff


def evaluate(p, yte, base_rate_test: float) -> dict:
    import numpy as np
    from sklearn.metrics import (average_precision_score, brier_score_loss,
                                 roc_auc_score)
    p = np.asarray(p)
    y = np.asarray(yte)
    k = max(1, len(y) // 10)
    top = np.argsort(-p)[:k]
    bottom = np.argsort(p)[:k]
    return {
        "auc": round(float(roc_auc_score(y, p)), 4),
        "brier": round(float(brier_score_loss(y, p)), 4),
        "pr_auc": round(float(average_precision_score(y, p)), 4),
        "top_decile_pred": round(float(p[top].mean()), 4),
        "top_decile_obs": round(float(y[top].mean()), 4),
        "bottom_decile_obs": round(float(y[bottom].mean()), 4),
        "top_decile_lift": round(float(y[top].mean() / max(base_rate_test, 1e-9)), 2),
    }


def _ledger_write(record: dict) -> None:
    os.makedirs(LAB_DIR, exist_ok=True)
    with open(f"{LAB_DIR}/ledger.jsonl", "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def run_variants(df, variants: dict, exp: str, extra_meta: dict | None = None):
    """variants: name -> (fit_predict callable) returning test-set probs."""
    import numpy as np
    np.random.seed(SEED)
    X, y, df = mc.featurize(df)
    tr, te, cutoff = split_masks(df)
    yte = y[te]
    meta = {
        "exp": exp, "run_at": datetime.now(timezone.utc).isoformat(),
        "n_train": int(tr.sum()), "n_test": int(te.sum()),
        "base_rate_train": round(float(y[tr].mean()), 4),
        "base_rate_test": round(float(yte.mean()), 4),
        "split_cutoff": str(cutoff), "seed": SEED, **(extra_meta or {}),
    }
    results = []
    for name, fn in variants.items():
        p = fn(df, X, y, tr, te)
        m = evaluate(p, yte, meta["base_rate_test"])
        rec = {**meta, "variant": name, **m}
        _ledger_write(rec)
        results.append(rec)
        logger.info(f"[{exp}] {name}: auc={m['auc']} brier={m['brier']} "
                    f"top_decile obs={m['top_decile_obs']} lift={m['top_decile_lift']}")
    return results


# ── experiments ───────────────────────────────────────────────────────────

def exp_e0(df):
    """Exact v3 replication on current data — the fresh champion baseline,
    plus the two no-improvement-rule baselines."""
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    def logistic(df, X, y, tr, te):
        m = make_pipeline(StandardScaler(),
                          LogisticRegression(max_iter=2000, random_state=SEED))
        m.fit(X[tr].values, y[tr])
        return m.predict_proba(X[te].values)[:, 1]

    def hgb(df, X, y, tr, te):
        m = HistGradientBoostingClassifier(random_state=SEED)
        m.fit(X[tr].values, y[tr])
        return m.predict_proba(X[te].values)[:, 1]

    def baseline_magnitude(df, X, y, tr, te):
        mag_p = df[tr].assign(y=y[tr]).groupby("magnitude")["y"].mean()
        return df[te]["magnitude"].map(mag_p).fillna(y[tr].mean()).values

    def baseline_const(df, X, y, tr, te):
        return np.full(int(te.sum()), y[tr].mean())

    return run_variants(df, {
        "v3_logistic": logistic, "v3_hgb": hgb,
        "baseline_magnitude": baseline_magnitude,
        "baseline_const": baseline_const,
    }, "E0")


def exp_e2(df):
    """Model sweep on the v3 feature set."""
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import (HistGradientBoostingClassifier,
                                  RandomForestClassifier)
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    def lr(C, cw):
        def f(df, X, y, tr, te):
            m = make_pipeline(StandardScaler(), LogisticRegression(
                C=C, class_weight=cw, max_iter=4000, random_state=SEED))
            m.fit(X[tr].values, y[tr])
            return m.predict_proba(X[te].values)[:, 1]
        return f

    def hgb(lr_, leaves, min_leaf):
        def f(df, X, y, tr, te):
            m = HistGradientBoostingClassifier(
                learning_rate=lr_, max_leaf_nodes=leaves,
                min_samples_leaf=min_leaf, l2_regularization=1.0,
                random_state=SEED)
            m.fit(X[tr].values, y[tr])
            return m.predict_proba(X[te].values)[:, 1]
        return f

    def hgb_iso(df, X, y, tr, te):
        base = HistGradientBoostingClassifier(
            learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=50,
            l2_regularization=1.0, random_state=SEED)
        m = CalibratedClassifierCV(base, method="isotonic", cv=3)
        m.fit(X[tr].values, y[tr])
        return m.predict_proba(X[te].values)[:, 1]

    def rf(df, X, y, tr, te):
        m = RandomForestClassifier(
            n_estimators=400, min_samples_leaf=20,
            class_weight="balanced_subsample", random_state=SEED, n_jobs=2)
        m.fit(X[tr].values, y[tr])
        return m.predict_proba(X[te].values)[:, 1]

    return run_variants(df, {
        "lr_C0.1": lr(0.1, None), "lr_C1_balanced": lr(1.0, "balanced"),
        "hgb_slow_small": hgb(0.05, 15, 50), "hgb_slow_31": hgb(0.05, 31, 50),
        "hgb_default_lr_31": hgb(0.1, 31, 50),
        "hgb_isotonic": hgb_iso, "rf_balanced": rf,
    }, "E2")


def exp_e1(df):
    """Filing BODY text via TF-IDF -> linear models. The untapped source."""
    from sklearn.linear_model import LogisticRegression

    def tfidf_lr(strip_digits, C):
        def f(df, X, y, tr, te):
            import re
            from sklearn.feature_extraction.text import TfidfVectorizer
            text = (df["headline"].fillna("") + " " + df["body"].fillna(""))
            if strip_digits:
                text = text.map(lambda s: re.sub(r"\d+", " ", s))
            vec = TfidfVectorizer(max_features=50000, ngram_range=(1, 2),
                                  min_df=3, sublinear_tf=True)
            Ttr = vec.fit_transform(text[tr])
            Tte = vec.transform(text[te])
            m = LogisticRegression(C=C, class_weight="balanced",
                                   max_iter=3000, random_state=SEED)
            m.fit(Ttr, y[tr])
            return m.predict_proba(Tte)[:, 1]
        return f

    return run_variants(df, {
        "tfidf_lr_C0.5": tfidf_lr(False, 0.5),
        "tfidf_lr_C0.1": tfidf_lr(False, 0.1),
        "tfidf_nodigits_lr_C0.5": tfidf_lr(True, 0.5),
    }, "E1", {"body_chars": BODY_CHARS})


def exp_e3(df):
    """Body-TF-IDF -> SVD (train-fit) stacked with v3 structured features."""
    def combo(model_kind):
        def f(df, X, y, tr, te):
            import numpy as np
            from sklearn.decomposition import TruncatedSVD
            from sklearn.ensemble import HistGradientBoostingClassifier
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.linear_model import LogisticRegression
            from sklearn.pipeline import make_pipeline
            from sklearn.preprocessing import StandardScaler
            text = (df["headline"].fillna("") + " " + df["body"].fillna(""))
            vec = TfidfVectorizer(max_features=50000, ngram_range=(1, 2),
                                  min_df=3, sublinear_tf=True)
            Ttr = vec.fit_transform(text[tr])
            Tte = vec.transform(text[te])
            svd = TruncatedSVD(n_components=128, random_state=SEED)
            Etr, Ete = svd.fit_transform(Ttr), svd.transform(Tte)
            htr = np.hstack([X[tr].values, Etr])
            hte = np.hstack([X[te].values, Ete])
            if model_kind == "lr":
                m = make_pipeline(StandardScaler(), LogisticRegression(
                    C=0.5, class_weight="balanced", max_iter=4000,
                    random_state=SEED))
            else:
                m = HistGradientBoostingClassifier(
                    learning_rate=0.05, max_leaf_nodes=31,
                    min_samples_leaf=50, l2_regularization=1.0,
                    random_state=SEED)
            m.fit(htr, y[tr])
            return m.predict_proba(hte)[:, 1]
        return f

    return run_variants(df, {
        "svd128_lr": combo("lr"), "svd128_hgb": combo("hgb"),
    }, "E3")


def _iso_calibrated(fit_predict_inner):
    """Time-ordered calibration: fit on the first 80% of TRAIN, isotonic on
    the last 20%, apply to TEST. Monotone -> ranking (AUC) preserved."""
    def f(df, X, y, tr, te):
        import numpy as np
        from sklearn.isotonic import IsotonicRegression
        tr_times = df.loc[tr, "created_at"]
        inner_cut = tr_times.quantile(0.8)
        tr1 = tr & (df["created_at"] <= inner_cut)
        cal = tr & (df["created_at"] > inner_cut)
        p_cal, p_te = fit_predict_inner(df, X, y, tr1, cal, te)
        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(p_cal, y[cal])
        return np.clip(iso.predict(p_te), 1e-4, 1 - 1e-4)
    return f


def _rf_inner(df, X, y, tr1, cal, te):
    from sklearn.ensemble import RandomForestClassifier
    m = RandomForestClassifier(n_estimators=400, min_samples_leaf=20,
                               class_weight="balanced_subsample",
                               random_state=SEED, n_jobs=2)
    m.fit(X[tr1].values, y[tr1])
    return (m.predict_proba(X[cal].values)[:, 1],
            m.predict_proba(X[te].values)[:, 1])


def _svdhgb_inner(df, X, y, tr1, cal, te):
    import numpy as np
    from sklearn.decomposition import TruncatedSVD
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.feature_extraction.text import TfidfVectorizer
    text = (df["headline"].fillna("") + " " + df["body"].fillna(""))
    vec = TfidfVectorizer(max_features=50000, ngram_range=(1, 2),
                          min_df=3, sublinear_tf=True)
    Ttr1 = vec.fit_transform(text[tr1])
    svd = TruncatedSVD(n_components=128, random_state=SEED)
    Etr1 = svd.fit_transform(Ttr1)
    m = HistGradientBoostingClassifier(
        learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=50,
        l2_regularization=1.0, random_state=SEED)
    m.fit(np.hstack([X[tr1].values, Etr1]), y[tr1])

    def proj(mask):
        E = svd.transform(vec.transform(text[mask]))
        return m.predict_proba(np.hstack([X[mask].values, E]))[:, 1]
    return proj(cal), proj(te)


def _lr_c01(df, X, y, tr, te):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    m = make_pipeline(StandardScaler(), LogisticRegression(
        C=0.1, max_iter=4000, random_state=SEED))
    m.fit(X[tr].values, y[tr])
    return m.predict_proba(X[te].values)[:, 1]


def _svdhgb_full(df, X, y, tr, te):
    """svd128_hgb fit on ALL of train (the E3 winner), for blending."""
    import numpy as np
    from sklearn.decomposition import TruncatedSVD
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.feature_extraction.text import TfidfVectorizer
    text = (df["headline"].fillna("") + " " + df["body"].fillna(""))
    vec = TfidfVectorizer(max_features=50000, ngram_range=(1, 2),
                          min_df=3, sublinear_tf=True)
    Ttr = vec.fit_transform(text[tr])
    svd = TruncatedSVD(n_components=128, random_state=SEED)
    Etr = svd.fit_transform(Ttr)
    m = HistGradientBoostingClassifier(
        learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=50,
        l2_regularization=1.0, random_state=SEED)
    m.fit(np.hstack([X[tr].values, Etr]), y[tr])
    Ete = svd.transform(vec.transform(text[te]))
    return m.predict_proba(np.hstack([X[te].values, Ete]))[:, 1]


def exp_e4(df):
    """Calibrate the strong rankers; blend the diverse ones."""
    import numpy as np
    rf_iso = _iso_calibrated(_rf_inner)
    svdhgb_iso = _iso_calibrated(_svdhgb_inner)

    def blend2(df, X, y, tr, te):
        return (_lr_c01(df, X, y, tr, te) + _svdhgb_full(df, X, y, tr, te)) / 2

    def blend3(df, X, y, tr, te):
        return np.mean([_lr_c01(df, X, y, tr, te),
                        _svdhgb_full(df, X, y, tr, te),
                        rf_iso(df, X, y, tr, te)], axis=0)

    return run_variants(df, {
        "rf_isotonic": rf_iso, "svdhgb_isotonic": svdhgb_iso,
        "blend_lr+svdhgb": blend2, "blend_lr+svdhgb+rfiso": blend3,
    }, "E4")


def exp_e5(df):
    """Split-stability: top variants across alternate time cutoffs (0.3 is
    already covered by E2/E3/E4 ledger rows). Champion claims must survive
    split perturbation, not win one lucky window."""
    global SPLIT_FRAC
    results, orig = [], SPLIT_FRAC
    variants = {
        "lr_C0.1": _lr_c01,
        "svd128_hgb": _svdhgb_full,
        "blend_lr+svdhgb": lambda df, X, y, tr, te: (
            (_lr_c01(df, X, y, tr, te) + _svdhgb_full(df, X, y, tr, te)) / 2),
    }
    for frac in (0.2, 0.25, 0.35):
        try:
            SPLIT_FRAC = frac
            results += run_variants(df, variants, "E5", {"split_frac": frac})
        finally:
            SPLIT_FRAC = orig
    return results


def exp_e6(df):
    """Per-event-type diagnostic of the v4 champion blend: where the AUC
    lives, what fills the top decile, and per-type base rates. Also exports
    the fitted v4 artifact (both parents + vectorizer + svd) to LAB_DIR."""
    import numpy as np
    import pandas as pd
    from sklearn.metrics import roc_auc_score

    np.random.seed(SEED)
    X, y, dff = mc.featurize(df)
    tr, te, cutoff = split_masks(dff)
    p = (_lr_c01(dff, X, y, tr, te) + _svdhgb_full(dff, X, y, tr, te)) / 2
    yte = y[te].values
    types = dff.loc[te, "event_type"].values

    d = pd.DataFrame({"p": p, "y": yte, "type": types})
    k = max(1, len(d) // 10)
    top_idx = d["p"].nlargest(k).index
    d["in_top"] = d.index.isin(top_idx)

    rows = []
    for t, g in d.groupby("type"):
        auc = (round(float(roc_auc_score(g["y"], g["p"])), 4)
               if g["y"].nunique() > 1 and len(g) >= 30 else None)
        rows.append({
            "exp": "E6", "variant": f"type_{t}", "n_test": int(len(g)),
            "base_rate_test": round(float(g["y"].mean()), 4),
            "auc": auc,
            "share_of_top_decile": round(float(g["in_top"].sum() / k), 3),
            "top_hits_in_type": int(g.loc[g["in_top"], "y"].sum()),
            "split_cutoff": str(cutoff), "seed": SEED,
            "run_at": datetime.now(timezone.utc).isoformat(),
        })
        _ledger_write(rows[-1])
        logger.info(f"[E6] {t:22s} n={len(g):5d} base={g['y'].mean():.3f} "
                    f"auc={auc} top_share={g['in_top'].sum()/k:.2f}")

    # v4 artifact: refit both parents on ALL data up to the cutoff and save.
    try:
        import joblib
        from sklearn.decomposition import TruncatedSVD
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        lr = make_pipeline(StandardScaler(), LogisticRegression(
            C=0.1, max_iter=4000, random_state=SEED))
        lr.fit(X[tr].values, y[tr])
        text = (dff["headline"].fillna("") + " " + dff["body"].fillna(""))
        vec = TfidfVectorizer(max_features=50000, ngram_range=(1, 2),
                              min_df=3, sublinear_tf=True)
        Ttr = vec.fit_transform(text[tr])
        svd = TruncatedSVD(n_components=128, random_state=SEED)
        Etr = svd.fit_transform(Ttr)
        hgb = HistGradientBoostingClassifier(
            learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=50,
            l2_regularization=1.0, random_state=SEED)
        hgb.fit(np.hstack([X[tr].values, Etr]), y[tr])
        path = f"{LAB_DIR}/materiality_v4_blend.joblib"
        joblib.dump({"lr": lr, "vec": vec, "svd": svd, "hgb": hgb,
                     "feature_columns": list(X.columns),
                     "train_cutoff": str(cutoff), "seed": SEED,
                     "recipe": "p = (lr.predict_proba + hgb.predict_proba)/2; "
                               "hgb input = [X, svd(vec(headline+body))]"}, path)
        logger.info(f"[E6] v4 artifact -> {path}")
    except Exception as e:
        logger.warning(f"[E6] artifact export failed: {e}")
    return rows


# E7: mined from the 3,642-row 'other' bucket (2026-07-28) — top patterns
# were press releases (~500), investor presentations (~340), literal
# "General Updates" (429), operational updates, litigation/CIRP, and
# procedural reports. Checked AFTER the calibrator's 16 buckets so every
# existing assignment is unchanged; only 'other' rows can move.
_EXTRA_EVENT_TYPES = [
    ("litigation_insolvency", ("litigation", "dispute", "insolvency",
                               "cirp", "nclt")),
    ("business_update", ("business update", "capacity addition",
                         "commercial production", "commencement of commercial",
                         "operational update")),
    ("investor_presentation", ("investor presentation",)),
    ("procedural_report", ("responsibility and sustainability",
                           "statement of deviation", "monitoring agency report",
                           "demat report", "change in auditor", "stock option",
                           "esop", "amendment to aoa")),
    ("press_release", ("press release", "media release")),
    ("general_updates", ("general update",)),
]


def event_type_v2(headline: str) -> str:
    h = (headline or "").lower()
    for bucket, keys in mc._EVENT_TYPES:
        if any(k in h for k in keys):
            return bucket
    for bucket, keys in _EXTRA_EVENT_TYPES:
        if any(k in h for k in keys):
            return bucket
    return "other"


def exp_e7(df):
    """A: v4 blend with the extended 22-bucket taxonomy (only 'other' rows
    reassigned). B: results-specialist blend trained on results rows only
    (global model's within-results AUC was 0.6749 in E6)."""
    def blend(df, X, y, tr, te):
        return (_lr_c01(df, X, y, tr, te) + _svdhgb_full(df, X, y, tr, te)) / 2

    results = []
    orig = mc.event_type
    try:
        mc.event_type = event_type_v2       # featurize resolves at call time
        results += run_variants(df, {"blend_tax22": blend}, "E7",
                                {"taxonomy": "v2_22buckets"})
    finally:
        mc.event_type = orig

    sub = df[df["headline"].map(mc.event_type) == "results"].copy()
    results += run_variants(sub, {"blend_results_specialist": blend}, "E7",
                            {"subset": "results",
                             "global_within_results_auc": 0.6749})
    return results


def exp_e8(df):
    """Stability gauntlet for the tax22 blend (E7-A) across the same
    alternate splits E5 used. Per-split benchmarks to beat (v4 blend on the
    16-bucket taxonomy): 0.2→0.7183, 0.25→0.7205, 0.35→0.7234."""
    global SPLIT_FRAC
    def blend(df, X, y, tr, te):
        return (_lr_c01(df, X, y, tr, te) + _svdhgb_full(df, X, y, tr, te)) / 2

    results, orig_frac, orig_et = [], SPLIT_FRAC, mc.event_type
    try:
        mc.event_type = event_type_v2
        for frac in (0.2, 0.25, 0.35):
            try:
                SPLIT_FRAC = frac
                results += run_variants(df, {"blend_tax22": blend}, "E8",
                                        {"split_frac": frac,
                                         "taxonomy": "v2_22buckets"})
            finally:
                SPLIT_FRAC = orig_frac
    finally:
        mc.event_type = orig_et
    return results


# ── D-series: DIRECTION research (feeds the paper-trading signal rules) ──
# Direction is a different target from materiality: y = sign(next-session
# return). The 2026-07-04 audit killed LLM direction globally; the
# 2026-07-28 confusion matrix found the asymmetry (negatives real,
# positives = drift). D-series quantifies baselines per magnitude band and
# tries to beat them, incl. per-filing-type specialization.

async def build_cache_v2(days: int):
    """v2 cache: body + SIGNED sue_composite (direction needs the sign)."""
    _SUE_SEL = "SELECT abs(s.sue_composite) AS sue_abs, 1 AS sue_found"
    join_v2 = mc._JOIN_SQL.replace(
        _SUE_SEL,
        "SELECT abs(s.sue_composite) AS sue_abs, s.sue_composite AS sue_signed,"
        " 1 AS sue_found", 1)
    join_v2 = join_v2.replace("es.sue_abs, es.sue_found",
                              "es.sue_abs, es.sue_signed, es.sue_found", 1)
    assert join_v2.count("sue_signed") == 2, "calibrator JOIN anchor moved"
    mc._EVENTS_SQL = _EVENTS_SQL_WITH_BODY
    mc._JOIN_SQL = join_v2
    df = await mc.build_dataset(days, filings_only=True)
    os.makedirs(LAB_DIR, exist_ok=True)
    df.to_pickle(f"{LAB_DIR}/dataset_{days}d_v2.pkl.gz", compression="gzip")
    logger.info(f"cached v2 {len(df)} rows")
    return df


def _dir_frame(df):
    """Direction working frame: signed return, band, event_type(v2)."""
    import numpy as np
    df = df.copy()
    df["ret"] = df["react_close"] / df["base_close"] - 1
    df = df[df["ret"] != 0]
    df["up"] = (df["ret"] > 0).astype(int)
    df["band"] = np.where(df["magnitude"] >= 4, "high",
                          np.where(df["magnitude"] == 3, "medium", "low"))
    df["event_type"] = df["headline"].map(event_type_v2)
    return df


def exp_d0(df):
    """Baseline directional accuracy of the LLM classification per band,
    drift-adjusted against same-band NEUTRAL rows (the control)."""
    d = _dir_frame(df)
    neutral_up = d[d["sentiment"] == "neutral"].groupby("band")["up"].mean()
    rows = []
    for (band, sent), g in d.groupby(["band", "sentiment"]):
        if sent not in ("positive", "negative") or len(g) < 20:
            continue
        nu = float(neutral_up.get(band, d["up"].mean()))
        hit = float(g["up"].mean()) if sent == "positive" else 1 - float(g["up"].mean())
        base = nu if sent == "positive" else 1 - nu
        rec = {"exp": "D0", "variant": f"{band}_{sent}", "n": int(len(g)),
               "hit_rate": round(hit, 4), "drift_base": round(base, 4),
               "edge_pp": round(100 * (hit - base), 2),
               "median_ret_pct": round(100 * float(g["ret"].median()), 3),
               "run_at": datetime.now(timezone.utc).isoformat(), "seed": SEED}
        _ledger_write(rec); rows.append(rec)
        logger.info(f"[D0] {band:6s} {sent:8s} n={len(g):5d} hit={hit:.3f} "
                    f"base={base:.3f} edge={100*(hit-base):+.1f}pp")
    # specialization targets: per event_type × sentiment (non-neutral)
    for (et, sent), g in d.groupby(["event_type", "sentiment"]):
        if sent not in ("positive", "negative") or len(g) < 40:
            continue
        nu = float(d[d["sentiment"] == "neutral"]["up"].mean())
        hit = float(g["up"].mean()) if sent == "positive" else 1 - float(g["up"].mean())
        base = nu if sent == "positive" else 1 - nu
        rec = {"exp": "D0T", "variant": f"{et}_{sent}", "n": int(len(g)),
               "hit_rate": round(hit, 4), "drift_base": round(base, 4),
               "edge_pp": round(100 * (hit - base), 2),
               "run_at": datetime.now(timezone.utc).isoformat(), "seed": SEED}
        _ledger_write(rec); rows.append(rec)
        logger.info(f"[D0T] {et:22s} {sent:8s} n={len(g):5d} "
                    f"hit={hit:.3f} edge={100*(hit-base):+.1f}pp")
    return rows


def _dir_features(d):
    """Event-time features for DIRECTION (signed variants allowed)."""
    import numpy as np
    import pandas as pd
    X = pd.DataFrame(index=d.index)
    X["magnitude"] = d["magnitude"].clip(1, 5)
    X["confidence"] = d["confidence"].fillna(0)
    X["runup_signed"] = (d["ret5_pre"]
                         / (d["vol60"] * np.sqrt(5))).clip(-10, 10).fillna(0)
    X["sue_signed"] = d.get("sue_signed", pd.Series(0, index=d.index)).fillna(0).clip(-10, 10)
    X["has_sue"] = d["sue_found"].fillna(0).astype(int)
    for col, prefix in (("sentiment", "sent"), ("horizon", "hz"),
                        ("event_type", "ev")):
        dm = pd.get_dummies(d[col].fillna("").astype(str).str.lower(),
                            prefix=prefix, dtype=int)
        X = pd.concat([X, dm.iloc[:, :26]], axis=1)
    return X


def exp_d1(df):
    """Global direction models vs the D0 sentiment baseline, per band."""
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    np.random.seed(SEED)
    d = _dir_frame(df)
    X = _dir_features(d)
    y = d["up"]
    cutoff = d["created_at"].quantile(1 - SPLIT_FRAC)
    tr, te = d["created_at"] <= cutoff, d["created_at"] > cutoff

    def _text_mat(mask_fit, masks):
        from sklearn.decomposition import TruncatedSVD
        from sklearn.feature_extraction.text import TfidfVectorizer
        text = (d["headline"].fillna("") + " " + d["body"].fillna(""))
        vec = TfidfVectorizer(max_features=50000, ngram_range=(1, 2),
                              min_df=3, sublinear_tf=True)
        T = vec.fit_transform(text[mask_fit])
        svd = TruncatedSVD(n_components=128, random_state=SEED)
        svd.fit(T)
        return [svd.transform(vec.transform(text[m])) for m in masks]

    models = {}
    lr = make_pipeline(StandardScaler(), LogisticRegression(
        C=0.1, max_iter=4000, random_state=SEED))
    lr.fit(X[tr].values, y[tr])
    models["dir_lr"] = lr.predict_proba(X[te].values)[:, 1]
    hgb = HistGradientBoostingClassifier(
        learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=50,
        l2_regularization=1.0, random_state=SEED)
    hgb.fit(X[tr].values, y[tr])
    models["dir_hgb"] = hgb.predict_proba(X[te].values)[:, 1]
    Etr, Ete = _text_mat(tr, [tr, te])
    hgb2 = HistGradientBoostingClassifier(
        learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=50,
        l2_regularization=1.0, random_state=SEED)
    hgb2.fit(np.hstack([X[tr].values, Etr]), y[tr])
    models["dir_hgb_text"] = hgb2.predict_proba(np.hstack([X[te].values, Ete]))[:, 1]

    yte = y[te].values
    bands = d.loc[te, "band"].values
    rows = []
    for name, p in models.items():
        acc = float(((p > 0.5) == yte).mean())
        auc = float(roc_auc_score(yte, p))
        conv = np.abs(p - 0.5)
        k = max(1, len(p) // 10)
        topc = np.argsort(-conv)[:k]
        conv_hit = float(((p[topc] > 0.5) == yte[topc]).mean())
        rec = {"exp": "D1", "variant": name, "n_test": int(te.sum()),
               "auc_up": round(auc, 4), "acc": round(acc, 4),
               "top_conviction_decile_hit": round(conv_hit, 4),
               "split_cutoff": str(cutoff), "seed": SEED,
               "run_at": datetime.now(timezone.utc).isoformat()}
        for b in ("high", "medium", "low"):
            m = bands == b
            if m.sum() >= 30:
                rec[f"acc_{b}"] = round(float(((p[m] > 0.5) == yte[m]).mean()), 4)
        _ledger_write(rec); rows.append(rec)
        logger.info(f"[D1] {name}: auc={auc:.4f} acc={acc:.4f} "
                    f"conv_hit={conv_hit:.4f} "
                    + " ".join(f"{b}={rec.get(f'acc_{b}')}" for b in ('high','medium','low')))
    return rows


def exp_d2(df):
    """Specialists: results (signed-SUE core) + rating keyword rule."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    d = _dir_frame(df)
    rows = []

    res = d[d["event_type"] == "results"]
    if len(res) >= 300:
        cutoff = res["created_at"].quantile(1 - SPLIT_FRAC)
        tr, te = res["created_at"] <= cutoff, res["created_at"] > cutoff
        X = _dir_features(res)
        lr = make_pipeline(StandardScaler(), LogisticRegression(
            C=0.5, max_iter=4000, random_state=SEED))
        lr.fit(X[tr].values, res.loc[tr, "up"])
        p = lr.predict_proba(X[te].values)[:, 1]
        yte = res.loc[te, "up"].values
        sue_rule = (res.loc[te, "sue_signed"].fillna(0) > 0).astype(int).values
        rec = {"exp": "D2", "variant": "results_dir_lr", "n_test": int(te.sum()),
               "acc": round(float(((p > 0.5) == yte).mean()), 4),
               "sue_sign_rule_acc": round(float((sue_rule == yte).mean()), 4),
               "sue_coverage": round(float((res.loc[te, "sue_signed"].fillna(0) != 0).mean()), 3),
               "run_at": datetime.now(timezone.utc).isoformat(), "seed": SEED}
        _ledger_write(rec); rows.append(rec)
        logger.info(f"[D2] results: model_acc={rec['acc']} "
                    f"sue_rule_acc={rec['sue_sign_rule_acc']} n={rec['n_test']}")

    rat = d[d["event_type"] == "rating"]
    if len(rat) >= 60:
        text = (rat["headline"].fillna("") + " " + rat["body"].fillna("")).str.lower()
        pred_up = np.where(text.str.contains("downgrad"), 0,
                           np.where(text.str.contains("upgrad"), 1,
                                    (rat["sentiment"] == "positive").astype(int)))
        rec = {"exp": "D2", "variant": "rating_keyword_rule", "n": int(len(rat)),
               "acc": round(float((pred_up == rat["up"].values).mean()), 4),
               "run_at": datetime.now(timezone.utc).isoformat(), "seed": SEED}
        _ledger_write(rec); rows.append(rec)
        logger.info(f"[D2] rating rule: acc={rec['acc']} n={rec['n']}")
    return rows


def exp_ex(df):
    """Refresh the PRODUCTION materiality-gate artifact: v4.1 (tax22
    taxonomy), both parents fit on ALL cached rows — this serves live
    gating going forward, so no holdout (metrics live in E7/E8 ledger)."""
    import numpy as np
    orig = mc.event_type
    try:
        mc.event_type = event_type_v2
        X, y, dff = mc.featurize(df)
    finally:
        mc.event_type = orig
    import joblib
    from sklearn.decomposition import TruncatedSVD
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    lr = make_pipeline(StandardScaler(), LogisticRegression(
        C=0.1, max_iter=4000, random_state=SEED))
    lr.fit(X.values, y)
    text = (dff["headline"].fillna("") + " " + dff["body"].fillna(""))
    vec = TfidfVectorizer(max_features=50000, ngram_range=(1, 2),
                          min_df=3, sublinear_tf=True)
    T = vec.fit_transform(text)
    svd = TruncatedSVD(n_components=128, random_state=SEED)
    E = svd.fit_transform(T)
    hgb = HistGradientBoostingClassifier(
        learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=50,
        l2_regularization=1.0, random_state=SEED)
    hgb.fit(np.hstack([X.values, E]), y)
    path = f"{LAB_DIR}/materiality_v4_1_tax22.joblib"
    joblib.dump({"lr": lr, "vec": vec, "svd": svd, "hgb": hgb,
                 "feature_columns": list(X.columns), "taxonomy": "v2_22buckets",
                 "fit_through": str(dff["created_at"].max()), "seed": SEED,
                 "recipe": "p = (lr + hgb)/2; hgb input = [X, svd(vec(headline+body))]"},
                path)
    logger.info(f"[EX] v4.1 gate artifact -> {path}")
    return [{"exp": "EX", "variant": "v4_1_artifact", "n": int(len(y))}]


# ── S-series: STEP 0 of the SOTA revision (§8, 2026-07-28) ───────────────
# Relabel in TRADE-SPACE: decompose every event into the untradeable gap
# (base close -> reaction-day open) and the capturable component (open ->
# close for off-session filings; same-day close-to-close for
# during-session ones, where we can act at t). Market-adjust with the
# universe cross-sectional median. Fix the PIT bug review found: the SUE
# join accepted rows announced up to +1 DAY after the event (training saw
# surprise data the live gate never has).

_JOIN_SQL_V3 = """
SELECT e.*, b.close AS base_close, b.vol60, b.adv_cr, b.ret5_pre,
       r.close AS react_close, r.open AS react_open, r.date AS react_date,
       r3.close AS close_h3, r5.close AS close_h5, r20.close AS close_h20,
       c.mcap_cr,
       es.sue_abs, es.sue_signed, es.sue_found
FROM _cal_events e
JOIN LATERAL (
    SELECT close, vol60, adv_cr, nret, ret5_pre FROM _cal_px p
    WHERE p.symbol = e.symbol AND p.date < e.public_day
    ORDER BY date DESC LIMIT 1
) b ON b.nret >= :min_vol_obs AND b.vol60 IS NOT NULL AND b.vol60 > 0
JOIN LATERAL (
    SELECT close, open, date FROM _cal_px p
    WHERE p.symbol = e.symbol AND p.date >= e.public_day
    ORDER BY date ASC LIMIT 1
) r ON r.date <= e.public_day + 7
LEFT JOIN LATERAL (
    SELECT close FROM _cal_px p
    WHERE p.symbol = e.symbol AND p.date >= e.public_day
    ORDER BY date ASC OFFSET 3 LIMIT 1) r3 ON true
LEFT JOIN LATERAL (
    SELECT close FROM _cal_px p
    WHERE p.symbol = e.symbol AND p.date >= e.public_day
    ORDER BY date ASC OFFSET 5 LIMIT 1) r5 ON true
LEFT JOIN LATERAL (
    SELECT close FROM _cal_px p
    WHERE p.symbol = e.symbol AND p.date >= e.public_day
    ORDER BY date ASC OFFSET 20 LIMIT 1) r20 ON true
LEFT JOIN companies c ON c.symbol = e.symbol
LEFT JOIN LATERAL (
    SELECT abs(s.sue_composite) AS sue_abs, s.sue_composite AS sue_signed,
           1 AS sue_found
    FROM earnings_surprises s
    WHERE s.symbol = e.symbol AND s.sue_composite IS NOT NULL
      -- PIT: only surprise rows announced AT OR BEFORE analysis time.
      AND s.announced_at BETWEEN e.created_at - interval '3 days'
                             AND e.created_at
    ORDER BY abs(extract(epoch FROM s.announced_at - e.created_at)) ASC
    LIMIT 1
) es ON true
WHERE e.magnitude > 0
"""

_MKT_MEDIANS_SQL = """
SELECT date,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY ret) AS med_ret,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY gap) AS med_gap,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY oc) AS med_oc
FROM (
    SELECT date,
           close / nullif(lag(close) OVER w, 0) - 1 AS ret,
           open / nullif(lag(close) OVER w, 0) - 1 AS gap,
           close / nullif(open, 0) - 1 AS oc
    FROM price_points
    WHERE date >= cast(:d0 AS date)
    WINDOW w AS (PARTITION BY symbol ORDER BY date)
) t
WHERE ret IS NOT NULL
GROUP BY date
"""


async def build_cache_v3(days: int):
    """v3 cache: body + signed PIT-correct SUE + reaction OHLC decomposition
    + h3/h5/h20 closes, and a per-date universe-median adjustment table."""
    import pandas as pd
    from sqlalchemy import text
    from bharat_terminal.kb.sync.utils import create_async_session

    mc._EVENTS_SQL = _EVENTS_SQL_WITH_BODY
    _px_with_open = mc._PX_SQL.replace(
        "SELECT symbol, date, close, ret5_pre,",
        "SELECT symbol, date, close, open, ret5_pre,", 1).replace(
        "    SELECT symbol, date, close,\n",
        "    SELECT symbol, date, close, open,\n", 1)
    assert _px_with_open.count("open") == 2, "PX_SQL anchor moved"
    mc._PX_SQL = _px_with_open
    mc._JOIN_SQL = _JOIN_SQL_V3
    df = await mc.build_dataset(days, filings_only=True)

    SF = create_async_session()
    async with SF() as db:
        rows = (await db.execute(
            text(_MKT_MEDIANS_SQL),
            {"d0": pd.Timestamp(df["public_day"].min()).date()})).mappings().all()
        await db.rollback()
    mkt = pd.DataFrame([dict(r) for r in rows])

    os.makedirs(LAB_DIR, exist_ok=True)
    df.to_pickle(f"{LAB_DIR}/dataset_{days}d_v3.pkl.gz", compression="gzip")
    mkt.to_pickle(f"{LAB_DIR}/mkt_medians.pkl.gz", compression="gzip")
    logger.info(f"cached v3 {len(df)} rows + {len(mkt)} market-median dates")
    return df, mkt


def _tradespace_frame(df, mkt):
    """Attach market-adjusted, vol-scaled components to every event.

    capturable = what a next-open fill (off-session) or an at-t entry
    (during-session, close-to-close proxy) could actually earn.
    """
    import numpy as np
    import pandas as pd
    df = df.copy()
    mkt = mkt.copy()
    mkt["date"] = pd.to_datetime(mkt["date"])
    df["react_date"] = pd.to_datetime(df["react_date"])
    df = df.merge(mkt, left_on="react_date", right_on="date", how="left")
    for c in ("med_ret", "med_gap", "med_oc"):
        df[c] = df[c].fillna(0.0).astype(float)

    df["gap_ar"] = (df["react_open"] / df["base_close"] - 1) - df["med_gap"]
    df["postopen_ar"] = (df["react_close"] / df["react_open"] - 1) - df["med_oc"]
    df["cc_ar"] = (df["react_close"] / df["base_close"] - 1) - df["med_ret"]
    # during-session events: public_day == filing day, so cc_ar IS the
    # from-decision reaction; off-session events: only open->close is ours.
    df["capturable_ar"] = np.where(df["market_hours"], df["cc_ar"],
                                   df["postopen_ar"])
    for h, col in ((3, "close_h3"), (5, "close_h5"), (20, "close_h20")):
        df[f"car_h{h}"] = df[col] / df["base_close"] - 1 - df["med_ret"]
    for c in ("gap_ar", "postopen_ar", "cc_ar", "capturable_ar",
              "car_h3", "car_h5", "car_h20"):
        df[c.replace("_ar", "_z").replace("car_", "car_z_")] = (
            df[c] / df["vol60"])
    df["material_capturable"] = (
        df["capturable_ar"].abs()
        >= np.maximum(mc.K_SIGMA * df["vol60"], mc.MIN_ABS_MOVE)).astype(int)
    df["event_type"] = df["headline"].map(event_type_v2)
    df["band"] = np.where(df["magnitude"] >= 4, "high",
                          np.where(df["magnitude"] == 3, "medium", "low"))
    return df


def exp_s0(df, mkt=None):
    """Step 0: the trade-space failure map. Direction + materiality on the
    CAPTURABLE component, time-split, plus the PIT-fix impact and a
    champion-family refit on the capturable label."""
    import numpy as np
    import pandas as pd
    if mkt is None:
        mkt = pd.read_pickle(f"{LAB_DIR}/mkt_medians.pkl.gz",
                             compression="gzip")
    d = _tradespace_frame(df, mkt)
    cutoff = d["created_at"].quantile(1 - SPLIT_FRAC)
    test = d[d["created_at"] > cutoff]
    rows = []

    # T1: where does the move LIVE per type — gap vs capturable (all rows).
    for et, g in d.groupby("event_type"):
        if len(g) < 60:
            continue
        rec = {"exp": "S0_MAP", "variant": et, "n": int(len(g)),
               "mat_capturable": round(float(g["material_capturable"].mean()), 3),
               "med_abs_gap_z": round(float(g["gap_z"].abs().median()), 2),
               "med_abs_capturable_z": round(float(g["capturable_z"].abs().median()), 2),
               "med_abs_car_z_h5": round(float(g["car_z_h5"].abs().median()), 2),
               "run_at": datetime.now(timezone.utc).isoformat(), "seed": SEED}
        _ledger_write(rec); rows.append(rec)
        logger.info(f"[S0_MAP] {et:22s} n={len(g):5d} "
                    f"matC={rec['mat_capturable']:.3f} "
                    f"|gapZ|={rec['med_abs_gap_z']:.2f} "
                    f"|capZ|={rec['med_abs_capturable_z']:.2f} "
                    f"|carZ5|={rec['med_abs_car_z_h5']:.2f}")

    # T2: direction on the capturable component, TEST WINDOW, hop-0,
    # drift-adjusted vs same-window neutral controls.
    t = test[test["hop"] == 0].copy()
    t["cap_up"] = (t["capturable_ar"] > 0).astype(int)
    neutral_up = float(t.loc[t["sentiment"] == "neutral", "cap_up"].mean())
    for (band, sent), g in t.groupby(["band", "sentiment"]):
        if sent not in ("positive", "negative") or len(g) < 25:
            continue
        hit = float(g["cap_up"].mean()) if sent == "positive" \
            else 1 - float(g["cap_up"].mean())
        base = neutral_up if sent == "positive" else 1 - neutral_up
        rec = {"exp": "S0_DIR", "variant": f"{band}_{sent}", "n": int(len(g)),
               "hit_capturable": round(hit, 3), "drift_base": round(base, 3),
               "edge_pp": round(100 * (hit - base), 1),
               "med_dir_cap_z": round(float(
                   (g["capturable_z"] * np.where(
                       g["sentiment"] == "positive", 1, -1)).median()), 2),
               "split_cutoff": str(cutoff), "seed": SEED,
               "run_at": datetime.now(timezone.utc).isoformat()}
        _ledger_write(rec); rows.append(rec)
        logger.info(f"[S0_DIR] {band:6s} {sent:8s} n={len(g):4d} "
                    f"hit={hit:.3f} edge={100 * (hit - base):+.1f}pp "
                    f"medZ={rec['med_dir_cap_z']:+.2f}")
    for (et, sent), g in t.groupby(["event_type", "sentiment"]):
        if sent not in ("positive", "negative") or len(g) < 30:
            continue
        hit = float(g["cap_up"].mean()) if sent == "positive" \
            else 1 - float(g["cap_up"].mean())
        base = neutral_up if sent == "positive" else 1 - neutral_up
        rec = {"exp": "S0_DIRT", "variant": f"{et}_{sent}", "n": int(len(g)),
               "hit_capturable": round(hit, 3),
               "edge_pp": round(100 * (hit - base), 1),
               "split_cutoff": str(cutoff), "seed": SEED,
               "run_at": datetime.now(timezone.utc).isoformat()}
        _ledger_write(rec); rows.append(rec)
        logger.info(f"[S0_DIRT] {et:22s} {sent:8s} n={len(g):4d} "
                    f"hit={hit:.3f} edge={100 * (hit - base):+.1f}pp")

    # T3: champion-family refit. (a) old close-to-close label with PIT-fixed
    # features -> isolates the look-ahead inflation; (b) capturable label ->
    # the honest trading-relevant baseline for the node program.
    d2 = d.copy()
    orig_et = mc.event_type
    try:
        mc.event_type = event_type_v2
        for label_name, series in (
                ("cc_pitfix", None),          # featurize computes cc label
                ("capturable", d2["material_capturable"])):
            X, y, dff = mc.featurize(d2)
            if series is not None:
                y = series.loc[X.index]
            tr = dff["created_at"] <= cutoff
            te = dff["created_at"] > cutoff
            p = (_lr_c01(dff, X, y, tr, te)
                 + _svdhgb_full(dff, X, y, tr, te)) / 2
            m = evaluate(p, y[te].values, float(y[te].mean()))
            rec = {"exp": "S0_GATE", "variant": f"blend_{label_name}",
                   "n_test": int(te.sum()),
                   "base_rate_test": round(float(y[te].mean()), 4),
                   **m, "split_cutoff": str(cutoff), "seed": SEED,
                   "run_at": datetime.now(timezone.utc).isoformat()}
            _ledger_write(rec); rows.append(rec)
            logger.info(f"[S0_GATE] {label_name}: auc={m['auc']} "
                        f"brier={m['brier']} top_obs={m['top_decile_obs']}")
    finally:
        mc.event_type = orig_et

    pit_cov = float(d["sue_found"].fillna(0).mean())
    logger.info(f"[S0] PIT SUE coverage (announced<=analysis): {pit_cov:.3f}")
    _ledger_write({"exp": "S0_PIT", "variant": "sue_coverage",
                   "value": round(pit_cov, 4), "seed": SEED,
                   "run_at": datetime.now(timezone.utc).isoformat()})
    return rows


V2_EXPS = {"D0", "D1", "D2", "EX"}
V3_EXPS = {"S0"}
EXPERIMENTS = {"E0": exp_e0, "E1": exp_e1, "E2": exp_e2, "E3": exp_e3,
               "E4": exp_e4, "E5": exp_e5, "E6": exp_e6, "E7": exp_e7,
               "E8": exp_e8, "D0": exp_d0, "D1": exp_d1, "D2": exp_d2,
               "EX": exp_ex, "S0": exp_s0}


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true",
                    help="(Re)build the dataset cache from the DB first.")
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--exp", default="",
                    help="Comma-separated experiment ids, or 'all'.")
    args = ap.parse_args()

    wanted = (list(EXPERIMENTS) if args.exp == "all"
              else [e.strip().upper() for e in args.exp.split(",") if e.strip()])
    need_v2 = any(e in V2_EXPS for e in wanted)
    need_v3 = any(e in V3_EXPS for e in wanted)
    if args.build:
        if need_v3:
            df, _ = asyncio.run(build_cache_v3(args.days))
        elif need_v2:
            df = asyncio.run(build_cache_v2(args.days))
        else:
            df = asyncio.run(build_cache(args.days))
    else:
        import pandas as pd
        suffix = "_v3" if need_v3 else ("_v2" if need_v2 else "")
        df = pd.read_pickle(f"{LAB_DIR}/dataset_{args.days}d{suffix}.pkl.gz",
                            compression="gzip")
    logger.info(f"dataset: {len(df)} rows, "
                f"{df['created_at'].min()} -> {df['created_at'].max()}")
    all_results = []
    for e in wanted:
        all_results += EXPERIMENTS[e](df)

    if all_results:
        best = max(all_results, key=lambda r: r.get("auc") or -1)
        print(json.dumps({"n_variants": len(all_results), "best": best},
                         indent=2, default=str))


if __name__ == "__main__":
    main()
