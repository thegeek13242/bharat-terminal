"""Signal rules — turn an ImpactReport into auditable paper-trade intents.

WHY this is a gate cascade with provenance, not a scoring formula: the
2026-07-04 signal audit showed LLM direction is a coin flip, so every strategy
here is grounded in something that survived out-of-time validation (PEAD SUE
deciles: +2.11% @20d, t=4.7 / 51 cohorts) or is an explicit hypothesis being
paper-tested with money-free stakes (headwind_short). Each gate writes its
verdict into gate_flags so a losing trade can be traced to the exact decision
inputs later — the audit-trail discipline live trading will require.

Materiality gate stance: PAPER runs FAIL-OPEN when the artifact is missing or
featurization breaks (trade proceeds, provenance.gate_fallback=true, WARNING
logged) so pilot data keeps flowing. LIVE trading must flip this to
FAIL-CLOSED: no materiality score → no order. Do not copy this default.
"""
from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from bharat_terminal.types import CompanyImpact, ImpactReport

logger = logging.getLogger(__name__)

FILING_SOURCES = ("NSE_FILINGS", "BSE_FILINGS")

PAPER_MAX_EVENT_AGE_S = float(os.getenv("PAPER_MAX_EVENT_AGE_S", "900"))
PAPER_MATERIALITY_MIN = float(os.getenv("PAPER_MATERIALITY_MIN", "0.10"))
MATERIALITY_ARTIFACT = os.getenv(
    "MATERIALITY_ARTIFACT", "exports/materiality_lab/materiality_v4_1_tax22.joblib")
ENABLE_HEADWIND_SHORT = os.getenv("PAPER_ENABLE_HEADWIND_SHORT", "1") == "1"
ENABLE_PEAD_LONG = os.getenv("PAPER_ENABLE_PEAD_LONG", "1") == "1"
# S0 (2026-07-29, TRADE-SPACE relabel — supersedes D0's close-to-close
# read): medium-band negatives' +19.3pp edge was entirely the untradeable
# overnight gap; on the capturable component (next-open fill) it is
# +1.6pp ≈ zero, while high-band survives at +14.5pp. Shorts take
# magnitude >= 4 only.
HEADWIND_MIN_MAG = int(os.getenv("PAPER_HEADWIND_MIN_MAG", "4"))
# D0T: order_win is the ONLY positive class with real directional edge
# (64.1% hit, +11.5pp over drift, n=237). Long the event, hold ~5 sessions.
ENABLE_ORDERWIN_LONG = os.getenv("PAPER_ENABLE_ORDERWIN_LONG", "1") == "1"
# OFF by default: D0 confirmed over 400 days that positive classifications
# in aggregate sit AT/BELOW market drift (-1 to -4.5pp per band). The branch
# exists so flipping the env var is the whole experiment.
ENABLE_TAILWIND_LONG = os.getenv("PAPER_ENABLE_TAILWIND_LONG", "0") == "1"

# SUE-percentile gate for pead_long: threshold = P90 of trailing-90d
# sue_composite, recomputed at most once an hour (it moves slowly).
SUE_PERCENTILE = 0.90
SUE_LOOKBACK_DAYS = 90
SUE_THRESHOLD_CACHE_S = 3600
# TF-IDF text cap — mirrors research/materiality_lab.py BODY_CHARS (the
# artifact's vectorizer was fit on left(body, 8000)).
_TEXT_BODY_CHARS = 8000

# Event taxonomy: prefer the lab's 22-bucket v2 — that is what the v4.1
# artifact was fit with. Fall back to the calibrator's 16 buckets; the
# reindex-to-feature_columns step keeps either alignment safe (unknown
# ev_* dummies simply fill as 0).
try:
    from bharat_terminal.research.materiality_lab import event_type_v2 as _event_type
except Exception:                                            # pragma: no cover
    from bharat_terminal.research.materiality_calibrator import event_type as _event_type


def _aware_utc(dt: datetime) -> datetime:
    """Serialized timestamps sometimes round-trip tz-naive; they are UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class SignalIntent:
    """One would-be order, with everything needed to record it honestly."""
    strategy: str                    # headwind_short | pead_long | tailwind_long
    symbol: str
    side: str                        # LONG | SHORT
    instrument: str                  # CASH | FUT
    impact_id: str
    news_id: str
    provenance: dict = field(default_factory=dict)
    rejected_reason: Optional[str] = None   # e.g. 'stale_event' → REJECTED row


class MaterialityGate:
    """v4.1 materiality blend: p = (lr + hgb)/2, hgb input = [X, svd(vec(text))].

    Serve-time featurization mirrors research/materiality_calibrator.featurize()
    (post-v3 columns: magnitude, confidence, sue_abs, has_sue, runup_sigma +
    sent_/hz_/ev_ dummies), built as a single row and reindexed EXACTLY to the
    artifact's feature_columns with missing columns filled 0.
    """

    def __init__(self, path: str):
        self.path = path
        self._art = None
        try:
            import joblib
            art = joblib.load(path)
            missing = {"lr", "vec", "svd", "hgb", "feature_columns"} - set(art)
            if missing:
                raise ValueError(f"artifact missing keys: {sorted(missing)}")
            self._art = art
            logger.info(
                f"[GATE] materiality artifact loaded: {path} "
                f"fit_through={art.get('fit_through')} taxonomy={art.get('taxonomy')}")
        except Exception as e:
            # PAPER: fail-open (see module docstring). LIVE: must fail closed.
            logger.warning(
                f"[GATE] materiality artifact unavailable ({path}): {e} — "
                f"headwind_short will FAIL-OPEN with provenance.gate_fallback=true")

    @property
    def loaded(self) -> bool:
        return self._art is not None

    def describe(self) -> Optional[dict]:
        if self._art is None:
            return None
        return {"path": self.path,
                "fit_through": str(self._art.get("fit_through"))}

    def score(self, features: dict, text: str) -> float:
        import numpy as np
        import pandas as pd
        art = self._art
        X = (pd.DataFrame([features])
             .reindex(columns=art["feature_columns"], fill_value=0.0)
             .astype(float).fillna(0.0))
        p_lr = float(art["lr"].predict_proba(X.values)[:, 1][0])
        emb = art["svd"].transform(art["vec"].transform([text]))
        p_hgb = float(art["hgb"].predict_proba(
            np.hstack([X.values, emb]))[:, 1][0])
        return (p_lr + p_hgb) / 2.0


# ── single-event feature SQL (mirrors calibrator's _PX_SQL / SUE lateral) ────

_VOL_RUNUP_SQL = """
SELECT date, vol60, ret5_pre FROM (
    SELECT date,
           stddev_samp(ret) OVER w AS vol60,
           ret5_pre
    FROM (
        SELECT date,
               close / nullif(lag(close) OVER (ORDER BY date), 0) - 1 AS ret,
               close / nullif(lag(close, 5) OVER (ORDER BY date), 0) - 1 AS ret5_pre
        FROM price_points
        WHERE symbol = $1 AND date >= CURRENT_DATE - 120
    ) t
    WINDOW w AS (ORDER BY date ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING)
) x
ORDER BY date DESC
LIMIT 1
"""

_SUE_ABS_SQL = """
SELECT abs(sue_composite) AS sue_abs
FROM earnings_surprises
WHERE symbol = $1 AND sue_composite IS NOT NULL
  AND announced_at BETWEEN $2::timestamptz - interval '3 days'
                       AND $2::timestamptz + interval '1 day'
ORDER BY abs(extract(epoch FROM announced_at - $2::timestamptz)) ASC
LIMIT 1
"""

_PEAD_SUE_SQL = """
SELECT sue_composite
FROM earnings_surprises
WHERE symbol = $1 AND sue_composite IS NOT NULL
  AND announced_at BETWEEN $2::timestamptz - interval '2 days'
                       AND $2::timestamptz + interval '2 days'
ORDER BY abs(extract(epoch FROM announced_at - $2::timestamptz)) ASC
LIMIT 1
"""

_SUE_THRESHOLD_SQL = f"""
SELECT percentile_cont({SUE_PERCENTILE})
       WITHIN GROUP (ORDER BY sue_composite) AS thr
FROM earnings_surprises
WHERE sue_composite IS NOT NULL
  AND announced_at >= now() - interval '{SUE_LOOKBACK_DAYS} days'
"""

# Concentration guard: one live position per (symbol, strategy). Companies
# file several negative documents in one evening (VSTIND filed two on
# 2026-07-28); each is a distinct impact_id so idempotency alone lets
# exposure stack to 2x+ notional on one name. Second signals become
# auditable REJECTED rows, not silent skips.
_LIVE_POS_SQL = """
SELECT 1 FROM paper_trades
WHERE symbol = $1 AND strategy = $2 AND status IN ('PENDING', 'OPEN')
LIMIT 1
"""


class RulesEngine:
    """Stateful evaluator: holds the DB pool, the materiality artifact and a
    1h-cached SUE threshold. `fno_supplier` is an async () -> set[str]."""

    def __init__(self, pool, fno_supplier):
        self._pool = pool
        self._fno_supplier = fno_supplier
        self.gate = MaterialityGate(MATERIALITY_ARTIFACT)
        self._sue_thr: Optional[float] = None
        self._sue_thr_at: float = 0.0

    # ── public entrypoint ────────────────────────────────────────────────────

    async def evaluate_report(self, report: ImpactReport) -> list[SignalIntent]:
        """Gate cascade → intents. Stale events yield REJECTED-marked intents
        (auditable rows) rather than silence; sub-threshold gates log and skip.
        """
        intents: list[SignalIntent] = []
        ni = report.news_item
        headline = ni.headline or ""

        if not report.relevant:
            logger.info(f"[GATED] not relevant: {headline[:80]}")
            return intents
        if ni.source not in FILING_SOURCES:
            logger.info(f"[GATED] non-filing source {ni.source}: {headline[:80]}")
            return intents

        event_ts = _aware_utc(ni.timestamp_utc)
        now = datetime.now(timezone.utc)
        age_s = (now - event_ts).total_seconds()
        fresh = age_s <= PAPER_MAX_EVENT_AGE_S
        latency_ms = int(age_s * 1000)
        etype = _event_type(headline)

        for ci in report.company_impacts:
            if ci.hop_distance != 0:
                logger.info(
                    f"[GATED] hop={ci.hop_distance} skipped: {ci.symbol}")
                continue

            base_prov = {
                "model_artifact": self.gate.describe(),
                "materiality_p": None,
                "magnitude": ci.magnitude,
                "sentiment": ci.sentiment,
                "event_type": etype,
                "hop": ci.hop_distance,
                "decision_latency_ms": latency_ms,
            }
            base_flags = {"relevant": True, "source_ok": True,
                          "fresh": fresh, "hop0": True,
                          "event_age_s": round(age_s, 1)}

            intents += await self._headwind_short(
                report, ci, etype, fresh, base_prov, base_flags)
            intents += await self._pead_long(
                report, ci, etype, fresh, event_ts, base_prov, base_flags)
            intents += await self._orderwin_long(
                report, ci, etype, fresh, base_prov, base_flags)
            intents += await self._tailwind_long(
                report, ci, etype, fresh, base_prov, base_flags)

        return intents

    # ── strategies ───────────────────────────────────────────────────────────

    async def _headwind_short(self, report, ci: CompanyImpact, etype, fresh,
                              base_prov, base_flags) -> list[SignalIntent]:
        if not ENABLE_HEADWIND_SHORT:
            return []
        if ci.sentiment != "negative" or ci.magnitude < HEADWIND_MIN_MAG:
            return []
        flags = dict(base_flags, strategy_enabled=True)

        if not fresh:
            logger.info(
                f"[REJECT] headwind_short {ci.symbol}: stale_event "
                f"(age {flags['event_age_s']}s > {PAPER_MAX_EVENT_AGE_S}s)")
            return [SignalIntent(
                "headwind_short", ci.symbol, "SHORT", "CASH",
                report.id, report.news_id,
                provenance={**base_prov, "gate_flags": flags},
                rejected_reason="stale_event")]

        dup = await self._dup_reject("headwind_short", "SHORT", "CASH",
                                     report, ci, base_prov, flags)
        if dup is not None:
            return dup

        p, fallback = await self._materiality_p(report, ci, etype)
        flags["materiality_p"] = p
        flags["materiality_pass"] = (
            None if p is None else bool(p >= PAPER_MATERIALITY_MIN))
        if p is not None and p < PAPER_MATERIALITY_MIN:
            logger.info(
                f"[GATED] headwind_short {ci.symbol}: materiality p={p:.3f} "
                f"< {PAPER_MATERIALITY_MIN}")
            return []

        fno = await self._fno_supplier()
        in_fno = ci.symbol.upper() in fno
        flags["fno"] = in_fno
        prov = {**base_prov, "materiality_p": p, "gate_flags": flags}
        if fallback:
            prov["gate_fallback"] = True
        instrument = "FUT" if in_fno else "CASH"
        logger.info(
            f"[SIGNAL] headwind_short SHORT {ci.symbol} {instrument} "
            f"mag={ci.magnitude} p={'n/a' if p is None else f'{p:.3f}'}"
            f"{' (gate fallback)' if fallback else ''}")
        return [SignalIntent("headwind_short", ci.symbol, "SHORT", instrument,
                             report.id, report.news_id, provenance=prov)]

    async def _pead_long(self, report, ci: CompanyImpact, etype, fresh,
                         event_ts, base_prov, base_flags) -> list[SignalIntent]:
        if not ENABLE_PEAD_LONG:
            return []
        # Results filing only — same keyword bucket the calibrator uses.
        if etype != "results":
            return []
        flags = dict(base_flags, strategy_enabled=True, results_filing=True)

        if not fresh:
            logger.info(f"[REJECT] pead_long {ci.symbol}: stale_event")
            return [SignalIntent(
                "pead_long", ci.symbol, "LONG", "CASH",
                report.id, report.news_id,
                provenance={**base_prov, "gate_flags": flags},
                rejected_reason="stale_event")]

        dup = await self._dup_reject("pead_long", "LONG", "CASH",
                                     report, ci, base_prov, flags)
        if dup is not None:
            return dup

        sue = await self._fetch_pead_sue(ci.symbol, event_ts)
        thr = await self._sue_threshold()
        flags["sue_found"] = sue is not None
        flags["sue_composite"] = sue
        flags["sue_threshold"] = thr
        if sue is None:
            logger.info(f"[GATED] pead_long {ci.symbol}: no earnings_surprises "
                        f"row within ±2d of filing")
            return []
        if thr is None:
            logger.info(f"[GATED] pead_long {ci.symbol}: SUE threshold "
                        f"unavailable (trailing window empty)")
            return []
        if sue < thr:
            logger.info(f"[GATED] pead_long {ci.symbol}: sue={sue:.3f} < "
                        f"P{int(SUE_PERCENTILE * 100)}={thr:.3f}")
            return []

        flags["sue_pass"] = True
        prov = {**base_prov, "gate_flags": flags,
                "sue_composite": sue, "sue_threshold": thr}
        logger.info(f"[SIGNAL] pead_long LONG {ci.symbol} CASH "
                    f"sue={sue:.3f} >= thr={thr:.3f}")
        return [SignalIntent("pead_long", ci.symbol, "LONG", "CASH",
                             report.id, report.news_id, provenance=prov)]

    async def _orderwin_long(self, report, ci: CompanyImpact, etype, fresh,
                             base_prov, base_flags) -> list[SignalIntent]:
        """D0T (2026-07-28): order_win + positive = the one long-side class
        with real drift-adjusted edge (64.1% hit, +11.5pp, n=237). The
        validated stat is unconditional on magnitude/materiality, so no
        extra gates — but materiality_p is RECORDED so the paper pilot can
        tell us whether gating would have helped."""
        if not ENABLE_ORDERWIN_LONG:
            return []
        if ci.sentiment != "positive" or etype != "order_win":
            return []
        flags = dict(base_flags, strategy_enabled=True)

        if not fresh:
            logger.info(f"[REJECT] orderwin_long {ci.symbol}: stale_event")
            return [SignalIntent(
                "orderwin_long", ci.symbol, "LONG", "CASH",
                report.id, report.news_id,
                provenance={**base_prov, "gate_flags": flags},
                rejected_reason="stale_event")]

        dup = await self._dup_reject("orderwin_long", "LONG", "CASH",
                                     report, ci, base_prov, flags)
        if dup is not None:
            return dup

        p, fallback = await self._materiality_p(report, ci, etype)
        flags["materiality_p"] = p       # measured, deliberately NOT gated
        prov = {**base_prov, "materiality_p": p, "gate_flags": flags}
        if fallback:
            prov["gate_fallback"] = True
        logger.info(
            f"[SIGNAL] orderwin_long LONG {ci.symbol} CASH mag={ci.magnitude} "
            f"p={'n/a' if p is None else f'{p:.3f}'} (recorded, ungated)")
        return [SignalIntent("orderwin_long", ci.symbol, "LONG", "CASH",
                             report.id, report.news_id, provenance=prov)]

    async def _tailwind_long(self, report, ci: CompanyImpact, etype, fresh,
                             base_prov, base_flags) -> list[SignalIntent]:
        # Default OFF (PAPER_ENABLE_TAILWIND_LONG=0): the direction audit
        # found no long-side edge on positive headlines. Branch kept complete
        # so enabling it is a config change, not a code change.
        if not ENABLE_TAILWIND_LONG:
            return []
        if ci.sentiment != "positive" or ci.magnitude < 4:
            return []
        flags = dict(base_flags, strategy_enabled=True)

        if not fresh:
            logger.info(f"[REJECT] tailwind_long {ci.symbol}: stale_event")
            return [SignalIntent(
                "tailwind_long", ci.symbol, "LONG", "CASH",
                report.id, report.news_id,
                provenance={**base_prov, "gate_flags": flags},
                rejected_reason="stale_event")]

        dup = await self._dup_reject("tailwind_long", "LONG", "CASH",
                                     report, ci, base_prov, flags)
        if dup is not None:
            return dup

        fno = await self._fno_supplier()
        in_fno = ci.symbol.upper() in fno
        flags["fno"] = in_fno
        instrument = "FUT" if in_fno else "CASH"
        prov = {**base_prov, "gate_flags": flags}
        logger.info(f"[SIGNAL] tailwind_long LONG {ci.symbol} {instrument} "
                    f"mag={ci.magnitude}")
        return [SignalIntent("tailwind_long", ci.symbol, "LONG", instrument,
                             report.id, report.news_id, provenance=prov)]

    async def _dup_reject(self, strategy: str, side: str, instrument: str,
                          report, ci: CompanyImpact, base_prov,
                          flags) -> Optional[list[SignalIntent]]:
        """None when no live position exists; else the REJECTED intent."""
        row = await self._pool.fetchrow(_LIVE_POS_SQL, ci.symbol, strategy)
        if row is None:
            return None
        flags["duplicate_position"] = True
        logger.info(f"[REJECT] {strategy} {ci.symbol}: duplicate_position "
                    f"(a PENDING/OPEN {strategy} position already exists)")
        return [SignalIntent(
            strategy, ci.symbol, side, instrument,
            report.id, report.news_id,
            provenance={**base_prov, "gate_flags": flags},
            rejected_reason="duplicate_position")]

    # ── materiality features ─────────────────────────────────────────────────

    async def _materiality_p(self, report, ci: CompanyImpact,
                             etype: str) -> tuple[Optional[float], bool]:
        """Returns (p, gate_fallback). PAPER stance: any failure → (None, True)
        i.e. FAIL-OPEN with provenance. Live trading must flip to fail-closed.
        """
        if not self.gate.loaded:
            return None, True
        try:
            vol60, ret5_pre = await self._fetch_vol_runup(ci.symbol)
            sue_abs = await self._fetch_sue_abs(
                ci.symbol, _aware_utc(report.created_at))
            runup_sigma = 0.0
            if vol60 and vol60 > 0 and ret5_pre is not None:
                runup_sigma = min(abs(ret5_pre) / (vol60 * math.sqrt(5)), 10.0)
            features = {
                "magnitude": float(min(max(ci.magnitude, 1), 5)),
                "confidence": float(report.confidence or 0.0),
                "sue_abs": min(float(sue_abs), 10.0) if sue_abs is not None else 0.0,
                "has_sue": 1.0 if sue_abs is not None else 0.0,
                "runup_sigma": float(runup_sigma),
                f"sent_{(ci.sentiment or '').lower()}": 1.0,
                f"hz_{(ci.time_horizon or '').lower()}": 1.0,
                f"ev_{etype}": 1.0,
            }
            ni = report.news_item
            text = f"{ni.headline or ''} {(ni.body or '')[:_TEXT_BODY_CHARS]}"
            return self.gate.score(features, text), False
        except Exception as e:
            logger.warning(
                f"[GATE] materiality featurize/score failed for {ci.symbol}: "
                f"{e} — FAIL-OPEN (paper only; live must fail closed)")
            return None, True

    # ── DB lookups ───────────────────────────────────────────────────────────

    async def _fetch_vol_runup(self, symbol: str):
        row = await self._pool.fetchrow(_VOL_RUNUP_SQL, symbol.upper())
        if row is None:
            return None, None
        return (float(row["vol60"]) if row["vol60"] is not None else None,
                float(row["ret5_pre"]) if row["ret5_pre"] is not None else None)

    async def _fetch_sue_abs(self, symbol: str, around: datetime):
        row = await self._pool.fetchrow(_SUE_ABS_SQL, symbol.upper(), around)
        return float(row["sue_abs"]) if row else None

    async def _fetch_pead_sue(self, symbol: str, around: datetime):
        row = await self._pool.fetchrow(_PEAD_SUE_SQL, symbol.upper(), around)
        return float(row["sue_composite"]) if row else None

    async def _sue_threshold(self) -> Optional[float]:
        """P90 of trailing-90d sue_composite, cached for 1h."""
        now = time.monotonic()
        if self._sue_thr is not None and (now - self._sue_thr_at) < SUE_THRESHOLD_CACHE_S:
            return self._sue_thr
        try:
            row = await self._pool.fetchrow(_SUE_THRESHOLD_SQL)
            thr = float(row["thr"]) if row and row["thr"] is not None else None
        except Exception as e:
            logger.warning(f"[PEAD] SUE threshold query failed: {e}")
            return self._sue_thr        # keep last known value if any
        if thr is not None:
            self._sue_thr, self._sue_thr_at = thr, now
            logger.info(f"[PEAD] SUE P{int(SUE_PERCENTILE * 100)} threshold "
                        f"refreshed: {thr:.3f}")
        return thr
