"""Multi-tier event memory for the analysis agent.

The `memory_items` table is a DERIVED, embedded index over `impact_reports`
(the source of truth) — one row per consolidated real-world event. Retrieval
ranks by relevance (embedding) + recency + importance, scoped to a company, a
sector, or the whole market. Because the index is rebuildable from
`impact_reports`, it can never drift; the only eviction is a prune of rows past a
generous retention window.

Modules:
  core      — shared helpers + write-side extraction/consolidation from impact_reports
  index     — build/refresh/prune the memory_items index (uses embeddings)
  retrieve  — read-side scoped retrieval over memory_items
  company / sector / market — per-tier config + prompt formatting
  digest    — LLM-synthesised sector/market narrative (overwrite cache)
"""
