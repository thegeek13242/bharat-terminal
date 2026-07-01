"""
Stage 5: Trade signal generation.
Outputs direction, instrument type, position size, and conviction.
NO specific price targets — rationale only.
"""
import time
import logging
import os
import uuid
from typing import List, Optional
from bharat_terminal import llm
from bharat_terminal.llm import provider_for_model
from bharat_terminal.types import CompanyImpact, TradeSignal, NewsItem
from bharat_terminal.analysis.llm_logger import get_llm_logger, LLMCallRecord, compute_cost
from bharat_terminal.analysis.signal_gates import (
    is_derivative_headline, is_stale_article, filter_signals,
)

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL = os.getenv("SIGNALS_MODEL", "gemini-3.1-pro-preview")
MEMORY_CONTEXT_ENABLED = os.getenv("ANALYSIS_MEMORY_CONTEXT_ENABLED", "1") == "1"

SIGNAL_TOOL = {
    "name": "generate_trade_signals",
    "description": "Generate actionable trade signals based on market impact analysis",
    "input_schema": {
        "type": "object",
        "properties": {
            "signals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "direction": {"type": "string", "enum": ["long", "short", "neutral"]},
                        "instrument_type": {"type": "string", "enum": ["equity", "futures", "options_call", "options_put", "avoid"]},
                        "suggested_strike_rationale": {"type": "string", "description": "Rationale for strike selection (no specific prices)"},
                        "position_size_pct_of_portfolio": {"type": "number", "minimum": 0, "maximum": 10},
                        "stop_loss_rationale": {"type": "string"},
                        "conviction": {"type": "string", "enum": ["low", "medium", "high"]},
                        "reasoning": {"type": "string", "maxLength": 400}
                    },
                    "required": ["symbol", "direction", "instrument_type", "position_size_pct_of_portfolio", "stop_loss_rationale", "conviction", "reasoning"]
                }
            }
        },
        "required": ["signals"]
    }
}


async def generate_signals(
    news_item: NewsItem,
    company_impacts: List[CompanyImpact],
    macro_theme: str,
    affected_sectors: Optional[List[str]] = None,
) -> List[TradeSignal]:
    """
    Generate trade signals for high-impact direct companies (hop_distance=0, magnitude≥3).
    Returns list of TradeSignal objects.
    """
    llm_logger = get_llm_logger()

    # Derivative content (recaps, gainers/losers, third-party recommendations,
    # multibagger stories) describes moves that already happened or recycles
    # someone else's call — never signal from it, and skip the LLM cost.
    if is_derivative_headline(news_item.headline):
        logger.info(f"[stage5] skipped derivative headline: {news_item.headline[:80]!r}")
        return []

    # Stale article: published too long before we processed it (slow feed /
    # syndicated re-publish). The event is already priced — never signal.
    if is_stale_article(news_item):
        logger.info(f"[stage5] skipped stale article ({news_item.timestamp_utc}): "
                    f"{news_item.headline[:80]!r}")
        return []

    # Only generate signals for direct, material impacts
    signal_candidates = [
        imp for imp in company_impacts
        if imp.hop_distance == 0 and imp.magnitude >= 3
    ]

    if not signal_candidates:
        return []

    # Mock only when the console-Anthropic backend is selected but has no key.
    # Vertex/Gemini backends authenticate via GCP ADC, not ANTHROPIC_API_KEY.
    if provider_for_model(LLM_MODEL) == "anthropic" and not ANTHROPIC_API_KEY:
        return [
            TradeSignal(
                symbol=imp.symbol,
                direction="neutral",
                instrument_type="equity",
                position_size_pct_of_portfolio=1.0,
                stop_loss_rationale="Monitor key support levels",
                conviction="low",
                reasoning=f"Mock signal for {imp.symbol} (no API key configured)",
            )
            for imp in signal_candidates[:3]
        ]

    impact_summaries = "\n".join([
        f"- {imp.symbol}: {imp.sentiment.upper()}, magnitude={imp.magnitude}/5, "
        f"horizon={imp.time_horizon}, items={imp.affected_line_items}, "
        f"reason={imp.explanation[:150]}"
        for imp in signal_candidates
    ])

    # Market/sector regime backdrop (fail-soft, gated). Company-specific memory was
    # already folded into the impact magnitudes at Stage 3, so fetch regime only.
    backdrop = ""
    if MEMORY_CONTEXT_ENABLED:
        from bharat_terminal.analysis import memory_client
        mem_context = await memory_client.fetch_memory_context(
            headline=news_item.headline, macro_theme=macro_theme,
            sectors=affected_sectors,
            symbols=[imp.symbol for imp in signal_candidates],
            include_company=False)
        if mem_context:
            backdrop = memory_client.market_and_sector_backdrop(mem_context)
    backdrop_section = f"\nMARKET & SECTOR BACKDROP:\n{backdrop}\n" if backdrop else ""

    prompt = f"""You are an Indian equity market trading advisor. Generate trade signals based on the following analysis.

NEWS TRIGGER:
{news_item.headline}

MACRO THEME: {macro_theme}
{backdrop_section}
COMPANY IMPACTS (direct exposures only):
{impact_summaries}

INSTRUCTIONS:
- Generate signals only for companies with clear, material impact (magnitude ≥ 3)
- Weigh the MARKET & SECTOR BACKDROP (if shown): temper conviction when a signal
  fights the prevailing regime; a supportive regime can reinforce it
- For HIGH conviction: news has large, unambiguous directional impact
- For MEDIUM conviction: impact is clear but market may partially price it in
- For LOW conviction: impact is real but uncertain in magnitude or timing
- Position size: HIGH conviction max 5%, MEDIUM max 3%, LOW max 1.5%
- DO NOT suggest specific price targets or specific option strikes
- For options: describe the rationale (e.g., "ATM call for near-term momentum play with defined risk")
- Stop loss must reference a logical framework (support levels, volatility bands, event re-rating)"""

    llm_start = time.time()
    try:
        # Async client so the call never blocks the event loop (see stage3 note).
        response = await llm.acomplete(
            model=LLM_MODEL,
            max_tokens=2048,
            tools=[SIGNAL_TOOL],
            tool_choice={"type": "tool", "name": "generate_trade_signals"},
            messages=[{"role": "user", "content": prompt}],
        )

        llm_latency = (time.time() - llm_start) * 1000
        record = LLMCallRecord(
            call_id=str(uuid.uuid4()),
            model=LLM_MODEL,
            stage="stage5_signals",
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            latency_ms=llm_latency,
            cost_usd=compute_cost(LLM_MODEL, response.usage.input_tokens, response.usage.output_tokens),
            success=True,
        )
        llm_logger.log(record)

        tool_result = None
        for block in response.content:
            if block.type == "tool_use" and block.name == "generate_trade_signals":
                tool_result = block.input
                break

        if not tool_result:
            return []

        signals = []
        for s in tool_result.get("signals", []):
            signals.append(TradeSignal(
                symbol=s["symbol"],
                direction=s["direction"],
                instrument_type=s["instrument_type"],
                suggested_strike_rationale=s.get("suggested_strike_rationale"),
                position_size_pct_of_portfolio=s["position_size_pct_of_portfolio"],
                stop_loss_rationale=s["stop_loss_rationale"],
                conviction=s["conviction"],
                reasoning=s["reasoning"],
            ))

        # Quality gates: canonical NSE symbol (drop unroutable), dedup window.
        return filter_signals(signals, news_item.headline)

    except Exception as e:
        llm_latency = (time.time() - llm_start) * 1000
        record = LLMCallRecord(
            call_id=str(uuid.uuid4()),
            model=LLM_MODEL,
            stage="stage5_signals",
            prompt_tokens=0, completion_tokens=0,
            latency_ms=llm_latency, cost_usd=0.0,
            success=False, error=str(e),
        )
        llm_logger.log(record)
        logger.error(f"Stage 5 error: {e}")
        return []
