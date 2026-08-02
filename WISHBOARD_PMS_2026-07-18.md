# PMS Wish Board — buyer-value framing (2026-07-18)

Working document for feature validation. The premise: the **PMS firm pays,
the analyst consumes**. A feature earns a place here only if we can name the
firm-level metric it moves. Every feature carries the validation question to
ask in customer conversations — we validate the *pain*, not the feature
("how do you do this today, how long does it take, who does it"), because
"would you use this?" always gets a polite yes.

Status vocabulary for the "we have" column: DEPLOYED (live now) ·
BUILT (in repo, verified) · DATA (the data exists, no surface) · NEW (build
from scratch).

---

## 1. The reframe

What we built so far serves the analyst's **reading** workflow (feeds,
dossiers, surprises). Firms don't pay for reading; they pay for what changes
their P&L or their risk. An Indian sub-₹2,000 Cr PMS firm's operating
metrics, in rough order of what the principal actually watches:

| Firm metric | Why it matters | Product lever |
|---|---|---|
| Coverage per analyst | Analysts are the biggest cost line; results season is the bottleneck | Results-season throughput tools |
| Client retention / AUM | Fees are % of AUM; retention is driven by performance + communication quality | Client-facing output drafting |
| FM risk oversight | One blow-up loses clients; "was I blindsided?" is the principal's fear | Portfolio exposure rollups + alerting |
| Compliance cost/risk | SEBI PMS audits require documented investment rationale; RIA/RA boundaries | Rationale audit trail |
| Data subscription spend | ACE Equity / Capitaline / Trendlyne seats; Bloomberg unaffordable | Cost displacement |

The pitch changes from "your analysts see events faster" to: **"two
analysts cover what three used to, the quarterly letter writes its first
draft itself, and every decision has an audit-ready evidence file."**

---

## 2. Wish board

### A. Results-season machine (metric: coverage per analyst)

**A1. Concall intelligence (the AlphaSense gap)** — ingest earnings-call
transcripts (SEBI mandates filing within 5 days; audio in 24h — both arrive
through the filings pipeline we already run), extract guidance, one-offs,
margin walk, capex plans; diff guidance vs the previous quarter's call;
cross-check management claims against our parsed XBRL (grounding layer
exists). We have: filings ingestion DEPLOYED (currently classifies
transcript filings as "procedural, magnitude 1" and reads nothing);
LLM/tool-loop + grounding + per-company memory DEPLOYED. Proof of need:
BAJAJCON Q1 — the −20% story lived in the concall; our pipeline was blind.
*Validate: "Who listens to concalls at your firm, for how many companies,
and what happens to the notes?"*

**A2. Instant results brief (T0 one-pager)** — within minutes of a filing:
actuals vs 8-quarter history, YoY/QoQ, segment view, surprise decile +
market reaction + divergence (DEPLOYED as of this week), scenario-rubric
classification (DEPLOYED), quality flags (A3). One screen, printable,
citation-linked. *Validate: "Results day with 6 portfolio companies
reporting — walk me through the evening."*

**A3. Earnings-quality flags at T0** — deterministic checks computable from
XBRL at parse time: growth-mix shifts (BAJAJCON: raw materials halved,
traded-goods purchases 4×), other-income share of PBT, tax-rate anomalies,
derived-Q4 artifacts. DATA (all fields parsed; no checks run). *Validate:
"What made you last distrust a reported number, and how did you catch it?"*

### B. Portfolio operations for the FM (metric: risk oversight)

**B4. Model portfolios, portfolio-first surfaces** — upload/maintain the
actual book (holdings + weights), every surface filters by it. The
watchlist symbol-filter shipped 2026-07-18 is the primitive; this is
watchlist × weights × persistence. BUILT primitive; NEW surface.
*Validate: "Where does the model portfolio live today?" (Excel, always —
ask to see the tabs.)*

**B5. Exposure rollups** — aggregate trigger betas + linkage graph over the
book: "your portfolio's crude exposure: X% direct, Y% one hop via
suppliers; top correlated concentration: Z". DATA (362 betas, 1,717 edges,
dossier sensitivities) — coverage must widen for credibility. *Validate:
"When crude spiked last, how did you check book exposure, and how long did
it take?"*

**B6. Morning / weekend brief (push)** — 08:30 IST per-portfolio email or
WhatsApp: overnight + weekend events ranked by materiality for YOUR names,
today's result calendar for holdings, SHP/bulk-deal changes in holdings.
Solves the weekend catch-up problem at the workflow level, reaches the
analyst before the terminal is opened, and is visible to the FM (the payer
sees the product daily). DATA + pipeline DEPLOYED; composition + delivery
NEW. *Validate: "What do you read between 8:00 and 9:15, in what order?"*

### C. Client-facing output (metric: retention / AUM)

**C7. "Why did it move" draft answers** — client asks "why is my X down
4%?"; the RM pulls an evidence-cited, factual event attribution (event →
channel → citation) drafted from our stored impact reports. RA-safe framing
required: factual attribution, no forward view, standing disclaimer.
DATA (40K impact reports). *Validate: "How many client queries per red
day, who answers, how long per answer?"*

**C8. Quarterly letter builder** — assemble the quarter's portfolio events,
results summaries, and attributions into a first draft with citations.
*Validate: "Who writes the quarterly letter, how many hours, what's the
worst part?"*

### D. Compliance & audit (metric: compliance cost/risk)

**D9. Investment-rationale log** — attach dossier snapshot + impact
evidence + analyst note to each buy/sell decision; exportable audit pack.
SEBI PMS audits ask for documented rationale; today it's scattered emails
and Excel. Our `analysis_audit` immutability pattern is the template. DATA
+ pattern; NEW surface. *Validate: "What did your last SEBI/internal audit
ask for from research, and what did producing it cost you?"*

### E. Cost displacement (metric: subscription spend)

**E10. Screening + export** — screens and CSV/Excel export over our
financials / SHP / corporate actions / deals / prices. If Taperead answers
80% of the daily ACE Equity / Capitaline lookups, the CFO math is legible:
we cost less than the seat we replace. DATA (all tables current).
*Validate: "Which data products do you pay for, what do they cost, what do
you actually use them for daily?"*

**E11. Smart-money monitoring for the book** — marquee-holder changes,
bulk/block deals in holdings (DEPLOYED data + Investors page; needs
portfolio filter + alerting). *Validate: "How do you track who's entering
or exiting your names?"*

### F. Interface layer

**F12. Research chat (AlphaSense-style)** — conversational retrieval over
the whole corpus: "what was in the transcript?", "why did BAJAJCON fall
after a good result?", "which of my names have crude exposure?". Answers
composed ONLY from tool results (the dossier agent's proven grounded
tool-loop, generalized to user turns), mandatory citations with deep links,
explicit "not in my data" refusals, RA scope guard (factual/attributive,
no forward views), every Q&A audit-logged (`analysis_audit` pattern → a
research assistant the principal can defend to SEBI — synergy with D9).
We have: tool-loop + grounding + MCP query layer DEPLOYED (dossier quality
0.75→0.90 proved the grounding discipline); conversation state, streaming,
citation UI NEW. **Prerequisite: A1 concall corpus** — without transcripts
the flagship question faceplants; "why did it move" works on existing data.
Sequencing: internal-only dogfood (INTERNAL_RESEARCH_EMAILS gate) →
groundedness measured → customer beta. Chat replaces *navigation*, not
surfaces — it is the pull complement to the B6 push digest, not a bandaid
over the IA redesign. Best validation-call demo we could own.
*Validate: "Last time you needed one number from an old concall or annual
report — walk me through finding it."*

---

## 3. Validation protocol

- Never pitch the feature first. Ask the workflow question, time-box
  ("how long did that take last time"), then show the closest thing we
  have.
- Ask for artifacts: the Excel portfolio, last quarterly letter, the audit
  request list. Artifacts don't flatter.
- Talk to all three roles separately: principal/FM (budget), analyst
  (workflow), ops/compliance (audit). The wish board columns map to who
  should validate each row.
- Score each feature after each call: pain frequency × pain severity ×
  willingness to name a number ("what would that be worth?").

## 4. What we do NOT build (standing constraints)

- Anything performance-claiming or recommendation-shaped (SEBI RA; and our
  own signal audit says the edge isn't there).
- Live-price redistribution to customers until an exchange-licensed feed
  exists (Angel One personal-use boundary, already enforced in code).
- Features that serve the analyst but move no firm metric — they go to the
  bottom of the board, not into the sprint.

---

## 5. Frontend redesign brief

Two validated complaints:

**5a. The catch-up problem (structural, confirmed).** The feed is a
reverse-chron stream; at ~470 relevant reports/day the initial window is
hours. Anyone opening Monday morning after a weekend faces endless scroll —
the same window-blindness we just fixed for the watchlist, but for time
instead of symbols. Direction: a **"While you were away" digest** as the
default surface when returning after >N hours — grouped by day, ranked by
materiality (flags already exist per impact), sector- and
portfolio-clustered, expandable into the full feed. The live feed remains
for market hours. Powered entirely by existing data; this is an IA change,
not a pipeline change. (B6 push-brief is the same content leaving the app.)

**5b. Identity ("looks AI-generated").** Fair. Dark surface + single muted
teal accent + spaced-out mono microlabels is now the default output of
every AI page generator; restraint executed in default tokens reads as
template. The fix is not more color — it's owning an identity no template
has: **the tape**. The brand is literally "taperead" and the tape appears
nowhere except the logo. Directions to explore (with a designer, options
not prescriptions): a persistent thin tape ribbon as the signature element;
tape-segment visual language for event cards; a phosphor-terminal accent
(amber/green heritage) instead of the ubiquitous teal; keyboard-first
interaction (command palette, symbol-jump from anywhere) — density and
keyboard fluency are what make real terminals feel like terminals, and
analysts respect that more than any palette. Type (IBM Plex Mono/Sans) can
stay — it's the sameness of the layout+accent combo, not the type, that
reads generic.

**5c. The deeper redesign: portfolio-first IA.** If the buyer is the firm,
the home surface is not a firehose — it's **the book**: holdings with
what-changed, next week's result calendar, exposure summary, then the feed
as a tab. The feed-first IA is an artifact of having built ingestion first.

---

*Next step: rank rows A1–E11 by (existing-asset leverage × firm-metric
legibility), pick 2–3 to prototype before validation calls so there's
something concrete to react to. Candidates on leverage alone: A1 (concall),
B6 (brief), A2 (results one-pager).*
