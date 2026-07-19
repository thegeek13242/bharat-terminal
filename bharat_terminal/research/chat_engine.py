"""F12 research chat — grounded tool-loop Q&A over the Taperead corpus.

The dossier agent's proven loop, generalized to conversation. Hard gates
(wish board F12, non-negotiable):
- Answers composed ONLY from tool results. Every tool result is issued a
  ref (T1, T2 …); the model must cite claims as [T1]. Markers that don't
  correspond to an issued ref are stripped server-side (fail-closed) and
  counted — a citation either points at real evidence or it doesn't exist.
- Explicit refusal when the data isn't there ("not in my data").
- SEBI RA scope guard: factual/attributive answers; no recommendations,
  targets, or forward views of ours. Management's own guidance is quotable
  fact; our opinion of it is not offered.
- The caller audit-logs every answer with the full tool trace.

INTERNAL-ONLY: the route mounting this is gated on INTERNAL_RESEARCH_EMAILS.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from bharat_terminal import grounding, llm

logger = logging.getLogger(__name__)

CHAT_MODEL = os.getenv("RESEARCH_CHAT_MODEL", "gemini-3.1-pro-preview")
CHAT_MAX_TURNS = int(os.getenv("RESEARCH_CHAT_MAX_TURNS", "8"))
CHAT_MAX_TOKENS = int(os.getenv("RESEARCH_CHAT_MAX_TOKENS", "4096"))
_RESULT_CAP = 40000  # per tool result, chars of JSON fed back to the model

# Matches [T1] and grouped [T1, T2, T5] — the audit found grouped brackets
# slipping through a single-ref pattern UNVALIDATED. Each ref in a group is
# validated individually; the group is rewritten as consecutive singles.
_REF_RE = re.compile(r"\[(T\d+(?:\s*,\s*T\d+)*)\]")

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_transcripts",
        "description": (
            "Semantic search over complete earnings-call transcripts (NSE "
            "filings, verbatim). Returns ranked excerpts — each is an exact "
            "slice of the filed document with transcript_id and char offsets. "
            "Use for anything management said on calls: guidance colour, "
            "strategy, one-offs, Q&A exchanges. Optional symbol filter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "symbol": {"type": "string", "description": "optional NSE symbol"},
                "k": {"type": "integer", "default": 6},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_guidance",
        "description": (
            "Structured management guidance extracted from the company's "
            "concalls: metric, period, value AS STATED, unit, verbatim quote, "
            "and the computed change vs the previous call (new / raised / "
            "lowered / maintained / changed). Authoritative for 'what did "
            "management guide and how did it change'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_recent_impacts",
        "description": (
            "Our analysis pipeline's assessed events for a company (news + "
            "filings with per-company sentiment, magnitude 1-5, direct vs "
            "propagated, and the reasoning). The evidence base for 'why did "
            "the stock move' — pair with get_price_returns for the move itself. "
            "CAVEAT: assessments are the pipeline's interpretation of source "
            "articles; numeric price/return claims INSIDE them are not "
            "independently verified — confirm any such number against "
            "get_price_returns before repeating it, and attribute event "
            "narratives as 'our event analysis assessed…'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "days": {"type": "integer", "default": 30},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_financials",
        "description": (
            "Authoritative quarterly financials from our NSE/BSE-parsed "
            "database (current to the latest reported quarter). Read the "
            "per-period `taxonomy` first: banks/insurers carry their real "
            "lines in `sector_financials`. Use for every reported number — "
            "never web_search for financials."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "periods": {"type": "integer", "default": 8},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_price_returns",
        "description": (
            "Price returns over trailing windows (days) from daily bhavcopy "
            "closes, 2017→present. Use for 'how has it moved'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "windows": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_shareholding",
        "description": "Shareholding pattern history (promoter/FII/DII/public) by quarter.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "quarters": {"type": "integer", "default": 4},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Google-grounded web search. ONLY for context our database cannot "
            "hold (industry news, competitor moves, regulation). Never for "
            "our companies' financials, prices, guidance, or transcripts — "
            "those come from the database tools and are authoritative."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]

_SYSTEM = """You are Taperead's research assistant for professional equity
analysts at Indian PMS firms. You answer questions about Indian listed
companies using ONLY the provided tools — our own verbatim transcript
corpus, parsed financials, price history, shareholding, guidance
extractions, and analysed events.

EVIDENCE RULES (absolute):
- Every factual claim in your answer must come from a tool result and be
  cited with that result's ref marker, e.g. [T1]. Multiple claims from one
  result reuse its ref. Never state a number, quote, or event without a ref.
- If the tools cannot answer, say plainly what is NOT in the data — never
  fill gaps from general knowledge. Naming what's missing is a good answer.
- Database tools are authoritative for our companies; web_search is only
  for outside context and its claims must also carry refs.

SCOPE (SEBI research-analyst boundary — absolute):
- You provide information and factual analysis only: what happened, what
  was reported, what management said, how numbers changed.
- NEVER give buy/sell/hold views, price targets, valuation opinions,
  position advice, or predictions of stock direction. If asked, decline
  briefly: "I provide information, not investment advice or views — that
  judgment stays with you." Management's own stated guidance is quotable
  fact; your opinion of whether to act on it is never offered.

STYLE:
- Terminal-brief. Lead with the answer. Short paragraphs, no headers
  unless genuinely needed. Numbers exactly as sourced ("as stated" units).
- Markdown: ONLY **bold** and flat bullet (*) or numbered lists — never
  tables, headers, nested lists, or links.
- When quoting a transcript, quote verbatim and keep it short."""


def _tool_result_summary(name: str, args: Dict[str, Any], result: Any) -> str:
    """Compact human-readable line for the audit trace."""
    try:
        if name == "search_transcripts":
            n = len(result.get("results", []))
            return f"{n} excerpt(s) for {args.get('query', '')!r}"
        if isinstance(result, dict):
            for k in ("items", "events", "results", "periods"):
                if isinstance(result.get(k), list):
                    return f"{len(result[k])} {k}"
        return "ok"
    except Exception:
        return "ok"


def _citation_from(ref: str, name: str, args: Dict[str, Any],
                   result: Any) -> Dict[str, Any]:
    """Build the user-facing citation object for one tool result."""
    cite: Dict[str, Any] = {"ref": ref, "tool": name,
                            "symbol": (args.get("symbol") or "").upper() or None}
    if name == "search_transcripts":
        hits = (result or {}).get("results") or []
        cite["kind"] = "transcript"
        cite["hits"] = [{
            "transcript_id": h["transcript_id"], "symbol": h["symbol"],
            "announced_at": h["announced_at"], "char_start": h["char_start"],
        } for h in hits[:6]]
        cite["label"] = f"transcript search: {args.get('query', '')[:60]}"
    elif name == "get_guidance":
        cite["kind"] = "guidance"
        cite["label"] = f"{cite['symbol']} guidance extractions"
    elif name == "get_recent_impacts":
        cite["kind"] = "impact"
        cite["label"] = f"{cite['symbol']} analysed events"
    elif name == "web_search":
        cite["kind"] = "web"
        cite["sources"] = (result or {}).get("sources") or []
        cite["label"] = f"web: {args.get('query', '')[:60]}"
    else:
        cite["kind"] = "database"
        cite["label"] = f"{cite['symbol'] or ''} {name}".strip()
    return cite


async def _execute(name: str, args: Dict[str, Any]) -> Any:
    if name == "search_transcripts":
        return await grounding.search_transcript_chunks(
            args["query"], symbol=args.get("symbol"),
            k=int(args.get("k", 6)))
    if name == "get_guidance":
        return await grounding.get_guidance_timeline(args["symbol"])
    if name == "get_recent_impacts":
        return await grounding.get_recent_impacts(
            args["symbol"], days=int(args.get("days", 30)))
    if name == "get_financials":
        return await grounding.get_financials(
            args["symbol"], periods=int(args.get("periods", 8)))
    if name == "get_price_returns":
        windows = args.get("windows") or [5, 30, 90, 365]
        return await grounding.get_price_returns(
            args["symbol"], windows=[int(w) for w in windows])
    if name == "get_shareholding":
        return await grounding.get_shareholding(
            args["symbol"], quarters=int(args.get("quarters", 4)))
    if name == "web_search":
        return await llm.grounded_search(args["query"])
    return {"error": f"Unknown tool: {name}"}


def finalize_answer(text: str, issued: Dict[str, Dict[str, Any]]
                    ) -> Dict[str, Any]:
    """Fail-closed citation resolution (pure, unit-testable): markers that
    were never issued are stripped and counted; citations list contains only
    refs the answer actually uses, in first-use order."""
    used: List[str] = []
    invalid = 0

    def _sub(m: re.Match) -> str:
        nonlocal invalid
        out = []
        for ref in re.split(r"\s*,\s*", m.group(1)):
            if ref in issued:
                if ref not in used:
                    used.append(ref)
                out.append(f"[{ref}]")
            else:
                invalid += 1
        return "".join(out)
    cleaned = _REF_RE.sub(_sub, text or "").strip()
    return {
        "answer": cleaned,
        "citations": [issued[r] for r in used],
        "invalid_refs_stripped": invalid,
    }


async def ask(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """Run one conversational ask. `messages` = prior turns (text-only,
    role user|assistant), last entry the current user question."""
    t0 = time.monotonic()
    convo: List[Dict[str, Any]] = [
        {"role": m["role"], "content": m["content"]}
        for m in messages if m.get("role") in ("user", "assistant")
    ]
    issued: Dict[str, Dict[str, Any]] = {}
    trace: List[Dict[str, Any]] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
             "web_search_calls": 0}
    ref_n = 0
    answer_text: Optional[str] = None
    turns = 0

    for turn in range(CHAT_MAX_TURNS):
        turns = turn + 1
        resp = await llm.acomplete(
            model=CHAT_MODEL, max_tokens=CHAT_MAX_TOKENS,
            system=_SYSTEM, tools=TOOLS, messages=convo,
        )
        usage["input_tokens"] += resp.usage.input_tokens or 0
        usage["output_tokens"] += resp.usage.output_tokens or 0
        usage["thinking_tokens"] += getattr(resp.usage, "thinking_tokens", 0) or 0

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            answer_text = resp.text or ""
            break

        tool_results = []
        for tu in tool_uses:
            if tu.name == "web_search":
                usage["web_search_calls"] += 1
            try:
                result = await _execute(tu.name, dict(tu.input))
            except Exception as e:
                logger.warning(f"[chat] tool {tu.name} error: {e}")
                result = {"error": str(e)[:300]}
            ref_n += 1
            ref = f"T{ref_n}"
            issued[ref] = _citation_from(ref, tu.name, dict(tu.input), result)
            trace.append({"ref": ref, "tool": tu.name, "args": dict(tu.input),
                          "summary": _tool_result_summary(tu.name, dict(tu.input), result)})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps({"ref": ref, "data": result},
                                      default=str)[:_RESULT_CAP],
            })
        convo.append({"role": "assistant", "content": llm.assistant_content(resp)})
        convo.append({"role": "user", "content": tool_results})

    status = "ok" if answer_text else "no_answer"
    final = finalize_answer(answer_text or "", issued)
    if final["invalid_refs_stripped"]:
        logger.warning(f"[chat] stripped {final['invalid_refs_stripped']} "
                       f"uncited ref marker(s)")
    return {
        "answer": final["answer"],
        "citations": final["citations"],
        "status": status,
        "model": CHAT_MODEL,
        "turns": turns,
        "tool_trace": trace,
        "usage": usage,
        "latency_ms": (time.monotonic() - t0) * 1000,
        "invalid_refs_stripped": final["invalid_refs_stripped"],
    }
