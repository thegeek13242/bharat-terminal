"""Claim reconciliation — nothing ships that the ledger cannot back.

Every number in agent prose must match a fact value, and every H-/EXP-/FAIL-/
DS- identifier must exist in the facts. Sentences that fail are DROPPED, not
flagged for someone to notice later, and the drop count goes into the report's
own quality block.

Tolerance is precision-aware, following the lesson from
`bharat_terminal/outreach/checks.py`: "12.2%" against a true 12.6% is wrong, so
a stated figure is matched to within half of its own last digit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# Numbers, with optional sign, thousands separators, decimals, % or x suffix.
_NUM_RE = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d+(?:\.\d+)?")
_ID_RE = re.compile(r"\b(?:H-\d{6}|EXP-\d{6}|FAIL-\d{5}|RQ-\d{6}|DS-\d{8}-\d{2})\b")
_ABBREV = ("vs.", "Rs.", "Cr.", "approx.", "e.g.", "i.e.", "No.")

# Words that assert more than a ledger row can support.
_OVERCLAIM = (
    "proves", "proven", "guaranteed", "certain", "certainly", "definitely",
    "always", "never fails", "risk-free", "riskless", "sure thing",
)


@dataclass
class ReconcileResult:
    kept: List[str] = field(default_factory=list)
    dropped: List[Tuple[str, str]] = field(default_factory=list)   # (sentence, reason)

    @property
    def text(self) -> str:
        return " ".join(self.kept)

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)

    @property
    def clean(self) -> bool:
        return not self.dropped

    def as_dict(self) -> Dict[str, object]:
        return {
            "kept": len(self.kept),
            "dropped": [{"sentence": s, "reason": r} for s, r in self.dropped],
            "clean": self.clean,
        }


def split_sentences(text: str) -> List[str]:
    """Sentence split that survives 'vs.' and 'Rs.' — the abbreviation trap."""
    if not text:
        return []
    guarded = text
    for i, ab in enumerate(_ABBREV):
        guarded = guarded.replace(ab, f"\x00{i}\x00")
    parts = re.split(r"(?<=[.!?])\s+", guarded)
    out = []
    for p in parts:
        for i, ab in enumerate(_ABBREV):
            p = p.replace(f"\x00{i}\x00", ab)
        p = p.strip()
        if p:
            out.append(p)
    return out


def _parse(num: str) -> float:
    return float(num.replace(",", ""))


def _tolerance(literal: str) -> float:
    """Half of the last stated digit — so precision claimed is precision checked."""
    if "." in literal:
        decimals = len(literal.split(".")[1])
        return 0.5 * (10 ** -decimals) + 1e-9
    return 0.5 + 1e-9


def _matches_any(value: float, literal: str, allowed: Sequence[float]) -> bool:
    tol = _tolerance(literal)
    for a in allowed:
        if abs(value - a) <= tol:
            return True
        # A figure may be quoted as a percentage of a stored fraction.
        if abs(value - a * 100.0) <= tol:
            return True
    return False


def reconcile_narrative(
    text: str,
    allowed_numbers: Sequence[float],
    allowed_ids: Sequence[str] = (),
    forbid_overclaims: bool = True,
) -> ReconcileResult:
    """Keep only sentences whose every number and ID is backed by the facts."""
    result = ReconcileResult()
    allowed = [float(n) for n in allowed_numbers]
    known_ids = set(allowed_ids)

    for sentence in split_sentences(text):
        lowered = sentence.lower()

        if forbid_overclaims:
            hit = next((w for w in _OVERCLAIM if w in lowered), None)
            if hit:
                result.dropped.append((sentence, f"overclaim:{hit}"))
                continue

        bad_id = next((i for i in _ID_RE.findall(sentence) if i not in known_ids), None)
        if bad_id:
            result.dropped.append((sentence, f"unknown_id:{bad_id}"))
            continue

        bad_num = None
        for literal in _NUM_RE.findall(sentence):
            # Skip numbers that are part of an identifier we already cleared.
            if any(literal in i for i in _ID_RE.findall(sentence)):
                continue
            value = _parse(literal)
            if not _matches_any(value, literal, allowed):
                bad_num = literal
                break
        if bad_num is not None:
            result.dropped.append((sentence, f"unbacked_number:{bad_num}"))
            continue

        result.kept.append(sentence)

    return result
