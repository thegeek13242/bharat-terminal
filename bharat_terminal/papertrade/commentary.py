"""C0 — management-commentary extraction + deterministic veto rules.

WHY (MANAGEMENT_COMMENTARY_2026-07-31.md §C0, HYUNDAI founding exhibit):
the impact classifier verbalizes offsetting forward guidance in its own
rationale and still scores on trailing numbers — on 120d/855 results events
the direction call was wrong in 42.4% of decisive moves. The fix is NOT a
better prompt (the Tier-B replay falsified rubric rewrites): an LLM does
EXTRACTION ONLY here (guidance numerics, one-off attribution, warning and
hedging language — where LLMs verifiably beat dictionaries) and this module's
pure-Python rules decide the veto. No direct LLM return-scoring anywhere.

Veto rules (deterministic, auditable, validated by research/commentary_replay
before enforcement — pre-registered bar: veto rate on wrong-direction events
must be >= 3x the rate on right-direction events):
  SHORT veto — quantified guidance reaffirmed/raised (HYUNDAI), quantified
    one-off behind the miss (GOCOLORS), or quantified recovery drivers
    (FINOPB). Qualitative optimism alone ("confident", "well-positioned")
    NEVER vetoes: every filing has it.
  LONG veto — explicit warning language (KPITTECH 30-Jun) or high hedging
    (ADFFOODS).

Extraction input = the triggering filing's own text plus same-symbol exchange
filings from the previous 2h (the cluster: results table before a press
release, or a press release before a presentation — table arrives first in
99.2% of clusters, so the lookback is how a later-piece trigger sees the
numbers and an earlier-landed PR is not wasted, cf. UFO 28-Jul).

Fail-open: any extraction failure returns None and the caller must proceed
un-vetoed with provenance.c0.error set — a broken extractor must never block
the validated baseline strategies.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from bharat_terminal import llm

logger = logging.getLogger(__name__)

COMMENTARY_MODEL = os.getenv("COMMENTARY_MODEL", "gemini-2.5-flash")
COMMENTARY_TIMEOUT_S = float(os.getenv("COMMENTARY_TIMEOUT_S", "45"))
# Trigger doc dominates; siblings give the numbers/commentary context.
_TRIGGER_CHARS = 60_000
_SIBLING_CHARS = 20_000
_MAX_SIBLINGS = 4

# Same-symbol exchange filings that landed before the trigger (2h window).
# news_items rows appear ~1-3 min after a sibling finishes analysis, so a
# piece filed seconds before the trigger may be missed — acceptable: the
# press release trails the results table by median 22 min when it exists.
_SIBLINGS_SQL = """
SELECT id, headline, body, timestamp_utc
FROM news_items
WHERE source IN ('NSE_FILINGS', 'BSE_FILINGS')
  AND symbols_mentioned ? $1
  AND timestamp_utc >= $2::timestamptz - interval '2 hours'
  AND timestamp_utc <= $2::timestamptz
  AND id <> $3
  AND char_length(coalesce(body, '')) > 500
ORDER BY timestamp_utc DESC
LIMIT 4
"""

EXTRACT_TOOL = {
    "name": "extract_commentary",
    "description": (
        "Extract management commentary facts from Indian exchange filings. "
        "Report ONLY what the text states — no inference, no scoring, no "
        "opinion about the stock. Empty lists/false when absent."),
    "input_schema": {
        "type": "object",
        "properties": {
            "guidance": {
                "type": "array",
                "description": (
                    "Forward guidance statements BY MANAGEMENT with a metric "
                    "and period (e.g. 'FY27 EBITDA margin 11-14%', '8-10% "
                    "volume growth'). Not analyst views, not boilerplate."),
                "items": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string"},
                        "period": {"type": "string"},
                        "value_text": {"type": "string",
                                       "description": "verbatim number/range"},
                        "quantified": {
                            "type": "boolean",
                            "description": "true only if a number/range/"
                                           "percentage is stated"},
                        "verdict": {
                            "type": "string",
                            "enum": ["new", "reaffirmed", "raised", "cut",
                                     "withdrawn"],
                            "description": "reaffirmed/raised/cut ONLY when "
                                           "the text itself says so (e.g. "
                                           "'stated guidance', 'we maintain', "
                                           "'revised upwards')"},
                        "quote": {"type": "string",
                                  "description": "supporting verbatim quote, "
                                                 "<= 200 chars"},
                    },
                    "required": ["metric", "quantified", "verdict", "quote"],
                },
            },
            "one_off_attribution": {
                "type": "object",
                "description": (
                    "Management attributing the period's weakness to one-time/"
                    "temporary items (exceptional expense, write-off, plant "
                    "shutdown, transition)."),
                "properties": {
                    "present": {"type": "boolean"},
                    "items": {"type": "array", "items": {"type": "string"}},
                    "quantified_cr": {
                        "type": "number",
                        "description": "total quantified one-off amount in "
                                       "Rs crore; 0 if none stated"},
                    "quote": {"type": "string"},
                },
                "required": ["present"],
            },
            "recovery_narrative": {
                "type": "object",
                "description": (
                    "Management stating recovery/normalization ahead ('recovery "
                    "from Q2', '100% normalization of production', 'degrowth "
                    "eased', 'expansion on track')."),
                "properties": {
                    "present": {"type": "boolean"},
                    "quantified": {
                        "type": "boolean",
                        "description": "true only if tied to numbers (capacity "
                                       "%, order book Rs, growth %, disbursal "
                                       "multiples)"},
                    "drivers": {"type": "array", "items": {"type": "string"}},
                    "horizon": {"type": "string",
                                "description": "e.g. 'Q2 FY27', 'H2', 'next "
                                               "12 months', '' if unstated"},
                    "quote": {"type": "string"},
                },
                "required": ["present", "quantified"],
            },
            "warning_language": {
                "type": "object",
                "description": (
                    "Explicit negative forward statements ('lower than "
                    "expected', 'margins to decline', 'constraints limiting "
                    "revenue', 'demand remains weak'). NOT generic risk "
                    "boilerplate."),
                "properties": {
                    "present": {"type": "boolean"},
                    "quotes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["present"],
            },
            "hedging_score": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": (
                    "Density of uncertainty/hedging in FORWARD statements "
                    "('cautiously optimistic', 'subject to', 'monitoring', "
                    "'headwinds persist'). high = hedged on multiple "
                    "material dimensions."),
            },
            "mgmt_quote_present": {
                "type": "boolean",
                "description": "a named-executive quote exists in the text",
            },
        },
        "required": ["guidance", "one_off_attribution", "recovery_narrative",
                     "warning_language", "hedging_score",
                     "mgmt_quote_present"],
    },
}


async def fetch_siblings(pool, symbol: str, trigger_ts, trigger_news_id: str
                         ) -> list[dict]:
    """Cluster lookback; [] on any error (fail-open)."""
    try:
        rows = await pool.fetch(_SIBLINGS_SQL, symbol.upper(), trigger_ts,
                                trigger_news_id)
        return [dict(r) for r in rows[:_MAX_SIBLINGS]]
    except Exception as e:
        logger.warning(f"[C0] sibling lookback failed for {symbol}: {e}")
        return []


def build_prompt(symbol: str, headline: str, body: str,
                 siblings: list[dict]) -> str:
    parts = [
        f"Exchange filing(s) by {symbol} (Indian listed company). Extract "
        f"management-commentary facts via the extract_commentary tool. "
        f"Report only what is stated in the text.",
        f"\n== TRIGGERING FILING ==\nHeadline: {headline}\n"
        f"{(body or '')[:_TRIGGER_CHARS]}",
    ]
    for s in siblings:
        parts.append(
            f"\n== EARLIER SAME-DAY FILING ({s['timestamp_utc']}) ==\n"
            f"Headline: {s['headline']}\n{(s['body'] or '')[:_SIBLING_CHARS]}")
    return "\n".join(parts)


async def extract(symbol: str, headline: str, body: str,
                  siblings: list[dict],
                  model: str = COMMENTARY_MODEL) -> dict | None:
    """One forced-tool extraction call. None on failure (caller fail-opens)."""
    prompt = build_prompt(symbol, headline, body, siblings)
    t0 = time.time()
    try:
        response = await asyncio.wait_for(
            llm.acomplete(
                model=model,
                max_tokens=2048,
                tools=[EXTRACT_TOOL],
                tool_choice={"type": "tool", "name": "extract_commentary"},
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=COMMENTARY_TIMEOUT_S,
        )
        result = None
        for block in response.content:
            if block.type == "tool_use" and block.name == "extract_commentary":
                result = block.input
                break
        if not isinstance(result, dict):
            raise ValueError("no extract_commentary tool_use in response")
        result["_meta"] = {
            "model": model,
            "latency_ms": round((time.time() - t0) * 1000),
            "siblings_used": [s["id"] for s in siblings],
            "trigger_chars": len(body or ""),
        }
        return result
    except Exception as e:
        logger.warning(f"[C0] extraction failed for {symbol}: {e}")
        return None


def veto_decision(extraction: dict, side: str) -> tuple[bool, str | None]:
    """Pure deterministic veto off extracted fields. Returns (veto, rule)."""
    if not extraction:
        return False, None
    if side == "SHORT":
        for g in extraction.get("guidance") or []:
            if (isinstance(g, dict) and g.get("quantified")
                    and g.get("verdict") in ("reaffirmed", "raised")):
                return True, "guidance_reaffirmed"
        oo = extraction.get("one_off_attribution") or {}
        if oo.get("present") and (oo.get("quantified_cr") or 0) > 0:
            return True, "one_off_quantified"
        rn = extraction.get("recovery_narrative") or {}
        if rn.get("present") and rn.get("quantified"):
            return True, "quantified_recovery"
    elif side == "LONG":
        if (extraction.get("warning_language") or {}).get("present"):
            return True, "warning_language"
        if extraction.get("hedging_score") == "high":
            return True, "hedging_high"
    return False, None


def provenance_fields(extraction: dict | None, veto: bool,
                      rule: str | None, mode: str) -> dict:
    """Compact, audit-complete c0 block for trade provenance."""
    if extraction is None:
        return {"mode": mode, "error": "extraction_failed", "veto": False}
    slim = {k: v for k, v in extraction.items() if k != "_meta"}
    return {"mode": mode, "veto": veto, "rule": rule,
            "extraction": slim, **extraction.get("_meta", {})}
