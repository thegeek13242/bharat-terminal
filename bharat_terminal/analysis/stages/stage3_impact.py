"""
Stage 3: Per-company impact scoring using Claude Haiku with company context.
SLA: ≤1.5s
"""
import time
import logging
import os
import json
import uuid
import asyncio
import httpx
from typing import List, Optional
from bharat_terminal import llm
from bharat_terminal.llm import provider_for_model
from bharat_terminal.types import NewsItem, CompanyImpact
from bharat_terminal.analysis.llm_logger import get_llm_logger, LLMCallRecord, compute_cost

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL = os.getenv("IMPACT_MODEL", "gemini-2.5-flash")
KNOWLEDGE_BASE_URL = os.getenv("KNOWLEDGE_BASE_URL", "http://kb-service:8001")
# Combined market/sector/company memory context (relevance-ranked). When off,
# falls back to per-symbol recency-only company memory (legacy behaviour).
MEMORY_CONTEXT_ENABLED = os.getenv("ANALYSIS_MEMORY_CONTEXT_ENABLED", "1") == "1"
# Body budget for the impact assessment. Accuracy over latency: feed the full
# filing/article (filings carry full attachment text), not a clipped first page.
# Bounded only by the PDF extraction cap (FILING_PDF_TEXT_MAX_CHARS).
IMPACT_BODY_CHARS = int(os.getenv("IMPACT_BODY_CHARS", "120000"))

IMPACT_TOOL = {
    "name": "score_company_impacts",
    "description": "Score the financial impact of news on a set of companies",
    "input_schema": {
        "type": "object",
        "properties": {
            "event_scope": {
                "type": "string",
                "enum": ["company_specific", "supply_chain", "sector_wide", "macro"],
                "description": (
                    "How this event transmits beyond the directly named companies. "
                    "company_specific: idiosyncratic — litigation/settlements, fraud, "
                    "penalties, management changes, one-off writeoffs, buybacks, "
                    "pledges, company ratings actions; does NOT affect competitors or "
                    "sector peers. supply_chain: transmits along customer/supplier "
                    "links — order wins/losses, capacity changes, production cuts. "
                    "sector_wide: affects the whole sector — regulation, sector demand "
                    "or pricing shifts, sector-relevant commodity moves. macro: "
                    "economy-wide — rates, FX, budget, geopolitics."
                ),
            },
            "impacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "company_name": {"type": "string"},
                        "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral"]},
                        "magnitude": {"type": "integer", "minimum": 1, "maximum": 5, "description": "1=trivial, 5=transformative"},
                        "time_horizon": {"type": "string", "enum": ["immediate", "short_term", "medium_term", "long_term"]},
                        "affected_line_items": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["revenue", "ebitda", "eps", "working_capital", "margins", "debt", "capex"]}
                        },
                        "explanation": {"type": "string", "maxLength": 300}
                    },
                    "required": ["symbol", "company_name", "sentiment", "magnitude", "time_horizon", "explanation"]
                }
            }
        },
        "required": ["impacts", "event_scope"]
    }
}

VALID_SCOPES = {"company_specific", "supply_chain", "sector_wide", "macro"}
# Conservative default: an unclassified event does not fan out through the
# relationship graph beyond ownership edges (see stage4 gating).
DEFAULT_SCOPE = "company_specific"


async def fetch_company_memory(symbol: str) -> Optional[dict]:
    """Fetch bounded recent-event memory for a company from the KB. Fail-soft:
    None on any error so memory is purely additive context."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{KNOWLEDGE_BASE_URL}/company/{symbol}/memory")
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.debug(f"Memory fetch failed for {symbol}: {e}")
    return None


async def fetch_company_context(symbol: str) -> Optional[dict]:
    """Fetch company profile from knowledge base REST API."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{KNOWLEDGE_BASE_URL}/company/{symbol}")
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.debug(f"KB lookup failed for {symbol}: {e}")
    return None


def _format_memory_block(mem: Optional[dict]) -> str:
    """Render the KB memory JSON as a compact prompt block (self-contained — no
    kb/sqlalchemy import, since this runs in the analysis image). '' when empty."""
    if not mem or not mem.get("events"):
        return ""
    lines = [
        f"  RECENT EVENT MEMORY (last {mem.get('window_days', 15)}d, material only; "
        f"net tilt: {mem.get('net_tilt', 'n/a')}):"
    ]
    for e in mem["events"]:
        lines.append(
            f"    [{e.get('days_ago', 0):.0f}d ago] {e.get('sentiment')} "
            f"{e.get('magnitude')}/5 ({e.get('time_horizon') or 'n/a'}): {e.get('summary', '')}"
        )
    return "\n".join(lines)


def build_impact_prompt(
    news_item: NewsItem,
    symbols: List[str],
    company_contexts: dict,
    company_memories: Optional[dict] = None,
    market_sector_backdrop: str = "",
) -> str:
    """Build the impact scoring prompt with market/sector backdrop + company
    context + recent-event memory."""

    company_memories = company_memories or {}
    context_sections = []
    for symbol in symbols:
        ctx = company_contexts.get(symbol)
        mem_block = _format_memory_block(company_memories.get(symbol))
        if ctx:
            fins = ctx.get("financials", {})
            dcf = ctx.get("dcf_model", {})
            consensus = ctx.get("analyst_consensus", {})
            context_sections.append(f"""
Company: {ctx.get('identity', {}).get('company_name', symbol)} ({symbol})
Sector: {ctx.get('identity', {}).get('sector_nse', 'Unknown')}
Revenue TTM: ₹{fins.get('revenue_ttm_cr', 'N/A')} Cr | EBITDA Margin: {fins.get('ebitda_margin_pct', 'N/A')}%
P/E: {fins.get('pe_ratio', 'N/A')} | EPS TTM: ₹{fins.get('eps_ttm', 'N/A')}
Fair Value: ₹{dcf.get('fair_value_per_share', 'N/A')} | Margin of Safety: {dcf.get('margin_of_safety_pct', 'N/A')}%
Analyst Target: ₹{consensus.get('median_target_price', 'N/A')} | Buy/Hold/Sell: {consensus.get('buy_pct', 'N/A')}%/{consensus.get('hold_pct', 'N/A')}%/{consensus.get('sell_pct', 'N/A')}%
Revenue Segments: {json.dumps(ctx.get('business', {}).get('revenue_segments', []))[:200]}
""")
        else:
            context_sections.append(f"\nCompany: {symbol} (no profile available)")

        # Recent-event memory gives the assessor the "is this NEW, a continuation,
        # or already-priced/covered?" judgment. Purely additive — omitted if empty.
        if mem_block:
            context_sections.append(mem_block)

    company_ctx_text = "\n".join(context_sections) if context_sections else "No company context available."

    backdrop_section = (
        f"MARKET & SECTOR BACKDROP (recent event regime — use to calibrate, not to "
        f"override, the direct read of THIS news):\n{market_sector_backdrop}\n\n"
        if market_sector_backdrop else ""
    )

    return f"""You are an Indian equity market analyst. Assess the financial impact of this news on each affected company.

NEWS:
Headline: {news_item.headline}
Body: {(news_item.full_body or news_item.body or '')[:IMPACT_BODY_CHARS]}
Source: {news_item.source}

{backdrop_section}COMPANY PROFILES:
{company_ctx_text}

Score the impact for each company based on:
1. Direct revenue/earnings impact
2. Regulatory/structural implications
3. Competitive positioning changes
4. Time horizon for the impact to materialize
5. RECENT EVENT MEMORY (if shown): judge whether this news is genuinely NEW, a
   continuation/escalation of an ongoing situation, or merely repeat coverage of
   an event already captured. If the memory shows the same event was already
   assessed, lower the magnitude (it is largely priced in / already counted) and
   say so in the explanation. Do NOT double-count the same event.
6. MARKET & SECTOR BACKDROP (if shown): use the prevailing regime to calibrate
   conviction — a company-positive read in a sector under sustained pressure (or
   vice-versa) warrants more caution. Do NOT let the backdrop override a clear,
   material, company-specific signal in the news itself.

Also classify event_scope — does this event transmit beyond the directly named
companies? Be strict: a litigation settlement, penalty, fraud, management exit,
or one-off cash outflow at one company is company_specific — it does NOT make
competitors or sector peers exposed (e.g. a bank paying to settle its own
lawsuit says nothing about other banks). Use sector_wide only when the CAUSE
itself is sector-level (regulation, sector demand/pricing, input costs for the
whole sector), supply_chain when the effect travels through named customers or
suppliers, and macro for economy-wide drivers.

Be precise. If impact is unclear, use magnitude 1-2 with "neutral" sentiment."""


async def score_impacts(
    news_item: NewsItem,
    symbols: List[str],
    macro_theme: str,
    affected_sectors: Optional[List[str]] = None,
) -> tuple[List[CompanyImpact], str]:
    """
    Score impact of news on each company symbol, and classify the event's
    transmission scope (gates Stage-4 propagation).
    Returns (impacts, event_scope).
    SLA: ≤1.5s
    """
    if not symbols:
        return [], DEFAULT_SCOPE

    start_time = time.time()
    llm_logger = get_llm_logger()

    # Fetch company contexts in parallel (up to 5 companies)
    symbols_to_score = symbols[:5]

    contexts = {}
    memories = {}
    backdrop = ""
    if KNOWLEDGE_BASE_URL:
        context_tasks = [fetch_company_context(sym) for sym in symbols_to_score]
        if MEMORY_CONTEXT_ENABLED:
            # One combined, relevance-ranked fetch: company memories (replacing the
            # recency-only per-symbol path) + market/sector backdrop. Fail-soft.
            from bharat_terminal.analysis import memory_client
            context_results, mem_context = await asyncio.gather(
                asyncio.gather(*context_tasks, return_exceptions=True),
                memory_client.fetch_memory_context(
                    headline=news_item.headline, macro_theme=macro_theme,
                    sectors=affected_sectors, symbols=symbols_to_score),
            )
            if mem_context:
                backdrop = memory_client.market_and_sector_backdrop(mem_context)
                comp_mem = mem_context.get("companies") or {}
                for sym in symbols_to_score:
                    if isinstance(comp_mem.get(sym), dict):
                        memories[sym] = comp_mem[sym]
        else:
            # Legacy path: per-symbol recency-only company memory.
            memory_tasks = [fetch_company_memory(sym) for sym in symbols_to_score]
            context_results, memory_results = await asyncio.gather(
                asyncio.gather(*context_tasks, return_exceptions=True),
                asyncio.gather(*memory_tasks, return_exceptions=True),
            )
            for sym, mem in zip(symbols_to_score, memory_results):
                if isinstance(mem, dict):
                    memories[sym] = mem
        for sym, ctx in zip(symbols_to_score, context_results):
            if isinstance(ctx, dict):
                contexts[sym] = ctx

    if provider_for_model(LLM_MODEL) == "anthropic" and not ANTHROPIC_API_KEY:
        # latency tracking skipped on degraded path
        return [
            CompanyImpact(
                symbol=sym,
                company_name=contexts.get(sym, {}).get("identity", {}).get("company_name", sym),
                sentiment="neutral",
                magnitude=2,
                time_horizon="short_term",
                affected_line_items=["revenue"],
                explanation=f"Mock impact for {sym} (no API key configured)",
                hop_distance=0,
                decay_factor=1.0,
            )
            for sym in symbols_to_score
        ], DEFAULT_SCOPE

    prompt = build_impact_prompt(news_item, symbols_to_score, contexts, memories,
                                 market_sector_backdrop=backdrop)

    llm_start = time.time()
    try:
        # MUST be the async client: score_impacts runs on the event loop (async
        # node). A blocking llm.complete() here freezes the loop for the whole
        # call (30-90s with full_body), which stalls aiokafka heartbeats →
        # consumer eviction → rebalance storm → pipeline hang.
        response = await llm.acomplete(
            model=LLM_MODEL,
            max_tokens=2048,
            tools=[IMPACT_TOOL],
            tool_choice={"type": "tool", "name": "score_company_impacts"},
            messages=[{"role": "user", "content": prompt}],
        )

        llm_latency = (time.time() - llm_start) * 1000
        record = LLMCallRecord(
            call_id=str(uuid.uuid4()),
            model=LLM_MODEL,
            stage="stage3_impact",
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            latency_ms=llm_latency,
            cost_usd=compute_cost(LLM_MODEL, response.usage.input_tokens, response.usage.output_tokens),
            success=True,
        )
        llm_logger.log(record)

        tool_result = None
        for block in response.content:
            if block.type == "tool_use" and block.name == "score_company_impacts":
                tool_result = block.input
                break

        if not tool_result:
            raise ValueError("No tool_use block in response")

        total_latency = (time.time() - start_time) * 1000
        if total_latency > 1500:
            logger.warning(f"Stage 3 SLA breach: {total_latency:.0f}ms > 1500ms")

        impacts = []
        for item in tool_result.get("impacts", []):
            impacts.append(CompanyImpact(
                symbol=item["symbol"],
                company_name=item["company_name"],
                sentiment=item["sentiment"],
                magnitude=item["magnitude"],
                time_horizon=item["time_horizon"],
                affected_line_items=item.get("affected_line_items", []),
                explanation=item["explanation"],
                hop_distance=0,
                decay_factor=1.0,
            ))

        event_scope = tool_result.get("event_scope")
        if event_scope not in VALID_SCOPES:
            logger.warning(f"Stage 3: invalid event_scope {event_scope!r} — using {DEFAULT_SCOPE}")
            event_scope = DEFAULT_SCOPE

        return impacts, event_scope

    except Exception as e:
        llm_latency = (time.time() - llm_start) * 1000
        record = LLMCallRecord(
            call_id=str(uuid.uuid4()),
            model=LLM_MODEL,
            stage="stage3_impact",
            prompt_tokens=0, completion_tokens=0,
            latency_ms=llm_latency, cost_usd=0.0,
            success=False, error=str(e),
        )
        llm_logger.log(record)
        logger.error(f"Stage 3 error: {e}")
        return [], DEFAULT_SCOPE
