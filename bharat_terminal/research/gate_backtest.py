"""Retro-backtest of the frozen v1 signal gates over historical outcomes.

READ-ONLY and zero-LLM: replays analysis/filing_policy + analysis/materiality
over every already-evaluated exchange-filing signal in signal_outcomes and
reports what the gates WOULD have kept/suppressed, against realized returns.

This produces the go/no-go numbers (founder decision 2026-07-04): the gate
set deploys only if gated Materiality Precision beats ungated.

Honesty notes baked into the output:
- IN-SAMPLE: the v1 rules were designed partly on Jul 2-3 data that is inside
  this history.
- Historical NSE rows carry only coarse categories, so classification runs on
  the headline-subject fallback — weaker than the live fine-grained capture.
- Arrival-price effects are not measurable retroactively (entry = next close).
- Volatility control: each category's MP is shown next to the unconditional
  same-symbol materiality base rate (share of ALL recent trading days with a
  |1d move| >= 2%). A category only carries information if MP clearly beats
  its control — high raw MP on volatile microcaps is volatility, not signal.

Metric (frozen v1_2026_07):
  material := |ret_1d| >= 2.0  OR  |ret_5d| >= 3.0
  MP       := P(material | signal emitted), Wilson 95% CI
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import asyncpg

from bharat_terminal.analysis.filing_policy import (
    ALWAYS_ELIGIBLE, DENY, GATE_VERSION, PASS_THROUGH, RATIO_REQUIRED,
    classify, horizon_route,
)
from bharat_terminal.analysis.materiality import (
    MATERIALITY_RATIO_MIN, extract_value_cr, materiality_ratio, ratio_passes,
)
from bharat_terminal.analysis.conviction import compute_conviction

MATERIAL_1D = 2.0
MATERIAL_5D = 3.0


def wilson_ci(hits: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def is_material(ret_1d: Optional[float], ret_5d: Optional[float]) -> Optional[bool]:
    """None when neither horizon is evaluable yet."""
    if ret_1d is None and ret_5d is None:
        return None
    m = False
    if ret_1d is not None and abs(ret_1d) >= MATERIAL_1D:
        m = True
    if ret_5d is not None and abs(ret_5d) >= MATERIAL_5D:
        m = True
    return m


def fmt_rate(hits: int, n: int) -> str:
    if n == 0:
        return "  n=0"
    lo, hi = wilson_ci(hits, n)
    return f"{100 * hits / n:5.1f}%  [{100 * lo:4.1f}–{100 * hi:4.1f}]  (n={n})"


async def run() -> Dict:
    dsn = (os.environ["DATABASE_URL"]).replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn=dsn, timeout=20)
    try:
        rows = await conn.fetch("""
            SELECT so.signal_id, so.symbol, so.direction, so.conviction,
                   so.source, so.ret_1d, so.ret_5d, so.eval_status,
                   n.headline, n.category,
                   left(coalesce(n.body, ''), 20000) AS body,
                   c.revenue_ttm_cr, c.mcap_cr,
                   (SELECT imp->>'time_horizon'
                      FROM jsonb_array_elements(ir.company_impacts) imp
                     WHERE upper(imp->>'symbol') = so.symbol LIMIT 1) AS time_horizon
            FROM signal_outcomes so
            JOIN news_items n ON n.id = so.news_id
            JOIN impact_reports ir ON ir.id = so.impact_report_id
            LEFT JOIN companies c ON c.symbol = so.symbol
            WHERE so.source IN ('NSE_FILINGS', 'BSE_FILINGS')
              AND so.eval_status IN ('partial', 'complete')
        """)

        # Volatility control: unconditional share of trading days (last 90d)
        # with |1d move| >= MATERIAL_1D, per symbol.
        symbols = sorted({r["symbol"] for r in rows})
        vol_rows = await conn.fetch("""
            WITH d AS (
              SELECT symbol, date, close,
                     lag(close) OVER (PARTITION BY symbol ORDER BY date) AS prev
              FROM price_points
              WHERE symbol = ANY($1) AND date >= current_date - 120
            )
            SELECT symbol,
                   count(*) FILTER (WHERE prev > 0
                       AND abs(close / prev - 1) * 100 >= $2)::float
                     / NULLIF(count(*) FILTER (WHERE prev > 0), 0) AS base_rate
            FROM d GROUP BY symbol
        """, symbols, MATERIAL_1D)
        base_rate = {r["symbol"]: r["base_rate"] for r in vol_rows if r["base_rate"] is not None}
    finally:
        await conn.close()

    per_signal: List[dict] = []
    for r in rows:
        dec = classify(r["source"], r["category"], r["headline"] or "")
        value_cr = ratio = None
        verdict, reason = "kept", dec.rule
        if dec.action == DENY:
            verdict, reason = "suppressed", f"deny:{dec.rule}"
        elif dec.action == RATIO_REQUIRED:
            value_cr = extract_value_cr((r["headline"] or "") + "\n" + (r["body"] or ""))
            ratio = materiality_ratio(value_cr, r["revenue_ttm_cr"])
            if value_cr is None:
                verdict, reason = "suppressed", "ratio_extraction_failed"
            elif ratio is None:
                verdict, reason = "suppressed", "no_financials"
            elif not ratio_passes(ratio):
                verdict, reason = "suppressed", f"ratio_below_{MATERIALITY_RATIO_MIN}"
        # v1.1 horizon routing: classification, not suppression — "immediate"
        # events are the best materiality flags but the worst post-signal
        # trades, so they route to the event-alert surface.
        route = horizon_route(r["time_horizon"]) if verdict == "kept" else None
        conviction_v2 = compute_conviction(
            dec.action, ratio, priced_in_downgraded=False, mcap_cr=r["mcap_cr"],
        ) if verdict == "kept" else None
        per_signal.append({
            "symbol": r["symbol"], "source": r["source"],
            "category": dec.category, "action": dec.action, "rule": dec.rule,
            "verdict": verdict, "reason": reason,
            "value_cr": value_cr, "ratio": ratio,
            "llm_conviction": r["conviction"], "conviction_v2": conviction_v2,
            "time_horizon": r["time_horizon"], "route": route,
            "material": is_material(r["ret_1d"], r["ret_5d"]),
            "ret_1d": r["ret_1d"], "ret_5d": r["ret_5d"],
            "base_rate": base_rate.get(r["symbol"]),
        })

    evaluable = [s for s in per_signal if s["material"] is not None]
    kept = [s for s in evaluable if s["verdict"] == "kept"]
    suppressed = [s for s in evaluable if s["verdict"] == "suppressed"]

    def mp(group):
        hits = sum(1 for s in group if s["material"])
        return hits, len(group)

    print(f"=== GATE BACKTEST — {GATE_VERSION} (IN-SAMPLE; see module docstring) ===")
    print(f"signals: {len(per_signal)} filing signals, {len(evaluable)} evaluable\n")
    print(f"Materiality Precision (|1d|>={MATERIAL_1D}% or |5d|>={MATERIAL_5D}%):")
    print(f"  UNGATED (all):        {fmt_rate(*mp(evaluable))}")
    print(f"  GATED   (kept):       {fmt_rate(*mp(kept))}")
    print(f"  SUPPRESSED tail loss: {fmt_rate(*mp(suppressed))}   <- guardrail: material events we'd hide")
    vol = len(kept) / len(evaluable) * 100 if evaluable else 0
    print(f"  volume kept: {len(kept)}/{len(evaluable)} ({vol:.0f}%)\n")

    print("By policy action (kept sets only where applicable):")
    for action in (ALWAYS_ELIGIBLE, RATIO_REQUIRED, PASS_THROUGH, DENY):
        grp = [s for s in evaluable if s["action"] == action]
        if grp:
            ctrl = [s["base_rate"] for s in grp if s["base_rate"] is not None]
            ctrl_str = f"  control(base-rate)={100 * sum(ctrl) / len(ctrl):.1f}%" if ctrl else ""
            print(f"  {action:<16} {fmt_rate(*mp(grp))}{ctrl_str}")

    print("\nSuppression reasons:")
    by_reason = defaultdict(list)
    for s in suppressed:
        by_reason[s["reason"]].append(s)
    for reason, grp in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print(f"  {reason:<32} {fmt_rate(*mp(grp))}")

    print("\nTop categories (evaluable n>=5), MP vs volatility control:")
    by_cat = defaultdict(list)
    for s in evaluable:
        by_cat[(s["category"][:44], s["action"])].append(s)
    for (cat, action), grp in sorted(by_cat.items(), key=lambda kv: -len(kv[1]))[:15]:
        if len(grp) >= 5:
            ctrl = [s["base_rate"] for s in grp if s["base_rate"] is not None]
            ctrl_str = f" ctrl={100 * sum(ctrl) / len(ctrl):4.1f}%" if ctrl else ""
            print(f"  [{action:<14}] {cat:<44} {fmt_rate(*mp(grp))}{ctrl_str}")

    print("\nConviction calibration on KEPT set (realized materiality by bucket):")
    for label, key in (("LLM", "llm_conviction"), ("v2 ", "conviction_v2")):
        for bucket in ("high", "medium", "low"):
            grp = [s for s in kept if s[key] == bucket]
            if grp:
                print(f"  {label} {bucket:<7} {fmt_rate(*mp(grp))}")

    print("\nKept set by horizon route (v1.1 — classification, not suppression):")
    by_r = defaultdict(list)
    for s in kept:
        by_r[s["route"] or "?"].append(s)
    for route, grp in sorted(by_r.items(), key=lambda kv: -len(kv[1])):
        print(f"  {route:<12} MP: {fmt_rate(*mp(grp))}")
    print("  (event_alert counts toward materiality precision; tradeable-signal"
          "\n   claims use the drift route only — see intraday study 2026-07-04)")

    print("\nRATIO_REQUIRED extraction audit (evaluable):")
    rr = [s for s in evaluable if s["action"] == RATIO_REQUIRED]
    ok = [s for s in rr if s["ratio"] is not None]
    fail = [s for s in rr if s["value_cr"] is None]
    print(f"  extracted value: {len(ok)}/{len(rr)}; extraction failed: {len(fail)}; "
          f"no financials: {len(rr) - len(ok) - len(fail)}")
    for s in sorted((x for x in rr if x["ratio"] is not None),
                    key=lambda x: -(x["ratio"] or 0))[:8]:
        print(f"    {s['symbol']:<12} value={s['value_cr']:>9.1f}Cr ratio={s['ratio']:<7} "
              f"mat={s['material']} ret1d={s['ret_1d']}")

    return {"evaluable": len(evaluable), "kept_mp": mp(kept), "ungated_mp": mp(evaluable),
            "suppressed_mp": mp(suppressed)}


if __name__ == "__main__":
    out = asyncio.run(run())
    print("\nJSON:", json.dumps(out))
