"""
Research agent — derives a citation-backed beta for (symbol, trigger).

Flow per pair:
  1. Pull company context from KB (financial_results + segment data already
     parsed from XBRL by the existing kb-sync pipeline).
  2. Invoke Claude with three tools:
       - fetch_bse_filings      (BSE corporate filings index for the symbol)
       - fetch_bse_xbrl_text    (raw XBRL text of a chosen filing)
       - web_search             (Anthropic server-side web search)
     Plus a final structured-output tool the agent MUST call to submit:
       - submit_exposure        (the structured beta + exposure record)
  3. Persist the submitted record into trigger_betas + fundamental_exposures.

The agent is told explicitly:
  • Do NOT guess. If the filings + web sources don't give you the
    cost-structure data, return confidence='low' and a small magnitude beta.
  • Every numeric claim must have a citation (filing URL + period, or web
    page URL + quote).
  • Beta units: signed return-elasticity to a 1% move in the trigger.
    e.g. beta=-0.45 for INDIGO/CRUDEOIL means "+1% crude → -0.45% INDIGO".

The agent runs serially per (symbol, trigger). Concurrency is at the
per-pair level — orchestrate with asyncio.gather in main.py.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import anthropic

from bharat_terminal.research import bse_xbrl, db

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
RESEARCH_MODEL = os.getenv(
    "RESEARCH_MODEL", "gemini-3.1-pro-preview"
)
MAX_AGENT_TURNS = int(os.getenv("RESEARCH_MAX_TURNS", "8"))
ENABLE_WEB_SEARCH = os.getenv("RESEARCH_ENABLE_WEB_SEARCH", "1") == "1"


# ── Tool schemas exposed to Claude ──────────────────────────────────────

TOOL_FETCH_BSE_FILINGS = {
    "name": "fetch_bse_filings",
    "description": (
        "List recent BSE corporate filings for a security (results, annual "
        "reports, etc). Returns a list with headline, filing_date, pdf_url "
        "and xbrl_url. Use this to discover which filing to read."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "NSE-style symbol, e.g. RELIANCE, INDIGO, ASIANPAINT",
            },
            "category": {
                "type": "string",
                "enum": ["Result", "Annual Report", "Corp. Action"],
                "default": "Result",
            },
            "months_back": {"type": "integer", "default": 18, "minimum": 3, "maximum": 36},
        },
        "required": ["symbol"],
    },
}

TOOL_FETCH_BSE_XBRL_TEXT = {
    "name": "fetch_bse_xbrl_text",
    "description": (
        "Fetch the raw XBRL document text for a previously-listed filing. "
        "Returns up to 30k chars of textual content (numeric facts preserved, "
        "XML tags stripped). Use only after fetch_bse_filings to pick a URL."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "xbrl_url": {"type": "string", "description": "URL from fetch_bse_filings"},
        },
        "required": ["xbrl_url"],
    },
}

TOOL_GET_KB_CONTEXT = {
    "name": "get_kb_company_context",
    "description": (
        "Fetch the company's existing structured profile from our knowledge "
        "base — sector, revenue/EBITDA, financial-results history (already "
        "parsed from past XBRL filings), and segment breakdown. Cheap. "
        "Always call this first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
        },
        "required": ["symbol"],
    },
}

TOOL_SUBMIT_EXPOSURE = {
    "name": "submit_exposure",
    "description": (
        "FINAL output — call this exactly once at the end with your "
        "structured assessment. After calling this you MUST stop. "
        "If you cannot find cost-structure evidence, still call it with "
        "confidence='low' and small magnitude beta (or beta=0 if no plausible "
        "exposure exists)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "beta": {
                "type": "number",
                "description": (
                    "Signed return-elasticity to a 1% move in the trigger. "
                    "Positive: stock moves with the trigger. Negative: inverse. "
                    "Realistic range for most stocks is [-1.0, +1.0]. Use 0.0 "
                    "if no plausible exposure."
                ),
            },
            "beta_low": {"type": "number", "description": "Lower bound of 80% credible interval"},
            "beta_high": {"type": "number", "description": "Upper bound of 80% credible interval"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "rationale": {
                "type": "string",
                "description": "1-3 sentence summary of WHY this beta — must reference cited evidence, not assertion.",
            },
            "exposure_kind": {
                "type": "string",
                "enum": ["cost", "revenue", "mixed", "none"],
                "description": "Is the trigger primarily a cost input, revenue source, or both for this company?",
            },
            "pct_of_total": {
                "type": "number",
                "description": "Approximate share of opex (cost-side) or revenue (revenue-side) attributable to this trigger. 0-1 scale. Null if not derivable.",
            },
            "total_basis": {
                "type": "string",
                "description": "What 'pct_of_total' is a percentage of, e.g. 'FY25 total opex'.",
            },
            "mechanism": {
                "type": "string",
                "description": "1-2 sentence causal explanation of how the trigger affects this company.",
            },
            "citations": {
                "type": "array",
                "description": "Every numeric claim or quoted figure must have an entry here.",
                "items": {
                    "type": "object",
                    "properties": {
                        "url":    {"type": "string"},
                        "source": {"type": "string", "description": "BSE_XBRL | BSE_PDF | KB | WEB"},
                        "period": {"type": "string"},
                        "quote":  {"type": "string", "description": "Short verbatim quote (≤200 chars)"},
                    },
                    "required": ["url", "source"],
                },
            },
        },
        "required": ["beta", "confidence", "rationale", "exposure_kind", "mechanism", "citations"],
    },
}


def _build_tools() -> List[Dict[str, Any]]:
    tools: List[Dict[str, Any]] = [
        TOOL_GET_KB_CONTEXT,
        TOOL_FETCH_BSE_FILINGS,
        TOOL_FETCH_BSE_XBRL_TEXT,
        TOOL_SUBMIT_EXPOSURE,
    ]
    if ENABLE_WEB_SEARCH:
        tools.append({"type": "web_search_20250305", "name": "web_search"})
    return tools


def _build_system_prompt() -> str:
    return (
        "You are a sell-side equity research analyst calibrating macro-factor "
        "betas for Indian equities. Your job: derive a SIGNED beta — the "
        "return-elasticity of a stock to a 1% move in a macro/commodity "
        "trigger — and back every numeric claim with a citation.\n\n"
        "RULES:\n"
        "1. Always start by calling get_kb_company_context to see what we "
        "   already know about the company.\n"
        "2. Use fetch_bse_filings + fetch_bse_xbrl_text to read the latest "
        "   annual report or quarterly result for cost-structure data. "
        "   This is the ground truth.\n"
        "3. Use web_search to fill gaps (analyst commentary, segment "
        "   commentary, supplier disclosures). Quote short snippets only.\n"
        "4. NEVER invent numbers. If filings don't disclose the input cost "
        "   you need, set pct_of_total=null and rely on qualitative reasoning "
        "   for a SMALL beta with confidence='low'.\n"
        "5. End with EXACTLY ONE call to submit_exposure. Every numeric "
        "   claim in `rationale`, `mechanism`, `pct_of_total` must appear "
        "   in `citations` with a real URL.\n"
        "6. Beta sign convention: positive beta = stock moves WITH the "
        "   trigger; negative = inverse. Producers of a commodity are "
        "   positive to its price; consumers are negative.\n"
        "7. Realistic absolute beta for most stocks is below 1.0. Pure-play "
        "   producers can reach ~0.9. Reject any reasoning that yields |β|>1.5 "
        "   without overwhelming evidence."
    )


def _build_user_prompt(symbol: str, trigger_id: str, trigger_label: str) -> str:
    return (
        f"Calibrate the beta of {symbol} (NSE) to {trigger_label} "
        f"(trigger_id={trigger_id}).\n\n"
        f"Specifically: by how many percent does {symbol}'s equity return move "
        f"for a +1% move in {trigger_label}? Use BSE filings (XBRL preferred) "
        f"plus web research. Output via submit_exposure."
    )


# ── Tool execution ──────────────────────────────────────────────────────

async def _execute_tool(name: str, args: Dict[str, Any]) -> Any:
    if name == "get_kb_company_context":
        ctx = await db.get_company_context(args["symbol"])
        return ctx or {"error": f"No KB record for {args['symbol']}"}

    if name == "fetch_bse_filings":
        symbol = args["symbol"]
        scrip = await bse_xbrl.resolve_security_code(symbol)
        if not scrip:
            return {"error": f"Could not resolve BSE security code for {symbol}"}
        filings = await bse_xbrl.list_filings(
            scrip,
            category=args.get("category", "Result"),
            months_back=int(args.get("months_back", 18)),
        )
        return {"security_code": scrip, "filings": filings}

    if name == "fetch_bse_xbrl_text":
        text = await bse_xbrl.fetch_xbrl_text(args["xbrl_url"])
        if text is None:
            return {"error": "Could not fetch XBRL text"}
        return {"text": text}

    return {"error": f"Unknown tool: {name}"}


# ── Main agent loop ─────────────────────────────────────────────────────

async def research_pair(
    symbol: str,
    trigger_id: str,
    trigger_label: str,
    *,
    client: Optional[anthropic.AsyncAnthropic] = None,
) -> Optional[Dict[str, Any]]:
    """
    Run the agent for one (symbol, trigger) pair. Persists results into
    `trigger_betas` and (if exposure_kind != 'none') `fundamental_exposures`.
    Returns the submitted JSON, or None on agent failure.
    """
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set — cannot run research agent")
        return None

    client = client or anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    tools = _build_tools()
    system = _build_system_prompt()
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": _build_user_prompt(symbol, trigger_id, trigger_label)}
    ]

    submitted: Optional[Dict[str, Any]] = None

    for turn in range(MAX_AGENT_TURNS):
        try:
            resp = await client.messages.create(
                model=RESEARCH_MODEL,
                max_tokens=2048,
                system=system,
                tools=tools,
                messages=messages,
            )
        except Exception as e:
            logger.error(f"[AGENT] {symbol}/{trigger_id} model error turn={turn}: {e}")
            return None

        # If no tool use, agent is done talking — bail
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            logger.warning(
                f"[AGENT] {symbol}/{trigger_id} ended without submit_exposure (turn={turn})"
            )
            break

        # Append the assistant turn
        messages.append({"role": "assistant", "content": resp.content})

        # Handle each tool_use, building a single user turn with tool_results
        tool_results = []
        for tu in tool_uses:
            if tu.name == "submit_exposure":
                submitted = dict(tu.input)
                # We acknowledge but the agent should stop here per system prompt.
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": "Exposure recorded. Stop.",
                })
                continue
            if tu.name == "web_search":
                # Server-side; should never appear here as a client-side tool_use.
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": "Web search is server-side; results were inlined.",
                })
                continue
            try:
                out = await _execute_tool(tu.name, dict(tu.input))
                content = json.dumps(out, default=str)[:50_000]
            except Exception as e:
                content = json.dumps({"error": str(e)})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": content,
            })

        messages.append({"role": "user", "content": tool_results})

        if submitted is not None:
            break

        if resp.stop_reason == "end_turn":
            break

    if submitted is None:
        logger.warning(f"[AGENT] {symbol}/{trigger_id} produced no submission")
        return None

    await _persist(symbol, trigger_id, submitted)
    return submitted


async def _persist(symbol: str, trigger_id: str, sub: Dict[str, Any]) -> None:
    beta = float(sub.get("beta") or 0.0)
    confidence = sub.get("confidence", "low")
    citations = sub.get("citations") or []
    rationale = sub.get("rationale")

    await db.upsert_trigger_beta(
        symbol=symbol,
        trigger_id=trigger_id,
        beta=beta,
        beta_low=sub.get("beta_low"),
        beta_high=sub.get("beta_high"),
        derivation_method="agent_research",
        confidence=confidence,
        rationale=rationale,
        citations=citations,
        model_used=RESEARCH_MODEL,
    )

    exposure_kind = sub.get("exposure_kind") or "none"
    if exposure_kind != "none":
        # Try to pull a period from the strongest BSE_XBRL citation
        period = None
        source_url = None
        source_kind = None
        for c in citations:
            if (c.get("source") or "").upper() == "BSE_XBRL":
                period = c.get("period")
                source_url = c.get("url")
                source_kind = "bse_xbrl"
                break
        if not period:
            period = "agent-derived"
        await db.upsert_fundamental_exposure(
            symbol=symbol,
            trigger_id=trigger_id,
            exposure_kind=exposure_kind,
            mechanism=sub.get("mechanism") or rationale or "",
            pct_of_total=sub.get("pct_of_total"),
            total_basis=sub.get("total_basis"),
            source_doc_url=source_url,
            source_period=period,
            source_kind=source_kind,
            citations=citations,
        )

    logger.info(
        f"[AGENT] persisted {symbol}/{trigger_id} β={beta:+.2f} "
        f"({confidence}, {len(citations)} citations)"
    )
