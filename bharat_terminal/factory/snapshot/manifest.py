"""Snapshot manifests.

The manifest is what makes "reproducible" checkable rather than aspirational:
every table carries its row count, its maximum date and the sha256 of the file
written. `Manifest.verify()` re-hashes the files, so a silently rewritten
snapshot is detectable rather than merely unlikely.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

CHUNK = 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def dataset_version_for(day: date, sequence: int = 1) -> str:
    """DS-YYYYMMDD-NN — sortable, and unique per snapshot run within a day."""
    if sequence < 1 or sequence > 99:
        raise ValueError("sequence must be 1..99")
    return f"DS-{day.strftime('%Y%m%d')}-{sequence:02d}"


@dataclass(frozen=True)
class TableSnapshot:
    table: str
    rows: int
    sha256: str
    file: str
    max_date: Optional[str] = None      # ISO date of the table's clock column
    bytes: int = 0
    note: str = ""

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, object]) -> "TableSnapshot":
        return TableSnapshot(
            table=str(d["table"]), rows=int(d["rows"]), sha256=str(d["sha256"]),
            file=str(d["file"]), max_date=d.get("max_date"),  # type: ignore[arg-type]
            bytes=int(d.get("bytes", 0)), note=str(d.get("note", "")),
        )


@dataclass(frozen=True)
class Manifest:
    dataset_version: str
    created_at: datetime
    sealed_from: date
    tables: Dict[str, TableSnapshot] = field(default_factory=dict)
    embargoes: List[Dict[str, str]] = field(default_factory=list)
    code_commit: Optional[str] = None
    seed: int = 42

    def to_dict(self) -> Dict[str, object]:
        return {
            "dataset_version": self.dataset_version,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
            "sealed_from": self.sealed_from.isoformat(),
            "tables": {k: v.to_dict() for k, v in sorted(self.tables.items())},
            "embargoes": self.embargoes,
            "code_commit": self.code_commit,
            "seed": self.seed,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def from_dict(d: Dict[str, object]) -> "Manifest":
        return Manifest(
            dataset_version=str(d["dataset_version"]),
            created_at=datetime.fromisoformat(str(d["created_at"])),
            sealed_from=date.fromisoformat(str(d["sealed_from"])),
            tables={k: TableSnapshot.from_dict(v)  # type: ignore[arg-type]
                    for k, v in dict(d.get("tables", {})).items()},
            embargoes=list(d.get("embargoes", [])),  # type: ignore[arg-type]
            code_commit=d.get("code_commit"),        # type: ignore[arg-type]
            seed=int(d.get("seed", 42)),
        )

    @property
    def digest(self) -> str:
        """One hash covering the whole snapshot — cheap equality for runs."""
        return sha256_bytes(self.to_json().encode())

    def age_hours(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - self.created_at).total_seconds() / 3600.0

    def verify(self, root: str) -> List[str]:
        """Re-hash every file. Returns a list of problems (empty = intact)."""
        problems: List[str] = []
        for name, snap in sorted(self.tables.items()):
            path = os.path.join(root, snap.file)
            if not os.path.exists(path):
                problems.append(f"{name}: file missing ({snap.file})")
                continue
            actual = sha256_file(path)
            if actual != snap.sha256:
                problems.append(f"{name}: sha256 mismatch (manifest {snap.sha256[:12]}…, "
                                f"file {actual[:12]}…)")
        return problems

    def max_date(self, table: str) -> Optional[date]:
        snap = self.tables.get(table)
        if snap is None or not snap.max_date:
            return None
        return date.fromisoformat(snap.max_date)

    def rows(self, table: str) -> Optional[int]:
        snap = self.tables.get(table)
        return snap.rows if snap else None
