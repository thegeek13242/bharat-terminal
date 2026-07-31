"""C0 validation replay — pre-registered (MANAGEMENT_COMMENTARY_2026-07-31.md §C0).

Question: do the deterministic commentary-veto rules (papertrade/commentary.py)
discriminate wrong-direction results classifications from right-direction ones?

SHIP BAR (pre-registered BEFORE this harness ran): veto rate on the
wrong-direction events must be >= 3x the veto rate on right-direction events,
on the 120d mag>=3 EARNINGS filing population (the '855-event frame',
exports side: commentary_residual SQL). Otherwise C0 stays in shadow.

Population = the failure-map frame (one row per symbol x reaction-day, earliest
filing, hop-0 mag>=3 positive/negative EARNINGS impacts from exchange filings,
priced close-to-close). Classes at +/-3%:
  wrong  = (negative & ret >= +3%) or (positive & ret <= -3%)   [n~136]
  right  = (negative & ret <= -3%) or (positive & ret >= +3%)   [n~185]
  flat   = the rest                                             [exposure read]

Extraction input mirrors live C0 exactly: the triggering filing's own text +
same-symbol filings from the prior 2h (backward-only — point-in-time clean).
LLM-leakage note: gemini-2.5-flash's training predates these events (Apr-Jul
2026), so outcome recall is not a contamination channel here; company-identity
recognition remains and is disclosed in the report.

Usage (research-runner container, repo + exports mounted):
  python -m bharat_terminal.research.commentary_replay --estimate
  python -m bharat_terminal.research.commentary_replay --run [--max-n 900]
Deterministic: seed 42 bootstrap, ORDER BY symbol, reaction_day. Results to
$MATERIALITY_LAB_DIR/commentary_replay_<rundate>.json + ledger.jsonl exp='C0'.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import date

import numpy as np

from bharat_terminal.papertrade import commentary as c0

LAB_DIR = os.getenv("MATERIALITY_LAB_DIR", "exports/materiality_lab")
DATABASE_URL = os.getenv("DATABASE_URL", "")
CONCURRENCY = int(os.getenv("C0_REPLAY_CONCURRENCY", "6"))
SEED = 42
WRONG_RIGHT_THRESHOLD_PCT = 3.0
SHIP_RATIO_BAR = 3.0
# gemini-2.5-flash Vertex list pricing (USD/1M tokens) for the estimate.
_IN_PER_M, _OUT_PER_M = 0.30, 2.50

# The failure-map frame (research sweep 2026-07-31), with headline/body joined
# for extraction. One row per (symbol, reaction-day), earliest filing = the t0
# classification. Same conventions as the quantification that set the bar.
_FRAME_SQL = """
WITH base AS (
  SELECT ir.id AS report_id, ir.news_id, n.source, n.timestamp_utc,
         n.headline, n.body,
         (n.timestamp_utc AT TIME ZONE 'Asia/Kolkata') AS ts_ist,
         ci->>'symbol' AS symbol,
         ci->>'sentiment' AS sentiment,
         (ci->>'magnitude')::int AS magnitude
  FROM impact_reports ir
  JOIN news_items n ON n.id = ir.news_id
  CROSS JOIN LATERAL jsonb_array_elements(ir.company_impacts) ci
  WHERE ir.relevant
    AND ir.macro_theme = 'EARNINGS'
    AND n.source IN ('NSE_FILINGS','BSE_FILINGS')
    AND ir.created_at > now() - interval '120 days'
    AND (ci->>'hop_distance')::int = 0
    AND (ci->>'magnitude')::int >= 3
    AND ci->>'sentiment' IN ('positive','negative')
),
timed AS (
  SELECT *,
         CASE WHEN ts_ist::time < time '15:30' THEN ts_ist::date
              ELSE ts_ist::date + 1 END AS candidate_day
  FROM base
),
dedup AS (
  SELECT DISTINCT ON (symbol, candidate_day) *
  FROM timed
  ORDER BY symbol, candidate_day, timestamp_utc ASC
),
priced AS (
  SELECT d.*,
         rx.date  AS reaction_date, rx.close AS reaction_close,
         bs.close AS base_close
  FROM dedup d
  CROSS JOIN LATERAL (
    SELECT p.date, p.close FROM price_points p
    WHERE p.symbol = d.symbol AND p.date >= d.candidate_day
    ORDER BY p.date ASC LIMIT 1
  ) rx
  CROSS JOIN LATERAL (
    SELECT p.date, p.close FROM price_points p
    WHERE p.symbol = d.symbol AND p.date < rx.date
    ORDER BY p.date DESC LIMIT 1
  ) bs
  WHERE rx.date <= d.candidate_day + 7
    AND bs.date >= d.candidate_day - 7
    AND bs.close > 0
)
SELECT report_id, news_id, symbol, sentiment, magnitude, headline, body,
       timestamp_utc, reaction_date,
       (reaction_close/base_close - 1.0)*100.0 AS ret_pct
FROM priced
ORDER BY symbol, candidate_day
"""


def _klass(sentiment: str, ret_pct: float) -> str:
    t = WRONG_RIGHT_THRESHOLD_PCT
    if sentiment == "negative":
        return "wrong" if ret_pct >= t else ("right" if ret_pct <= -t else "flat")
    return "wrong" if ret_pct <= -t else ("right" if ret_pct >= t else "flat")


async def _load_frame(pool, max_n: int | None):
    rows = [dict(r) for r in await pool.fetch(_FRAME_SQL)]
    for r in rows:
        r["klass"] = _klass(r["sentiment"], float(r["ret_pct"]))
        # Live veto side for this classification: negatives are short
        # candidates, positives long candidates.
        r["side"] = "SHORT" if r["sentiment"] == "negative" else "LONG"
    if max_n and len(rows) > max_n:
        # Deterministic stratified thinning: keep ALL wrong+right, thin flats.
        keep = [r for r in rows if r["klass"] != "flat"]
        flats = [r for r in rows if r["klass"] == "flat"]
        rng = np.random.default_rng(SEED)
        idx = rng.choice(len(flats), size=max(0, max_n - len(keep)),
                         replace=False)
        keep += [flats[i] for i in sorted(idx)]
        rows = keep
    return rows


def _estimate_cost(rows) -> dict:
    in_tok = sum(min(len(r["body"] or ""), 60000) / 4.0 + 2500 for r in rows)
    out_tok = 400 * len(rows)
    usd = in_tok / 1e6 * _IN_PER_M + out_tok / 1e6 * _OUT_PER_M
    return {"events": len(rows), "est_input_tokens": int(in_tok),
            "est_output_tokens": int(out_tok), "est_usd": round(usd, 2)}


async def _extract_one(pool, sem, r) -> None:
    async with sem:
        siblings = await c0.fetch_siblings(
            pool, r["symbol"], r["timestamp_utc"], r["news_id"])
        ext = await c0.extract(r["symbol"], r["headline"] or "",
                               r["body"] or "", siblings)
        veto, rule = c0.veto_decision(ext, r["side"])
        r["extraction_ok"] = ext is not None
        r["veto"], r["rule"] = veto, rule
        if ext:
            r["mgmt_quote_present"] = ext.get("mgmt_quote_present")


def _rate(rows, klass) -> tuple[int, int, float]:
    sub = [r for r in rows if r["klass"] == klass and r.get("extraction_ok")]
    v = sum(1 for r in sub if r["veto"])
    return v, len(sub), (v / len(sub) if sub else 0.0)


def _bootstrap_ratio(rows, n_boot=10_000) -> dict:
    """Seed-42 bootstrap CI of veto_rate(wrong)/veto_rate(right)."""
    rng = np.random.default_rng(SEED)
    wrong = np.array([r["veto"] for r in rows
                      if r["klass"] == "wrong" and r.get("extraction_ok")], bool)
    right = np.array([r["veto"] for r in rows
                      if r["klass"] == "right" and r.get("extraction_ok")], bool)
    if not len(wrong) or not len(right):
        return {"ci_lo": None, "ci_hi": None}
    ratios = []
    for _ in range(n_boot):
        w = wrong[rng.integers(0, len(wrong), len(wrong))].mean()
        rr = right[rng.integers(0, len(right), len(right))].mean()
        ratios.append(w / rr if rr > 0 else np.inf)
    ratios = np.array(ratios)
    return {"ci_lo": round(float(np.quantile(ratios, 0.025)), 2),
            "ci_hi": (None if np.isinf(np.quantile(ratios, 0.975))
                      else round(float(np.quantile(ratios, 0.975)), 2)),
            "inf_share": round(float(np.isinf(ratios).mean()), 3)}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--max-n", type=int, default=None)
    args = ap.parse_args()

    import asyncpg
    dsn = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    rows = await _load_frame(pool, args.max_n)
    by = lambda k: sum(1 for r in rows if r["klass"] == k)
    print(f"frame: {len(rows)} events — wrong={by('wrong')} "
          f"right={by('right')} flat={by('flat')}")
    est = _estimate_cost(rows)
    print(f"estimate: {json.dumps(est)}")
    if args.estimate or not args.run:
        return

    sem = asyncio.Semaphore(CONCURRENCY)
    await asyncio.gather(*(_extract_one(pool, sem, r) for r in rows))
    ok = sum(1 for r in rows if r.get("extraction_ok"))
    print(f"extractions ok: {ok}/{len(rows)}")

    result = {"run_date": str(date.today()), "exp": "C0", "seed": SEED,
              "model": c0.COMMENTARY_MODEL, "estimate": est,
              "extraction_ok": ok, "n": len(rows),
              "threshold_pct": WRONG_RIGHT_THRESHOLD_PCT,
              "ship_bar_ratio": SHIP_RATIO_BAR, "cells": {}, "rules": {}}
    for k in ("wrong", "right", "flat"):
        v, n, rate = _rate(rows, k)
        result["cells"][k] = {"vetoed": v, "n": n, "rate": round(rate, 4)}
    wr, rr = result["cells"]["wrong"]["rate"], result["cells"]["right"]["rate"]
    result["ratio"] = round(wr / rr, 2) if rr > 0 else None
    result["ratio_bootstrap"] = _bootstrap_ratio(rows)
    result["pass"] = bool(rr == 0 and wr > 0) or bool(
        rr > 0 and wr / rr >= SHIP_RATIO_BAR)
    # Per-side and per-rule breakdowns (secondary, exploratory).
    for side in ("SHORT", "LONG"):
        sub = [r for r in rows if r["side"] == side]
        result[f"side_{side}"] = {
            k: _rate(sub, k)[:2] for k in ("wrong", "right", "flat")}
    for r in rows:
        if r.get("veto"):
            result["rules"].setdefault(r["rule"], {"wrong": 0, "right": 0,
                                                   "flat": 0})
            result["rules"][r["rule"]][r["klass"]] += 1
    result["vetoed_examples"] = [
        {"symbol": r["symbol"], "klass": r["klass"], "rule": r["rule"],
         "sentiment": r["sentiment"], "mag": r["magnitude"],
         "ret_pct": round(float(r["ret_pct"]), 2)}
        for r in rows if r.get("veto")][:60]

    out = os.path.join(LAB_DIR, f"commentary_replay_{date.today()}.json")
    with open(out, "w") as f:
        json.dump(result, f, indent=1, default=str)
    with open(os.path.join(LAB_DIR, "ledger.jsonl"), "a") as f:
        f.write(json.dumps({k: v for k, v in result.items()
                            if k != "vetoed_examples"}, default=str) + "\n")
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ("vetoed_examples",)}, indent=1, default=str))
    print(f"written: {out}")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
