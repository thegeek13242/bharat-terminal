"""Feature 1 — 'is the news already priced in?'

If a stock has already moved materially in the SAME direction as the news
sentiment, the market has likely priced the news in, so its impact magnitude is
downgraded (bounded) before signals are generated. Runs as a pipeline node
between stage3 (impact) and stage4 (propagation) so 2nd/3rd-degree impacts
inherit the corrected base magnitude.

Deterministic and fail-open: no LLM, no sampling; any price-lookup failure
leaves the impact untouched.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Optional, Tuple

from bharat_terminal.types import CompanyImpact
from bharat_terminal.analysis.recent_move import get_recent_move

logger = logging.getLogger(__name__)

PRICED_IN_ENABLED = os.getenv("PRICED_IN_ENABLED", "true").lower() == "true"
# Minimum same-direction move (percent) before we consider the news priced in.
PRICED_IN_MIN_MOVE_PCT = float(os.getenv("PRICED_IN_MIN_MOVE_PCT", "2.0"))
# Second threshold for a 2-step downgrade.
PRICED_IN_BIG_MOVE_PCT = float(os.getenv("PRICED_IN_BIG_MOVE_PCT", "4.0"))

_SENTIMENT_SIGN = {"positive": 1, "negative": -1, "neutral": 0}


def compute_downgrade(
    sentiment: str, magnitude: int, move_pct: Optional[float]
) -> Tuple[int, Optional[str]]:
    """Pure, deterministic downgrade.

    Returns (new_magnitude, reason). reason is None when nothing changed.
    Downgrade only when the recent move is in the SAME direction as the news:
      |move| >= MIN  -> magnitude - 1
      |move| >= BIG  -> magnitude - 2
    Clamped to a floor of 1 (CompanyImpact.magnitude has ge=1). Neutral news,
    opposite-direction moves, sub-threshold moves, or unknown move -> unchanged.
    """
    expected = _SENTIMENT_SIGN.get(sentiment, 0)
    if expected == 0 or move_pct is None:
        return magnitude, None

    same_direction = (move_pct > 0) == (expected > 0) and abs(move_pct) >= PRICED_IN_MIN_MOVE_PCT
    if not same_direction:
        return magnitude, None

    steps = 2 if abs(move_pct) >= PRICED_IN_BIG_MOVE_PCT else 1
    new_mag = max(1, magnitude - steps)
    if new_mag == magnitude:
        return magnitude, None

    reason = (
        f"[priced-in: stock already moved {move_pct:+.1f}% recently in the same "
        f"direction as {sentiment} news -> magnitude {magnitude}->{new_mag}]"
    )
    return new_mag, reason


async def apply_priced_in(impacts: List[CompanyImpact]) -> Tuple[List[CompanyImpact], List[dict]]:
    """Downgrade direct (hop_distance==0) impacts that are already priced in.

    Propagated impacts (hop_distance>0) are left to stage4, which decays from the
    corrected base. Returns (possibly-new impacts list, adjustment records).
    """
    if not PRICED_IN_ENABLED or not impacts:
        return impacts, []

    direct = [i for i in impacts if i.hop_distance == 0]
    if not direct:
        return impacts, []

    # Fetch recent moves concurrently; fail-open per symbol.
    async def _move(sym: str) -> Optional[float]:
        try:
            return await get_recent_move(sym)
        except Exception as e:
            logger.debug(f"[priced_in] move fetch error {sym}: {e}")
            return None

    moves = await asyncio.gather(*[_move(i.symbol) for i in direct])
    move_by_symbol = {i.symbol: m for i, m in zip(direct, moves)}

    out: List[CompanyImpact] = []
    adjustments: List[dict] = []
    for imp in impacts:
        move = move_by_symbol.get(imp.symbol) if imp.hop_distance == 0 else None
        new_mag, reason = compute_downgrade(imp.sentiment, imp.magnitude, move)
        if reason is None:
            out.append(imp)
            continue
        out.append(imp.model_copy(update={
            "magnitude": new_mag,
            "explanation": f"{imp.explanation} {reason}".strip(),
        }))
        adjustments.append({
            "symbol": imp.symbol,
            "from_magnitude": imp.magnitude,
            "to_magnitude": new_mag,
            "recent_move_pct": move,
            "sentiment": imp.sentiment,
        })

    if adjustments:
        logger.info(f"[priced_in] downgraded {len(adjustments)} impact(s): "
                    + ", ".join(f"{a['symbol']} {a['from_magnitude']}->{a['to_magnitude']}" for a in adjustments))
    return out, adjustments
