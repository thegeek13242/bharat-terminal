"""Deterministic materiality extraction — pure functions, no I/O.

Extracts the headline monetary value of a filing (order/contract wins) and
converts it to a ratio against company scale. Pass/fail decisions on signals
must come from these code paths, never from LLM prose (frozen gate_version
v1_2026_07; see plan 2026-07-04).

Conservative by design: when no value can be extracted with confidence the
answer is None, and RATIO_REQUIRED categories then fail closed (no signal,
counted as ratio_extraction_failed — a tracked rate).
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

# Frozen v1 threshold: order value must be ≥ 10% of TTM revenue.
MATERIALITY_RATIO_MIN = 0.10

# Unit → crore multiplier (INR). USD amounts are skipped in v1 (fail-closed).
_UNIT_TO_CR = {
    "crore": 1.0, "crores": 1.0, "cr": 1.0, "cr.": 1.0,
    "lakh": 0.01, "lakhs": 0.01, "lac": 0.01, "lacs": 0.01,
    "million": 0.1, "mn": 0.1,       # INR million = 0.1 Cr
    "billion": 100.0, "bn": 100.0,   # INR billion = 100 Cr
}

# ₹/Rs/INR-prefixed amount with an optional unit, or a bare "<num> crore/lakh".
_AMOUNT_RE = re.compile(
    r"""
    (?P<usd>(?:USD|US\$|\$)\s*)?                                   # USD → skip
    (?:₹|Rs\.?|INR\.?)?\s*
    (?P<num>\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?)
    \s*
    (?P<unit>crores?|cr\.?|lakhs?|lacs?|million|mn|billion|bn)\b
    """,
    re.I | re.X,
)

# An amount only counts when an order-ish keyword appears nearby — a filing
# body is full of incidental numbers (paid-up capital, face value, dates).
_CONTEXT_KEYWORDS = re.compile(
    r"order|contract|loi|letter\s+of\s+intent|work\s*order|project|"
    r"valued|worth|aggregating|amounting|bagged|awarded|received",
    re.I,
)
_CONTEXT_WINDOW = 250  # chars on each side of the amount

# Sanity bounds (Cr): outside these the number is treated as garbage/noise.
_MIN_VALUE_CR = 0.05
_MAX_VALUE_CR = 5_000_000.0


def extract_value_cr(text: str) -> Optional[float]:
    """Largest INR amount (in ₹ Cr) appearing near an order/contract keyword.

    Returns None when nothing qualifies — including USD-denominated amounts
    (v1 skips them rather than guessing an FX rate).
    """
    if not text:
        return None
    candidates: List[Tuple[float, int]] = []
    for m in _AMOUNT_RE.finditer(text):
        if m.group("usd"):
            continue  # v1: fail-closed on foreign currency
        unit = (m.group("unit") or "").lower().rstrip(".")
        mult = _UNIT_TO_CR.get(unit)
        if mult is None:
            continue
        try:
            value = float(m.group("num").replace(",", "")) * mult
        except ValueError:
            continue
        if not (_MIN_VALUE_CR <= value <= _MAX_VALUE_CR):
            continue
        candidates.append((value, m.start()))

    if not candidates:
        return None

    near_keyword = [
        v for v, pos in candidates
        if _CONTEXT_KEYWORDS.search(text[max(0, pos - _CONTEXT_WINDOW): pos + _CONTEXT_WINDOW])
    ]
    if not near_keyword:
        return None
    return max(near_keyword)


def materiality_ratio(value_cr: Optional[float],
                      revenue_ttm_cr: Optional[float]) -> Optional[float]:
    """value / TTM revenue. None when either side is missing/degenerate —
    callers in RATIO_REQUIRED contexts must treat None as fail."""
    if value_cr is None or not revenue_ttm_cr or revenue_ttm_cr <= 0:
        return None
    return round(value_cr / revenue_ttm_cr, 4)


def ratio_passes(ratio: Optional[float]) -> bool:
    return ratio is not None and ratio >= MATERIALITY_RATIO_MIN
