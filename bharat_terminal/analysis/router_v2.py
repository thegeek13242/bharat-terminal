"""Router v2 — the three-axis routing layer (EVENT × COMPANY-TYPE × MARKET-STATE).

Implements Phase-1 "router v2" of ANALYST_CLONE_ARCHITECTURE_2026-07-29.md
(§1 events / Para-A, §2 company types, §3 market state, §4 tier T0), under the
gate-coupling and power rules of SPECIALIST_TREE_ARCHITECTURE_2026-07-28.md §8
— which is why the module is split into two activation tiers:

TIER A — always on (deterministic safety + strictly-additive factual context;
changes no rubric text and no judgment prompts):
  * route(): three-axis RouteDecision per (event, company).
  * Suppression gates in code: fund-scheme rows (not equities) and
    unknown-type + suspended-overlay names never reach the impact LLM.
    GSM III+/ESM-II do NOT suppress classification — they only set
    state_flags (reaction suppressed by exchange rules is a downstream /
    trader concern, and a label-corruption class for outcome scoring).
  * Para-A detector: SEBI LODR Reg 30 Schedule III Para A deemed-material
    leaves. Consumed by stage-3 as a MAGNITUDE FLOOR (never a sentiment
    change): regulation says material; the model may not dismiss it.
  * SEBI quantitative context + holdco factual hint lines for the stage-3
    prompt context block (facts about the company, not rubric overrides).

TIER B — behind ROUTER_V2_RUBRICS (default '0', OFF until replay-validated
per §8.3): taxonomy-v3 buckets drive scenario-rubric selection (contamination
fixes: court/tax/SEBI orders out of order_win, ESOP/NCD out of fund_raise,
results-content wrappers re-routed to the results rubric with a genre note,
holdco results addendum, classification-preferred lender addendum). With the
flag OFF, scenario selection behaves exactly as today.

Pure functions, no I/O — unit-testable without infra. The classification /
market_state inputs are the KB /company payload's "classification" and
"market_state" objects (kb/models.py Company.to_dict + kb/api.py; both are
None/empty until migration 033's sync jobs run — every path here fail-softs
to "no axis data" behaviour).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

ROUTER_VERSION = "v2"

# Company types whose results statements follow the lender (bank/NBFC) format.
LENDER_TYPES = ("bank", "nbfc_lender")

PARA_A_MAGNITUDE_FLOOR = 3


def rubrics_enabled() -> bool:
    """TIER-B switch. Read at call time (not import time) so tests and the
    replay harness can flip it per-run."""
    return os.getenv("ROUTER_V2_RUBRICS", "0") == "1"


# ═══════════════════════════════════════════════════════════════════════════
# Axis 1 — EVENTS: taxonomy v3 (22 legacy buckets + measured contamination
# fixes from ANALYST_CLONE §1: 17% of order_win is court/tax orders; 85
# auditor events buried in mgmt_change; 218 approvals own no bucket; 44
# default/OTS events route to 'other'; fund_raise merges QIP/NCD/ESOP).
# ═══════════════════════════════════════════════════════════════════════════

# Legacy 16-bucket list — copied VERBATIM from
# research/materiality_calibrator.py::_EVENT_TYPES (substring, first match
# wins). Inlined rather than imported so the analysis image never depends on
# the research package and the router stays deterministic if research code
# moves. Keep in sync when the calibrator taxonomy changes.
_LEGACY_EVENT_TYPES: List[Tuple[str, Tuple[str, ...]]] = [
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

# Extra buckets — copied VERBATIM from
# research/materiality_lab.py::_EXTRA_EVENT_TYPES (checked after the 16).
_EXTRA_EVENT_TYPES: List[Tuple[str, Tuple[str, ...]]] = [
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

# High-precision procedural buckets that keep first claim on a headline even
# before the v3 contamination fixes run (a results filing whose headline
# happens to also say "penalty" must stay results).
_PRE_V3_BUCKETS = ("results", "transcript", "concall_invite", "investor_meet")

# v3 contamination fixes — regex, first match wins, checked BEFORE the
# contaminated legacy buckets (order_win / mgmt_change / fund_raise / other).
_LIT_REG_RE = re.compile(
    r"(?:order|judgm?ent|ruling|direction)s?\s+(?:of|from|passed|issued|received|pronounced)\s+"
    r"(?:by\s+)?(?:the\s+)?(?:hon'?ble\s+)?(?:supreme\s+court|high\s+court|court|tribunal|"
    r"nclt|nclat|itat|cestat|drt|sebi|arbitr\w+)"
    r"|(?:supreme\s+court|high\s+court|\bcourt\b|tribunal|\bnclt\b|\bnclat\b|\bitat\b|"
    r"\bcestat\b|\bdrt\b|\bsebi\b|arbitral|arbitration)\b.{0,60}\border"
    r"|\border\b.{0,60}\b(?:supreme\s+court|high\s+court|court|tribunal|nclt|nclat|itat|cestat|sebi)\b"
    # NSE/BSE Reg-30 templates for regulatory/tax orders (measured misses):
    # "Action(s) taken or orders passed by ...", "order passed by Additional
    # Commissioner (Appeals)", "Rectification Order No. ...", "Appeal
    # Order-in-Original from Commissioner of CGST".
    r"|orders?\s+passed\s+(?:by|under|against)"
    r"|action\(?s?\)?\s+taken\s+or\s+orders?\s+passed"
    r"|rectification\s+order|order[-\s]in[-\s]original|appeal\s+order"
    r"|appellate\s+(?:tribunal|authority|order)"
    r"|orders?\b.{0,80}\bcommissioner\b|\bcommissioner\b.{0,80}\borders?\b"
    r"|\bpenalt(?:y|ies)\b"
    r"|show[-\s]cause\s+notice"
    r"|(?:assessment|adjudication|demand)\s+order"
    r"|(?:gst|income[-\s]?tax|customs|excise)\s+(?:\w+\s+){0,2}(?:order|demand|penalt)",
    re.I)
# "Resignation of M/s B S R & Co. LLP as Joint Statutory Auditors" — the
# auditor-firm name (punctuation, ampersands) sits between the verb and the
# word "auditor", so the bridge is a bounded any-char gap, not word tokens.
_GOVERNANCE_RE = re.compile(
    r"resignation\s+of\s+.{0,80}?auditors?\b"
    r"|auditors?\b.{0,40}resign"
    r"|resign\w*\s+as\s+.{0,60}?auditors?\b"
    r"|qualified\s+opinion|audit\s+qualification|adverse\s+opinion"
    r"|disclaimer\s+of\s+opinion|forensic\s+(?:audit|review)",
    re.I)
_APPROVAL_RE = re.compile(
    r"\busfda\b|\bus[-\s]fda\b|\bfda\b|\beir\b|establishment\s+inspection\s+report"
    r"|\banda\b|abbreviated\s+new\s+drug|\bdcgi\b|\bcdsco\b|who[-\s]gmp"
    r"|gmp\s+certificat\w+|marketing\s+authori[sz]ation"
    r"|(?:final|tentative)\s+approval"
    r"|(?:approval|licen[cs]e|registration|authori[sz]ation)\s+(?:received|granted|obtained)"
    r"|receipt\s+of\s+(?:approval|licen[cs]e)"
    r"|grant\s+of\s+(?:approval|licen[cs]e|registration)"
    r"|environmental\s+clearance",
    re.I)
_CREDIT_EVENT_RE = re.compile(
    r"default\s+(?:in|on)\s+(?:payment|repayment|interest|principal|servicing|redemption)"
    r"|payment\s+default|default\s+in\s+(?:the\s+)?(?:payment|servicing)"
    r"|one[-\s]time\s+settlement|\bots\b"
    r"|debt\s+restructur\w+|restructuring\s+of\s+(?:debt|loans?|borrowings?)"
    r"|(?:debt|loan)\s+resolution\s+plan",
    re.I)
_ESOP_RE = re.compile(
    r"\besops?\b|employee\s+stock\s+option|stock\s+option\s+(?:scheme|plan)"
    r"|\besps\b|employee\s+share\s+purchase",
    re.I)
_DEBT_ISSUE_RE = re.compile(
    # "non convertible" standalone covers the NSE template "allotment of N
    # securities pursuant to Non Convertible Securities" (measured misses).
    r"non[-\s]convertible\b|\bncds?\b|commercial\s+paper"
    r"|(?:issue|issuance|allotment|redemption)\s+of\s+(?:\w+\s+){0,3}(?:bonds?|debentures?)\b"
    r"|\bbonds?\s+(?:issue|issuance|allotment)",
    re.I)

_V3_PRE_BUCKETS: List[Tuple[str, re.Pattern]] = [
    ("litigation_regulatory", _LIT_REG_RE),
    ("governance_redflag", _GOVERNANCE_RE),
    ("regulatory_approval", _APPROVAL_RE),
    ("credit_event", _CREDIT_EVENT_RE),
    ("esop_allotment", _ESOP_RE),
    ("debt_issue", _DEBT_ISSUE_RE),
]

# Subsidiary housekeeping inside acquisition_stake (122 rows/60d measured):
# incorporations, name changes, strike-offs, dissolutions of subsidiaries.
_SUBSIDIARY_HOUSEKEEPING_RE = re.compile(
    r"incorporation\s+of\s+(?:a\s+)?(?:new\s+)?(?:wholly[-\s]owned\s+)?(?:step[-\s]down\s+)?subsidiary"
    r"|(?:striking|struck|strike)[-\s]off"
    r"|dissolution\s+of\s+(?:\w+\s+){0,4}subsidiary"
    r"|winding[-\s]up\s+of\s+(?:\w+\s+){0,4}subsidiary"
    r"|(?:voluntary\s+)?liquidation\s+of\s+(?:\w+\s+){0,4}subsidiary"
    r"|change\s+(?:of|in)\s+name\s+of\s+(?:\w+\s+){0,4}subsidiary"
    r"|cessation\s+of\s+(?:\w+\s+){0,4}subsidiary",
    re.I)

# Results-content sniff: the P&L-table signature (same signature
# scenario_rubrics' body gate uses) inside a procedural wrapper genre. Genre
# is detected from the HEADLINE PHRASE, not only the bucket: the BSE template
# "Announcement under Regulation 30 (LODR)-Investor Presentation" buckets as
# regulation_generic (measured), so a bucket-only sniff misses it.
_RESULTS_CONTENT_RE = re.compile(
    r"statement\s+of\s+(?:un)?audited.{0,60}financial\s+results"
    r"|revenue\s+from\s+operations",
    re.I)
_SNIFF_WRAPPER_BUCKETS = ("investor_presentation", "press_release", "business_update")
_SNIFF_GENRE_RE = re.compile(
    r"investor\s+presentation|press\s+release|media\s+release|business\s+update"
    r"|earnings\s+presentation",
    re.I)
# Buckets that already own results content — sniff adds nothing there.
_SNIFF_EXCLUDE_BUCKETS = ("results", "transcript", "concall_invite")
_SNIFF_BODY_CHARS = 40000


def classify_event(headline: str, body: str = "") -> Tuple[str, List[str]]:
    """Taxonomy v3: (bucket, flags). Deterministic, headline-driven, with a
    bounded body sniff for results content hidden in procedural wrappers.

    Order: high-precision legacy buckets (results/transcript/concall/meet) →
    v3 contamination fixes → remaining legacy 11 → 6 extras → 'other'.
    """
    h = (headline or "").lower()
    flags: List[str] = []

    bucket = None
    for b, keys in _LEGACY_EVENT_TYPES[:4]:
        assert b in _PRE_V3_BUCKETS
        if any(k in h for k in keys):
            bucket = b
            break
    if bucket is None:
        for b, rx in _V3_PRE_BUCKETS:
            if rx.search(h):
                bucket = b
                break
    if bucket is None:
        for b, keys in _LEGACY_EVENT_TYPES[4:]:
            if any(k in h for k in keys):
                bucket = b
                break
    if bucket is None:
        for b, keys in _EXTRA_EVENT_TYPES:
            if any(k in h for k in keys):
                bucket = b
                break
    if bucket is None:
        bucket = "other"

    if bucket == "acquisition_stake" and _SUBSIDIARY_HOUSEKEEPING_RE.search(h):
        flags.append("subsidiary_housekeeping")
    is_wrapper = (bucket in _SNIFF_WRAPPER_BUCKETS
                  or (bucket not in _SNIFF_EXCLUDE_BUCKETS
                      and _SNIFF_GENRE_RE.search(h)))
    if is_wrapper and _RESULTS_CONTENT_RE.search((body or "")[:_SNIFF_BODY_CHARS]):
        flags.append("results_content_sniff")
    return bucket, flags


# ═══════════════════════════════════════════════════════════════════════════
# Para A — SEBI LODR Reg 30 Schedule III deemed-material leaves (no
# materiality discretion; the magnitude model may never auto-dismiss them).
# Detection is deliberately conservative: a missed leaf costs nothing (the
# model still judges); a false fire distorts a magnitude upward.
# ═══════════════════════════════════════════════════════════════════════════

# "To consider ..." board-meeting intimations are pre-decision: the Para-A
# leaf is the board DECISION (30-minute rule), so consideration notices must
# not trigger the decision-class leaves (buyback/dividend/delisting).
_CONSIDER_RE = re.compile(
    r"intimation|to\s+consider|proposal\s+to\s+consider|prior\s+intimation",
    re.I)
# "fraud" in a policy/prevention context is housekeeping, not a Para-A event.
_FRAUD_NEG_RE = re.compile(r"policy|framework|prevention|vigil\s+mechanism", re.I)
# Buyback PROCESS documents (post-buyback announcements, corrigenda, letters
# of offer, extinguishment/escrow/record-date steps) follow a decision that
# was already separately disclosed — the Para-A leaf is the DECISION only
# (measured 2026-07-30: 3 of 3 buyback fires in a 2,000-headline sample were
# process docs before this guard).
_BUYBACK_PROCESS_RE = re.compile(
    r"post[-\s]buy[-\s]?back|corrigendum|letter\s+of\s+offer|extinguishment"
    r"|escrow|record\s+date|public\s+announcement|dispatch",
    re.I)

_KMP_TERMS = (r"promoter|director|kmp|chairman|managing\s+director|\bmd\b|ceo|"
              r"cfo|whole[-\s]time|key\s+managerial")

# (leaf, regex, scan_body) — scan_body leaves also check the first
# _PARA_A_BODY_CHARS of the body (phrases precise enough to trust there).
_PARA_A_BODY_CHARS = 2000
_PARA_A_LEAVES: List[Tuple[str, re.Pattern, bool]] = [
    ("fraud_default_promoter_kmp", re.compile(
        r"\bfraud(?:ulent)?\b"
        r"|default\s+(?:in|on)\s+(?:the\s+)?(?:payment|repayment|interest|principal|debt|servicing|redemption)"
        r"|payment\s+default",
        re.I), False),
    ("arrest_kmp_promoter", re.compile(
        rf"\barrest(?:ed)?\b.{{0,80}}\b(?:{_KMP_TERMS})"
        rf"|(?:{_KMP_TERMS})\b.{{0,80}}\barrest(?:ed)?\b",
        re.I), True),
    ("forensic_audit", re.compile(
        r"forensic\s+(?:audit|review|investigation)", re.I), True),
    ("one_time_settlement", re.compile(
        r"one[-\s]time\s+settlement|\bots\b", re.I), False),
    # Headline-only (scan_body False): companies already under CIRP carry the
    # phrase as boilerplate in the body head of ROUTINE filings (measured
    # false fire: a newspaper-ad-of-results filing) — only a headline mention
    # marks the insolvency event itself.
    ("cirp_insolvency_admission", re.compile(
        r"corporate\s+insolvency\s+resolution|\bcirp\b"
        r"|insolvency\s+(?:petition|proceeding|application|resolution|commencement)"
        r"|(?:admission|admitted)\b.{0,60}\binsolvency"
        r"|insolvency\b.{0,60}\badmitted",
        re.I), False),
    ("auditor_resignation", re.compile(
        r"resignation\s+of\s+.{0,80}?auditors?\b"
        r"|auditors?\b.{0,40}resign"
        r"|resign\w*\s+as\s+.{0,60}?auditors?\b",
        re.I), False),
    # SEBI leaf is "revision in rating(s)" (incl. outlook-only, per the 2023
    # circular): upgrades/downgrades/watch/outlook changes fire; pure
    # reaffirmations are NOT revisions and stay with the model (deliberate —
    # flooring every reaffirmation to 3 would be a mass false-material class).
    ("rating_action", re.compile(
        r"(?:credit\s+)?ratings?\b.{0,60}\b(?:upgrade[ds]?|downgrade[ds]?|revis(?:ed|ion)|"
        r"watch|withdrawn|suspended)"
        r"|(?:upgrade[ds]?|downgrade[ds]?)\b.{0,40}\bratings?\b"
        r"|revision\s+(?:in|of)\s+(?:credit\s+)?ratings?"
        r"|outlook\s+revised",
        re.I), False),
    ("dividend_cancellation", re.compile(
        r"(?:cancel|withdraw|revok)\w*\s+(?:\w+\s+){0,4}dividend"
        r"|dividend\b.{0,40}\b(?:cancell?ed|withdrawn|revoked)",
        re.I), False),
    ("delisting_decision", re.compile(r"\bdelist", re.I), False),
    ("buyback_decision", re.compile(r"buy[-\s]?back", re.I), False),
    ("control_agreement_30a", re.compile(
        r"regulation\s*30a"
        r"|agreements?\b.{0,80}\b(?:impact|affect)\w*\s+(?:\w+\s+){0,3}(?:management|control)\b"
        r"|(?:shareholders?|promoters?)['’]?s?\s+agreement",
        re.I), False),
    ("license_regulatory_cancellation", re.compile(
        r"(?:cancellation|suspension|revocation|withdrawal|termination)\s+of\s+"
        r"(?:\w+\s+){0,5}(?:licen[cs]e|approval|registration|permit|authori[sz]ation)"
        r"|licen[cs]e\b.{0,50}\b(?:cancell?ed|suspended|revoked|withdrawn)",
        re.I), False),
]

# Leaves that describe a board DECISION — masked on consideration notices.
_DECISION_LEAVES = ("dividend_cancellation", "delisting_decision", "buyback_decision")


def detect_para_a(headline: str, body: str = "") -> Optional[str]:
    """First-matching Para-A leaf name, or None. Headline-first; a few
    high-precision phrases are also trusted in the body head."""
    h = headline or ""
    body_head = (body or "")[:_PARA_A_BODY_CHARS]
    consider = bool(_CONSIDER_RE.search(h))
    for leaf, rx, scan_body in _PARA_A_LEAVES:
        if consider and leaf in _DECISION_LEAVES:
            continue
        if leaf == "fraud_default_promoter_kmp" and _FRAUD_NEG_RE.search(h):
            continue
        if leaf == "buyback_decision" and _BUYBACK_PROCESS_RE.search(h):
            continue
        if rx.search(h) or (scan_body and rx.search(body_head)):
            return leaf
    return None


def apply_para_a_floor(magnitude: int, para_a: bool) -> Tuple[int, bool]:
    """The Para-A magnitude FLOOR (code-level, deterministic, auditable):
    regulation deems the event material, so the model may not return a
    below-material magnitude. Returns (magnitude, floor_applied). Sentiment
    is NEVER touched by this rule."""
    if para_a and magnitude < PARA_A_MAGNITUDE_FLOOR:
        return PARA_A_MAGNITUDE_FLOOR, True
    return magnitude, False


# ═══════════════════════════════════════════════════════════════════════════
# Axes 2 & 3 — company type + market state: suppression and state flags
# ═══════════════════════════════════════════════════════════════════════════

def _state_flags(market_state: Optional[dict]) -> List[str]:
    """Provenance flags for exchange-suppressed reaction regimes. These do
    NOT suppress classification (the event still gets judged) — they mark
    that no normal price reaction is possible by exchange design."""
    ms = market_state or {}
    flags: List[str] = []
    gsm = ms.get("gsm_stage")
    if gsm in ("III", "IV", "V", "VI"):
        flags.append(f"gsm_{gsm}_reaction_suppressed")
    if ms.get("esm_stage") == "II":
        flags.append("esm_II_call_auction")
    return flags


def _suppression(company_type: Optional[str], overlays: List[str],
                 is_fund_scheme: bool) -> Tuple[bool, Optional[str]]:
    """Code-level suppression gates (Tier A). Fund schemes are not equities;
    unknown-type suspended names have no analyzable regime AND no observable
    reaction — neither may reach the impact LLM."""
    if is_fund_scheme:
        return True, "fund_scheme"
    if company_type in (None, "unknown") and "suspended" in overlays:
        return True, "unknown_type_suspended"
    return False, None


@dataclass
class RouteDecision:
    """One (event, company) routing decision. `provenance` is the dict that
    goes into the report's router_meta."""
    event_bucket: str
    bucket_flags: List[str]
    para_a: bool
    para_a_leaf: Optional[str]
    company_type: Optional[str]
    type_overlays: List[str]
    is_fund_scheme: bool
    market_state: Optional[dict]
    state_flags: List[str]
    suppress: bool
    suppress_reason: Optional[str]
    provenance: Dict[str, Any] = field(default_factory=dict)


def route(headline: str, body: str,
          classification: Optional[dict],
          market_state: Optional[dict],
          filing_ts_ist: Optional[datetime] = None) -> RouteDecision:
    """Three-axis route for one (filing, company). All inputs fail-soft:
    classification/market_state None (KB down, pre-migration, unsynced)
    yields no suppression and empty axes — never an error."""
    cls = classification or {}
    company_type = cls.get("company_type")
    overlays = list(cls.get("type_overlays") or [])
    is_fund = bool(cls.get("is_fund_scheme"))

    bucket, bucket_flags = classify_event(headline, body)
    leaf = detect_para_a(headline, body)
    state_flags = _state_flags(market_state)
    suppress, reason = _suppression(company_type, overlays, is_fund)

    provenance = {
        "router_version": ROUTER_VERSION,
        "event_bucket": bucket,
        "bucket_flags": bucket_flags,
        "para_a": leaf is not None,
        "leaf": leaf,
        "company_type": company_type,
        "overlays": overlays,
        "state_flags": state_flags,
        "suppressed": suppress,
        "suppress_reason": reason,
        "filing_ts_ist": filing_ts_ist.isoformat() if filing_ts_ist else None,
    }
    return RouteDecision(
        event_bucket=bucket, bucket_flags=bucket_flags,
        para_a=leaf is not None, para_a_leaf=leaf,
        company_type=company_type, type_overlays=overlays,
        is_fund_scheme=is_fund, market_state=market_state,
        state_flags=state_flags, suppress=suppress, suppress_reason=reason,
        provenance=provenance,
    )


# ═══════════════════════════════════════════════════════════════════════════
# TIER A prompt context — factual lines, not rubric overrides
# ═══════════════════════════════════════════════════════════════════════════

# The MAHSCOOTER fix: facts about what a holdco P&L means. CONTEXT, not a
# judgment rule — ships ON.
HOLDCO_HINT = (
    "HOLDING COMPANY (CIC): market value derives from NAV of group holdings; "
    "standalone P&L is dominated by dividend-receipt timing and fair-value "
    "moves — a revenue/profit swing from dividend timing is not an operating "
    "change."
)


def holdco_hint(company_type: Optional[str]) -> str:
    return HOLDCO_HINT if company_type == "holdco" else ""


def _fmt_state(market_state: Optional[dict]) -> str:
    ms = market_state or {}
    bits = []
    if ms.get("asm_stage"):
        bits.append(f"ASM {ms['asm_stage']}")
    if ms.get("gsm_stage"):
        bits.append(f"GSM {ms['gsm_stage']}")
    if ms.get("esm_stage"):
        bits.append(f"ESM {ms['esm_stage']}")
    if ms.get("price_band"):
        bits.append(f"price band {ms['price_band']}%")
    return ", ".join(bits) if bits else "none"


def sebi_context_line(symbol: str,
                      financials: Optional[dict],
                      company_type: Optional[str],
                      type_overlays: Optional[List[str]],
                      market_state: Optional[dict]) -> str:
    """SEBI LODR Reg 30 quantitative-materiality context (2023 amendment:
    lower of 2% turnover / 2% net worth / 5% of 3-yr-avg |PAT|), computed
    from what the KB payload carries TODAY: TTM revenue and TTM PAT — the
    PAT leg is therefore an honest 1-yr approximation, marked as such.
    Returns '' when nothing at all is known (no fabricated context)."""
    fins = financials or {}
    rev = fins.get("revenue_ttm_cr")
    pat = fins.get("pat_cr")
    overlays = list(type_overlays or [])

    known_anything = any((
        isinstance(rev, (int, float)) and rev > 0,
        isinstance(pat, (int, float)) and pat != 0,
        company_type is not None,
        bool(market_state),
    ))
    if not known_anything:
        return ""

    if isinstance(rev, (int, float)) and rev > 0:
        rev_part = f"2% of TTM revenue = ₹{0.02 * rev:,.1f} Cr"
    else:
        rev_part = "2% of TTM revenue = n/a"
    if isinstance(pat, (int, float)) and pat != 0:
        pat_part = (f"5% of avg |PAT| = ₹{0.05 * abs(pat):,.1f} Cr "
                    f"(approx: TTM |PAT|, not 3-yr avg)")
    else:
        pat_part = "5% of avg |PAT| = n/a"
    type_part = company_type or "unknown"
    if overlays:
        type_part += " [" + ", ".join(str(o) for o in overlays) + "]"
    return (f"SEBI materiality context for {symbol}: {rev_part}; {pat_part}. "
            f"Company type: {type_part}. Market state: {_fmt_state(market_state)}.")


# ═══════════════════════════════════════════════════════════════════════════
# Provenance assembly (goes into ImpactReport.router_meta)
# ═══════════════════════════════════════════════════════════════════════════

def build_router_meta(route_decisions: Dict[str, RouteDecision],
                      scored_symbols: List[str],
                      rubric_scenario: Optional[str] = None) -> Dict[str, Any]:
    """Audit-traceable router provenance for one news item. Event-level
    fields come from any decision (they are headline/body-derived, identical
    across symbols); company axes are recorded per symbol."""
    syms = list(route_decisions.keys())
    primary = route_decisions[syms[0]] if syms else None
    meta: Dict[str, Any] = {
        "router_version": ROUTER_VERSION,
        "event_bucket": primary.event_bucket if primary else None,
        "bucket_flags": list(primary.bucket_flags) if primary else [],
        "para_a": bool(primary.para_a) if primary else False,
        "leaf": primary.para_a_leaf if primary else None,
        "rubrics_flag_on": rubrics_enabled(),
        "rubric_scenario": rubric_scenario,
        "para_a_floor_applied": False,  # flipped by stage-3 when the floor fires
        "suppressed": [s for s in syms if route_decisions[s].suppress],
        "scored": list(scored_symbols),
        "companies": {
            s: {
                "company_type": d.company_type,
                "overlays": d.type_overlays,
                "state_flags": d.state_flags,
                "suppressed": d.suppress,
                "suppress_reason": d.suppress_reason,
                "para_a_floor_applied": False,
                "gate_flags": [],
            }
            for s, d in route_decisions.items()
        },
    }
    return meta


def record_para_a_floor(meta: Dict[str, Any], symbol: str) -> None:
    """Stamp the 'para_a_floor' gate flag for one symbol into router meta."""
    meta["para_a_floor_applied"] = True
    comp = meta.get("companies", {}).get(symbol)
    if comp is not None:
        comp["para_a_floor_applied"] = True
        if "para_a_floor" not in comp["gate_flags"]:
            comp["gate_flags"].append("para_a_floor")


# ═══════════════════════════════════════════════════════════════════════════
# TIER B — taxonomy-v3-driven rubric selection (ROUTER_V2_RUBRICS=1 only)
# ═══════════════════════════════════════════════════════════════════════════

def scenario_v3(headline: str, body: str,
                decision: RouteDecision) -> Tuple[Optional[str], str]:
    """(scenario_key, genre_note) under taxonomy v3. MUST only be called with
    ROUTER_V2_RUBRICS on — the flag-off path in stage-3 never reaches here
    (assert-guarded so a wiring mistake fails loudly, not silently).

    Starts from today's detect_scenario and applies the MEASURED
    contamination fixes:
      * court/NCLT/tax/SEBI orders lose the order_win rubric;
      * ESOP allotments and NCD/debenture issues lose the fund_raise rubric;
      * results content inside presentation/press-release/business-update
        wrappers gains the results rubric, with a genre note.
    """
    assert rubrics_enabled(), (
        "router_v2.scenario_v3 called with ROUTER_V2_RUBRICS off — the "
        "flag-off path must use scenario_rubrics.detect_scenario unchanged")
    from bharat_terminal.analysis.scenario_rubrics import detect_scenario

    key = detect_scenario(headline or "", body or "")
    genre_note = ""
    if key == "order_win" and decision.event_bucket == "litigation_regulatory":
        key = None  # court/tax/regulatory order, not an order win
    elif key == "fund_raise" and decision.event_bucket in ("esop_allotment", "debt_issue"):
        key = None  # ESOP/plain-debt mechanics are not equity fund-raise dilution
    if key is None and "results_content_sniff" in decision.bucket_flags:
        key = "financial_results"
        genre_note = (
            f"\nGENRE NOTE: this filing is wrapped as a "
            f"{decision.event_bucket.replace('_', ' ')}, but its body carries a "
            f"results P&L table — apply the results rubric to that numbers "
            f"table, and note the promotional-wrapper genre when judging tone.\n")
    return key, genre_note


def scenario_block_v3(headline: str, body: str, decision: RouteDecision,
                      company_type: Optional[str] = None) -> Tuple[Optional[str], str]:
    """Full scenario prompt block under taxonomy v3 (Tier B): rubric + genre
    note + lender addendum (classification-preferred, body-signature
    fallback) + holdco results addendum. Returns (scenario_key, block)."""
    from bharat_terminal.analysis.scenario_rubrics import (
        rubric_block, lender_addendum, holdco_results_addendum,
    )
    key, genre_note = scenario_v3(headline, body, decision)
    if key is None:
        return None, ""
    block = rubric_block(key) + genre_note
    block += lender_addendum(key, body or "", company_type=company_type)
    block += holdco_results_addendum(key, company_type)
    return key, block
