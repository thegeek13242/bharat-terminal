"""Filing-category signal policy — deterministic, pure, no I/O.

Decides whether an exchange filing is ELIGIBLE to produce trade signals.
This gates SIGNALS ONLY: the analysis pipeline (relevance, impacts, feed,
dossiers) never consults it.

Design rules (frozen as gate_version v1_2026_07 — see plan 2026-07-04):
- DENY: filing types with no quantifiable economics (median outcome ≈ noise;
  the Jul 2-3 study's worst loss came from a "General Updates" filing whose
  price move was exogenous to the filing).
- RATIO_REQUIRED: order/contract awards — economically real ONLY when the
  value is large relative to the company (HECPROJECT: order = 27% of TTM
  revenue → +6.39%; routine small orders ≈ 0). The materiality ratio is
  computed by analysis/materiality.py; this module only labels the class.
- ALWAYS_ELIGIBLE: event types that are material by construction (results,
  M&A, rating downgrades, regulatory action...).
- PASS_THROUGH (default): unmatched types stay eligible AND labelled, so
  accumulated outcome data — not guesswork — decides what v2 gates.

Classification sources, in priority order:
1. structured category (fine-grained NSE/BSE adapter category, when present)
2. the headline subject phrase ("... has informed the Exchange about X")
The same tables serve the live gate and the retro-backtest.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

GATE_VERSION = "v1.1_2026_07"

# ── Horizon routing (v1.1) ────────────────────────────────────────────────
# Stage-3 emits a time_horizon per impact. Two measurements point OPPOSITE
# ways, so the route is a CLASSIFICATION, never a suppression:
#   - Materiality (next-close, direction-agnostic): "immediate" is the BEST
#     material-event flag (66.7% MP vs 57.4% for other labels, n=42).
#   - Tradeability (arrival-anchored, 1-min candles, 96 signals): "immediate"
#     is the WORST post-signal trade (30% in-direction at +15m, mean −0.13%)
#     — the move completes before entry; medium/long carry positive drift
#     (+0.28%/+0.64% at +15m).
# Route semantics: EVENT_ALERT = flag it, market already reacting (counts
# toward materiality precision; excluded from tradeable-signal claims).
# DRIFT = post-signal entry had positive measured expectancy. SHORT_TERM =
# weak drift evidence, kept and tracked separately.
ROUTE_EVENT_ALERT = "event_alert"
ROUTE_DRIFT = "drift"
ROUTE_SHORT_TERM = "short_term"


def horizon_route(time_horizon: Optional[str]) -> str:
    h = (time_horizon or "").strip().lower()
    if h == "immediate":
        return ROUTE_EVENT_ALERT
    if h in ("medium_term", "long_term"):
        return ROUTE_DRIFT
    return ROUTE_SHORT_TERM

# Policy actions
DENY = "deny"
RATIO_REQUIRED = "ratio_required"
ALWAYS_ELIGIBLE = "always_eligible"
PASS_THROUGH = "pass_through"

FILING_SOURCES = {"NSE_FILINGS", "BSE_FILINGS"}

# Coarse adapter categories that carry no subject information — the headline
# fallback runs for these.
_COARSE_CATEGORIES = {"", "CORPORATE_ANNOUNCEMENT", "BOARD_MEETING", None}


def normalize_filing_category(raw: Optional[str]) -> str:
    """Shared normalizer (mirrors the BSE adapter): upper-case, collapse
    non-alphanumerics to single underscores."""
    s = (raw or "").strip().upper()
    s = re.sub(r"[^A-Z0-9]+", "_", s).strip("_")
    return s or "CORPORATE_ANNOUNCEMENT"


_SUBJECT_RE = re.compile(
    r"has\s+informed\s+the\s+Exchange\s+(?:about|regarding)\s*[:\-']?\s*(.+)", re.I
)


def headline_subject(headline: str) -> Optional[str]:
    """Extract the filing subject from an NSE-style headline. None when the
    headline doesn't follow the '... has informed the Exchange about X' shape."""
    m = _SUBJECT_RE.search(headline or "")
    if not m:
        return None
    return m.group(1).strip().strip('."')


# ── Policy tables (ordered; first match wins) ────────────────────────────
# Patterns run against the NORMALIZED category/subject (upper, underscored).

_DOWNGRADE_KW_RE = re.compile(r"downgrad|negative|default|watch|withdraw", re.I)

# (rule_name, regex on normalized text, action)
_RULES: List[Tuple[str, re.Pattern, str]] = [
    # ---- RATIO_REQUIRED: order/contract awards need a size test -----------
    ("order_award", re.compile(
        r"BAGGING|RECEIVING_OF_ORDER|AWARDING_OF_ORDER|RECEIPT_OF_ORDER"
        r"|WORK_ORDER|LETTER_OF_INTENT|\bLOI\b|ORDER_S_CONTRACT|AWARD_OF_ORDER"
    ), RATIO_REQUIRED),

    # ---- ALWAYS_ELIGIBLE: material by construction -------------------------
    ("results", re.compile(
        r"FINANCIAL_RESULT|RESULTS?_PRESS|OUTCOME_OF_BOARD_MEETING|INTEGRATED_FILING"
    ), ALWAYS_ELIGIBLE),
    ("press_release", re.compile(r"PRESS_RELEASE"), ALWAYS_ELIGIBLE),
    ("m_and_a", re.compile(
        r"ACQUISITION|AMALGAMATION|MERGER|DEMERGER|DIVESTMENT|SCHEME_OF_ARRANGEMENT|SLUMP_SALE"
    ), ALWAYS_ELIGIBLE),
    ("guidance", re.compile(r"GUIDANCE"), ALWAYS_ELIGIBLE),
    ("distress_regulatory", re.compile(
        r"INSOLVENCY|DEFAULT|SEBI_ORDER|PENALT|CCI_|SHOW_CAUSE|FRAUD"
    ), ALWAYS_ELIGIBLE),
    ("capital_return", re.compile(r"BUY_?BACK|DELISTING|OPEN_OFFER"), ALWAYS_ELIGIBLE),
    ("disruption", re.compile(
        r"SHUT_?DOWN|PLANT_CLOSURE|FIRE|ACCIDENT|DISRUPTION_OF_OPERATION|STRIKE|LOCKOUT"
    ), ALWAYS_ELIGIBLE),

    # ---- DENY: no quantifiable economics -----------------------------------
    ("general_updates", re.compile(r"GENERAL_UPDATES?"), DENY),
    ("monthly_business_update", re.compile(r"MONTHLY_BUSINESS_UPDATE"), DENY),
    ("presentation_meet", re.compile(
        r"INVESTOR_PRESENTATION|ANALYSTS?_.*MEET|INVESTOR_MEET|EARNINGS_CALL|CONFERENCE_CALL"
        r"|^PRESENTATION$|TRANSCRIPT"
    ), DENY),
    # credit rating: denied UNLESS the headline carries downgrade keywords —
    # handled specially in classify() so the exception sees the raw headline.
    ("credit_rating", re.compile(r"CREDIT_RATING"), DENY),
    ("routine_compliance", re.compile(
        r"COMPLIANCE_WITH_REGULATION|CERTIFICATE_UNDER|REG_74_5|RECONCILIATION_OF_SHARE"
        r"|NEWSPAPER_PUBLICATION|COPY_OF_NEWSPAPER"
    ), DENY),
    ("calendar_admin", re.compile(
        r"TRADING_WINDOW|BOOK_CLOSURE|RECORD_DATE|^AGM(_|$)|^EGM(_|$)|ANNUAL_GENERAL_MEETING"
        r"|EXTRA_ORDINARY_GENERAL_MEETING|POSTAL_BALLOT|BOARD_MEETING_INTIMATION"
        r"|NOTICE_OF_.*MEETING|PROCEEDINGS_OF"
    ), DENY),
    ("dilution_admin", re.compile(
        r"ALLOTMENT|ESOP|PREFERENTIAL_ISSUE|WARRANT|FUND_RAISING|RAISING_OF_FUNDS"
    ), DENY),
    ("share_certificates", re.compile(
        r"LOSS_OF_SHARE_CERTIFICATE|DUPLICATE_SHARE|LETTER_OF_CONFIRMATION"
    ), DENY),
]


# ── Feed-noise filter (distinct from signal policy) ───────────────────────
# Suppresses ONLY zero-news-value administrative filings from the feed. This
# is DELIBERATELY NARROWER than the signal DENY list: a credit-rating change,
# an investor presentation, or a management change is a poor *trade signal*
# but is legitimate *news* worth showing. We only hide filings that carry no
# information for a human analyst: routine compliance certificates, trading-
# window notices, newspaper-ad copies, share-certificate admin, and board-
# meeting INTIMATIONS (the scheduled-meeting notice — the OUTCOME is kept).
# "No room for error" applies inverted here: prefer leaving noise over hiding
# a material filing, so every pattern is anchored to unambiguous admin text.
_FEED_NOISE_RE = re.compile(
    r"certificate\s+under\s+reg"
    r"|certificate\s+under\s+sebi\s*\(?\s*depositor"   # NSE wording of the Reg 74(5) DP cert
    r"|under\s+reg(?:ulation)?\.?\s*74"
    r"|reg(?:ulation)?\.?\s*7\s*\(?\s*3"
    r"|reg(?:ulation)?\.?\s*40\s*\(?\s*(?:9|10)"
    r"|reg(?:ulation)?\.?\s*57\s*\(?\s*5"
    r"|reconciliation\s+of\s+share\s+capital"
    r"|compliance\s+with\s+regulation\s+30"          # bond-interest / LODR compliance CERTS
    r"|compliance\s+certificate"
    r"|trading\s+window"
    r"|newspaper\s+(?:publication|advertisement|clipping|copy)"
    r"|copy\s+of\s+newspaper"
    r"|(?:loss|duplicate)\s+of?\s+share\s+certificate"
    r"|letter\s+of\s+confirmation"
    r"|board\s+meeting\s+intimation"
    r"|intimation\s+of\s+board\s+meeting"
    # investor/analyst-meet scheduling notices (not the substance)
    r"|schedule\s+of\s+(?:analyst|institutional|investor|meet)"
    r"|schedule\s+of\s+meet"
    r"|cancellation\s+of\s+schedule\s+of"
    # meeting-notice admin (OUTCOMES/voting results kept via _FEED_KEEP_RE)
    r"|notice\s+of\s+postal\s+ballot"
    r"|notice\s+of\s+(?:annual\s+general|extra[\s-]?ordinary\s+general|e?gm)"
    # exchange surveillance queries (exchange-initiated, not company news)
    r"|(?:sought|seeking)\s+clarification|clarification\s+sought"
    r"|clarification\s+(?:on|of)\s+price\s+movement"
    r"|reply\s+and\s+clarification\s+on\s+price"
    r"|significant\s+(?:increase|movement|price).{0,20}(?:volume|price|movement|observed)"
    r"|members\s+of\s+the\s+exchange\s+are\s+hereby"
    # routine allotments — ESOP exercise / small share issues (preferential /
    # QIP / rights capital raises are kept via _FEED_KEEP_RE)
    r"|allotment\s+of\s+[\d,]+\s+(?:equity\s+)?(?:shares|securities)"
    r"|allotment.{0,40}(?:esop|employee\s+stock|e\s?sops)",
    re.I,
)
# Guard: never suppress a filing whose text carries a MATERIAL signal, even if
# an admin pattern also matched. Terms are SPECIFIC phrasings — bare common
# words (order, contract, award, split, default) are avoided because they
# false-match exchange-surveillance boilerplate like "in order to ensure…" or
# "Financial Results not submitted", which would keep noise. "No room for
# error" cuts both ways: this must catch real material filings without being
# tripped by incidental vocabulary.
_FEED_KEEP_RE = re.compile(
    r"outcome\s+of\s+(?:board|e?gm|agm|postal)"
    r"|(?:receipt|bagg(?:ed|ing)|award(?:ed|ing)?|winning|won|execution|secured?"
    r"|new|fresh)\s+(?:of\s+)?(?:an?\s+)?(?:order|contract|work\s*order|loa|loi|project)"
    r"|order.{0,15}(?:worth|valued|aggregat|rs\.?\s*\d|inr|₹|crore)"
    r"|acqui|merger|amalgamat|demerger|slump\s+sale|divest"
    r"|buy\s?back|open\s+offer|delisting"
    r"|fund\s+rais|preferential\s+(?:basis|issue|allotment)|qualified\s+institution"
    r"|\bqip\b|rights\s+issue"
    r"|dividend|bonus\s+issue|stock\s+split|sub-?division\s+of"
    r"|resignation\s+of|appointment\s+of|change\s+in\s+(?:management|director|kmp|cfo|ceo|md|key\s+manage)"
    r"|credit\s+rating|rating\s+(?:revision|downgrade|upgrade|action|reaffirm|assigned)"
    r"|insolvency|nclt|resolution\s+plan|winding\s+up"
    r"|revision\s+in\s+guidance|profit\s+guidance"
    r"|voting\s+result",
    re.I,
)


def is_feed_noise(source: str, category: Optional[str], headline: str) -> bool:
    """True if this filing is administrative noise that should NOT reach the
    feed. Filing sources only; fail-safe (any doubt → keep = not noise)."""
    if source not in FILING_SOURCES:
        return False
    text = f"{category or ''} {headline or ''}"
    if _FEED_KEEP_RE.search(text):
        return False
    return bool(_FEED_NOISE_RE.search(text))


@dataclass(frozen=True)
class PolicyDecision:
    action: str            # deny | ratio_required | always_eligible | pass_through
    rule: str              # matched rule name, or "default"
    category: str          # the normalized category the decision keyed on
    matched_on: str        # "category" | "headline_subject" | "none"
    gate_version: str = GATE_VERSION


def classify(source: str, category: Optional[str], headline: str) -> PolicyDecision:
    """Classify a filing for signal eligibility. Non-filing sources always
    pass through (this policy is exchange-filings-only)."""
    if source not in FILING_SOURCES:
        return PolicyDecision(PASS_THROUGH, "non_filing_source",
                              normalize_filing_category(category), "none")

    # Board-meeting *intimations* (the adapter's BOARD_MEETING category) are
    # calendar admin — denied before the coarse-category fallback below, which
    # would otherwise reclassify them off the headline. Meeting OUTCOMES
    # arrive as announcements and match the results rule.
    if normalize_filing_category(category) == "BOARD_MEETING":
        return PolicyDecision(DENY, "calendar_admin", "BOARD_MEETING", "category")

    # Prefer the structured fine-grained category; fall back to the headline
    # subject for coarse/legacy rows (this is what the retro-backtest uses).
    matched_on = "none"
    norm = normalize_filing_category(category) if category else ""
    if norm in {normalize_filing_category(c) for c in _COARSE_CATEGORIES if c}or not norm:
        subject = headline_subject(headline)
        if subject:
            norm = normalize_filing_category(subject)
            matched_on = "headline_subject"
        else:
            # No structured category and no recognizable subject phrase:
            # treat the whole headline as the classification text.
            norm = normalize_filing_category(headline)
            matched_on = "headline_full"
    else:
        matched_on = "category"

    for rule, pat, action in _RULES:
        if pat.search(norm):
            if rule == "credit_rating" and _DOWNGRADE_KW_RE.search(headline or ""):
                return PolicyDecision(ALWAYS_ELIGIBLE, "credit_rating_downgrade",
                                      norm, matched_on)
            return PolicyDecision(action, rule, norm, matched_on)

    return PolicyDecision(PASS_THROUGH, "default", norm, matched_on)
