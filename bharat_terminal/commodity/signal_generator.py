"""
Turn a CommodityShock into an ImpactReport with TradeSignals.

Signal generation pipeline (deterministic structure, data-backed coefficients):

  shock + KB-resolved (symbol, β, confidence, citations)
        →  CompanyImpact[]   (1st-degree only, strict-mode)
        →  TradeSignal[]     (high-conviction subset)
        →  ImpactReport      (wraps a synthesised NewsItem)

CHANGE FROM v0:
  • betas come from `trigger_betas` (research-agent or regression-derived),
    not from a hand-tuned dict.
  • symbols without a calibrated beta are DROPPED — no fallback. This is the
    "no assumptions" contract.
  • CompanyImpact.explanation now carries the agent's rationale and citation
    count instead of an analyst-style one-liner.

If the KB has not been seeded yet (i.e. nobody has run
`python -m bharat_terminal.research.main seed && … run`), this generator
returns an empty impacts list. That is intentional — better to emit nothing
than to ship hallucinated signals.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import List

from bharat_terminal.commodity.kb_lookup import ResolvedExposure, resolve_exposures
from bharat_terminal.types import (
    CommodityShock,
    CompanyImpact,
    ImpactReport,
    NewsItem,
    TradeSignal,
)

logger = logging.getLogger(__name__)


# Macro-theme tags align with the sentiment classifier taxonomy so existing
# UI filters continue to work.
COMMODITY_MACRO_THEME = {
    "CRUDEOIL":   "OIL_PRICES",
    "BRENT":      "OIL_PRICES",
    "GOLD":       "GOLD_PRICES",
    "SILVER":     "SILVER_PRICES",
    "COPPER":     "COPPER_PRICES",
    "ALUMINIUM":  "ALUMINIUM_PRICES",
    "NATURALGAS": "NATGAS_PRICES",
}

# Map a shock's commodity name to the trigger_id we look up in the KB.
COMMODITY_TO_TRIGGER = {
    "CRUDEOIL":   "CRUDEOIL",
    "GOLD":       "GOLD",
    "SILVER":     "SILVER",
    "COPPER":     "COPPER",
    "ALUMINIUM":  "ALUMINIUM",
    "NATURALGAS": "NATURALGAS",
}

# Continuous price-shock events all flow through the umbrella event id —
# the KB sector edges for `commodity_shock` cover the structural part, and
# the trigger_id discriminates which commodity actually fired.
EVENT_ID = "commodity_shock"

# Maximum number of company impacts to emit per shock (keeps the report
# readable; the highest-|β| names already dominate signal value).
MAX_IMPACTS_PER_SHOCK = 25


# ── Tiering helpers (unchanged from v0) ─────────────────────────────────

def _conviction_for(z_score: float, pct_move: float, beta_confidence: str) -> str:
    z = abs(z_score)
    p = abs(pct_move)
    base = "low"
    if z >= 4.5 and p >= 0.025:
        base = "high"
    elif z >= 3.5 and p >= 0.015:
        base = "medium"
    # Cap at the lower of (shock conviction, beta confidence) — even a huge
    # shock cannot generate a "high" signal if the beta itself is "low"
    # confidence (sparse data, weak citations).
    bc = (beta_confidence or "low").lower()
    cap = {"low": "low", "medium": "medium", "high": "high"}.get(bc, "low")
    order = ["low", "medium", "high"]
    return order[min(order.index(base), order.index(cap))]


def _impact_magnitude(beta: float, shock_pct: float) -> int:
    implied = abs(beta * shock_pct)
    if implied >= 0.025:
        return 5
    if implied >= 0.015:
        return 4
    if implied >= 0.008:
        return 3
    if implied >= 0.004:
        return 2
    return 1


def _position_size_pct(beta: float, shock_pct: float, conviction: str) -> float:
    implied = abs(beta * shock_pct)
    base = min(5.0, implied * 200.0)
    if conviction == "low":
        base *= 0.4
    elif conviction == "medium":
        base *= 0.7
    return round(min(5.0, max(0.5, base)), 2)


def _direction_for(beta: float, shock_direction: str) -> str:
    sign_shock = 1 if shock_direction == "up" else -1
    sign_beta = 1 if beta > 0 else -1
    return "long" if sign_shock * sign_beta > 0 else "short"


def _impact_sentiment(beta: float, shock_direction: str) -> str:
    sign_shock = 1 if shock_direction == "up" else -1
    sign_beta = 1 if beta > 0 else -1
    return "positive" if sign_shock * sign_beta > 0 else "negative"


def _synth_news_item(shock: CommodityShock, n_impacts: int, n_signals: int) -> NewsItem:
    direction = "spiked" if shock.direction == "up" else "dropped"
    headline = (
        f"MCX {shock.commodity} {direction} "
        f"{shock.pct_move*100:+.2f}% in {shock.window_minutes}min "
        f"(₹{shock.baseline_price:.2f} → ₹{shock.spot_price:.2f}, z={shock.z_score:+.1f})"
    )
    body = (
        f"Price-triggered shock on {shock.contract_symbol}. "
        f"Resolved {n_impacts} data-backed company impacts, {n_signals} trade "
        f"signals, all sourced from KB-calibrated betas (no hand-tuned overrides)."
    )
    return NewsItem(
        source="ANGELONE_MCX",
        timestamp_utc=shock.timestamp_utc,
        headline=headline,
        body=body,
        url=f"https://www.mcxindia.com/market-data/get-quote?symbol={shock.contract_symbol}",
        ingest_latency_ms=shock.detection_latency_ms,
        category="COMMODITY_SHOCK",
        symbols_mentioned=[],
        full_body=body,
        body_word_count=len(body.split()),
        body_fetch_status="cached",
        body_extraction_method="synthetic",
        tier="tier1",
    )


# ── Main builder ────────────────────────────────────────────────────────

async def build_impact_report(shock: CommodityShock) -> ImpactReport:
    started = time.time()

    trigger_id = COMMODITY_TO_TRIGGER.get(shock.commodity)
    macro_theme = COMMODITY_MACRO_THEME.get(shock.commodity, "COMMODITY_PRICES")

    if trigger_id is None:
        logger.warning(
            f"[SIGNAL-GEN] No trigger_id mapped for commodity={shock.commodity}; "
            f"emitting empty impact report"
        )
        exposures: List[ResolvedExposure] = []
    else:
        try:
            exposures = await resolve_exposures(event_id=EVENT_ID, trigger_id=trigger_id)
        except Exception as e:
            logger.error(
                f"[SIGNAL-GEN] KB lookup failed for {shock.commodity}: {e}",
                exc_info=True,
            )
            exposures = []

    if not exposures:
        logger.warning(
            f"[SIGNAL-GEN] {shock.commodity}: no calibrated betas found — "
            f"strict-mode dropping all symbols. Run "
            f"`python -m bharat_terminal.research.main seed && run` to populate."
        )

    exposures = exposures[:MAX_IMPACTS_PER_SHOCK]

    company_impacts: List[CompanyImpact] = []
    trade_signals: List[TradeSignal] = []
    affected_sectors_seen: List[str] = []

    move_summary = (
        f"{shock.commodity} {shock.direction} "
        f"{shock.pct_move*100:+.2f}% (z={shock.z_score:+.1f}) "
        f"over {shock.window_minutes}min"
    )

    for exp in exposures:
        sentiment = _impact_sentiment(exp.beta, shock.direction)
        magnitude = _impact_magnitude(exp.beta, shock.pct_move)
        conviction = _conviction_for(shock.z_score, shock.pct_move, exp.confidence)

        n_cit = len(exp.citations or [])
        rationale_short = (exp.rationale or "").strip()
        if len(rationale_short) > 220:
            rationale_short = rationale_short[:217] + "…"

        explanation = (
            f"{rationale_short} | β={exp.beta:+.2f} ({exp.derivation_method}, "
            f"{n_cit} citation{'s' if n_cit != 1 else ''}, conf={exp.confidence}) → "
            f"implied {exp.beta * shock.pct_move * 100:+.2f}% on {move_summary}."
        )

        company_impacts.append(
            CompanyImpact(
                symbol=exp.symbol,
                company_name=exp.company_name,
                sentiment=sentiment,                          # type: ignore[arg-type]
                magnitude=magnitude,
                time_horizon="immediate",
                affected_line_items=[exp.sector] if exp.sector else [],
                explanation=explanation,
                hop_distance=0,
                decay_factor=1.0,
            )
        )

        if exp.sector and exp.sector not in affected_sectors_seen:
            affected_sectors_seen.append(exp.sector)

        # Strict gating on trade-signal emission:
        #   • magnitude must be ≥ 3 (i.e. implied move ≥ ~0.8%)
        #   • conviction must be ≥ medium (≥ 'low' was too noisy)
        #   • beta confidence must be ≥ medium  ← new strictness
        if magnitude < 3 or conviction == "low":
            continue
        if (exp.confidence or "low").lower() == "low":
            continue

        direction = _direction_for(exp.beta, shock.direction)
        size = _position_size_pct(exp.beta, shock.pct_move, conviction)

        trade_signals.append(
            TradeSignal(
                symbol=exp.symbol,
                direction=direction,                          # type: ignore[arg-type]
                instrument_type="equity",
                suggested_strike_rationale=None,
                position_size_pct_of_portfolio=size,
                stop_loss_rationale=(
                    f"Invalidate if {shock.commodity} retraces past baseline "
                    f"(₹{shock.baseline_price:.2f}) within {shock.window_minutes*3} min"
                ),
                conviction=conviction,                         # type: ignore[arg-type]
                reasoning=explanation,
            )
        )

    news = _synth_news_item(shock, len(company_impacts), len(trade_signals))
    latency_ms = (time.time() - started) * 1000.0 + shock.detection_latency_ms

    return ImpactReport(
        news_id=news.id,
        news_item=news,
        relevant=True,
        confidence=min(0.99, 0.5 + min(abs(shock.z_score), 6) / 12),
        macro_theme=macro_theme,
        affected_sectors=affected_sectors_seen,
        company_impacts=company_impacts,
        trade_signals=trade_signals,
        processing_latency_ms=latency_ms,
        created_at=datetime.now(timezone.utc),
    )
