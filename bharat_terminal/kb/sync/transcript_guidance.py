"""Guidance extraction + QoQ diffs from concall transcripts (A1 phase 3).

Extracts management's forward-looking guidance as structured items, pairs
each against the company's PREVIOUS call, and computes the change verdict.

Trust contract (same doctrine as transcript_structure):
- The model reads the COMPLETE transcript — never clipped.
- Every item's quote must anchor verbatim into full_text or the item is
  DROPPED and counted. Values are stored AS STATED (no unit conversion).
- The model only PAIRS items to prior ids; the raised/lowered/maintained
  verdict is COMPUTED arithmetically here, and only when unit and period
  match the prior item — anything else is 'changed'. The model never gets
  to assert a direction.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from bharat_terminal.kb.sync.transcript_structure import (
    _find_span, _normalize_with_map, _parse_llm_json, _WS,
)

logger = logging.getLogger(__name__)

GUIDANCE_MODEL = os.getenv("TRANSCRIPT_GUIDANCE_MODEL", "gemini-3.5-flash")
GUIDANCE_MAX_TOKENS = int(os.getenv("TRANSCRIPT_GUIDANCE_MAX_TOKENS", "32768"))
GUIDANCE_VERSION = 1
GUIDANCE_ATTEMPTS = int(os.getenv("TRANSCRIPT_GUIDANCE_ATTEMPTS", "2"))

_SYSTEM = """You extract MANAGEMENT GUIDANCE from an Indian earnings-call
transcript: explicit forward-looking commitments or targets stated by the
company's management (growth, margins, volumes, capex, debt, ratios,
timelines with numbers). Not analyst speculation, not past results, not
vague aspiration without a number or date.

You may also receive the guidance items extracted from this company's
PREVIOUS call. When a new item is the same underlying commitment (same
metric, possibly re-stated or revised), reference that prior item's id.

Return STRICT JSON only (no prose, no fences):
{
  "guidance": [
    {"metric": "<short label, e.g. 'volume', 'EBITDA margin', 'capex'>",
     "period": "<'FY27' | 'Q2 FY27' | 'FY29' | null>",
     "value_low": <number | null>,
     "value_high": <number | null>,
     "unit": "<AS STATED: '%' | 'lakh tons' | 'crore' | 'million' | ...>" | null,
     "quote": "<VERBATIM contiguous excerpt (15-60 words) where management
              states this guidance>",
     "prior_id": <id of the matching PREVIOUS-call item | null>}
  ]
}

Hard rules:
- "quote" MUST be a verbatim contiguous substring of the transcript —
  copied exactly, typos included. Non-matching quotes are discarded.
- Numbers exactly as spoken: "6.5 lakh tons to 7 lakh tons" ->
  value_low 6.5, value_high 7, unit "lakh tons". A single number goes in
  BOTH value_low and value_high. NEVER convert units.
- One item per distinct commitment; prefer the most specific statement.
- prior_id only when it is genuinely the same commitment; else null."""


def _norm(s: Optional[str]) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


def compute_verdict(item: Dict[str, Any],
                    prior: Optional[Dict[str, Any]]) -> str:
    """Arithmetic verdict. 'maintained/raised/lowered' ONLY when the pair is
    numerically comparable (both numeric, same unit, same period); any other
    pairing is 'changed'; no pair is 'new'."""
    if not prior:
        return "new"
    try:
        lo, hi = item.get("value_low"), item.get("value_high")
        plo, phi = prior.get("value_low"), prior.get("value_high")
        if (lo is not None and plo is not None
                and _norm(item.get("unit")) == _norm(prior.get("unit"))
                and _norm(item.get("period")) == _norm(prior.get("period"))):
            mid = (float(lo) + float(hi if hi is not None else lo)) / 2
            pmid = (float(plo) + float(phi if phi is not None else plo)) / 2
            if mid > pmid:
                return "raised"
            if mid < pmid:
                return "lowered"
            return "maintained"
    except (TypeError, ValueError):
        pass
    return "changed"


def build_guidance(full_text: str, raw: Dict[str, Any],
                   prior_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Anchor + validate the model's guidance items. Pure function.
    Returns {"items": [...], "stats": {...}}; items carry quote offsets,
    prior_guidance_id and the COMPUTED change_verdict."""
    norm, offmap = _normalize_with_map(full_text)
    prior_by_id = {p["id"]: p for p in prior_items}
    stats = {"total": 0, "anchored": 0}
    items: List[Dict[str, Any]] = []
    for g in raw.get("guidance") or []:
        stats["total"] += 1
        metric = (g.get("metric") or "").strip()
        if not metric:
            continue
        span = _find_span(norm, offmap, g.get("quote") or "")
        if span is None:
            continue  # not verbatim -> does not exist
        lo, hi = g.get("value_low"), g.get("value_high")
        try:
            lo = float(lo) if lo is not None else None
            hi = float(hi) if hi is not None else lo
        except (TypeError, ValueError):
            lo = hi = None
        prior = prior_by_id.get(g.get("prior_id"))
        item = {
            "metric": metric,
            "period": (g.get("period") or None),
            "value_low": lo,
            "value_high": hi,
            "unit": (g.get("unit") or None),
            "quote_start": span[0],
            "quote_end": span[1],
            "prior_guidance_id": prior["id"] if prior else None,
        }
        item["change_verdict"] = compute_verdict(item, prior)
        items.append(item)
        stats["anchored"] += 1
    return {"items": items, "stats": stats}


async def extract_guidance(full_text: str, symbol: str,
                           prior_items: List[Dict[str, Any]]
                           ) -> Optional[Dict[str, Any]]:
    """LLM extraction over the COMPLETE transcript + anchoring/verdicts.
    Returns build_guidance() output, or None on model failure."""
    from bharat_terminal.llm.wrapper import acomplete

    prior_json = json.dumps([
        {"id": p["id"], "metric": p["metric"], "period": p["period"],
         "value_low": p["value_low"], "value_high": p["value_high"],
         "unit": p["unit"]}
        for p in prior_items
    ], ensure_ascii=False)
    user = (f"PREVIOUS-CALL GUIDANCE ITEMS:\n{prior_json}\n\n"
            f"TRANSCRIPT:\n{full_text}")

    raw = None
    for attempt in range(GUIDANCE_ATTEMPTS):
        resp = await acomplete(
            model=GUIDANCE_MODEL, system=_SYSTEM,
            messages=[{"role": "user", "content": user}],
            max_tokens=GUIDANCE_MAX_TOKENS,
            temperature=0.0 if attempt == 0 else 0.4,
        )
        raw = _parse_llm_json(resp.text or "")
        if raw is not None:
            break
        logger.warning(f"[guidance] {symbol}: unparseable JSON "
                       f"(attempt {attempt + 1}/{GUIDANCE_ATTEMPTS})")
    if raw is None:
        return None
    out = build_guidance(full_text, raw, prior_items)
    st = out["stats"]
    logger.info(f"[guidance] {symbol}: {st['anchored']}/{st['total']} items "
                f"anchored, {sum(1 for i in out['items'] if i['prior_guidance_id'])} "
                f"paired to prior call")
    return out
