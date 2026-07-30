"""Company-type detectors — Analyst Clone Phase 1, build item #0.

WHY THIS EXISTS: the MAHSCOOTER loss. The results classifier read
"revenue -81%" literally on an investment holdco whose income is lumpy
FY dividends (29->271->6->6 Cr). Two deterministic facts (mcap/revenue
45x, FY dividend-share 0.91 of income) falsify that read without any
LLM. This job materialises those facts as a PRIMARY accounting-regime
axis (companies.company_type) plus non-exclusive OVERLAYS
(companies.type_overlays), per ANALYST_CLONE_ARCHITECTURE_2026-07-29.md
section 2 and migration 033. Every agent's first tool call reads these
columns, so wrong-but-confident values here poison everything
downstream — which is why each detector below was measured against the
live universe before being encoded, and why taxonomy is treated as an
input, never ground truth (WESTLIFE, a QSR, files taxonomy='nbfc').

PRIMARY AXIS (first match wins):
  1. fund_scheme      — name ~* 'mutual fund' OR symbol '%-RE' (universe
                        hygiene: scheme listings must never route to
                        analysis as equities).
  2. bank / insurer_* — latest financial_results.taxonomy per symbol,
                        guarded: taxonomy in (nbfc, banking) with
                        interest_earned 0/NULL across the last 4 deduped
                        quarters AND mcap/revenue < 5 is an OPERATING
                        company mis-filed under a lender taxonomy
                        (the WESTLIFE guard) -> 'operating' at penalised
                        confidence, logged loudly.
  3. inside taxonomy='nbfc':
       holdco (a)     — FY dividend-share rule: on the FY-end row's
                        raw_xbrl_facts, FourD:DividendIncome /
                        FourD:Income > 0.5. FY ("FourD") sums ONLY —
                        single-quarter shares are unstable by
                        construction (the MAHSCOOTER lumping itself).
                        Measured 12/12 true incl. Summit Securities,
                        which is why rule (a) outranks the name rules.
       amc            — name ~* 'asset management'
       broker_wealth  — name ~* 'securities|broking|capital market|
                        advisors|wealth'
       holdco (b)     — mcap/revenue > 20 (names already amc/broker
                        excluded)
       nbfc_lender    — everything else in the nbfc taxonomy.
  4. non-nbfc holdco extender — name ~* '(holdings?|investments?)' AND
     mcap/revenue > 20, at name-rule confidence.
  5. operating (any financial_results row) else unknown (~125 dark).

OVERLAYS (non-exclusive, jsonb array): psu, distress_attached,
suspended, circuit_prone, loss_making, loss_making_growth,
recently_listed, cic_declared. loss_making_growth implies loss_making
(both attached) so consumers filtering the broad flag see every loss
maker. recently_listed excludes the 2026-04-20 listing_date backfill
batch (104 rows share that exact date — an ingestion artifact, not an
IPO wave). The cic_declared regex is the TIGHTENED self-referential
form ("is a core investment company"), trading the doc's 22 loose
candidates for high precision.

type_confidence tiers: 0.95 taxonomy-driven, 0.85 nbfc holdco rules,
0.6 name-based (incl. fund_scheme, the non-nbfc holdco extender and
the WESTLIFE-guard reclassification penalty), 0.5 default paths
(operating-without-taxonomy, unknown).

CLI:
  python -m bharat_terminal.kb.sync.company_type_sync --dry-run
      compute everything, print per-type / per-overlay counts and a
      DIFF vs current DB values, write NOTHING. Runs pre-migration
      too: if migration 033 has not been applied the diff section is
      skipped with a loud notice (detector inputs are all pre-existing
      tables).
  python -m bharat_terminal.kb.sync.company_type_sync --execute
      one UPDATE per active company: company_type, type_overlays,
      type_confidence, type_detected_at=now(), is_fund_scheme.

Scope: is_active companies only (the 3,998-name live universe the
detectors were measured on). Inactive rows keep NULL company_type,
which keeps "never classified" distinguishable from "classified".
Deterministic: no sampling, no LLM, fixed thresholds below.
"""
import argparse
import asyncio
import json
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from bharat_terminal.kb.sync.utils import create_async_session

logger = logging.getLogger("company_type_sync")

# ---------------------------------------------------------------------------
# Fixed detector constants (section-2 measured thresholds — change only with
# a re-measurement, these are load-bearing for every downstream agent).
# ---------------------------------------------------------------------------
DIVIDEND_SHARE_MIN = 0.5        # FY dividend-share holdco rule (a)
HOLDCO_MCAP_REV_MIN = 20.0      # mcap/revenue holdco rule (b) + extender
GUARD_MCAP_REV_MAX = 5.0        # WESTLIFE guard: operating-scale revenue
SUSPENDED_STALE_DAYS = 30       # no price row in 30d (or none ever)
CIRCUIT_WINDOW_DAYS = 90        # circuit-prone lookback
CIRCUIT_MIN_ROWS = 20           # minimum price rows in the window
CIRCUIT_HL_SHARE_MIN = 0.30     # share of high==low (band-locked) days
LOSS_GROWTH_REV_MIN = 0.20      # trailing-4Q vs prior-4Q revenue growth
RECENT_LISTING_DAYS = 365
LISTING_BACKFILL_DATE = date(2026, 4, 20)   # 104-row artifact, excluded

CONF_TAXONOMY = 0.95
CONF_HOLDCO = 0.85
CONF_NAME = 0.6
CONF_DEFAULT = 0.5

FUND_SCHEME_RE = re.compile(r"mutual fund", re.IGNORECASE)
AMC_RE = re.compile(r"asset management", re.IGNORECASE)
BROKER_RE = re.compile(
    r"securities|broking|capital market|advisors|wealth", re.IGNORECASE)
HOLDCO_NAME_RE = re.compile(r"holdings?|investments?", re.IGNORECASE)

OVERLAY_ORDER = [
    "psu", "distress_attached", "suspended", "circuit_prone",
    "loss_making", "loss_making_growth", "recently_listed", "cic_declared",
]

# ---------------------------------------------------------------------------
# Read-only detector queries. Each is self-contained (no list binding) and
# hits only pre-migration tables, so --dry-run works before migration 033.
# ---------------------------------------------------------------------------

Q_ACTIVES = text("""
    SELECT symbol, company_name, mcap_cr, revenue_ttm_cr, pat_cr,
           listing_date
    FROM companies WHERE is_active
""")

# Latest non-null taxonomy per active symbol. 403 symbols flip taxonomy
# across history — latest filing wins, ties broken toward consolidated.
Q_LATEST_TAXONOMY = text("""
    SELECT DISTINCT ON (fr.symbol) fr.symbol, fr.taxonomy
    FROM financial_results fr
    JOIN companies c ON c.symbol = fr.symbol AND c.is_active
    WHERE fr.taxonomy IS NOT NULL
    ORDER BY fr.symbol, fr.period_end DESC, fr.consolidated DESC
""")

# WESTLIFE guard input: nbfc/banking-taxonomy symbols whose last 4 deduped
# quarters report zero/NULL interest_earned. A lender that earns no
# interest for a year is not a lender; the mcap/revenue leg is applied in
# Python so the reclassification can be logged with its ratio.
Q_GUARD_IE_ZERO = text("""
    WITH latest_tax AS (
        SELECT DISTINCT ON (fr.symbol) fr.symbol, fr.taxonomy
        FROM financial_results fr
        JOIN companies c ON c.symbol = fr.symbol AND c.is_active
        WHERE fr.taxonomy IS NOT NULL
        ORDER BY fr.symbol, fr.period_end DESC, fr.consolidated DESC
    ),
    dedup AS (
        SELECT DISTINCT ON (fr.symbol, fr.period_end)
               fr.symbol, fr.period_end,
               CASE WHEN fr.sector_financials->>'interest_earned'
                         ~ '^-?[0-9.]+$'
                    THEN (fr.sector_financials->>'interest_earned')::float
                    ELSE 0 END AS ie
        FROM financial_results fr
        JOIN latest_tax lt ON lt.symbol = fr.symbol
                          AND lt.taxonomy IN ('nbfc', 'banking')
        ORDER BY fr.symbol, fr.period_end, fr.consolidated DESC
    ),
    ranked AS (
        SELECT symbol, ie,
               row_number() OVER (PARTITION BY symbol
                                  ORDER BY period_end DESC) AS rn
        FROM dedup
    )
    SELECT symbol FROM ranked WHERE rn <= 4
    GROUP BY symbol
    HAVING count(*) FILTER (WHERE ie <> 0) = 0
""")

# FY dividend-share per symbol from the latest FY-end row. FourD:* facts
# exist only on fiscal-year-end rows (full-year duration context), which
# is exactly the anti-lumping constraint the rule demands. Facts are
# stored as strings; non-numeric values are filtered, not cast-crashed.
Q_DIVIDEND_SHARE = text("""
    SELECT DISTINCT ON (fr.symbol) fr.symbol,
           (fr.raw_xbrl_facts->>'FourD:DividendIncome')::numeric
           / (fr.raw_xbrl_facts->>'FourD:Income')::numeric AS div_share
    FROM financial_results fr
    JOIN companies c ON c.symbol = fr.symbol AND c.is_active
    WHERE fr.raw_xbrl_facts ? 'FourD:DividendIncome'
      AND fr.raw_xbrl_facts ? 'FourD:Income'
      AND fr.raw_xbrl_facts->>'FourD:DividendIncome' ~ '^-?[0-9.]+$'
      AND fr.raw_xbrl_facts->>'FourD:Income' ~ '^-?[0-9.]+$'
      AND (fr.raw_xbrl_facts->>'FourD:Income')::numeric > 0
    ORDER BY fr.symbol, fr.period_end DESC, fr.consolidated DESC
""")

Q_HAS_RESULTS = text("""
    SELECT DISTINCT fr.symbol
    FROM financial_results fr
    JOIN companies c ON c.symbol = fr.symbol AND c.is_active
""")

# PSU: government holder >= 26% in the LATEST shareholding quarter per
# symbol. The 'singapore' exclusion kills the GIC ("Government of
# Singapore") trap on both branches.
Q_PSU = text("""
    WITH latest_q AS (
        SELECT symbol, max(quarter_end) AS q
        FROM investor_holdings GROUP BY symbol
    )
    SELECT DISTINCT ih.symbol
    FROM investor_holdings ih
    JOIN latest_q lq ON lq.symbol = ih.symbol AND lq.q = ih.quarter_end
    JOIN companies c ON c.symbol = ih.symbol AND c.is_active
    WHERE ih.holding_pct >= 26
      AND (ih.category = 'government'
           OR ih.holder_name_norm ~ 'president of india|governor')
      AND ih.holder_name_norm !~ 'singapore'
""")

# Distress: ED/liquidator/DRT ever appearing as a named holder. ANY
# quarter, deliberately — attachment history is the red flag, and the
# section-2 count (14) was measured that way.
Q_DISTRESS = text("""
    SELECT DISTINCT ih.symbol
    FROM investor_holdings ih
    JOIN companies c ON c.symbol = ih.symbol AND c.is_active
    WHERE ih.holder_name_norm
          ~ 'enforcement directorate|liquidator|recovery officer|debts recovery'
""")

Q_LAST_PRICE = text("""
    SELECT pp.symbol, max(pp.date) AS last_px
    FROM price_points pp
    JOIN companies c ON c.symbol = pp.symbol AND c.is_active
    GROUP BY pp.symbol
""")

Q_CIRCUIT_PRONE = text("""
    SELECT pp.symbol
    FROM price_points pp
    JOIN companies c ON c.symbol = pp.symbol AND c.is_active
    WHERE pp.date >= now()::date - CAST(:window_days AS integer)
    GROUP BY pp.symbol
    HAVING count(*) >= :min_rows
       AND count(*) FILTER (WHERE pp.high = pp.low)::float / count(*)
           >= :hl_share
""")

# Trailing-4Q vs prior-4Q revenue growth. Rows deduped per period_end
# (consolidated preferred) and all 8 revenue values required — a partial
# window says nothing about growth.
Q_REV_GROWTH = text("""
    WITH dedup AS (
        SELECT DISTINCT ON (fr.symbol, fr.period_end)
               fr.symbol, fr.period_end, fr.revenue_cr
        FROM financial_results fr
        JOIN companies c ON c.symbol = fr.symbol AND c.is_active
        ORDER BY fr.symbol, fr.period_end DESC, fr.consolidated DESC
    ),
    ranked AS (
        SELECT symbol, revenue_cr,
               row_number() OVER (PARTITION BY symbol
                                  ORDER BY period_end DESC) AS rn
        FROM dedup
    )
    SELECT symbol
    FROM ranked WHERE rn <= 8
    GROUP BY symbol
    HAVING count(*) FILTER (WHERE revenue_cr IS NOT NULL) = 8
       AND sum(revenue_cr) FILTER (WHERE rn BETWEEN 5 AND 8) > 0
       AND (sum(revenue_cr) FILTER (WHERE rn BETWEEN 1 AND 4)
            - sum(revenue_cr) FILTER (WHERE rn BETWEEN 5 AND 8))
           / sum(revenue_cr) FILTER (WHERE rn BETWEEN 5 AND 8)
           > :growth_min
""")

# Tightened self-referential CIC declaration only ("is a core investment
# company" / "being a CIC") — the loose candidate list from the research
# sat at ~0.65 precision; this form trades recall for precision.
Q_CIC_DECLARED = text(r"""
    SELECT DISTINCT s.sym AS symbol
    FROM (
        SELECT jsonb_array_elements_text(n.symbols_mentioned) AS sym
        FROM news_items n
        WHERE n.body ~* 'is (a|an)?\s*(unregistered )?core investment company|being a (unregistered )?CIC'
    ) s
    JOIN companies c ON c.symbol = s.sym AND c.is_active
""")

Q_CURRENT_VALUES = text("""
    SELECT symbol, company_type, type_overlays, type_confidence,
           is_fund_scheme
    FROM companies WHERE is_active
""")

Q_UPDATE = text("""
    UPDATE companies
    SET company_type = :company_type,
        type_overlays = CAST(:type_overlays AS jsonb),
        type_confidence = :type_confidence,
        type_detected_at = now(),
        is_fund_scheme = :is_fund_scheme
    WHERE symbol = :symbol
""")


@dataclass
class Classification:
    company_type: str
    confidence: float
    overlays: list[str] = field(default_factory=list)
    is_fund_scheme: bool = False
    reason: str = ""


def _mcap_rev(mcap_cr, revenue_ttm_cr):
    """mcap/revenue, or None when either side is absent/non-positive."""
    if mcap_cr is None or revenue_ttm_cr is None or revenue_ttm_cr <= 0:
        return None
    return mcap_cr / revenue_ttm_cr


def classify_primary(company, taxonomy, div_share, guard_ie_zero,
                     has_results):
    """Primary-axis decision for one company. First match wins."""
    symbol = company["symbol"]
    name = company["company_name"] or ""
    ratio = _mcap_rev(company["mcap_cr"], company["revenue_ttm_cr"])

    # 1. fund scheme — hygiene before anything else.
    if FUND_SCHEME_RE.search(name) or symbol.endswith("-RE"):
        return Classification("fund_scheme", CONF_NAME,
                              is_fund_scheme=True, reason="name/-RE")

    # 2. WESTLIFE guard, THEN lender taxonomies. Taxonomy is an input,
    # never ground truth: a "lender" with no interest income for four
    # quarters and operating-scale revenue vs mcap is mis-filed.
    if taxonomy in ("nbfc", "banking") and symbol in guard_ie_zero \
            and ratio is not None and ratio < GUARD_MCAP_REV_MAX:
        logger.warning(
            "WESTLIFE guard: %s taxonomy=%s but interest_earned 0/NULL "
            "across last 4 quarters and mcap/rev=%.1f < %.0f — "
            "classifying operating at penalised confidence %.2f",
            symbol, taxonomy, ratio, GUARD_MCAP_REV_MAX, CONF_NAME)
        return Classification("operating", CONF_NAME,
                              reason=f"guard:{taxonomy}")

    if taxonomy == "banking":
        return Classification("bank", CONF_TAXONOMY, reason="taxonomy")
    if taxonomy == "life_insurance":
        return Classification("insurer_life", CONF_TAXONOMY,
                              reason="taxonomy")
    if taxonomy == "general_insurance":
        return Classification("insurer_general", CONF_TAXONOMY,
                              reason="taxonomy")

    # 3. nbfc sub-splits. The FY dividend-share rule outranks the name
    # rules: Summit Securities is a holdco despite its broker name, and
    # the rule measured 12/12 true.
    if taxonomy == "nbfc":
        if div_share is not None and div_share > DIVIDEND_SHARE_MIN:
            return Classification("holdco", CONF_HOLDCO,
                                  reason=f"div_share={div_share:.2f}")
        if AMC_RE.search(name):
            return Classification("amc", CONF_NAME, reason="name")
        if BROKER_RE.search(name):
            return Classification("broker_wealth", CONF_NAME,
                                  reason="name")
        if ratio is not None and ratio > HOLDCO_MCAP_REV_MIN:
            return Classification("holdco", CONF_HOLDCO,
                                  reason=f"mcap/rev={ratio:.0f}")
        return Classification("nbfc_lender", CONF_TAXONOMY,
                              reason="taxonomy")

    # 4. non-nbfc holdco extender — name + ratio, lower confidence.
    if HOLDCO_NAME_RE.search(name) and ratio is not None \
            and ratio > HOLDCO_MCAP_REV_MIN:
        return Classification("holdco", CONF_NAME, reason="name+ratio")

    # 5. defaults.
    if taxonomy in ("indas", "nonindas"):
        return Classification("operating", CONF_TAXONOMY,
                              reason="taxonomy")
    if symbol in has_results:
        return Classification("operating", CONF_DEFAULT,
                              reason="has_results")
    return Classification("unknown", CONF_DEFAULT, reason="dark")


def attach_overlays(company, cls, detectors, today):
    """Non-exclusive overlays; order is canonical for stable jsonb."""
    symbol = company["symbol"]
    flags = set()
    if symbol in detectors["psu"]:
        flags.add("psu")
    if symbol in detectors["distress"]:
        flags.add("distress_attached")
    last_px = detectors["last_price"].get(symbol)
    if last_px is None or last_px < today - timedelta(
            days=SUSPENDED_STALE_DAYS):
        flags.add("suspended")
    if symbol in detectors["circuit"]:
        flags.add("circuit_prone")
    if company["pat_cr"] is not None and company["pat_cr"] < 0:
        flags.add("loss_making")
        if symbol in detectors["rev_growth"]:
            flags.add("loss_making_growth")
    ld = company["listing_date"]
    if ld is not None and ld != LISTING_BACKFILL_DATE \
            and ld > today - timedelta(days=RECENT_LISTING_DAYS):
        flags.add("recently_listed")
    if symbol in detectors["cic"]:
        flags.add("cic_declared")
    cls.overlays = [o for o in OVERLAY_ORDER if o in flags]


async def compute(session):
    """Run every detector query and classify the active universe."""
    actives = [dict(r._mapping) for r in
               (await session.execute(Q_ACTIVES)).fetchall()]
    taxonomy = {r.symbol: r.taxonomy for r in
                (await session.execute(Q_LATEST_TAXONOMY)).fetchall()}
    guard_ie_zero = {r.symbol for r in
                     (await session.execute(Q_GUARD_IE_ZERO)).fetchall()}
    div_share = {r.symbol: float(r.div_share) for r in
                 (await session.execute(Q_DIVIDEND_SHARE)).fetchall()}
    has_results = {r.symbol for r in
                   (await session.execute(Q_HAS_RESULTS)).fetchall()}
    detectors = {
        "psu": {r.symbol for r in
                (await session.execute(Q_PSU)).fetchall()},
        "distress": {r.symbol for r in
                     (await session.execute(Q_DISTRESS)).fetchall()},
        "last_price": {r.symbol: r.last_px for r in
                       (await session.execute(Q_LAST_PRICE)).fetchall()},
        "circuit": {r.symbol for r in (await session.execute(
            Q_CIRCUIT_PRONE, {
                "window_days": CIRCUIT_WINDOW_DAYS,
                "min_rows": CIRCUIT_MIN_ROWS,
                "hl_share": CIRCUIT_HL_SHARE_MIN,
            })).fetchall()},
        "rev_growth": {r.symbol for r in (await session.execute(
            Q_REV_GROWTH, {"growth_min": LOSS_GROWTH_REV_MIN})).fetchall()},
        "cic": {r.symbol for r in
                (await session.execute(Q_CIC_DECLARED)).fetchall()},
    }

    today = date.today()
    results = {}
    for company in actives:
        sym = company["symbol"]
        cls = classify_primary(company, taxonomy.get(sym),
                               div_share.get(sym), guard_ie_zero,
                               has_results)
        attach_overlays(company, cls, detectors, today)
        results[sym] = cls
    return results


async def fetch_current(session):
    """Current column values for the diff; None if 033 not applied."""
    try:
        rows = (await session.execute(Q_CURRENT_VALUES)).fetchall()
    except ProgrammingError:
        await session.rollback()
        return None
    return {r.symbol: r for r in rows}


def print_report(results, current):
    type_counts = Counter(c.company_type for c in results.values())
    overlay_counts = Counter(o for c in results.values()
                             for o in c.overlays)

    print(f"\n=== company_type_sync — {len(results)} active companies ===")
    print("\nPrimary type:")
    for t, n in type_counts.most_common():
        print(f"  {t:16s} {n:5d}")
    print("\nOverlays (non-exclusive):")
    for o in OVERLAY_ORDER:
        print(f"  {o:20s} {overlay_counts.get(o, 0):5d}")
    n_fund = sum(1 for c in results.values() if c.is_fund_scheme)
    print(f"\nis_fund_scheme: {n_fund}")

    if current is None:
        print("\nDIFF: SKIPPED — companies.company_type does not exist "
              "yet (migration 033 not applied). Detector computation "
              "above ran on pre-existing tables only.")
        return

    changes = Counter()
    samples = {}
    for sym, cls in results.items():
        row = current.get(sym)
        old = row.company_type if row is not None else None
        old_ov = list(row.type_overlays or []) if row is not None else []
        if old != cls.company_type or old_ov != cls.overlays:
            key = (old or "NULL", cls.company_type)
            changes[key] += 1
            samples.setdefault(key, []).append(sym)
    print(f"\nDIFF vs current DB ({sum(changes.values())} rows change):")
    if not changes:
        print("  no changes")
    for (old, new), n in sorted(changes.items(),
                                key=lambda kv: -kv[1]):
        ex = ", ".join(samples[(old, new)][:5])
        print(f"  {old:16s} -> {new:16s} {n:5d}   e.g. {ex}")


async def run(execute: bool) -> int:
    session_factory = create_async_session()
    async with session_factory() as session:
        results = await compute(session)
        current = await fetch_current(session)
        print_report(results, current)

        if not execute:
            print("\nDRY RUN — nothing written.")
            return 0

        if current is None:
            print("\nEXECUTE ABORTED: migration 033 not applied — "
                  "target columns are absent.", file=sys.stderr)
            return 1

        for sym, cls in results.items():
            await session.execute(Q_UPDATE, {
                "symbol": sym,
                "company_type": cls.company_type,
                "type_overlays": json.dumps(cls.overlays),
                "type_confidence": cls.confidence,
                "is_fund_scheme": cls.is_fund_scheme,
            })
        await session.commit()
        print(f"\nEXECUTED: {len(results)} companies updated "
              "(company_type, type_overlays, type_confidence, "
              "type_detected_at, is_fund_scheme).")
        return 0


def main():
    parser = argparse.ArgumentParser(
        description="Derive companies.company_type/type_overlays from "
                    "the measured section-2 detectors.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="compute + report + diff, write nothing")
    mode.add_argument("--execute", action="store_true",
                      help="write one UPDATE per active company")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(run(execute=args.execute))


if __name__ == "__main__":
    sys.exit(main())
