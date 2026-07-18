"""LLM structuring of concall transcripts (A1 phase 2) — annotations only.

Produces the `concall_transcripts.structure` JSONB: speaker turns with roles
(management / analyst / moderator), category highlights ("interesting bits"),
the prepared-remarks→Q&A boundary, and page offsets.

Grounding contract (non-negotiable, mirrors the no-clipping rule):
- The model receives the COMPLETE transcript — never a truncated slice.
- The model returns VERBATIM anchors/quotes; we locate each one in the stored
  text via whitespace-normalized exact matching and store only (start, end)
  offsets into `full_text`. Anything that fails to anchor is DROPPED and
  counted in `stats` — an annotation is either provably in the transcript or
  it does not exist. The base text is never rewritten.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

STRUCTURE_MODEL = os.getenv("TRANSCRIPT_STRUCTURE_MODEL", "gemini-3.5-flash")
# Model output cap, NOT expected usage: Gemini thinking models spend the same
# max_tokens budget on internal thoughts BEFORE the JSON — a 32K budget was
# observed truncating GIPCL's answer at 4K visible tokens. Give the full cap.
STRUCTURE_MAX_TOKENS = int(os.getenv("TRANSCRIPT_STRUCTURE_MAX_TOKENS", "65535"))
STRUCTURE_ATTEMPTS = int(os.getenv("TRANSCRIPT_STRUCTURE_ATTEMPTS", "2"))

HIGHLIGHT_CATEGORIES = (
    "guidance", "outlook", "financials", "one_off", "risk",
    "strategy", "capex", "demand",
)

_SYSTEM = f"""You annotate Indian earnings-call transcripts for equity analysts.
You will receive one COMPLETE transcript. Return STRICT JSON only (no prose,
no markdown fences) with this shape:

{{
  "speakers": [
    {{"name": "<as printed in the transcript>",
      "role": "management" | "analyst" | "moderator",
      "title": "<e.g. CFO>" | null,
      "firm": "<company or brokerage>" | null}}
  ],
  "turns": [
    {{"speaker": "<name exactly as in speakers[]>",
      "anchor": "<the EXACT first 6-12 words of that speaking turn,
                 copied verbatim from the transcript>"}}
  ],
  "qa_anchor": "<EXACT words where the Q&A session begins (usually the
                moderator inviting the first question)>" | null,
  "highlights": [
    {{"category": one of {list(HIGHLIGHT_CATEGORIES)},
      "quote": "<a VERBATIM contiguous excerpt of 15-60 words that an
               analyst should not miss: guidance numbers, margin/order-book
               commentary, one-offs, capex plans, risks, demand signals>"}}
  ]
}}

Hard rules:
- Every "anchor" and "quote" MUST be a verbatim contiguous substring of the
  transcript. Copy characters exactly; do not fix typos, do not paraphrase,
  do not bridge across omitted words. Excerpts failing exact match are
  discarded downstream, so precision matters more than coverage.
- turns[] must list EVERY speaking turn in order, including the moderator's.
- 10-25 highlights for a typical call; only genuinely decision-relevant ones.
- Attribute speakers to management vs analyst from the call's own intro."""

_WS = re.compile(r"\s+")


def _normalize_with_map(text: str) -> Tuple[str, List[int]]:
    """Lowercased, whitespace-collapsed copy of `text` plus a map from each
    normalized char position back to the original offset."""
    norm_chars: List[str] = []
    offmap: List[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space:
                continue
            norm_chars.append(" ")
            offmap.append(i)
            prev_space = True
        else:
            norm_chars.append(ch.lower())
            offmap.append(i)
            prev_space = False
    return "".join(norm_chars), offmap


def _find_span(norm_text: str, offmap: List[int], needle: str,
               from_norm_pos: int = 0) -> Optional[Tuple[int, int, int]]:
    """Locate `needle` (verbatim, whitespace-insensitive) at/after
    `from_norm_pos`. Returns (orig_start, orig_end, norm_pos) or None."""
    n = _WS.sub(" ", needle.strip()).lower()
    if not n:
        return None
    pos = norm_text.find(n, from_norm_pos)
    if pos < 0 and from_norm_pos > 0:
        pos = norm_text.find(n)  # out-of-order fallback: search from start
    if pos < 0:
        return None
    start = offmap[pos]
    last = pos + len(n) - 1
    end = offmap[last] + 1
    return start, end, pos


def _parse_llm_json(raw: str) -> Optional[Dict[str, Any]]:
    """Parse the model's JSON, tolerating accidental code fences."""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.S)  # last resort: outermost object
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        return None


def build_structure(full_text: str, annotations: Dict[str, Any],
                    pages: Optional[List[Dict[str, int]]]) -> Dict[str, Any]:
    """Anchor the model's annotations into `full_text`. Drops what fails.
    Pure function — unit-testable without an LLM."""
    norm, offmap = _normalize_with_map(full_text)
    stats = {"turns_total": 0, "turns_anchored": 0,
             "highlights_total": 0, "highlights_anchored": 0}

    speakers = []
    role_by_name: Dict[str, str] = {}
    for s in annotations.get("speakers") or []:
        name = (s.get("name") or "").strip()
        role = s.get("role") if s.get("role") in ("management", "analyst", "moderator") else None
        if not name or not role:
            continue
        speakers.append({"name": name, "role": role,
                         "title": s.get("title"), "firm": s.get("firm")})
        role_by_name[name.lower()] = role

    # Sequential turn anchoring: each anchor is searched from the previous
    # turn's position, so repeated phrases resolve to the right occurrence.
    raw_turns = annotations.get("turns") or []
    stats["turns_total"] = len(raw_turns)
    anchored: List[Dict[str, Any]] = []
    cursor = 0
    for t in raw_turns:
        span = _find_span(norm, offmap, t.get("anchor") or "", cursor)
        if span is None:
            continue
        start, _, npos = span
        anchored.append({"speaker": (t.get("speaker") or "").strip(),
                         "start": start})
        cursor = npos + 1
    # Monotonic + de-duplicated starts → segment ranges.
    anchored.sort(key=lambda x: x["start"])
    segments: List[Dict[str, Any]] = []
    for i, t in enumerate(anchored):
        if segments and t["start"] <= segments[-1]["start"]:
            continue
        segments.append({
            "speaker": t["speaker"],
            "role": role_by_name.get(t["speaker"].lower(), "analyst"),
            "start": t["start"],
            "end": len(full_text),  # provisional; fixed below
        })
    for i in range(len(segments) - 1):
        segments[i]["end"] = segments[i + 1]["start"]
    stats["turns_anchored"] = len(segments)

    qa_start = None
    if annotations.get("qa_anchor"):
        span = _find_span(norm, offmap, annotations["qa_anchor"])
        if span:
            qa_start = span[0]

    raw_highlights = annotations.get("highlights") or []
    stats["highlights_total"] = len(raw_highlights)
    highlights: List[Dict[str, Any]] = []
    for h in raw_highlights:
        if h.get("category") not in HIGHLIGHT_CATEGORIES:
            continue
        span = _find_span(norm, offmap, h.get("quote") or "")
        if span is None:
            continue  # not verbatim -> does not exist
        highlights.append({"category": h["category"],
                           "start": span[0], "end": span[1]})
    highlights.sort(key=lambda x: x["start"])
    # Drop overlaps (keep first) — overlapping <mark>s render badly.
    pruned: List[Dict[str, Any]] = []
    for h in highlights:
        if pruned and h["start"] < pruned[-1]["end"]:
            continue
        pruned.append(h)
    stats["highlights_anchored"] = len(pruned)

    return {
        "version": 1,
        "pages": pages or [],
        "speakers": speakers,
        "segments": segments,
        "qa_start": qa_start,
        "highlights": pruned,
        "stats": stats,
    }


async def structure_transcript(full_text: str,
                               pages: Optional[List[Dict[str, int]]],
                               symbol: str = "?") -> Optional[Dict[str, Any]]:
    """Run the LLM over the COMPLETE transcript and anchor its annotations.
    Returns the structure dict, or None on failure (caller stores 'failed')."""
    from bharat_terminal.llm.wrapper import acomplete

    annotations = None
    for attempt in range(STRUCTURE_ATTEMPTS):
        resp = await acomplete(
            model=STRUCTURE_MODEL,
            system=_SYSTEM,
            messages=[{"role": "user", "content": full_text}],  # never clipped
            max_tokens=STRUCTURE_MAX_TOKENS,
            # Attempt 0 is deterministic; a truncation/format flake gets one
            # non-zero-temperature retry to escape the identical bad path.
            temperature=0.0 if attempt == 0 else 0.4,
        )
        annotations = _parse_llm_json(resp.text or "")
        if annotations:
            break
        logger.warning(
            f"[structure] {symbol}: unparseable JSON "
            f"(attempt {attempt + 1}/{STRUCTURE_ATTEMPTS}, "
            f"{resp.usage.output_tokens} output tokens)"
        )
    if not annotations:
        return None
    structure = build_structure(full_text, annotations, pages)
    st = structure["stats"]
    logger.info(
        f"[structure] {symbol}: {st['turns_anchored']}/{st['turns_total']} turns, "
        f"{st['highlights_anchored']}/{st['highlights_total']} highlights anchored, "
        f"{len(structure['speakers'])} speakers"
    )
    # Quality floor: if anchoring mostly failed, the annotation layer is not
    # trustworthy — better no structure than a wrong one.
    if st["turns_total"] and st["turns_anchored"] / st["turns_total"] < 0.5:
        logger.warning(f"[structure] {symbol}: <50% turns anchored — rejecting")
        return None
    return structure
