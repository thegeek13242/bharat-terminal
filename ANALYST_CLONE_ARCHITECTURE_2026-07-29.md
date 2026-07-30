# The Analyst Clone — Agentic Filing-Intelligence Architecture

Drafted 2026-07-29. Successor to SPECIALIST_TREE_ARCHITECTURE_2026-07-28.md
(§8 verdict + §9 trade-space priorities remain binding; this document adds
the AGENTIC layer the founder directed: "a clone of a fundamental analyst
who can take decisions on its own... LLMs are not good at working on these
problems directly, so we provide all the tools... a limited number of
things can happen to a company, so a specialized agent with tools for each").
Every number below is measured (session queries or the two research
workflows, wf_89dcc928 internal / wf_9c78e2d9 web-authoritative); analytical
priors are marked as such.

## 0. The organizing principle

The MAHSCOOTER loss is the design's founding exhibit: the classifier read
"revenue −81%" literally; two tool calls (company profile: mcap/revenue
45×, taxonomy nbfc-CIC; 8-quarter history: 29→271→6→6 Cr dividend lumping)
falsify the read deterministically. **No prompt fixes this class; tools
do.** The clone therefore: (1) routes every filing through three axes —
EVENT × COMPANY-TYPE × MARKET-STATE; (2) escalates the consequential
minority to a specialist agent that investigates with tools before
judging; (3) keeps the §8.2 inversion — agents produce EVIDENCE and typed
verdicts; the calibrated statistical layer still owns P(material),
direction and size. An agent is a better witness, not a licensed judge.

## 1. Axis 1 — EVENTS: SEBI's enumeration is the router's authority

SEBI LODR Reg 30 + Schedule III (2023 amendment, circular
SEBI/HO/CFD/CFD-PoD-1/P/CIR/2023/123) enumerates what can happen:

- **Para A: ~26 DEEMED-material leaves** (no discretion): acquisitions/
  schemes, security issuance/forfeiture/buyback, rating actions incl.
  outlook-only, board-meeting outcomes (results/dividend/buyback/raise/
  bonus/delisting — 30-minute disclosure rule), binding non-course
  agreements incl. shadow/promoter agreements (30A), fraud/defaults by
  promoters-KMP, arrests, forensic audits, one-time settlements, CIRP.
  **Rule: the magnitude model may never auto-dismiss a Para-A leaf** —
  regulation says it is material; our job is direction and size only.
- **Para B: threshold-tested leaves** (orders, capacity, litigation...)
  with the 2023 QUANTITATIVE test: lower of 2% of turnover, 2% of net
  worth, 5% of 3-yr-avg |PAT| — **all three denominators computable per
  company from financial_results today**. Every extracted quantum gets a
  deterministic materiality ratio before any model runs.
- **The disclosure clock is provenance**: 30-min = board decision;
  12h = internal event; 24h = external (rating/court/regulator). Free
  router feature.

Our 22-bucket regex taxonomy maps onto this with quantified holes
(60d flow = 15,152 hop-0 impacts, ~252/day): 17% of order_win is
court/tax orders; 85 auditor events buried in mgmt_change (1,196);
218 regulatory APPROVALS own no bucket; 44 default/restructuring/OTS
events route to 'other'; fund_raise merges QIP/rights/NCD/ESOP — four
mechanisms in the system's worst cell (AUC 0.487); 122 subsidiary-
housekeeping rows pollute the 2nd-largest, never-measured bucket
(acquisition_stake, 1,692). The full 12-node ontology (mechanism,
asymmetry, horizon, tool needs per node) lives in the research output;
its ranked agent-value list drives §4.

## 2. Axis 2 — COMPANY TYPE: primary regime + overlays, detectors measured

Structure (from the research): a mutually-exclusive PRIMARY axis — the
accounting regime that defines what the P&L means — plus non-exclusive
OVERLAYS. Detector precision measured against the live universe (3,998
actives):

| type | detector | count | verified precision |
|---|---|---|---|
| bank | financial_results.taxonomy='banking' | 42 | 41/42 |
| insurer | taxonomy life/general | 11 | 11/11 |
| NBFC-lender | taxonomy='nbfc' minus subsplits | ~261 | spot-checked |
| AMC / broker | name rules inside 'nbfc' | 6 / 29 | checked |
| **investment holdco** | FY dividend-share >0.5 of income (raw_xbrl) → 12/12 true (MAHSCOOTER .91); + mcap/rev>20 inside nbfc (~14 true); + name+ratio for non-nbfc | **~29-31** | ~0.85 |
| consolidating holdco | body self-declaration "Core Investment Company" (the low-ratio class: CHOLAHLDNG 0.8×, TVSHLTD 0.4×) | 22 syms candidate | ~0.65, tightenable |
| PSU | govt named-holders ≥26% (investor_holdings; excludes GIC Singapore trap) | 75 | top-25 all true |
| distress-attached | ED/liquidator/DRT named holders | 14 | high |
| suspended/shell | no price row >30d | 55 (+83 no prices) | sample all true |
| circuit-prone illiquid | high=low on ≥30% of 90d | 47 | top names true |
| loss-making (growth) | pat_cr<0 (+TTM rev +20%) | 323 (62) | — |
| recently-listed | listing_date cleaned of the 104-row 2026-04-20 backfill artifact | ~152-281 | — |

Known blind spots, stated loudly: **REITs/InvITs — zero coverage** (not
in the universe at all); **SME boards — zero coverage** (never ingested);
**WESTLIFE files taxonomy='nbfc'** (a QSR! — taxonomy is an input, never
ground truth); 403 symbols have taxonomy flips across history; ~125 real
companies (3.1%) reachable by no detector (default: operating,
low-confidence); 243 "active companies" are mutual-fund schemes polluting
the universe (hygiene fix required). Quarterly dividend-share is UNSTABLE
by construction (the MAHSCOOTER lumping itself) — type detection always
uses FY/trailing-4Q sums.

**Build item #0 (cheapest accuracy in the system): a derived
`company_type` + `type_overlays` on companies**, refreshed weekly from
these detectors, surfaced in the profile tool, consumed by every agent
and by the router. One migration; data already on hand.

## 3. Axis 3 — MARKET STATE: surveillance and bands gate everything

From the authoritative research (NSE FAQs + live-probed endpoints):
- **GSM stages 0–IV**: Stage III trades once a WEEK; Stage IV PROHIBITS
  upward movement. **A filing on a GSM-III/IV name cannot show a normal
  reaction by exchange design** — such events must be excluded from
  reaction labels (a measurable slice of our "no reaction" class is this
  artifact) and from paper fills.
- **ASM LT I–IV / ST I–II, ESM I–II**: margin/band/T4T restrictions that
  suppress reaction and gate tradeability (ESM-II settles by periodic
  call auction).
- **Price bands** (2/5/10/20%, 'No Band' = derivatives-linked): band-lock
  detection (the fiction-fill problem) and the authoritative F&O flag.
- **Feeds, all probed HTTP-200 from this VM on 2026-07-29**: NSE api
  reportASM / reportGSM / reportESM (JSON, staged, same-day) + daily
  sec_list.csv price-band file. One small ingestion job gives the clone
  its state axis, fixes a label-corruption class, and hardens execution.

## 4. The agents (tiered, tool-equipped)

**Escalation tiers** (cost follows judgment need):
- **T0 — router + suppression** (deterministic): three-axis routing;
  procedural mass (27.6% of flow) suppressed or content-sniff re-routed;
  Para-A floor applied; SEBI quantitative ratio computed in code.
- **T1 — single-call node prompt** (today's tree): routine leaves where
  the gate already performs (corp_action 0.899, regulation_generic,
  routine mgmt churn) — same cost as today.
- **T2 — ANALYST AGENT** (multi-turn tool loop on the chat-engine
  chassis): Para-A leaves, results + re-route family, M&A, fund-raise,
  governance red flags, credit non-affirmations, litigation/insolvency,
  and any T1 output that conflicts with the deterministic ratio.
  Estimated 15–25% of flow ≈ 40–60 filings/day; at 4–8 tool calls ≈
  30–60s and ~$0.03–0.08/filing (flash tier) ≈ **$2–5/day** — well inside
  the reaction window and budget.

**Agent contract** (uniform): first call is ALWAYS the company profile
(type + overlays + state — the #0 axis); tool trace is logged in full
(the audit trail is also the PIT record for replay and the training
exhaust); output = ImpactReport + typed extensions + `agent_version`;
verdicts feed the calibrated layer. `web_search` permitted, flagged
non-PIT for replay scoring.

**Rosters** (existing tools cover most of R1–R3; full mapping in the
research output): results-agent → get_financials(8q, taxonomy,
sector_financials), get_guidance, peer-SUE, get_price_returns(run-up),
profile; holdco overlay → route analysis to underlying group cos, NAV
lens; fade-family reader → get_recent_impacts diff ("new quantified facts
or adjectives?"); fund-raise → shareholding/pledge + mcap + price-vs-
issue math in code; M&A → consideration/mcap + related-party via named
holders; governance splitter → letter reading + auditor/pledge history;
credit → notch/scope parsing (+ rating-history store, a named gap).

**Build order** (R-ranking × S0 trade-space): #0 company-type column +
state-feed ingestion → R1 results-agent (with bank/NBFC/insurer/holdco
overlays; objective = materiality + hard-wrong hygiene + horizon, NOT
long-side direction) → R2 fade-family reader (the −7 to −11pp anti-signal
mass, F&O-constrained short candidate) → R3 fund-raise extraction-first →
R4 M&A bucket-cleaning + deal math → R5 governance splitter → R7
litigation mandatory-read gate. Demoted: order_win (gap-dominated),
rating (pair with agency-site latency crawl or defer), corp_action (no
agent needed).

## 5. Evaluation (unchanged discipline, extended to agents)

Extraction fidelity first (~100 hand-audited filings per type;
deterministic, iterate freely); outcome scoring on CAPTURABLE labels via
replay with PIT-safe tool bundles (grounding logged at decision time
going forward; historical replay reconstructs as-of or excludes);
§8.3 power rules (bootstrap CIs, minimum-positives floors, one frozen
confirmatory shot); protect-list non-inferiority on strategy populations.
GSM/ASM-state exclusions applied to labels retroactively once the feeds
land. Every agent is falsifiable before it routes live traffic.

## 6. New data asks surfaced (ranked)

1. company_type derived column (#0 — migration + weekly refresh job).
2. ASM/GSM/ESM/price-band ingestion (labels + execution + state axis).
3. Universe hygiene: flag the 243 fund-scheme rows; decide REIT/InvIT
   and SME onboarding (currently invisible asset classes).
4. Rating-history table (notch-delta is unknowable today).
5. Body-sniff for Regulation-30-wrapped penalties/approvals (218
   approvals + hidden penalties; E1 proved body text carries signal).
6. Auditor identity/history store (the 85-events-per-60d red-flag class).
7. Order-book/capex/monthly-volume series — accrue as agent-extraction
   exhaust (§6.4 pattern), never purchasable.

## 7. Phasing

- **Phase 1 (foundation)**: #0 migration + detectors job; state feeds;
  router v2 (three axes + Para-A floor + SEBI ratios); universe hygiene.
- **Phase 2 (first agent)**: results-agent behind the escalation tier,
  validated on extraction fidelity → replay → shadow provenance;
  MAHSCOOTER-class regression test in its eval set.
- **Phase 3**: fade-family reader + fund-raise extractor; label
  re-scoring under state exclusions; paper-trader consumes agent
  provenance (same ImpactReport — no downstream change).
- **Phase 4**: M&A/governance/litigation agents; rating crawl decision;
  November label-doubling re-runs.
Each phase: working tree → evidence → per-instance deploy approval.
