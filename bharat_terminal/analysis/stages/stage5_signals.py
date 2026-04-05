"""
Stage 5: Trade signal generation.
Outputs direction, instrument type, position size, and conviction.
NO specific price targets — rationale only.
"""
import time
import logging
import os
import uuid
import anthropic
from typing import List
from bharat_terminal.types import CompanyImpact, TradeSignal, NewsItem
from bharat_terminal.analysis.llm_logger import get_llm_logger, LLMCallRecord, compute_cost

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL = os.getenv("SIGNALS_MODEL", "claude-haiku-4-5-20251001")

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


def generate_signals(
    news_item: NewsItem,
    company_impacts: List[CompanyImpact],
    macro_theme: str,
) -> List[TradeSignal]:
    """
    Generate trade signals for high-impact direct companies (hop_distance=0, magnitude≥3).
    Returns list of TradeSignal objects.
    """
    start_time = time.time()
    llm_logger = get_llm_logger()

    # Only generate signals for direct, material impacts
    signal_candidates = [
        imp for imp in company_impacts
        if imp.hop_distance == 0 and imp.magnitude >= 3
    ]

    if not signal_candidates:
        return []

    if not ANTHROPIC_API_KEY:
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

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    impact_summaries = "\n".join([
        f"- {imp.symbol}: {imp.sentiment.upper()}, magnitude={imp.magnitude}/5, "
        f"horizon={imp.time_horizon}, items={imp.affected_line_items}, "
        f"reason={imp.explanation[:150]}"
        for imp in signal_candidates
    ])

    prompt = f"""You are an Indian equity market trading advisor. Generate trade signals based on the following analysis.

NEWS TRIGGER:
{news_item.headline}

MACRO THEME: {macro_theme}

COMPANY IMPACTS (direct exposures only):
{impact_summaries}

INSTRUCTIONS:
- Generate signals only for companies with clear, material impact (magnitude ≥ 3)
- For HIGH conviction: news has large, unambiguous directional impact
- For MEDIUM conviction: impact is clear but market may partially price it in
- For LOW conviction: impact is real but uncertain in magnitude or timing
- Position size: HIGH conviction max 5%, MEDIUM max 3%, LOW max 1.5%
- DO NOT suggest specific price targets or specific option strikes
- For options: describe the rationale (e.g., "ATM call for near-term momentum play with defined risk")
- Stop loss must reference a logical framework (support levels, volatility bands, event re-rating)"""

    llm_start = time.time()
    try:
        response = client.messages.create(
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

        return signals

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
