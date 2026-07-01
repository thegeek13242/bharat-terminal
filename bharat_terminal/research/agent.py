"""
Research agent — derives a citation-backed beta for (symbol, trigger).

Flow per pair:
  1. Pull company context from KB (financial_results + segment data already
     parsed from XBRL by the existing kb-sync pipeline).
  2. Invoke Claude with these tools:
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

from bharat_terminal import llm
from bharat_terminal.llm import provider_for_model
from bharat_terminal.research import db

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
RESEARCH_MODEL = os.getenv(
    "RESEARCH_MODEL", "gemini-3.1-pro-preview"
)
MAX_AGENT_TURNS = int(os.getenv("RESEARCH_MAX_TURNS", "8"))
ENABLE_WEB_SEARCH = os.getenv("RESEARCH_ENABLE_WEB_SEARCH", "1") == "1"


# ── Tool schemas exposed to Claude ──────────────────────────────────────

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
        TOOL_SUBMIT_EXPOSURE,
    ]
    if ENABLE_WEB_SEARCH:
        # Provider-agnostic client-side web search (Gemini google_search /
        # Vertex grounding via the llm wrapper), replacing the Anthropic-only
        # server-side web_search tool. Results are fed back as a tool_result.
        tools.append({
            "name": "web_search",
            "description": (
                "Search the web for analyst commentary, segment disclosures or "
                "macro data not in our DB. Returns text results with source URLs "
                "to cite. Use only to fill gaps our own data cannot cover."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        })
    return tools


def _build_system_prompt() -> str:
    return (
        "You are a sell-side equity research analyst calibrating macro-factor "
        "betas for Indian equities. Your job: derive a SIGNED beta — the "
        "return-elasticity of a stock to a 1% move in a macro/commodity "
        "trigger — and back every numeric claim with a citation.\n\n"
        "RULES:\n"
        "1. Always start by calling get_kb_company_context to see what we "
        "   already know about the company — financials and segment data are "
        "   already parsed from NSE XBRL into our DB; treat them as ground truth.\n"
        "2. Use web_search to fill gaps (analyst commentary, segment "
        "   commentary, supplier disclosures). Quote short snippets only.\n"
        "3. NEVER invent numbers. If filings don't disclose the input cost "
        "   you need, set pct_of_total=null and rely on qualitative reasoning "
        "   for a SMALL beta with confidence='low'.\n"
        "4. End with EXACTLY ONE call to submit_exposure. Every numeric "
        "   claim in `rationale`, `mechanism`, `pct_of_total` must appear "
        "   in `citations` with a real URL.\n"
        "5. Beta sign convention: positive beta = stock moves WITH the "
        "   trigger; negative = inverse. Producers of a commodity are "
        "   positive to its price; consumers are negative.\n"
        "6. Realistic absolute beta for most stocks is below 1.0. Pure-play "
        "   producers can reach ~0.9. Reject any reasoning that yields |β|>1.5 "
        "   without overwhelming evidence."
    )


def _build_user_prompt(symbol: str, trigger_id: str, trigger_label: str) -> str:
    return (
        f"Calibrate the beta of {symbol} (NSE) to {trigger_label} "
        f"(trigger_id={trigger_id}).\n\n"
        f"Specifically: by how many percent does {symbol}'s equity return move "
        f"for a +1% move in {trigger_label}? Use our KB financials/segment data "
        f"plus web research. Output via submit_exposure."
    )


# ── Tool execution ──────────────────────────────────────────────────────

async def _execute_tool(name: str, args: Dict[str, Any]) -> Any:
    if name == "get_kb_company_context":
        ctx = await db.get_company_context(args["symbol"])
        return ctx or {"error": f"No KB record for {args['symbol']}"}

    return {"error": f"Unknown tool: {name}"}


# ── Main agent loop ─────────────────────────────────────────────────────

async def research_pair(
    symbol: str,
    trigger_id: str,
    trigger_label: str,
    *,
    client: Optional[Any] = None,  # deprecated/unused: provider chosen by the llm wrapper
) -> Optional[Dict[str, Any]]:
    """
    Run the agent for one (symbol, trigger) pair. Persists results into
    `trigger_betas` and (if exposure_kind != 'none') `fundamental_exposures`.
    Returns the submitted JSON, or None on agent failure.

    The LLM provider is selected from RESEARCH_MODEL by the llm wrapper (Vertex
    Gemini by default); ANTHROPIC_API_KEY is only required if RESEARCH_MODEL is a
    console-Anthropic model.
    """
    if provider_for_model(RESEARCH_MODEL) == "anthropic" and not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set but RESEARCH_MODEL is an Anthropic model")
        return None

    tools = _build_tools()
    system = _build_system_prompt()
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": _build_user_prompt(symbol, trigger_id, trigger_label)}
    ]

    submitted: Optional[Dict[str, Any]] = None

    for turn in range(MAX_AGENT_TURNS):
        try:
            resp = await llm.acomplete(
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

        # Append the assistant turn (provider-agnostic normalized blocks).
        messages.append({"role": "assistant", "content": llm.assistant_content(resp)})

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
                # Client-side grounded search (Gemini/Vertex) via the llm wrapper.
                try:
                    out = await llm.grounded_search(dict(tu.input).get("query", ""))
                    content = json.dumps(out, default=str)[:50_000]
                except Exception as e:
                    content = json.dumps({"error": str(e)})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": content,
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
