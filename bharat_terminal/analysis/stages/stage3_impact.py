"""
Stage 3: Per-company impact scoring using Claude Haiku with company context.
SLA: ≤1.5s
"""
import time
import logging
import os
import json
import re
import uuid
import asyncio
import httpx
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple
from bharat_terminal import llm
from bharat_terminal.llm import provider_for_model
from bharat_terminal.types import NewsItem, CompanyImpact
from bharat_terminal.analysis import router_v2
from bharat_terminal.analysis.llm_logger import get_llm_logger, LLMCallRecord, compute_cost
from bharat_terminal.analysis.market_hours import IST
from bharat_terminal.analysis.scenario_rubrics import (
    detect_scenario, lender_addendum, rubric_for,
)

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
# C1b — include same-symbol filings from the previous 2h as prompt context
# (results cluster: table → press release → presentation; 42% of same-cluster
# impact pairs materially disagree when scored blind to each other). DEFAULT
# OFF: this changes classifier input and must pass replay validation first
# (the Tier-B lesson) — flag-off is byte-identical to today's prompt.
STAGE3_SIBLING_CONTEXT = os.getenv("STAGE3_SIBLING_CONTEXT", "0") == "1"

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


async def fetch_sibling_section(news_item: NewsItem, symbol: str) -> str:
    """C1b sibling-context block ('' when disabled/none/error — identity)."""
    if not STAGE3_SIBLING_CONTEXT or not news_item.timestamp_utc:
        return ""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(
                f"{KNOWLEDGE_BASE_URL}/company/{symbol}/filings/siblings",
                params={"before_ts": news_item.timestamp_utc.isoformat(),
                        "exclude_id": news_item.id})
            if resp.status_code != 200:
                return ""
            sibs = resp.json().get("siblings") or []
    except Exception as e:
        logger.debug(f"sibling fetch failed for {symbol}: {e}")
        return ""
    if not sibs:
        return ""
    blocks = "\n".join(
        f"== {s['headline']} ({s['timestamp_utc']}) ==\n{s['body']}"
        for s in sibs)
    return (
        "EARLIER SAME-DAY FILINGS BY THE COMPANY (same event cluster — a "
        "results table, press release and presentation describe ONE event; "
        "read them together with THIS filing, do not double-count):\n"
        f"{blocks}\n\n")


# ── Router-v2 provenance registry ───────────────────────────────────────────
# score_impacts stashes the per-item router meta here; the report assembler
# collects it via consume_router_meta(news_id) into ImpactReport.router_meta.
# Bounded FIFO so an unconsumed entry (error paths, non-pipeline callers) can
# never grow memory.
_ROUTER_META: "OrderedDict[str, dict]" = OrderedDict()
_ROUTER_META_MAX = 1024


def stash_router_meta(news_id: str, meta: dict) -> None:
    _ROUTER_META[news_id] = meta
    _ROUTER_META.move_to_end(news_id)
    while len(_ROUTER_META) > _ROUTER_META_MAX:
        _ROUTER_META.popitem(last=False)


def consume_router_meta(news_id: str) -> Optional[dict]:
    """Pop the router-v2 provenance for a news item ('None' when stage-3 never
    routed it — pre-router reports, skip paths)."""
    return _ROUTER_META.pop(news_id, None)


# ── Company-context prefetch (latency) ──────────────────────────────────────
# Stage-3's KB profile fetches depend ONLY on the symbol, which exchange
# filings already carry (adapters stamp symbols_mentioned). Kicking them off
# when stage-2 STARTS overlaps them with stage-2's LLM call instead of paying
# for them serially afterwards (measured ~1-2s on the tier-1 filing path,
# 2026-08-03 latency analysis). Purely an I/O reordering: stage-3 consumes the
# exact same context objects, so the prompt is byte-identical.
_CTX_PREFETCH: "OrderedDict[str, dict]" = OrderedDict()
_CTX_PREFETCH_MAX = 256


def prefetch_company_contexts(news_id: str, symbols: List[str]) -> None:
    """Start KB context fetches for `symbols` now; stage-3 awaits them later.
    Fire-and-forget and fail-soft — fetch_company_context returns None on any
    error, so a prefetch can never break the pipeline."""
    if not symbols or not KNOWLEDGE_BASE_URL or news_id in _CTX_PREFETCH:
        return
    try:
        _CTX_PREFETCH[news_id] = {
            sym: asyncio.create_task(fetch_company_context(sym))
            for sym in symbols[:5]
        }
    except RuntimeError:          # no running loop (sync caller) — skip
        return
    _CTX_PREFETCH.move_to_end(news_id)
    while len(_CTX_PREFETCH) > _CTX_PREFETCH_MAX:
        _, stale = _CTX_PREFETCH.popitem(last=False)
        for task in stale.values():
            if not task.done():
                task.cancel()


def _consume_prefetched(news_id: str) -> dict:
    return _CTX_PREFETCH.pop(news_id, {}) or {}


def _scenario_for(
    news_item: NewsItem,
    symbols: List[str],
    route_decisions: Optional[Dict[str, "router_v2.RouteDecision"]] = None,
) -> Tuple[Optional[str], str]:
    """(scenario_key, scenario prompt block) for this item.

    Flag OFF (ROUTER_V2_RUBRICS=0, the default) or no route decisions: exactly
    today's behaviour — detect_scenario + rubric_for + body-signature lender
    addendum. Flag ON: taxonomy-v3-driven selection (router_v2, Tier B) with
    the company-type addenda; the company_type is only trusted for addenda on
    single-symbol items (multi-symbol events fall back to body signature —
    one rubric cannot serve mixed company types).
    """
    _body = news_item.full_body or news_item.body or ""
    _headline = news_item.headline or ""
    primary = (route_decisions or {}).get(symbols[0]) if symbols else None
    if router_v2.rubrics_enabled() and primary is not None:
        addendum_type = primary.company_type if len(symbols) == 1 else None
        return router_v2.scenario_block_v3(
            _headline, _body, primary, company_type=addendum_type)
    # Legacy path — byte-identical to the pre-router composition.
    key = detect_scenario(_headline, _body)
    block = rubric_for(_headline, _body)
    if key:
        block += lender_addendum(key, _body)
    return key, block


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
    route_decisions: Optional[Dict[str, "router_v2.RouteDecision"]] = None,
    sibling_section: str = "",
) -> str:
    """Build the impact scoring prompt with market/sector backdrop + company
    context + recent-event memory (+ router-v2 factual context when routed)."""

    company_memories = company_memories or {}
    route_decisions = route_decisions or {}
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

        # Router-v2 Tier-A factual context (always on): SEBI quantitative
        # materiality thresholds + company-type/market-state line, and the
        # holdco NAV/dividend-timing fact (the MAHSCOOTER fix). These are
        # FACTS about the company, not rubric overrides — strictly additive.
        dec = route_decisions.get(symbol)
        if dec is not None:
            fins = (ctx or {}).get("financials", {})
            sebi_line = router_v2.sebi_context_line(
                symbol, fins, dec.company_type, dec.type_overlays,
                dec.market_state)
            if sebi_line:
                context_sections.append(sebi_line)
            hint = router_v2.holdco_hint(dec.company_type)
            if hint:
                context_sections.append(hint)

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

    # Scenario-specific rubric for known filing types (results, order wins,
    # fund raises, rating actions), plus the lender sector addendum when the
    # results body carries a bank/NBFC statement signature. '' otherwise —
    # see scenario_rubrics.py for the SWSOLAR postmortem that motivated this.
    # With ROUTER_V2_RUBRICS=1 (Tier B, default off) selection is taxonomy-v3
    # driven — see _scenario_for.
    _scenario_key, scenario_block = _scenario_for(
        news_item, symbols, route_decisions)

    return f"""You are a senior equity analyst on the research desk of an Indian PMS/AIF fund. Your impact assessments feed the fund's position decisions: overstating noise wastes risk budget, missing a material event loses alpha. Both errors cost the fund — be exact, quantified, and skeptical.

NEWS:
Headline: {news_item.headline}
Body: {(news_item.full_body or news_item.body or '')[:IMPACT_BODY_CHARS]}
Source: {news_item.source}

{sibling_section}{backdrop_section}COMPANY PROFILES:
{company_ctx_text}

ASSESSMENT DISCIPLINE:
1. QUANTIFY against company size. Relate any ₹/$ amounts in the news to the
   company's TTM revenue, PAT, and market cap from its profile. A ₹500 Cr order
   is transformative for a ₹2,000 Cr-revenue company and noise for a
   ₹2,00,000 Cr one. State the rough percentage in the explanation.
2. MAGNITUDE anchors (plausible effect on fair value / annual earnings power):
   5 = transformative (>10%)   4 = major (5-10%)   3 = clearly material (2-5%)
   2 = minor (<2%) or soft/qualitative   1 = negligible, procedural, PR.
   Most news items are a 1 or 2. Reserve ≥3 for quantified, verifiable substance.
3. MECHANISM over vibes: name the transmission channel — which line item moves,
   why, and over what horizon. If you cannot name the mechanism, the magnitude
   is 1-2 by definition.
4. SENTIMENT is the direction of THIS EVENT's effect on the company's
   fundamentals — never the stock's recent performance and never the net of
   every concurrent overhang. A record-output quarter is POSITIVE even if the
   stock fell 16% last month amid unrelated worries; a contract loss is
   NEGATIVE even after a rally. Surrounding negatives/positives belong in
   magnitude and the explanation text — never flip the sign with them. Use
   "negative" only when the event itself harms earnings power, the balance
   sheet, or business prospects.
5. RECENT EVENT MEMORY (if shown): judge whether this news is genuinely NEW, a
   continuation/escalation of an ongoing situation, or merely repeat coverage of
   an event already captured. If the memory shows the same event was already
   assessed, lower the magnitude (it is largely priced in / already counted) and
   say so in the explanation. Do NOT double-count the same event.
6. MARKET & SECTOR BACKDROP (if shown): use the prevailing regime to calibrate
   conviction — a company-positive read in a sector under sustained pressure (or
   vice-versa) warrants more caution. Do NOT let the backdrop override a clear,
   material, company-specific signal in the news itself.
{scenario_block}
Also classify event_scope — does this event transmit beyond the directly named
companies? Be strict: a litigation settlement, penalty, fraud, management exit,
or one-off cash outflow at one company is company_specific — it does NOT make
competitors or sector peers exposed (e.g. a bank paying to settle its own
lawsuit says nothing about other banks). Use sector_wide only when the CAUSE
itself is sector-level (regulation, sector demand/pricing, input costs for the
whole sector), supply_chain when the effect travels through named customers or
suppliers, and macro for economy-wide drivers.

EXPLANATION STYLE: mechanism → affected line item → rough quantum → horizon, in
2-3 factual sentences an analyst can verify against the source. No hype
adjectives, no advice or buy/sell language — describe the exposure; the reader
forms the view.

If impact is unclear, use magnitude 1-2 with "neutral" sentiment."""


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
    candidate_symbols = symbols[:5]

    contexts = {}
    memories = {}
    backdrop = ""
    if KNOWLEDGE_BASE_URL:
        # Reuse anything stage-2 already started for us (same coroutine, same
        # result); only symbols stage-2 newly discovered are fetched now.
        prefetched = _consume_prefetched(news_item.id)
        context_tasks = [prefetched.get(sym) or fetch_company_context(sym)
                         for sym in candidate_symbols]
        for sym, task in prefetched.items():
            if sym not in candidate_symbols and not task.done():
                task.cancel()
        if MEMORY_CONTEXT_ENABLED:
            # One combined, relevance-ranked fetch: company memories (replacing the
            # recency-only per-symbol path) + market/sector backdrop. Fail-soft.
            from bharat_terminal.analysis import memory_client
            context_results, mem_context = await asyncio.gather(
                asyncio.gather(*context_tasks, return_exceptions=True),
                memory_client.fetch_memory_context(
                    headline=news_item.headline, macro_theme=macro_theme,
                    sectors=affected_sectors, symbols=candidate_symbols),
            )
            if mem_context:
                backdrop = memory_client.market_and_sector_backdrop(mem_context)
                comp_mem = mem_context.get("companies") or {}
                for sym in candidate_symbols:
                    if isinstance(comp_mem.get(sym), dict):
                        memories[sym] = comp_mem[sym]
        else:
            # Legacy path: per-symbol recency-only company memory.
            memory_tasks = [fetch_company_memory(sym) for sym in candidate_symbols]
            context_results, memory_results = await asyncio.gather(
                asyncio.gather(*context_tasks, return_exceptions=True),
                asyncio.gather(*memory_tasks, return_exceptions=True),
            )
            for sym, mem in zip(candidate_symbols, memory_results):
                if isinstance(mem, dict):
                    memories[sym] = mem
        for sym, ctx in zip(candidate_symbols, context_results):
            if isinstance(ctx, dict):
                contexts[sym] = ctx

    # ── Router v2 (Tier A, always on): three-axis routing + suppression ────
    # EVENT (taxonomy v3 + Para-A) × COMPANY-TYPE × MARKET-STATE per symbol,
    # from the KB payload's classification/market_state (fail-soft: absent
    # axes route with no suppression). See analysis/router_v2.py.
    _route_body = news_item.full_body or news_item.body or ""
    _ts = news_item.timestamp_utc
    if _ts is not None and _ts.tzinfo is None:
        from datetime import timezone as _tz
        _ts = _ts.replace(tzinfo=_tz.utc)
    filing_ts_ist = _ts.astimezone(IST) if _ts is not None else None
    route_decisions: Dict[str, router_v2.RouteDecision] = {}
    for sym in candidate_symbols:
        ctx = contexts.get(sym) or {}
        route_decisions[sym] = router_v2.route(
            news_item.headline or "", _route_body,
            ctx.get("classification"), ctx.get("market_state"), filing_ts_ist)

    symbols_to_score = []
    for sym in candidate_symbols:
        dec = route_decisions[sym]
        if dec.suppress:
            logger.info(
                f"[router_v2] suppressed {sym} ({dec.suppress_reason}) for "
                f"news {news_item.id}: not routed to impact scoring")
        else:
            symbols_to_score.append(sym)

    _scenario_key_meta, _ = _scenario_for(
        news_item, symbols_to_score, route_decisions)
    router_meta = router_v2.build_router_meta(
        route_decisions, symbols_to_score, rubric_scenario=_scenario_key_meta)
    stash_router_meta(news_item.id, router_meta)
    logger.info(
        f"[router_v2] news={news_item.id} bucket={router_meta['event_bucket']}"
        f" flags={router_meta['bucket_flags']} para_a={router_meta['para_a']}"
        f" leaf={router_meta['leaf']} suppressed={router_meta['suppressed']}"
        f" scored={symbols_to_score}")

    if not symbols_to_score:
        # Everything suppressed (fund schemes / unknown+suspended): the item
        # is not analyzable as an equity event — no LLM call, empty impacts.
        return [], DEFAULT_SCOPE

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

    # C1b sibling context — single-symbol filings only (exchange adapters stamp
    # one symbol per filing; multi-symbol items skip to avoid ambiguity).
    # fetch_sibling_section returns '' unless STAGE3_SIBLING_CONTEXT=1.
    sibling_section = ""
    if len(symbols_to_score) == 1 and news_item.source in (
            "NSE_FILINGS", "BSE_FILINGS"):
        sibling_section = await fetch_sibling_section(
            news_item, symbols_to_score[0])

    prompt = build_impact_prompt(news_item, symbols_to_score, contexts, memories,
                                 market_sector_backdrop=backdrop,
                                 route_decisions=route_decisions,
                                 sibling_section=sibling_section)

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
            cache=True,
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
            cached_tokens=response.usage.cached_tokens,
            context_id=str(news_item.id),
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

        # The LLM was asked to score exactly symbols_to_score, but it sometimes
        # echoes an identifier from the news text instead — e.g. the numeric
        # BSE scrip code embedded in filing headlines ("Foo Ltd - 503626 - ...")
        # — which then surfaces verbatim in the feed. Enforce the contract:
        # every impact must map back to a provided symbol, else it is dropped.
        provided = {s.upper(): s for s in symbols_to_score}

        def _norm_name(s: str) -> str:
            """Company-name key tolerant of legal-suffix variants (Ltd vs Limited)."""
            s = re.sub(r"[.,&'()\-/]", " ", (s or "").upper())
            s = re.sub(r"\b(LIMITED|LTD|PRIVATE|PVT)\b", " ", s)
            return re.sub(r"\s+", " ", s).strip()

        name_to_symbol = {}
        for sym in symbols_to_score:
            nm = ((contexts.get(sym) or {}).get("identity") or {}).get("company_name", "")
            if nm:
                name_to_symbol[_norm_name(nm)] = sym

        def _canonical(item: dict) -> Optional[str]:
            raw = (item.get("symbol") or "").upper().strip()
            if raw in provided:
                return provided[raw]
            nm = _norm_name(item.get("company_name") or "")
            if nm and nm in name_to_symbol:
                return name_to_symbol[nm]
            return None

        impacts = []
        seen_symbols: set = set()
        for item in tool_result.get("impacts", []):
            canonical = _canonical(item)
            if canonical is None:
                # Last resort: KB resolution (cached, rare — only on LLM drift),
                # accepted only if it lands back in the provided set.
                from bharat_terminal.analysis.signal_gates import resolve_symbol
                for cand in (item.get("symbol"), item.get("company_name")):
                    if cand:
                        r = await asyncio.to_thread(resolve_symbol, cand)
                        if r and r.upper() in provided:
                            canonical = provided[r.upper()]
                            break
            if canonical is None and len(symbols_to_score) == 1:
                # Single-symbol contract: the LLM was asked to score exactly one
                # company, so whatever identifier it echoed (BSE scrip code,
                # name variant), the impact can only belong to that company.
                # Seen live 2026-07-16: BSE results filing scored as '535916'
                # instead of 'ALSL' and silently dropped. Multi-symbol events
                # keep the strict drop (remapping there would guess).
                canonical = symbols_to_score[0]
                logger.info(
                    f"Stage 3: remapped unmappable symbol {item.get('symbol')!r} "
                    f"-> {canonical} (single-symbol contract)"
                )
            if canonical is None:
                logger.warning(
                    f"Stage 3: dropping impact with unmappable symbol "
                    f"{item.get('symbol')!r} / {item.get('company_name')!r} "
                    f"(scored set: {symbols_to_score})"
                )
                continue
            if canonical in seen_symbols:
                continue
            seen_symbols.add(canonical)

            # Para-A magnitude FLOOR (router v2, Tier A): SEBI LODR Reg 30
            # Schedule III Para A deems these leaves material — the model may
            # not dismiss them below magnitude 3. Deterministic, code-level,
            # audit-stamped (gate_flags 'para_a_floor' in router_meta + an
            # explanation note, same convention as priced_in). Sentiment is
            # NEVER floored — regulation fixes materiality, not direction.
            magnitude = item["magnitude"]
            explanation = item["explanation"]
            dec = route_decisions.get(canonical)
            if dec is not None and isinstance(magnitude, (int, float)):
                new_mag, floored = router_v2.apply_para_a_floor(
                    int(magnitude), dec.para_a)
                if floored:
                    logger.info(
                        f"[router_v2] para_a_floor: {canonical} magnitude "
                        f"{magnitude}->{new_mag} (leaf={dec.para_a_leaf}, "
                        f"news={news_item.id})")
                    router_v2.record_para_a_floor(router_meta, canonical)
                    explanation = (
                        f"{explanation} [para_a_floor: SEBI Reg-30 Para-A "
                        f"leaf '{dec.para_a_leaf}' — deemed material, "
                        f"magnitude raised to {new_mag}]").strip()
                    magnitude = new_mag

            impacts.append(CompanyImpact(
                symbol=canonical,
                company_name=item["company_name"],
                sentiment=item["sentiment"],
                magnitude=magnitude,
                time_horizon=item["time_horizon"],
                affected_line_items=item.get("affected_line_items", []),
                explanation=explanation,
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
            context_id=str(news_item.id),
        )
        llm_logger.log(record)
        logger.error(f"Stage 3 error: {e}")
        # C1a (2026-07-31): a swallowed stage-3 failure used to persist as a
        # relevant=true report with EMPTY impacts, indistinguishable from a
        # genuine no-impact judgment — the item permanently lost its
        # assessment (DYCL 20-Jul press release; 245 reports/30d ≈ 2.2%).
        # Mark the failure in router_meta so main.py can requeue one retry
        # and downstream consumers can tell failure from judgment.
        stash_router_meta(news_item.id,
                          {**router_meta, "stage3_error": str(e)[:300]})
        return [], DEFAULT_SCOPE
