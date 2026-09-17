"""What mechanics the ledger has already seen.

E010 refuses a spec whose mechanics already exist. It used to consult
`factory.experiments` alone -- a table that is only written when a cycle
actually RUNS an experiment.

The observational pilot of 2026-09-17 evaluates dry on purpose, to keep a
finite alpha-wealth budget from being spent on a survey. So no experiment row
was ever written, the dedup index stayed frozen at 2 entries while hypotheses
grew to 24, and 33% of what the Scientist produced was duplicate mechanics --
one group four copies deep, and the only hypothesis that ever cleared the gate
(H-000007) had a twin (H-000009) proposed two cycles later.

A proposed-but-unrun hypothesis is still a duplicate. Both sources count now.

`writes.py` already documented this gap from the other direction: "A hypothesis
row carries no spec_hash column, so two agents can submit identical mechanics
and both land as hypotheses." Hashes are recomputed here rather than stored,
which costs one parse per hypothesis per cycle and needs no migration. If the
ledger grows past a few thousand rows, store the column instead.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Dict

from bharat_terminal.factory.dsl.hashing import spec_hash
from bharat_terminal.factory.dsl.models import HypothesisSpec

Executor = Callable[[str, Dict[str, Any]], Awaitable[Any]]


async def known_mechanics(ex: Executor) -> Dict[str, str]:
    """Map spec_hash -> the id that already carries those mechanics.

    Experiments win ties: "already run as EXP-000001" is a stronger citation
    than "already proposed as H-000007". Among hypotheses the lowest id wins,
    so the cited original is stable across cycles rather than drifting to
    whichever duplicate was written most recently.
    """
    out: Dict[str, str] = {}

    rows = await ex(
        """SELECT hypothesis_id, spec_json FROM factory.hypotheses
            ORDER BY hypothesis_id""", {})
    for r in rows:
        raw = r["spec_json"]
        try:
            stored = json.loads(raw) if isinstance(raw, str) else raw
            spec = HypothesisSpec.model_validate(stored)
        except Exception:
            # A spec that cannot be re-read cannot be deduped against. Skipping
            # it risks a duplicate; failing here would take the whole cycle
            # down over one bad row. Fail soft, stay open for business.
            continue
        out.setdefault(spec_hash(spec), r["hypothesis_id"])

    for e in await ex("SELECT spec_hash, experiment_id FROM factory.experiments", {}):
        out[e["spec_hash"]] = e["experiment_id"]

    return out


def without_self(known: Dict[str, str], hypothesis_id: str) -> Dict[str, str]:
    """The dedup index as one already-stored hypothesis should see it.

    Adding hypotheses to the index fixed cross-cycle duplicates and instantly
    broke re-evaluation: the cycle runner compiles each STORED hypothesis, and
    every one of them found its own ledger row and failed E010 as a duplicate
    of itself. Observed live on 2026-09-17 -- "identical mechanics already
    recorded as H-000026" while compiling H-000026 -- which killed the entire
    evaluate stage.

    Only the self-reference is dropped. If these mechanics are owned by a
    DIFFERENT id the entry stays and the duplicate is still refused, which is
    the whole point of the index.
    """
    return {h: owner for h, owner in known.items() if owner != hypothesis_id}
