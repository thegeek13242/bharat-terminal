"""
Deterministic classifier: SentimentItem → SentimentSnapshot.

v1 is rule-based against `taxonomy.THEMES`. We deliberately avoid an LLM call
in the hot path because:
  • Polymarket cycles are 60-300s; throughput matters.
  • Markets have stable wordings — keyword matching is high-precision.
  • LLM cost on 100s of markets per cycle is not worth it for a v1.

A future v2 can add an LLM fallback for items that miss the taxonomy
(`theme_confidence < 0.3`) and route them to a separate
`raw.sentiment.unclassified.in` topic for offline review.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from bharat_terminal.types import SentimentItem, SentimentSnapshot
from bharat_terminal.sentiment.taxonomy import (
    Theme,
    find_theme,
    resolve_direction,
    scale_magnitude,
)

logger = logging.getLogger(__name__)


def classify(item: SentimentItem) -> Optional[SentimentSnapshot]:
    """
    Classify a SentimentItem against the taxonomy.

    Returns None if:
      • No theme matched (out-of-universe market that slipped past the keyword
        ingest filter — log and skip).
      • |delta_1h| is below the theme's noise floor AND we already have a
        prev_probability (i.e. not a fresh market).

    Otherwise returns a SentimentSnapshot with direction, magnitude, sectors,
    and a human-readable explanation.
    """
    started = time.time()

    haystack_parts = [item.question or ""]
    haystack_parts.extend(item.tags or [])
    if item.market_url:
        haystack_parts.append(item.market_url)
    haystack = " ".join(haystack_parts)

    theme = find_theme(haystack)
    if theme is None:
        logger.debug(
            f"[CLASSIFY] No theme match for market {item.market_id}: "
            f"'{item.question[:80]}'"
        )
        return None

    delta = item.delta_1h
    delta_abs = abs(delta) if delta is not None else 0.0

    # Noise gate: skip stale markets that barely moved.
    # First-observation (prev_probability is None) always passes — we want the
    # initial point on the chart.
    if item.prev_probability is not None and delta_abs < theme.noise_floor:
        return None

    direction = resolve_direction(theme, delta)
    magnitude = scale_magnitude(theme.base_magnitude, delta_abs)

    delta_pp = (delta or 0.0) * 100.0
    explanation = theme.explanation_template.format(
        question=item.question,
        delta_pp=delta_pp,
        prob=item.probability,
        theme=theme.name,
        direction=direction,
    )

    # Confidence: 0.9 if we matched a theme keyword cleanly, scaled down if
    # the question is unusually short or generic.
    if len(item.question) < 30:
        theme_conf = 0.6
    elif theme.direction_on_yes in ("ambiguous", "neutral"):
        theme_conf = 0.5     # match was strong but direction is uncertain
    else:
        theme_conf = 0.85

    snapshot = SentimentSnapshot(
        sentiment_id=item.id,
        market_id=item.market_id,
        source=item.source,
        timestamp_utc=item.timestamp_utc,
        question=item.question,
        market_url=item.market_url,
        macro_theme=theme.name,
        theme_confidence=theme_conf,
        direction=direction,
        magnitude=magnitude,
        affected_sectors=list(
            theme.sectors_positive if direction == "bullish_in" else theme.sectors_negative
        ) or list(theme.sectors_positive),
        watchlist_symbols=list(theme.watchlist_symbols),
        explanation=explanation,
        probability=item.probability,
        prev_probability=item.prev_probability,
        delta_1h=item.delta_1h,
        volume_24h=item.volume_24h,
        processing_latency_ms=(time.time() - started) * 1000.0,
        created_at=datetime.now(timezone.utc),
    )

    return snapshot
