"""Ledger writes — dry-run by default.

Writing to `factory.*` is still a write to the production Postgres instance, so
this class refuses to touch the database unless constructed with
`dry_run=False`, which is a founder-approved action per instance.

In dry-run it records exactly what it would have written, which is what the
tests assert against and what a human reviews before the first real insert.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class PendingWrite:
    table: str
    row: Dict[str, Any]

    def as_sql(self) -> str:
        """Human-readable INSERT for review. Values are json-encoded, not
        interpolated into the statement the driver will run."""
        cols = ", ".join(sorted(self.row))
        vals = ", ".join(f"%({c})s" for c in sorted(self.row))
        return f"INSERT INTO {self.table} ({cols}) VALUES ({vals})"


@dataclass
class LedgerWriter:
    """Collects rows for the factory ledger.

    dry_run=True  -> nothing leaves the process; rows are queued for review.
    dry_run=False -> requires an injected executor (a callable taking
                     (sql, params)); no connection is opened here, so this
                     module can never open one by accident.
    """

    dataset_version: str
    code_commit: Optional[str] = None
    dry_run: bool = True
    executor: Optional[Any] = None
    pending: List[PendingWrite] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.dry_run and self.executor is None:
            raise ValueError("a live LedgerWriter requires an explicit executor")

    # ── row builders ────────────────────────────────────────────────────

    def hypothesis(self, spec_json: Dict[str, Any], claim: Dict[str, str],
                   family: str, data_tier: str, created_by: str) -> PendingWrite:
        return self._add("factory.hypotheses", {
            "hypothesis_id": spec_json["id"],
            "version": spec_json.get("version", 1),
            "family_id": family,
            "claim": json.dumps(claim),
            "spec_json": json.dumps(spec_json),
            "spec_yaml": "",
            "horizon": spec_json.get("horizon"),
            "data_tier": data_tier,
            "created_by": created_by,
        })

    def experiment(self, experiment_id: str, hypothesis_id: str, version: int,
                   spec_json: Dict[str, Any], spec_hash: str, trial_count: int,
                   bias_flags: Sequence[str], constraints: Sequence[str],
                   created_by: str) -> PendingWrite:
        return self._add("factory.experiments", {
            "experiment_id": experiment_id,
            "hypothesis_id": hypothesis_id,
            "hypothesis_version": version,
            "spec": json.dumps(spec_json),
            "spec_hash": spec_hash,
            "trial_count": trial_count,
            "bias_flags": json.dumps(list(bias_flags)),
            "constraints": json.dumps(list(constraints)),
            "created_by": created_by,
        })

    def experiment_run(self, experiment_id: str, result: Dict[str, Any],
                       result_hash: str, feature_versions: Dict[str, str],
                       seed: int, window_role: str, cpu_seconds: float,
                       trace_id: Optional[str] = None) -> PendingWrite:
        return self._add("factory.experiment_runs", {
            "experiment_id": experiment_id,
            "dataset_version": self.dataset_version,
            "feature_versions": json.dumps(feature_versions),
            "code_commit": self.code_commit or "",
            "seed": seed,
            "window_role": window_role,
            "status": "succeeded",
            "result": json.dumps(result),
            "result_hash": result_hash,
            "cpu_seconds": cpu_seconds,
            "trace_id": trace_id,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })

    def stat_test(self, kind: str, output: Dict[str, Any],
                  p_value: Optional[float], passed: Optional[bool]) -> PendingWrite:
        return self._add("factory.stat_tests", {
            "kind": kind,
            "output": json.dumps(output),
            "p_value": p_value,
            "passed": passed,
        })

    def gate_decision(self, hypothesis_id: str, version: int, outcome: Dict[str, Any],
                      decided_by: str = "gate_engine") -> PendingWrite:
        return self._add("factory.gate_decisions", {
            "hypothesis_id": hypothesis_id,
            "version": version,
            "from_status": outcome["from_status"],
            "to_status": outcome["to_status"],
            "gate_version": outcome["gate_version"],
            "inputs": json.dumps({"dataset_version": self.dataset_version}),
            "rule_results": json.dumps(outcome["rule_results"]),
            "decided_by": decided_by,
        })

    # ── plumbing ────────────────────────────────────────────────────────

    def _add(self, table: str, row: Dict[str, Any]) -> PendingWrite:
        w = PendingWrite(table=table, row=row)
        self.pending.append(w)
        return w

    def flush(self) -> int:
        """Apply queued writes. In dry-run this is a no-op that keeps them."""
        if self.dry_run:
            return 0
        applied = 0
        for w in self.pending:
            self.executor(w.as_sql(), w.row)   # type: ignore[misc]
            applied += 1
        self.pending.clear()
        return applied

    def review(self) -> str:
        """What would be written, for a human to read before approving."""
        if not self.pending:
            return "(no pending writes)"
        lines = [f"{len(self.pending)} pending write(s) against dataset "
                 f"{self.dataset_version}:"]
        for w in self.pending:
            keys = {k: v for k, v in w.row.items()
                    if k in ("hypothesis_id", "experiment_id", "spec_hash",
                             "result_hash", "to_status", "kind")}
            lines.append(f"  {w.table}: {keys or '(row)'}")
        return "\n".join(lines)
