# Taperead — NVIDIA Inception Pitch Deck Handoff

Handoff for a design agent preparing the Taperead deck for the **NVIDIA
Inception program application**. Self-contained; supersedes nothing — the
alpha-recruitment deck handoff (`PITCH_DECK_HANDOFF.md`, 2026-07-02) remains
valid for its own audience and this document inherits its claims policy and
brand system wholesale.

Prepared 2026-08-02; revised same day after incorporation details landed and
the observability/admin platform deployed to production. Every metric in §4
was verified against the production database on this date (read-only
queries). House rule, non-negotiable: **claims must match verified
reality**. Round down, never up.

---

## 1. Purpose and audience

**Audience: NVIDIA Inception program reviewers.** Inception is a startup
enablement program (no equity, no fees) that admits AI/accelerated-computing
startups. Reviewers evaluate: (a) is this a real, technically substantive AI
company; (b) relevance to accelerated computing — today or on a credible
roadmap; (c) stage and trajectory. This is NOT a fundraising deck and NOT a
customer deck — technical depth that would bore a fund manager is exactly
what belongs here.

**Arc in one line:** an AI-native market-intelligence terminal for Indian
equities, built solo on a disciplined LLM pipeline with an unusual
evidence-verification culture, now hitting the scale where self-hosted
accelerated inference is the next unlock.

## 2. Company facts (founder to confirm the [VERIFY] items before submission)

| Field | Value |
|---|---|
| Product | Taperead (repo/internal name: Bharat Terminal) |
| Founder | Aviral Verma — solo founder, full-stack (pipeline, infra, frontend) |
| Website | taperead.trade (landing + demo-request), terminal.taperead.trade (product, invite-only) |
| Stage | Invite-only alpha; single-digit professional users; demo-request → provisioning pipeline live |
| Target customer | Analysts at boutique Indian PMS/AIF firms (sub-₹2,000 Cr AUM); firm pays, analyst consumes |
| Legal entity | TAPEREAD TERMINAL (OPC) PRIVATE LIMITED (incorporated 2026, Uttar Pradesh) |
| CIN | U63119UP2026OPC249859 — displayed in the statutory footer on every page of taperead.trade and the terminal login (live, verifiable during diligence) |
| Contact | aviralv35@gmail.com |

Do not state team size beyond "solo founder" — it is a strength at this
execution level, not a gap to hide.

## 3. The product (reuse canonical copy)

Taperead ingests Indian exchange filings, news, and commodity/FX data in
real time, runs a multi-stage LLM analysis pipeline, and shows analysts —
within seconds of an event breaking — which companies are exposed, through
which channel (input costs, ownership, supply chain), at what likely
materiality, with citations back to source filings. Pre-computed company
dossiers carry measured trigger sensitivities and a curated company-linkage
graph that surfaces second-degree exposure a sell-side note won't cover.

- Headline: *"Read the tape before the desk does."*
- One-sentence pitch: when a market event breaks, the "which of my names is
  exposed, how much, and why" loop drops from hours of manual work to
  seconds — with the evidence attached.

## 4. Verified numbers (as of 2026-08-02 — safe to use)

| Claim | Verified value | Deck-safe phrasing |
|---|---|---|
| Companies tracked (NSE + BSE-only universe) | 3,998 | "3,900+ listed companies" |
| Analysed market events (impact reports, since May 2026) | 52,218 | "50,000+ analysed events" |
| Pre-computed company dossiers | 520 | "500+ dossiers" |
| Trigger sensitivities (beta + channel + lag + confidence) | 1,696 | "1,600+ measured sensitivities" |
| Curated company-linkage edges | 2,356 | "2,300+ curated linkage edges" |
| Quarterly financial results in KB (current to Q1 FY27 / 2026-06-30) | 103,700 rows | "100,000+ quarterly results, current to the latest quarter" |
| Exchange filings indexed | 55,571 | "55,000+ filings" |
| Daily price history (2017 → present) | 3.53M rows | "3.5M price points, 2017→present" |
| Earnings-call transcripts (full text, parsed + embedded) | 561 across 531 companies; 17,115 embedded chunks | "560+ concall transcripts, semantically indexed" |
| Structured guidance statements extracted from calls | 2,667 | "2,600+ extracted guidance statements" |
| Named-shareholder holdings records | 207,740 | "200,000+ holdings records" |
| Bulk/block deal records | 5,359 | "5,300+ deal records" |
| Pipeline latency SLA | ≤5s per event (tracked per-report in prod) | "event → analysis in seconds" |
| Dual-exchange ingestion | NSE + BSE, content-hash deduped; BSE frequently leads NSE by minutes on identical filings | "dual-exchange, first-source-wins" |

Footnote every number: *"as of Aug 2026, production database."*

## 5. AI substance (the section Inception actually reads)

This is what separates the application from "we call an API" startups —
present it as an engineering-culture story:

1. **Multi-stage LLM pipeline, extraction-first.** Filings/news flow through
   staged analysis (entity extraction → deterministic + LLM routing →
   impact scoring → materiality gating) on a Kafka (Redpanda) event bus.
   LLMs do *extraction and structured assessment*; deterministic, calibrated
   layers decide what surfaces. This "LLM extracts, calibrated layer
   decides" doctrine is a deliberate response to LLM look-ahead/leakage
   evidence in the finance-NLP literature.
2. **Provider-agnostic model layer.** In-house wrapper (multi-turn tool
   loop, thought-signature replay, retry/budget ladders) currently running
   Google Gemini (Vertex) for heavy agentic stages and Anthropic models for
   lightweight extraction. Models are swappable per stage — which is exactly
   what makes the self-hosted GPU roadmap (§6) credible.
3. **Agentic dossier generation with grounding.** A tool-loop research agent
   composes each dossier from DB-grounded tools (financials, prices,
   filings, guidance timeline) plus web search — every claim in a dossier
   resolves to a source. Grounding lifted measured dossier quality from
   0.75 → 0.90 on the internal rubric.
4. **Semantic retrieval substrate.** pgvector embeddings over transcript
   chunks (17k+) and event-memory items; recency-decayed multi-tier memory
   (market/sector/company) feeds the analysis agent.
5. **Evidence-verification culture — the differentiator.** Pre-registered
   validation criteria before any model change ships; a nightly
   classification-accuracy eval against a fixed baseline; replay harnesses
   with seeded bootstrap CIs; a no-improvement rule that has *rejected* more
   model changes than it has shipped (say this — restraint is credibility).
   A statistically validated post-earnings drift event study (51 quarterly
   cohorts) anchors the materiality work. **Frame as "statistical
   validation of event materiality" — see §7 for what may NOT be said.**
6. **Full-stack observability — live in production, not a slide.** An
   in-product operations panel (deployed Aug 2026) tracks per-stage LLM
   cost and latency to the individual-call row — mean/median **cost per
   filing analysed** is a first-class production metric — plus durable
   alert history, per-loop worker liveness (distinct from container
   status), Kafka consumer lag, and per-user engagement analytics. One
   person operates the whole platform because the platform reports on
   itself. Dossier regeneration is now a scheduled nightly staleness-driven
   job (shipped Aug 2026) rather than manual batches.

## 6. GPU / accelerated-computing relevance (honest today + credible roadmap)

**Today (state plainly):** inference runs on hosted APIs (Vertex AI /
Anthropic). No self-managed GPUs yet. Do not imply otherwise — Inception
admits pre-GPU startups; a credible roadmap is what they want to see.

**Why acceleration is the next unlock (all four are real, current pain):**

1. **Concall audio → text.** SEBI mandates call recordings within 24h, but
   official transcripts lag ~6 days (median 5.85d, measured). Self-hosted
   ASR (GPU) collapses a 6-day information lag to hours across hundreds of
   calls per results season — the single highest-value GPU workload on the
   roadmap and already scoped as pipeline phase "C3".
2. **Embedding + retrieval at universe scale.** Semantic index today covers
   561 transcripts; the target is every filing + transcript + presentation
   for 3,998 companies — continuous embedding/re-ranking workloads that are
   API-cost-prohibitive and latency-sensitive.
3. **Self-hosted open-weight inference for the extraction tier.** The
   high-volume, low-complexity stages (entity extraction, guidance
   extraction, filing summarisation) are exactly the tier where fine-tuned
   open-weight models on GPU (TensorRT-LLM / NIM) beat per-token API
   pricing — LLM cost per filing is instrumented in production as of Aug
   2026 (durable per-call cost log with per-filing rollups), so the
   API-vs-self-hosted crossover is computed from live data, not estimated.
4. **Data residency.** Indian financial-market customers increasingly
   expect in-country processing; self-hosted inference in an Indian region
   is the clean answer.

**What we'd use from Inception:** cloud GPU credits (ASR + embedding
backfills + open-weight eval), NIM/NeMo/TensorRT-LLM software access and
guidance for the extraction tier, and the India Inception ecosystem.
Capital Connect later, at seed.

## 7. Claims policy — FORBIDDEN (regulatory, non-negotiable)

Identical to `PITCH_DECK_HANDOFF.md` §4 and it applies to THIS deck too,
even though the audience is NVIDIA, not customers (the deck will be seen
publicly). Never include:

- "signals", "trade ideas", "calls", "buy/sell/hold", "conviction",
  "position size", "targets", or any recommendation language.
- **Any accuracy/hit-rate/returns/P&L claim** — including internal ones.
  The paper-trading pilot and drift statistics exist internally; on slides
  they may appear ONLY as: *"pre-registered statistical validation of
  event materiality (51 quarterly cohorts)"* — no return numbers, no
  t-stats, no strategy names.
- Price targets or fair values. "Actionable" as an adjective.

Safe framing: exposure, mechanism, magnitude context, evidence; the analyst
forms the view. Footer line on the closing slide: *"Taperead is an
information and analytics tool — not investment research or advice."*

Also forbidden (accuracy-of-claims, not regulation): "sub-second" anything,
uptime claims (single-VM deployment), "every filing" (say "across the NSE &
BSE universe"), and any user count more precise than "invite-only alpha".

## 8. Brand and design system

Inherit `PITCH_DECK_HANDOFF.md` §6 verbatim — dark terminal-native slides,
background `#10141A`, single petrol-teal accent `#3FA192`, IBM Plex
Mono/Sans, tailwind `#5FA98A` / headwind `#C68476` semantics, "taperead."
wordmark (SVG in `frontend/src/components/ds/index.tsx`). Voice: computed,
not promised; no "revolutionary/AI-powered/game-changing"; restraint is the
brand's credibility mechanism. 16:9, dark throughout.

One Inception-specific addition: architecture diagrams should use the same
token palette (surfaces + one accent), NOT vendor-logo soup. NVIDIA's own
brand assets must not be used in the deck (program rules; they're applying
to NVIDIA, not partnering yet).

## 9. Suggested arc (12 slides)

1. **Cold open** — a real filing timestamp + the 60-second question every
   desk asks ("who in my universe is exposed?"); the manual answer takes
   hours and covers ~200 large caps.
2. **Product answer** — the same event in the terminal seconds later
   (screenshot-led; assessment panel with channel attribution).
3. **What it is** — one-sentence pitch + the three surfaces (feed /
   dossiers / linkage graph).
4. **Data asset** — §4 numbers, styled as a terminal table. This is the
   moat slide: curated, citation-backed, dual-exchange, current-quarter.
5. **Architecture** — ingestion → event bus → staged LLM pipeline →
   grounded dossier agent → terminal. One diagram, token palette.
6. **AI doctrine** — "LLM extracts, calibrated layer decides" + the
   verification culture (pre-registration, nightly evals, no-improvement
   rule). §5 items 1/5.
7. **Grounded agents** — dossier agent with DB tools + citations; 0.75→0.90
   quality lift.
8. **Semantic layer** — transcripts, guidance ledger, event memory
   (embedded retrieval at universe scale).
9. **GPU roadmap** — §6, led by the concall-ASR 6-day→hours story; include
   the "cost per filing is instrumented, crossover computed from prod data"
   line — it's the most Inception-native sentence in the deck.
10. **Traction & stage** — invite-only alpha with professional analysts at
    boutique PMS/AIF desks; demo-request pipeline live; solo founder,
    full-stack.
11. **What we'd use from Inception** — credits (ASR/embeddings/open-weight
    eval), NIM/NeMo/TensorRT-LLM guidance, India ecosystem.
12. **Close** — headline, contact, information-tool disclaimer.

## 10. Screenshots (founder captures from terminal.taperead.trade)

Reuse the §7 list from `PITCH_DECK_HANDOFF.md` (feed, assessment panel,
dossier modal, universe, watchlist) and add:
- **Transcripts surface** (semantic search over calls) — supports slides 8–9.
- **A dossier's citation trail** — supports slide 7.
- Do NOT screenshot: the admin panel, paper-trading dashboard, research
  chat, or anything showing the internal-only nav items (§7 policy).
  Screenshots are claims too — pick events where the analysis reads well.

## 11. Practical notes

- Application likely also wants short text answers (company description,
  AI usage, funding stage) — draft them from §3, §5, §6 in the same voice;
  keep the §7 policy in force there too.
- Funding stage answer: bootstrapped / pre-seed. No revenue claims.
- Founder reviews every claim before submission. Anything not in §4's
  verified table: leave out or mark `[VERIFY]`.
- Deliverable: 16:9 deck (PDF for the application portal + editable
  source), plus a one-paragraph company blurb and a 100-word AI-usage
  summary — all three from this document alone.
