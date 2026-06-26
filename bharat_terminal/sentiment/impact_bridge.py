"""Feature 3(b) — Polymarket sentiment → company ImpactReport bridge.

The sentiment worker classifies Polymarket moves into themes (OIL_PRICES, …)
but never turned them into company-level impacts. This bridge closes that gap by
reusing the EXACT KB path the commodity engine uses: a classified
`SentimentSnapshot` is mapped to a commodity `trigger_id`, exposures are resolved
from `trigger_betas` (via `resolve_exposures`), and per-company impacts/signals
are built with the same tiering helpers as `commodity/signal_generator.py`.

A prediction-market probability move is converted to an implied *commodity*
direction first: for these price themes YES == "commodity price higher", and the
taxonomy already resolved the equity-direction, so
    commodity_direction = "up" if snapshot.direction == theme.direction_on_yes
                           else "down".
GOLD_PRICES (direction_on_yes='ambiguous') and FED_RATES (no commodity trigger)
do not yield a clean per-company commodity direction and are skipped at company
level for v1 (returns None — flagged).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import List, Optional

from bharat_terminal.commodity.kb_lookup import ResolvedExposure, resolve_exposures
from bharat_terminal.commodity.signal_generator import (
    EVENT_ID,
    MAX_IMPACTS_PER_SHOCK,
    _conviction_for,
    _direction_for,
    _impact_magnitude,
    _impact_sentiment,
    _position_size_pct,
)
from bharat_terminal.sentiment.taxonomy import THEMES
from bharat_terminal.types import (
    CompanyImpact,
    ImpactReport,
    NewsItem,
    SentimentSnapshot,
    TradeSignal,
)

logger = logging.getLogger(__name__)

# Themes that map to a directional commodity trigger. GOLD is intentionally
# excluded (ambiguous direction_on_yes → no clean per-company direction);
# FED_RATES has no commodity trigger (sector-level only — out of v1 scope).
THEME_TO_TRIGGER = {
    "OIL_PRICES": "CRUDEOIL",
    "INDUSTRIAL_METALS": "COPPER",
    "NATURAL_GAS": "NATURALGAS",
}

# theme name → its YES-direction equity bias (source of truth: taxonomy.THEMES).
_DIRECTION_ON_YES = {t.name: t.direction_on_yes for t in THEMES}

# SentimentSnapshot.magnitude (1-5) → synthesized commodity-move proxies that
# feed the SAME tiering helpers as a real shock. Deterministic, documented.
_MAGNITUDE_TO_PCT = {1: 0.005, 2: 0.010, 3: 0.020, 4: 0.030, 5: 0.050}


def _commodity_direction(snapshot: SentimentSnapshot) -> Optional[str]:
    """Map the snapshot's resolved equity-direction back to a commodity
    direction (up/down), or None when undefinable (ambiguous/neutral)."""
    bias = _DIRECTION_ON_YES.get(snapshot.macro_theme)
    if bias not in ("bullish_in", "bearish_in"):
        return None
    if snapshot.direction not in ("bullish_in", "bearish_in"):
        return None
    # YES == commodity higher, so the YES-event becoming more likely == "up".
    return "up" if snapshot.direction == bias else "down"


def _synth_news_item(snapshot: SentimentSnapshot, trigger_id: str,
                     direction: str, n_impacts: int, n_signals: int) -> NewsItem:
    move = "higher" if direction == "up" else "lower"
    headline = (
        f"Polymarket implies {trigger_id} {move}: '{snapshot.question[:90]}' "
        f"@ {snapshot.probability:.0%}"
        + (f" ({snapshot.delta_1h:+.2f} /1h)" if snapshot.delta_1h is not None else "")
    )
    body = (
        f"Prediction-market signal ({snapshot.macro_theme}, India-equity bias "
        f"{snapshot.direction}, magnitude {snapshot.magnitude}). Resolved {n_impacts} "
        f"data-backed company impacts and {n_signals} trade signals from KB-calibrated "
        f"{trigger_id} betas. {snapshot.explanation}"
    )
    return NewsItem(
        source="POLYMARKET_SENTIMENT",
        timestamp_utc=snapshot.timestamp_utc,
        headline=headline,
        body=body,
        url=snapshot.market_url or "https://polymarket.com",
        ingest_latency_ms=snapshot.processing_latency_ms,
        category="PREDICTION_MARKET",
        symbols_mentioned=[],
        full_body=body,
        body_word_count=len(body.split()),
        body_fetch_status="cached",
        body_extraction_method="synthetic",
        tier="tier1",
    )


async def build_impact_report_from_sentiment(snapshot: SentimentSnapshot) -> Optional[ImpactReport]:
    """Build a company-level ImpactReport from a classified Polymarket snapshot,
    or None when the theme doesn't map to a directional commodity trigger."""
    started = time.time()
    trigger_id = THEME_TO_TRIGGER.get(snapshot.macro_theme)
    if trigger_id is None:
        return None  # FED_RATES / GOLD / unmapped — no company-level commodity path

    direction = _commodity_direction(snapshot)
    if direction is None:
        return None  # ambiguous/neutral move

    pct_move = _MAGNITUDE_TO_PCT.get(snapshot.magnitude, 0.01)
    # z proxy so magnitude>=4 → 'high', >=3 → 'medium' under _conviction_for.
    z_proxy = 3.0 + 0.5 * snapshot.magnitude

    try:
        exposures: List[ResolvedExposure] = await resolve_exposures(
            event_id=EVENT_ID, trigger_id=trigger_id
        )
    except Exception as e:
        logger.error(f"[SENTIMENT-BRIDGE] KB lookup failed for {trigger_id}: {e}", exc_info=True)
        return None
    if not exposures:
        logger.info(f"[SENTIMENT-BRIDGE] {trigger_id}: no calibrated betas — emitting nothing")
        return None

    exposures = exposures[:MAX_IMPACTS_PER_SHOCK]
    company_impacts: List[CompanyImpact] = []
    trade_signals: List[TradeSignal] = []
    sectors_seen: List[str] = []

    for exp in exposures:
        sentiment = _impact_sentiment(exp.beta, direction)
        magnitude = _impact_magnitude(exp.beta, pct_move)
        conviction = _conviction_for(z_proxy, pct_move, exp.confidence)
        explanation = (
            f"Polymarket {snapshot.macro_theme} → implied {trigger_id} {direction}. "
            f"β={exp.beta:+.2f} ({exp.derivation_method}, conf={exp.confidence}) → "
            f"implied {exp.beta * pct_move * 100:+.2f}%. {(exp.rationale or '')[:160]}"
        )
        company_impacts.append(CompanyImpact(
            symbol=exp.symbol, company_name=exp.company_name,
            sentiment=sentiment, magnitude=magnitude, time_horizon="short_term",
            affected_line_items=[exp.sector] if exp.sector else [],
            explanation=explanation, hop_distance=0, decay_factor=1.0,
        ))
        if exp.sector and exp.sector not in sectors_seen:
            sectors_seen.append(exp.sector)

        # Same strict gating as the commodity engine.
        if magnitude < 3 or conviction == "low" or (exp.confidence or "low").lower() == "low":
            continue
        trade_signals.append(TradeSignal(
            symbol=exp.symbol,
            direction=_direction_for(exp.beta, direction),
            instrument_type="equity", suggested_strike_rationale=None,
            position_size_pct_of_portfolio=_position_size_pct(exp.beta, pct_move, conviction),
            stop_loss_rationale=(
                f"Invalidate if the {snapshot.macro_theme} prediction-market probability "
                f"reverts past {snapshot.probability:.0%}."
            ),
            conviction=conviction, reasoning=explanation,
        ))

    news = _synth_news_item(snapshot, trigger_id, direction,
                            len(company_impacts), len(trade_signals))
    latency_ms = (time.time() - started) * 1000.0

    return ImpactReport(
        news_id=news.id, news_item=news, relevant=True,
        confidence=min(0.95, 0.4 + snapshot.theme_confidence * 0.5),
        macro_theme=snapshot.macro_theme, affected_sectors=sectors_seen,
        company_impacts=company_impacts, trade_signals=trade_signals,
        processing_latency_ms=latency_ms, created_at=datetime.now(timezone.utc),
    )
