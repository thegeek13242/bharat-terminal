"""Scenario-specific prompt rubrics for stage-3 impact assessment.

Root cause (SWSOLAR postmortem 2026-07-16): the general stage-3 prompt has no
results-specific discipline, so on quarterly-results filings the model anchors
on the company's own promotional framing — typically the single most flattering
line (PAT YoY) — and misses revenue/PBT deterioration, tax-line artifacts and
one-off items. 5 of 21 "positive" results calls in 14 days fell >1.5% same-day
(SWSOLAR -7%: revenue -10% YoY, PBT -24% YoY, PAT "+70%" purely from a 50%→6%
tax-rate change; SUPREMEINF: one-off debt-resolution gain ~78x TTM revenue
called positive; ORIENTHOT: absurd 20x base-artifact comparative celebrated).

Fix: detect known filing scenarios and inject a scenario rubric that OVERRIDES
the general guidance where they conflict. Detection is deliberately
conservative — a missed detection costs nothing (general prompt still applies);
a misfired rubric could distort a non-results assessment.

Pure functions, no I/O — unit-testable without infra.
"""
from __future__ import annotations

import re
from typing import Optional

# ── Detection ──────────────────────────────────────────────────────────────

_RESULTS_HEADLINE_RE = re.compile(
    r"financial\s+results?"
    r"|(?:un)?audited\s+(?:consolidated\s+|standalone\s+)*(?:financial\s+)?results"
    r"|results?\s+for\s+the\s+(?:quarter|half\s*year|year|period)",
    re.I)
# Wire-style results coverage: "ITC Hotels Q1 Results: PAT jumps 35%...".
# Weak signal on its own — "Q1 results today: Jio, Wipro..." previews match
# too — so it additionally requires the body P&L gate below.
_WIRE_RESULTS_RE = re.compile(
    r"\bq[1-4]\s*(?:fy\s*\d{2,4})?\s+results?\b"
    r"|\b(?:quarterly|half[-\s]?yearly|annual)\s+results?\b",
    re.I)
# "Board Meeting Intimation ... to consider and approve Financial Results" is a
# SCHEDULING notice, not results — the strong headline pattern matches its
# "financial results" text, so explicitly demote intimations to the body-gated
# weak path (they only count if the filing actually carries a P&L, which a
# genuine intimation never does). Seen live 2026-07-16: 4 intimation notices
# picked up the results rubric harmlessly (neutral) but noisily.
_INTIMATION_RE = re.compile(
    r"board\s+meeting\s+intimation|intimation\s+of\s+(?:the\s+)?board\s+meeting"
    r"|prior\s+intimation\s+of\s+board\s+meeting",
    re.I)
# Board-meeting-outcome filings frequently ARE the results filing, but the
# headline phrasing varies wildly ("Outcome of Board Meeting", "Board Meeting
# Outcome", "Outcome Of The Meeting Of Board Of Directors" — all seen live
# 2026-07-16). Keep the headline side loose (outcome + board, any order) and
# let the body P&L check carry precision.
_RESULTS_BODY_RE = re.compile(
    r"statement\s+of\s+(?:un)?audited.{0,60}financial\s+results"
    r"|revenue\s+from\s+operations",
    re.I)
_OUTCOME_HEADLINE_RE = re.compile(r"(?=.*\boutcome\b)(?=.*\bboard\b)", re.I | re.S)

_ORDER_RE = re.compile(
    r"bagging\s*/?\s*receiv|receipt\s+of\s+order|work\s+order|letter\s+of\s+award"
    r"|\bloa\b|purchase\s+order|order\s+(?:worth|valued|book)|awarded?\s+.{0,40}(?:contract|order)"
    r"|secures?\s+.{0,40}(?:order|contract)",
    re.I)

_FUND_RAISE_RE = re.compile(
    r"preferential\s+(?:issue|allotment)|\bqip\b|qualified\s+institutions?\s+placement"
    r"|rights\s+issue|raising\s+of\s+funds|fund\s+rais|convertible\s+warrants?",
    re.I)

_RATING_RE = re.compile(
    r"credit\s+rating|rating\s+(?:action|upgrade|downgrade|reaffirm|revision|watch)"
    r"|\bicra\b|\bcrisil\b|care\s+ratings|india\s+ratings|brickwork",
    re.I)


def detect_scenario(headline: str, body: str = "") -> Optional[str]:
    """Return a scenario key for a known filing type, else None.

    Order matters: results first (highest-stakes known failure mode), and an
    'Outcome of Board Meeting' headline counts as results only when the body
    actually carries a results statement.
    """
    h = headline or ""
    if _RESULTS_HEADLINE_RE.search(h) and not _INTIMATION_RE.search(h):
        return "financial_results"
    # Weak headline signals (board-meeting outcomes, wire-style "Q1 results",
    # results-consideration intimations) only count when the body actually
    # carries P&L content — keeps previews ("Q1 results today: Jio, Wipro..."),
    # scheduling notices and non-results outcomes out.
    if ((_OUTCOME_HEADLINE_RE.search(h) or _WIRE_RESULTS_RE.search(h)
         or (_RESULTS_HEADLINE_RE.search(h) and _INTIMATION_RE.search(h)))
            and _RESULTS_BODY_RE.search((body or "")[:20000])):
        return "financial_results"
    if _ORDER_RE.search(h):
        return "order_win"
    if _FUND_RAISE_RE.search(h):
        return "fund_raise"
    if _RATING_RE.search(h):
        return "rating_action"
    return None


# ── Rubrics ────────────────────────────────────────────────────────────────
# Written as hard analyst discipline. Each rubric is injected verbatim under a
# "SCENARIO RUBRIC" header that declares it overrides the general rules.

_RESULTS_RUBRIC = """\
This is a QUARTERLY/ANNUAL RESULTS filing. Results filings have a documented
failure mode: the press release is the company's OWN promotional framing and
always leads with its most flattering metric. Ignore the adjectives and the
ordering of the press release entirely — work ONLY from the numbers table.

MANDATORY procedure:
a. Locate the P&L table and extract, for BOTH the year-ago quarter (YoY) and
   the immediately prior quarter (QoQ): Revenue from operations, Profit before
   exceptional items and tax (PBT), Exceptional items, Tax expense, PAT, EPS.
   Prefer CONSOLIDATED figures when both consolidated and standalone appear.
b. SENTIMENT comes from the CORE OPERATING trajectory: revenue and
   pre-exceptional PBT, YoY first (QoQ as secondary confirmation, mindful of
   seasonality). State the revenue and PBT YoY percentage changes explicitly
   in the explanation — an assessment that omits revenue is invalid.
b2. MARGIN TRAJECTORY is mandatory: compare profit growth against revenue
   growth and state the operating-margin direction (e.g. "EBITDA +32% vs
   revenue +39% = margin compression ~70bps YoY"). When profit growth
   materially lags revenue growth, the quarter is lower-quality than its
   headline suggests: say so and cap magnitude at 3 (POLYCAB 2026-07-16:
   a +39%-revenue quarter sold off on exactly this). Margin expansion is
   likewise worth stating — it upgrades quality.
c. QUALITY OF EARNINGS — PAT/EPS growth does NOT count toward direction when
   it is driven by: a lower effective tax rate, exceptional items, other
   income, write-backs, one-off recoveries (indemnities, settlements, debt
   resolution, asset sales), or deferred-tax credits. Compute the effective
   tax rate both periods; if PAT grows while PBT declines, the quarter is NOT
   positive and the explanation must say the growth is below-the-line. This
   applies even when the one-off sits INSIDE reported PBT: if PBT swings by
   an amount that is large relative to REVENUE itself (say >30% of revenue)
   while revenue is roughly flat, the swing is by definition non-operating —
   trace the driver (write-back, restructuring gain, fair-value gain) and
   exclude it from direction no matter which P&L line it was booked on.
d. If revenue AND pre-exceptional PBT both declined YoY, sentiment is negative
   (neutral only if the decline is demonstrably a disclosed base-period
   one-off — name it). A PAT increase cannot rescue such a quarter.
e. One-off receipts/gains (indemnity claims, arbitration awards, insurance,
   debt resolution) are balance-sheet/cash-flow events: mention them, but they
   must not set direction, and magnitude for them alone is 1-2.
f. SANITY-CHECK comparatives: a >3x change in PBT or PAT while revenue changed
   by less than ~20% is NEVER organic — it is a one-off, a write-back, or a
   base artifact (demerger, consolidation-scope change, suspended operations,
   restated base). In that situation sentiment MUST be neutral and magnitude
   capped at 2, with the driver named. The size of a one-off gain does not
   make it more positive — a windfall 100x revenue is still a windfall, not
   earnings power. The same cap applies to >3x revenue swings vs the
   comparative (suspect the base, not the business).
g. Do NOT infer beats/misses against market expectations you do not have; if
   the company's own trajectory is mixed, say mixed (neutral) rather than
   defaulting to positive."""

_ORDER_RUBRIC = """\
This is an ORDER/CONTRACT WIN filing.
a. Size it: order value as % of TTM revenue (state the %). Below ~2% of TTM
   revenue is routine business — magnitude 1-2 regardless of tone.
b. Distinguish FIRM orders from MoUs/LoIs/framework agreements/empanelments —
   the latter carry execution risk and are capped at magnitude 2.
c. Note execution period: a large order executed over 5 years moves annual
   revenue far less than its headline number suggests — annualise before
   sizing.
d. Repeat/routine orders from the same counterparty and internal/related-party
   orders are weaker signals — say so."""

_FUND_RAISE_RUBRIC = """\
This is a FUND-RAISE/ISSUANCE filing.
a. Compute dilution (new shares vs outstanding) and pricing vs current market
   price; state both.
b. Deep-discount or large dilution to outsiders is negative for existing
   holders; promoter infusion at/above market is a mild positive
   (skin-in-the-game); warrants convert only partially — treat as contingent.
c. Purpose matters: debt repayment (deleveraging) vs growth capex vs unnamed
   'general corporate purposes' (weakest). The instrument being announced is
   not by itself good news — direction follows dilution, pricing and use."""

_RATING_RUBRIC = """\
This is a CREDIT-RATING action.
a. Direction follows the action: upgrade positive, downgrade negative,
   reaffirmation neutral (magnitude 1) — regardless of the agency's polite
   language.
b. Scale by notches moved, investment-grade boundary crossings (e.g. falling
   below BBB-), and outlook changes; a downgrade WITH negative watch is worse.
c. Size the rated debt vs the balance sheet; a small facility's rating says
   little about the company."""

_RUBRICS = {
    "financial_results": _RESULTS_RUBRIC,
    "order_win": _ORDER_RUBRIC,
    "fund_raise": _FUND_RAISE_RUBRIC,
    "rating_action": _RATING_RUBRIC,
}


# ── Sector addendum: lenders (banks / NBFCs) ───────────────────────────────
# The universal results rubric speaks revenue/PBT — a lender's statement has
# neither (banking-format P&L starts at "Interest earned"; NBFCs at interest
# income + ECL impairment). Routed off the FILING BODY's own signature, not
# companies.sector_nse: industry_nse is 100% NULL (known landmine) and
# sector_nse lumps banks with brokers/insurers/AMCs. Two or more distinct
# signals required — single hits ("provision") occur in non-lender text.

_LENDER_SIGNALS_RE = re.compile(
    r"interest\s+earned|net\s+interest\s+(?:income|margin)|\bnii\b|\bnim\b"
    r"|gross\s+n\.?p\.?a\.?|\bgnpa\b|net\s+n\.?p\.?a\.?|\bnnpa\b"
    r"|provision\s+coverage|\bcasa\b|credit\s+cost|slippages?"
    r"|impairment\s+(?:of|on)\s+financial\s+instruments|expected\s+credit\s+loss"
    r"|assets?\s+under\s+management|\baum\b|capital\s+adequacy|\bcrar\b"
    r"|cost\s+of\s+(?:funds|borrowings?)|disbursements?",
    re.I)


def is_lender_results(body: str) -> bool:
    """True when the filing body carries >=2 distinct lender-statement signals."""
    hits = {m.group(0).lower().strip() for m in
            _LENDER_SIGNALS_RE.finditer((body or "")[:40000])}
    return len(hits) >= 2


_LENDER_ADDENDUM = """\
This results statement is a LENDER's (bank / NBFC) — the industrial
revenue/PBT framing above does NOT apply. Use the lender mapping:
a. CORE LINES: Net Interest Income (interest earned - interest expended),
   pre-provision operating profit (PPOP), provisions / credit cost, GNPA and
   NNPA percentages with slippages, advances & deposits growth (for NBFCs:
   AUM, disbursements, cost of funds, spread). State NII and PPOP YoY changes
   explicitly — an assessment that omits them is invalid.
b. DIRECTION comes from NII/PPOP trajectory AND asset-quality direction
   TOGETHER. Bottom-line PAT alone is never direction for a lender.
c. LENDER ONE-OFF CLASS: provision write-backs/releases, recoveries from
   written-off accounts, treasury/MTM gains, stake-sale gains, DTA
   remeasurement. PAT growth with flat/declining PPOP, or with credit cost
   far below the company's own run-rate, is below-the-line — say so; it does
   not make the quarter positive.
d. MARGIN ANALOG: state the NIM trajectory in bps (NBFCs: spread / cost of
   funds). NIM compression while profits grow must be stated and caps
   magnitude at 3, exactly like the industrial margin rule.
e. ASSET QUALITY TRUMPS GROWTH: rising slippages/GNPA or falling provision
   coverage alongside profit growth must be stated and tempers direction;
   improving asset quality with steady PPOP upgrades it.
f. FUNDING: deposit growth materially lagging advances growth (or rising
   cost of funds for NBFCs) is a constraint worth one sentence."""


def lender_addendum(scenario_key: str, body: str,
                    company_type: Optional[str] = None) -> str:
    """Lender sector addendum, '' unless a results scenario on a lender.

    Routing PREFERS the derived classification axis (companies.company_type
    in bank/nbfc_lender — migration 033, router v2) and falls back to the
    filing body's own statement signature. The preference is monotone-
    additive: classification can only ADD the addendum for a lender whose
    body text is atypical, never remove it for a body that carries lender
    signals — so legacy 2-arg callers (company_type=None) behave exactly as
    before this parameter existed.
    """
    if scenario_key != "financial_results":
        return ""
    if company_type not in ("bank", "nbfc_lender") and not is_lender_results(body):
        return ""
    return (
        "\nSECTOR ADDENDUM — LENDER RESULTS (overrides the industrial results "
        "mapping where they conflict):\n" + _LENDER_ADDENDUM + "\n"
    )


# ── Company-type addendum: investment holding companies (router v2, Tier B) ──
# The MAHSCOOTER founding exhibit: a holdco's standalone P&L is dividend-
# receipt timing (29→271→6→6 Cr quarterly lumping), so "revenue −81%" is not
# an operating collapse. The Tier-A factual hint (router_v2.HOLDCO_HINT)
# always ships; this RUBRIC (judgment rules) is selected only on the
# ROUTER_V2_RUBRICS path, gated exactly like the taxonomy-v3 selection.

_HOLDCO_RESULTS_ADDENDUM = """\
a. VALUE DRIVER: an investment holding company's market value derives from
   the NAV of its group holdings, not from its own P&L. Judge this filing
   against the NAV lens: did anything change about the underlying holdings,
   their stakes, or discounts/haircuts to NAV?
b. DIVIDEND-TIMING RULE: standalone revenue/PAT here is dominated by WHEN
   group companies pay dividends and by fair-value marks. A revenue or
   profit swing (however large) that traces to dividend-receipt timing or
   fair-value movement is NOT an operating change: sentiment neutral and
   magnitude capped at 2 for such swings.
c. NAME THE HOLDINGS: if the filing discloses the underlying group holdings
   (or the dividends' sources), name them in the explanation — the reader
   values the holdings, not this P&L.
d. Only a change in the holdings themselves (stake sale/purchase, holding-
   company-level leverage, restructuring of the structure) can justify
   magnitude >= 3."""


def holdco_results_addendum(scenario_key: str,
                            company_type: Optional[str]) -> str:
    """Holdco results addendum, '' unless results scenario on a holdco."""
    if scenario_key != "financial_results" or company_type != "holdco":
        return ""
    return (
        "\nCOMPANY-TYPE ADDENDUM — INVESTMENT HOLDING COMPANY RESULTS "
        "(overrides the industrial results mapping where they conflict):\n"
        + _HOLDCO_RESULTS_ADDENDUM + "\n"
    )


def rubric_block(key: Optional[str]) -> str:
    """Formatted rubric block for a known scenario key ('' otherwise).
    Extracted from rubric_for so router v2 (Tier B) can select a rubric by
    taxonomy-v3 bucket; output is byte-identical to the historic inline
    formatting."""
    if not key or key not in _RUBRICS:
        return ""
    return (
        f"\nSCENARIO RUBRIC — {key.upper().replace('_', ' ')} (this filing type has a "
        f"known failure mode; where these rules conflict with the general "
        f"discipline above, THESE RULES WIN):\n{_RUBRICS[key]}\n"
    )


def rubric_for(headline: str, body: str = "") -> str:
    """Full prompt block for a detected scenario, or '' when none applies."""
    return rubric_block(detect_scenario(headline, body))
