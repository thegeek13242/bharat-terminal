"""Autonomous research factory.

Deterministic research control plane: snapshots → DSL compile → backtest →
statistics → adversarial battery → gates → shadow/paper → feedback → report.
LLM agents (on AWS Bedrock AgentCore) only propose, attack, allocate and
narrate; nothing in this package lets them decide a promotion.

Spec: RESEARCH_FACTORY_SPEC_v2_2026-09-12.md (repo root).

Nothing here writes to production tables. The factory reads immutable parquet
snapshots and writes only the `factory` Postgres schema.
"""
from __future__ import annotations

# Every sampling / bootstrap / permutation operation in the factory uses this
# seed, and it is recorded on every experiment_run row (CLAUDE.md §5).
FACTORY_SEED = 42

# Gate/threshold configuration version. Any change to a promotion threshold is
# a human decision and MUST bump this — gate_decisions rows record it, so a
# later reader can tell which rulebook a decision was made under.
GATE_VERSION = "gates_v1"

__all__ = ["FACTORY_SEED", "GATE_VERSION"]
