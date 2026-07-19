# Taperead — Pitch Deck Handoff

Handoff for a design agent preparing a Taperead pitch deck. Self-contained:
everything needed is in this document. Follow the claims policy exactly —
this project has a hard rule that **claims must match verified reality**, and
a regulatory boundary on language (§4) that is not negotiable.

Prepared 2026-07-02. All metrics in §3 were verified against the production
database on this date.

---

## 1. Purpose and audience

**Primary deck: alpha-recruitment pitch.** Audience: analysts and fund
managers at boutique Indian PMS/AIF firms (sub-₹2,000 Cr AUM). Goal: get
8–12 of them to use the product daily during alpha and give feedback. They
are professional skeptics — the deck must feel like a terminal, not a
startup landing page. Understatement wins; hype loses this audience in one
slide.

**Investor variant (secondary, if requested):** same facts, different arc —
lead with the workflow gap and the data asset being built (curated,
citation-backed relationship graph for Indian mid/small caps), not with
product features. Do not include revenue projections or accuracy claims.

## 2. The product

Taperead (working title "Bharat Terminal") is an Indian equity
market-intelligence terminal. It ingests news and exchange filings in real
time, runs a multi-stage analysis pipeline, and shows analysts — within
seconds of an event breaking — which companies are exposed, through which
channel (input costs, ownership, supply chain), at what likely materiality,
with citations back to source filings. Pre-computed company dossiers carry
trigger sensitivities (measured betas with confidence and lag) and a curated
company-linkage graph that surfaces second-degree exposure a sell-side note
won't cover.

**Canonical copy (already shipped on the login page — reuse verbatim):**
- Headline: *"Read the tape before the desk does."*
- Sub: *"Structured, real-time assessment of every market-moving event
  across the NSE universe — pre-computed dossiers, exposure maps, and
  channel-attributed impact analysis for institutional equity desks."*
- Feature lines: *"Live event feed"*, *"Pre-computed dossiers · 460+ covered
  names"*, *"Exposure mapping · cross-sector propagation"*.

**The one-sentence pitch:** when a macro event breaks, the "which of my
names is exposed, how much, and why" loop drops from hours of manual work to
seconds — with the evidence attached.

## 3. Verified numbers (as of 2026-07-02 — safe to use)

| Claim | Verified value |
|---|---|
| NSE companies tracked | 2,367 |
| Pre-computed company dossiers | 469 ("460+") |
| Trigger sensitivities in dossiers (beta + channel + lag + confidence) | 1,562 |
| Curated company-linkage edges (supplier/customer/competitor/ownership) | 1,676 |
| Measured trigger betas (regression-calibrated elasticities) | 362 |
| Analysed market events (impact reports, since May 2026) | 24,600+ |
| Relevant events analysed per day (current run rate) | ~480 |
| Quarterly financial results in the knowledge base | 54,000+ rows |
| Corporate actions / shareholding records | 17,500+ / 8,900+ rows |
| Sources (primary-source doctrine, 2026-07-19) | NSE & BSE filings (XBRL + PDF) incl. complete concall transcripts, Polymarket prediction markets, MCX commodity ticks. Third-party news wires deliberately excluded — measured as lagged filing echo. |
| Event analysis latency | pipeline SLA ≤5s per item (P95 target) |

Rounding down is the house style ("460+", not "~500"). Never round up.

## 4. Claims policy — FORBIDDEN (regulatory, non-negotiable)

Taperead is positioned as an **information and analytics tool**, not a
research or advisory service (SEBI Research Analyst regulations). The deck
must never contain:

- The words **"signals"**, **"trade ideas"**, **"calls"**, **"conviction"**,
  **"buy/sell/hold"**, **"position size"**, **"stop loss"**, **"targets"**,
  or any recommendation language.
- **Any accuracy, hit-rate, returns, or performance claim.** Not even
  internal ones. No "X% accurate", no backtests, no P&L.
- Price targets or fair-value numbers for any stock.
- "Actionable" as an adjective for the analysis.
- Anything implying the product tells the user what to trade.

Safe framing: the product shows *exposure, mechanism, magnitude context,
and evidence*; the analyst forms the view. Direction language attaches to
**fundamentals** ("gross-margin headwind"), never to stocks or trades.
Include this standard line somewhere in the deck footer or closing slide:
*"Taperead is an information and analytics tool — not investment research
or advice. Investment decisions rest with your registered analysts."*

## 5. Claims that need verification before use

- "Sub-second ingestion" — ingest latency is tracked but not currently
  verified as sub-second; do not use without a fresh measurement.
- Any uptime/reliability claim — single-instance deployment today; avoid.
- Coverage claims beyond §3 ("all NSE events", "every filing") — say
  "across the NSE universe" (sources listed in §3), not "every".

## 6. Brand and design system (from the shipped product)

Dark, disciplined, terminal-native. The deck should look like the product.

**Colors** (exact tokens from the shipped UI):
- Background `#10141A`; elevated surfaces `#161B22`, `#1B212A`
- Ink (text) `#E7EAEE`; secondary `#C1C7D0`; dim `#8B94A1`; muted `#5E6775`
- Borders `#242B36` / `#323B49`
- **Single accent — muted petrol teal `#3FA192`** (dim fill
  `rgba(63,161,146,0.12)`). One accent, used sparingly. No gradients.
- Fundamental-direction semantics (not trade colors): tailwind `#5FA98A`
  (muted green), headwind `#C68476` (muted coral), neutral `#7E8794`,
  warn/star `#C2A05E`.

**Typography:** IBM Plex Mono (labels, numbers, tickers — uppercase micro
labels with wide letter-spacing) + IBM Plex Sans (body/headlines). Nothing
below 10px equivalent. Two weights.

**Logo:** "taperead." wordmark in Plex Mono semibold with a teal period,
preceded by a "tape" mark — horizontal bars of varying width/opacity with
one teal bar. SVG source: `frontend/src/components/ds/index.tsx` (Logo
component). Optional "TERMINAL" tag in letterspaced micro caps.

**Voice:** computed, not promised. Every number on a slide should be one we
can defend ("as of July 2026" footnotes welcome). No exclamation marks, no
"revolutionary/AI-powered/game-changing". The brand's credibility mechanism
is visible restraint.

## 7. Product surfaces to screenshot (founder to capture from the live terminal)

1. **Event Feed** (three-pane: nav/filters · feed · assessment) — show the
   time-range chips (90M/6H/24H/ALL), sector/source/direction/rating
   filters, day dividers, star toggles.
2. **Assessment panel** — an event with multiple affected companies:
   headline, fundamentals KPIs, affected-lines table with
   tailwind/headwind pills, numbered reasoning with indirect-exposure
   attribution ("via shareholder relationship with X, transmission 0.43x").
3. **Dossier modal** — sensitivities table with signed betas, confidence,
   lag, and evidence links; "Data as of" stamp.
4. **Universe** — coverage view (dossier-ready dots, search, sector chips).
5. **Watchlist + Starred** — personal workflow surfaces.

Pick events where the analysis reads well; screenshots are claims too.

## 8. Suggested arc (10 slides, alpha-recruitment)

1. Cold open: a real macro headline + the question every desk asks in the
   next 60 seconds ("who in my universe is exposed?").
2. The current answer: hours of manual work; sell-side notes arrive later
   and cover ~200 large caps.
3. Taperead answer: the same event in the terminal, seconds later —
   screenshot-led.
4. How: ingestion → analysis pipeline → exposure mapping with citations
   (one clean diagram; no vendor logos, no buzzwords).
5. The dossier: trigger sensitivities with measured betas + evidence links.
6. Second-degree exposure: the linkage graph, with the transmission-weight
   framing ("shareholder, 0.43x") — this is the thing nobody else shows.
7. Coverage & freshness numbers (§3 table, styled).
8. What alpha looks like: invite-only, direct line to the builder, product
   shaped by their workflow; what we ask (daily use + honest feedback).
9. Who it's for: boutique PMS/AIF desks — built for 1-5 analyst teams, not
   a Bloomberg replacement.
10. Close: canonical headline + contact + the information-tool disclaimer.

## 9. Competitive positioning (one-liners, honest)

- vs **sell-side research**: minutes not hours; 2,367 names not 200; your
  universe, not their coverage list.
- vs **screeners (Screener.in / Trendlyne / Tickertape)**: they answer
  "what are this company's numbers"; Taperead answers "what does this event
  do to which companies, and why".
- vs **Bloomberg/LSEG**: not a market-data terminal; an event-to-exposure
  layer for Indian mid/small caps at boutique pricing.
- Never name competitors negatively on a slide; frame as "the current
  workflow" vs "with Taperead".

## 10. Practical notes

- Aspect: 16:9. Dark theme throughout (the product is dark; light-mode
  slides would break brand).
- Footnote verified metrics with "as of July 2026".
- The founder (Aviral Verma, aviralv35@gmail.com) reviews every claim
  before the deck ships. When in doubt about a claim: leave it out or mark
  it `[VERIFY]`.
- Product URL for screenshots: the terminal at `terminal.taperead.trade`
  (invite-only; founder captures).
