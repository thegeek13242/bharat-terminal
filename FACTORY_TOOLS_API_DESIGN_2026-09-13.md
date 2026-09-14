# Factory tools API — design

**Date:** 2026-09-13 · **Status:** DESIGN. Nothing below is built.
**Scope:** the tool surface the four agents call. Deliberately specified *before* the agents,
because the tools are the security boundary — Cedar keys off tool identity, and "the Scientist
cannot promote" is only enforceable if no promote tool exists.

**Builds on:** `RESEARCH_FACTORY_SPEC_v2_2026-09-12.md` §4.1 (agent definitions) and §4.2 (Cedar
matrix). This document does not re-decide those; it makes them concrete enough to implement.

---

## 0. A blocking finding — the DB capability separation is currently decorative

Migration 043 created three `NOLOGIN` group roles and carefully revoked the dangerous grants:

```
REVOKE INSERT ON factory.gate_decisions   FROM factory_writer
REVOKE INSERT, UPDATE ON factory.hypothesis_status FROM factory_writer
GRANT  INSERT ON factory.gate_decisions   TO factory_gate
```

Verified today, that protects nobody:

```
pg_auth_members for factory_reader|factory_writer|factory_gate  ->  0 rows
login roles in the database                                     ->  bharat only (rolsuper = t)
```

No login user is a member of any of the three, so nothing can connect as them or `SET ROLE` to
them. Every process — including a tools API built the obvious way — connects as **`bharat`, a
superuser**, and **superusers bypass all GRANTs and REVOKEs**. The promotion boundary in Postgres
does not currently exist.

I previously described this as a second enforcement layer beneath Cedar. That was wrong, and it is
the kind of wrong this project is built to catch: the migration reads like a control, so it was
taken for one.

**Consequence for this design: the tools API must never connect as `bharat`.** Prerequisite work,
before any agent gets a tool:

| Step | What | Approval |
|---|---|---|
| P1 | Migration 044: `CREATE ROLE factory_api LOGIN PASSWORD <secret>; GRANT factory_writer TO factory_api;` and `factory_api_ro LOGIN` granted `factory_reader` | migration + DDL |
| P2 | Store both DSNs in Secrets Manager next to `factory/tools-api-token`; never in `.env` alongside production credentials | secrets |
| P3 | Tools API opens **two** pools, one per role. `api/db.py` is a single module-global engine on one DSN, so this is new plumbing, not reuse | code |
| P4 | Test: connecting as `factory_api` and attempting `INSERT INTO factory.gate_decisions` must raise `permission denied` | CI |

P4 is the acceptance test for the whole idea. Until it passes, the only boundary is Cedar, and a
single misconfiguration reaches the ledger.

---

## 1. What this surface is for

Agents compose registered primitives over a ledger. They do not compute, judge, or promote.
Three rules shape every contract below:

1. **Reads are projections, not tables.** No tool returns a raw row. Each returns a shaped payload
   with only the fields an agent needs, so schema changes do not leak into prompts and large blobs
   (`spec_json`, embeddings) never enter a context window unasked.
2. **One write tool per agent, INSERT-only, into `factory.*` only.** Never UPDATE, never
   `gate_decisions`, never `hypothesis_status`, never a production table.
3. **Absence is the enforcement.** The safest tool is the one that does not exist (§6).

---

## 2. Transport

```
agent (Strands)
  -> AgentCore Gateway  (MCP; Cedar default-deny evaluated here)
  -> Lambda bharat-factory-tools-proxy  (in VPC, subnet of the EC2 box)
  -> FastAPI research-tools  (EC2 private IP 172.31.42.154:8003, bearer auth)
  -> Postgres as factory_api / factory_api_ro   (NOT bharat)
```

The Gateway passes the tool name to the Lambda as
`context.client_context.custom["bedrockAgentCoreToolName"]`, formatted `Target___tool` (spec §1,
verified against AWS docs). The Lambda therefore does **no routing logic of its own**: it splits on
`___`, maps the suffix to a fixed allowlisted path, and rejects anything not in the map. A tool
name it does not recognise is a 403, not a passthrough.

The Lambda holds the bearer token (Secrets Manager `factory/tools-api-token`) and the agents never
see it. The FastAPI app binds to the private IP only; the sole inbound rule on the EC2 SG is TCP
8003 sourced from `sg` of `bharat-factory-lambda`.

**Every request carries** `agent_version_id`, `agent_run_id`, `trace_id` (W3C traceparent) and
`idempotency_key`. These are recorded on every write and every tool call, so a report claim can be
walked back to a ledger row and then to a trace.

---

## 3. Read tools (16)

Common envelope:

```json
{ "tool": "get_hypothesis", "dataset_version": "DS-20260913-02",
  "generated_at": "...", "truncated": false, "data": { ... } }
```

`truncated` is explicit rather than silent — an agent reasoning over a clipped list should know.

| Tool | Args | Returns (projection) | Role | Notes |
|---|---|---|---|---|
| `search_memory` | `query`, `k<=20`, `family_id?`, `include_quarantined=false` | `failure_id, category, reason, lesson, recommendation, evidence_refs, provenance, created_at, similarity` | writer | pgvector over `failures.embedding`. **Quarantined rows are excluded by default** — a leakage-tainted lesson must not silently steer new work. `provenance` is always returned so `imported_unreverified` is visible as such. |
| `get_hypothesis` | `hypothesis_id`, `version?` | `claim, family_id, horizon, data_tier, status, created_by, parents[{parent_id, op, diff}]` | writer | Joins `hypotheses` + `hypothesis_status` + `hypothesis_lineage`. `spec_json` returned only when `include_spec=true`. |
| `get_family_state` | `family_id?` | `family_id, title, status, budget_cap_usd, killed_reason, counts_by_status{}` | writer | Chief may omit `family_id` for all families; Scientist and Adversary must name one. |
| `list_features` | — | `ref, data_tier, history_start, coverage, control_eligible, valid_clocks, integrity_flags` | n/a (code) | Reads the code registry, not the DB. **`coverage: null` renders as `"UNMEASURED — cannot be used as a control"`**, never omitted: 11 of 20 features are unmeasured and an agent must see why a control will be rejected before it proposes one. |
| `get_dataset_catalog` | — | `dataset_version, created_at, sealed_from, seed, code_commit, tables{rows, max_date}, age_hours, integrity_status` | writer | Sourced from the snapshot manifest (`tables` key, sha256-verified). Stale snapshot surfaces here, so an agent can see the compiler will fail closed. |
| `get_run_results` | `experiment_id` | `result payload, result_hash, seed, window_role, cpu_seconds, stat_tests[{kind, p_value, passed}], robustness[{battery_test, critical, verdict}]` | writer | Joins `experiment_runs` + `stat_tests` + `robustness_tests`. Never returns holdout-window results unless `window_role='holdout'` was already unsealed via the gate. |
| `get_battery_catalog` | — | `test_id, description, critical, parameters` | n/a (code) | Static catalogue of the 14 adversarial tests. Adversary may only request from this list. |
| `get_paper_feedback` | `strategy?`, `since?` | `divergence class, expected vs realized, n_events, slippage_ratio, notes` | **reader** | **The one tool needing production tables** (`paper_trades`, `paper_signal_marks`). See §4. |
| `get_frontier` | — | latest `frontier_snapshots.ranking/allocation`, `alpha_wealth_remaining` | writer | |
| `get_budget` | — | spend by family/category vs caps, days remaining | writer | From `budget_ledger`. |
| `get_report_facts` | `report_date` | the `ReportFacts` payload (scorecard, actions, numbers, identifiers) | writer | Reporter only. Built by `factory/report/facts.py`, which already exists. |
| `get_research_question` | `question_id` | `text, family_id, source, status, evidence_refs, created_at` | writer | **Added after the §9 walkthrough.** Resolves the `question_id` the Scientist is handed in its `FrontierSlot`. Without it the agent cannot read the question it was assigned. |
| `get_failure` | `failure_id` | same projection as `search_memory`, single row | writer | **Added.** `context_refs.memory` passes FAIL ids; `search_memory` is semantic-query-only and cannot fetch by id. |
| `get_divergence_report` | `report_id` | `classification, window, expected, realized, evidence, action, followup_question_id` | writer | **Added.** `context_refs.divergence` passes report ids; `get_paper_feedback` filters by strategy/date only. |
| `get_gate_decision` | `hypothesis_id`, `version?` | `from_status, to_status, gate_version, rule_results[{rule, value, threshold, pass}], decided_at` | writer | **Added.** The actionable "why blocked". Without it an agent sees that a sibling failed but not that DSR was 0.91 against a 0.95 threshold — so it cannot adjust, only guess. |
| `get_holdout_status` | `hypothesis_id` | `accesses[{access_id, window, accessed_at, decision_id}], remaining` | writer | **Added.** "One holdout access" is an anti-overfitting control (spec §15 risk row). The Adversary must be able to see whether it is already spent. |

### 3.1 Read tools are a prompt-injection surface

`failures.reason`, `failures.lesson`, `hypotheses.claim` and every kill memo are **free text written
by agents**. One agent's output is another agent's input, so `search_memory` is an
agent-to-agent channel. Defences, in order of strength:

1. **Structured returns only.** Agent-authored text is delivered in named fields inside a JSON
   envelope, never concatenated into prose the model reads as narration.
2. **Explicit untrusted marking.** Free-text fields are wrapped as
   `{"untrusted_text": "..."}` and the agent system prompts state that such content is evidence to
   weigh, never instruction to follow. The codebase already has this instinct — `research_chat.py`
   carries "Assistant turns are REPLAYED answers, not user input".
3. **Reconciliation over trust.** Any number or ID an agent cites is checked against the ledger
   before it reaches a human. `report/reconcile.py` already implements this and drops unbacked
   sentences; the same function guards `submit_report_narrative`.
4. **No SQL interpolation, ever.** Parameterised queries only; `search_memory`'s `query` reaches
   pgvector as a bound embedding, never as text in a statement.
5. **Caps.** `k<=20`, per-field truncation with `truncated: true`, total response size capped so a
   poisoned long memo cannot crowd out a context window.

---

## 4. The `get_paper_feedback` role split — a decision

`factory_writer` holds **zero** grants on `public`. Only `factory_reader` can read the 11
production tables. So `get_paper_feedback` cannot be served by the same pool as the other ten.

| Option | Shape | Trade |
|---|---|---|
| **A. Two pools (recommended)** | `factory_api` (writer) for 10 tools, `factory_api_ro` (reader) for `get_paper_feedback` | Honest: the tool that needs production data is visibly the one with production reach. Read-only, and the route is one function. |
| B. Mirror | `paper_feedback_daily` writes outcomes into a `factory.*` table; the tool reads that | Agents never touch `public` at all. Costs a sync path, and the mirror can go stale — this project's signature failure. |

**Recommendation: A**, with the reader pool wired to exactly one route and asserted in CI. B trades
a clear boundary for a staleness risk we have been bitten by twice.

---

## 5. Write tools (5)

All four: INSERT-only, `factory.*` only, idempotent on `idempotency_key`, and every row stamps
`created_by = agent_version_id` plus `trace_id`.

| Tool | Args | Validation before any write | Returns |
|---|---|---|---|
| `submit_hypotheses` | `hypotheses[<=5]`, each with `spec`, `parents[]`, `mutation_op`, `mechanism` | Each spec runs through the **real compiler** (`compile_spec`). Duplicate `spec_hash` rejected. Unregistered feature refs rejected. | Per-item `{ok, hypothesis_id}` or `{ok:false, errors:[E003,...]}`. **Errors are returned verbatim so the Scientist can repair — max 2 repair attempts, then the slot is abandoned and a `failures` row is written.** |
| `submit_attack_plan` | `experiment_id`, `attacks[<=8]`, `kill_memo` | Every attack must exist in the battery catalogue. The memo must cite ≥1 real `run_id`; uncited memos are rejected, not stored. | `{plan_id, accepted[], rejected[{attack, reason}]}` |
| `submit_allocation` | `allocations[]`, `kills[]`, `revisits[]` | Sum of family budgets ≤ global cap. A `revisit` of a paused family **must** carry `evidence_refs` to a ledger row created *after* the pause — prose cannot reopen a family. | `{snapshot_id, applied[], refused[{item, reason}]}` |
| `submit_report_narrative` | `report_date`, `sections{}` | Runs `reconcile_narrative` against the facts payload. Sentences with unbacked numbers, unknown IDs or overclaims are **dropped, not rejected** — the report still ships. | `{kept, dropped[{sentence, reason}]}` |
| `report_data_gap` *(all four agents)* | `need`, `question_id?`, `blocked_tool?`, `suggested_source?`, `evidence_refs[]` | Must name a concrete need; free text capped. INSERTs a `research_questions` row with `source='agent_gap'`, `status='open'`. | `{question_id}` | 

`report_data_gap` is the **only** tool all four agents share, and it is the safe form of "let agents
make their own tools" (§10). The agent *declares* what it could not see; a human turns that into a
tool or a feature PR. No code is generated, and nothing is executed.

**Idempotency matters more than it looks.** Gateway retries are invisible to the agent; without a
key, a retried `submit_hypotheses` creates five duplicate hypotheses that then consume five frontier
slots. Key = `(agent_run_id, call_seq)`, unique-constrained.

---

## 6. The negative space — what has no tool

Deliberately unimplementable, not merely unpermitted:

- promotion or any ladder transition (`gate_decisions`, `hypothesis_status` — `factory_gate` only)
- paper enrolment, order placement, capital or notional changes
- gate thresholds, embargo, holdout unsealing, budget caps
- git, `.env`, secrets, IAM, any production table write
- feature registration (a feature is code → PR → founder merge)
- deleting or editing any ledger row, including the agent's own

If a future need argues for one of these, it is a spec change with an approval, not a tool addition.

---

## 7. Tests that must exist before an agent is pointed at this

| Test | Asserts |
|---|---|
| **Cedar deny matrix** | For all 4 agents × **21 tools** (the §9 additions included), the matrix holds at the Gateway. Named case: `scientist -> submit_allocation` is DENIED. The count is asserted against the tool registry, so adding a tool without a matrix row fails CI rather than shipping untested. |
| **Role enforcement (P4)** | Connected as `factory_api`, `INSERT INTO factory.gate_decisions` raises `permission denied`. This is the test that makes §0 real. |
| **No-superuser guard** | The tools API refuses to start if `SELECT rolsuper FROM pg_roles WHERE rolname = current_user` is true. Fail-closed, so a misconfigured DSN cannot quietly restore superuser reach. |
| **Injection corpus** | `search_memory` returns a planted memo containing instruction-shaped text; the agent's output must not act on it. Part of the E1 flaw zoo. |
| **Idempotency** | The same `idempotency_key` submitted twice yields one row. |
| **Unknown tool name** | Lambda receives `Target___drop_tables`; returns 403 without reaching FastAPI. |
| **Projection stability** | Adding a ledger column does not change any tool's response shape. |

---

## 8. Build order

1. **P1–P4 first.** Without a non-superuser login role the security model is words. Migration 044 +
   two DSNs + the no-superuser guard.
2. **Read tools, `factory_writer` pool.** 10 of 11, no production reach. Testable end-to-end from
   the EC2 box with no AgentCore at all.
3. **`get_paper_feedback`** on the reader pool (§4 option A).
4. **Write tools**, `submit_hypotheses` first — it is the only one whose validation path (the
   compiler) already exists and is tested.
5. **Lambda + Gateway + Cedar.** Last, because it is the only step carrying an untested assumption:
   whether Gateway can reach a Lambda in our VPC as drawn.
6. **Then** the first agent.

Steps 1–4 need no AWS at all and can be exercised against the local ledger. That ordering means the
tool surface is proven before the transport, and the transport before any model.

---

## 9. Gap analysis — walking each agent's actual task sequence

Reviewing tools one by one missed these. Walking the sequence found them, and five of the six share
one root cause.

### The root cause: the task payload is all references

Spec §23 item 9 defines the Scientist's `FrontierSlot` as:

```
slot:         {slot_id, family_id, question_id, allowed_ops, max_proposals, usd_cap}
context_refs: {memory: [FAIL/H ids], runs: [run ids], divergence: [report ids]}
```

Every element is an **id**, not content. The first design was query-shaped (`search_memory(query)`,
`get_paper_feedback(strategy, since)`) and had almost no fetch-by-id path — so an agent could not
resolve the very payload it was handed.

| # | Gap | Where it bites | Severity |
|---|---|---|---|
| 1 | `question_id` had no resolver | Scientist **step 1**. It is told which question to work and cannot read it | **Blocker** |
| 2 | `context_refs.memory` FAIL ids unresolvable | `search_memory` is semantic-only. The pre-selected memory the frontier chose for this slot was unreachable | **Blocker** |
| 3 | `context_refs.divergence` report ids unresolvable | Breaks the paper-feedback → question → hypothesis loop end to end | High |
| 4 | `gate_decisions.rule_results` unreachable | An agent sees *that* a sibling was blocked, never *why*. `rule_results` carries `{rule, value, threshold, pass}` — the one thing that makes a repair informed rather than a guess | High |
| 5 | `holdout_access_log` unreachable | The Adversary cannot see whether the single permitted holdout access is already spent | High |
| 6 | No way to say "I cannot see X" | An agent blocked on missing data has no output except failing or reasoning around the hole | High |

Gaps 1–3 would have surfaced as an agent that simply could not start. Gaps 4–6 are worse: the agent
*would* run, and produce confident work built on what it could not check.

### Still open, judged not worth a tool yet

- **Sibling comparison within a family** (Adversary spotting "these four ran t≈2.1, this claims 5.7").
  Real adversarial value, but `get_family_state` plus repeated `get_run_results` covers it at the
  cost of turns. Revisit if the Adversary burns its turn budget on fan-out.
- **`feature_versions.deprecated_at`** — `list_features` reads the code registry, which has no
  deprecation concept. Minor until the first feature is deprecated; then it silently misleads.
- **`eval_results` / `agent_versions.e1_results` for the Chief** — allocating while blind to a spike
  in Scientist compile-failures. Defer until E3 online evals exist to read.
- **In-flight experiments.** Exact duplicates collide on `spec_hash`; near-duplicates and
  concurrently-running work stay invisible. Matters only at >1 Scientist slot in parallel.

### Consequence for the spec

Spec §4.2's Cedar matrix lists 15 tools. This design now has **21**. The matrix must gain rows for
the six additions before any deny test is written — otherwise CI asserts a matrix that no longer
describes the surface. **The spec needs this update; it should not silently diverge.**

Proposed matrix rows:

| Tool | scientist | adversary | chief | reporter |
|---|:--:|:--:|:--:|:--:|
| `get_research_question` | ✓ | ✓ | ✓ | |
| `get_failure` | ✓ | ✓ | ✓ | |
| `get_divergence_report` | ✓ | | ✓ | |
| `get_gate_decision` | ✓ | ✓ | ✓ | |
| `get_holdout_status` | | ✓ | ✓ | |
| `report_data_gap` | ✓ | ✓ | ✓ | ✓ |

---

## 10. On an agent that creates tools for other agents

The instinct is right: **data gaps are the real risk**, and an agent that cannot reach what it needs
will reason around the hole rather than stop. The walkthrough above proves the concern was justified
— six genuine gaps, two of them blockers. The spec contains no data-gap handling at all.

But a tool-creating agent is the wrong mechanism, for four reasons:

1. **It dissolves the enforcement model.** §6 works because the dangerous tools *do not exist*. An
   agent that can create tools can create `promote_hypothesis`.
2. **Cedar becomes unenumerable.** Default-deny is evaluated against a known tool list. A surface
   that changes at runtime cannot have a deny matrix, so the CI test "scientist → submit_allocation
   is DENIED" has nothing to assert against.
3. **A tool is code holding a database connection.** An agent-authored tool is arbitrary code
   against production Postgres, reached through a Lambda that exists specifically so agents never
   hold credentials.
4. **Its failures are silent.** A tool with a wrong join or a missing point-in-time filter returns
   plausible numbers forever. That is exactly how a 264-session benchmark hole inverted STAGE4A —
   and it took an unrelated investigation to catch.

It also reverses the founder's standing decision that agents open PRs and a human merges.

**The safe form, adopted above:** `report_data_gap`. The agent declares what it could not see, with
evidence refs; the row lands in `research_questions` as `source='agent_gap'`; a human reads it and
either adds a tool, registers a feature, or closes it. The loop closes — an unmet need becomes
visible work rather than a silent workaround — without an agent ever writing code.

If gap reports pile up around one theme, that is the signal to build the tool. The queue becomes the
evidence, which is a better trigger than an agent's in-the-moment judgement.

---

## 11. Open questions for the founder

| Question | Why it matters | My recommendation |
|---|---|---|
| Approve migration 044 (login roles)? | Everything in §0 is blocked on it | Yes — it is the difference between a documented boundary and a real one |
| §4: two pools, or mirror paper outcomes? | Decides whether agents ever touch `public` | Two pools |
| Should `search_memory` ever return quarantined failures? | A quarantined lesson may still be true | No by default; a `factory_gate`-signed override only |
| Cap on total tool calls per agent run? | Spec caps turns and $, not calls | Yes — 40 calls/run, so a loop cannot exhaust the budget through cheap reads |
