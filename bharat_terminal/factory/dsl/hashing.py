"""Canonical hashing — the factory's duplicate detector and reproducibility key.

`spec_hash` covers only the *mechanics* of an experiment (universe, event,
signal, entry, exit, benchmark, costs, controls, statistics, validation,
sweep). Prose, ids, family and lineage are excluded on purpose: two agents
that dress the same experiment in different words must collide, so the
compiler can return the existing experiment instead of burning budget twice.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from bharat_terminal.factory.dsl.models import HypothesisSpec

# Mechanics only. Adding a field here changes every hash — treat as a DSL
# version bump.
_HASHED_FIELDS = (
    "dsl_version",
    "horizon",
    "universe",
    "event",
    "signal",
    "entry",
    "exit",
    "benchmark",
    "costs",
    "controls",
    "statistics",
    "validation",
    "sweep",
)


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def spec_hash(spec: HypothesisSpec) -> str:
    payload: Dict[str, Any] = spec.model_dump(mode="json", by_alias=True)
    mechanics = {k: payload[k] for k in _HASHED_FIELDS if k in payload}
    return hashlib.sha256(_canonical(mechanics).encode()).hexdigest()


def sweep_point_hash(point: Dict[str, Any]) -> str:
    """Stable id for one parameter combination inside an experiment."""
    return hashlib.sha256(_canonical(point).encode()).hexdigest()[:16]


def result_hash(result: Dict[str, Any]) -> str:
    """Hash of a run's numeric result — E6 reproducibility compares this.

    Floats are rounded to 10 decimals so a re-run on the same data is
    bit-noise tolerant but genuinely different numbers still diverge.
    """

    def _round(o: Any) -> Any:
        if isinstance(o, float):
            return round(o, 10)
        if isinstance(o, dict):
            return {k: _round(v) for k, v in sorted(o.items())}
        if isinstance(o, (list, tuple)):
            return [_round(v) for v in o]
        return o

    return hashlib.sha256(_canonical(_round(result)).encode()).hexdigest()
