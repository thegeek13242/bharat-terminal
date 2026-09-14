"""Read tools for the factory agents.

The tool surface IS the security boundary (FACTORY_TOOLS_API_DESIGN_2026-09-13):
Cedar keys off tool identity, and "the Scientist cannot promote" is only
enforceable because no promote tool exists here to call.

Three rules hold throughout this package:

  1. Reads are PROJECTIONS, not tables. No tool returns a raw row, so schema
     changes do not leak into prompts and large blobs never enter a context
     window unasked.
  2. Nothing here opens a database connection. Every tool takes an injected
     `Executor`, mirroring `workflows.ledger.LedgerWriter`. `scripts/test.sh`
     points DATABASE_URL at an unresolvable host on purpose, so a tool that
     opened its own pool would fail every test -- which is the intended
     pressure, not an obstacle.
  3. Agent-authored free text is UNTRUSTED. `failures.lesson`, kill memos and
     `hypotheses.claim` are written by agents, so search_memory is an
     agent-to-agent channel. Such text is delivered inside a labelled envelope,
     never as prose the model reads as narration.
"""
from bharat_terminal.factory.tools_api.catalogue import BATTERY, battery_catalog
from bharat_terminal.factory.tools_api.envelope import (
    MAX_ROWS,
    MAX_TEXT_CHARS,
    envelope,
    untrusted,
)

__all__ = [
    "BATTERY",
    "MAX_ROWS",
    "MAX_TEXT_CHARS",
    "battery_catalog",
    "envelope",
    "untrusted",
]
