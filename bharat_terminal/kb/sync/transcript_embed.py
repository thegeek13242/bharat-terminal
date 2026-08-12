"""Transcript chunking + embedding (A1 phase 3.2) — the retrieval substrate.

Chunks are contiguous character SPANS of full_text (verbatim doctrine:
retrieval can only ever surface provable slices of the filed document).
Chunking is structure-aware: whole speaker turns are grouped up to a target
size, never crossing the prepared-remarks/Q&A boundary; a single turn longer
than the hard cap is split at sentence boundaries. The embedded input is the
span text with a small symbol/date prefix (conditions the vector only — the
STORED chunk is the bare span).

Sizes align with llm/embeddings.py's per-instance cap (EMBEDDING_MAX_CHARS
2000): target 1600, hard max 2000 — also the right retrieval granularity
(~2 speaker turns).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

EMBED_VERSION = 1
CHUNK_TARGET_CHARS = int(os.getenv("TRANSCRIPT_CHUNK_TARGET", "1600"))
CHUNK_MAX_CHARS = int(os.getenv("TRANSCRIPT_CHUNK_MAX", "2000"))

_SENT_RE = re.compile(r"[.!?]\s+")


def _split_long_span(text: str, start: int, end: int,
                     hard_max: int) -> List[tuple[int, int]]:
    """Split [start, end) into <=hard_max sub-spans at sentence boundaries
    (falling back to a hard cut when a single sentence exceeds the cap)."""
    spans: List[tuple[int, int]] = []
    pos = start
    while end - pos > hard_max:
        window = text[pos:pos + hard_max]
        cut = 0
        for m in _SENT_RE.finditer(window):
            cut = m.end()
        if cut < hard_max // 4:  # no usable boundary — hard cut
            cut = hard_max
        spans.append((pos, pos + cut))
        pos += cut
    if pos < end:
        spans.append((pos, end))
    return spans


def chunk_transcript(full_text: str,
                     structure: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pure function: (start, end, section) chunk spans for one transcript."""
    qa = (structure or {}).get("qa_start")
    segments = (structure or {}).get("segments") or []

    def section_of(off: int) -> Optional[str]:
        if qa is None:
            return None
        return "qa" if off >= qa else "prepared"

    # Base spans: speaker turns (structure), else blank-line paragraphs.
    base: List[tuple[int, int]] = []
    if segments:
        base = [(g["start"], g["end"]) for g in segments]
    else:
        pos = 0
        for m in re.finditer(r"\n\s*\n", full_text):
            if m.start() > pos:
                base.append((pos, m.start()))
            pos = m.end()
        if pos < len(full_text):
            base.append((pos, len(full_text)))

    # Split any base span beyond the hard cap, then greedily group
    # consecutive spans up to the target, never crossing the Q&A boundary.
    atoms: List[tuple[int, int]] = []
    for s, e in base:
        if e - s > CHUNK_MAX_CHARS:
            atoms.extend(_split_long_span(full_text, s, e, CHUNK_MAX_CHARS))
        elif e > s:
            atoms.append((s, e))

    chunks: List[Dict[str, Any]] = []
    cur_s = cur_e = None
    cur_section = None
    for s, e in atoms:
        sec = section_of(s)
        if (cur_s is not None
                and (e - cur_s > CHUNK_TARGET_CHARS or sec != cur_section)):
            chunks.append({"start": cur_s, "end": cur_e, "section": cur_section})
            cur_s = None
        if cur_s is None:
            cur_s, cur_section = s, sec
        cur_e = e
    if cur_s is not None:
        chunks.append({"start": cur_s, "end": cur_e, "section": cur_section})

    for i, c in enumerate(chunks):
        c["chunk_index"] = i
    return chunks


async def embed_transcript(SessionFactory, transcript_id: int, symbol: str,
                           announced_at, full_text: str,
                           structure: Optional[Dict[str, Any]]) -> int:
    """Chunk + embed + store one transcript (idempotent replace).
    Returns the chunk count. Raises on embedding failure (caller fail-softs)."""
    from sqlalchemy import text as sql_text
    from bharat_terminal.llm.embeddings import embed_texts, EMBEDDING_COLUMN

    chunks = chunk_transcript(full_text, structure)
    if not chunks:
        return 0
    prefix = f"{symbol} earnings call {announced_at.date().isoformat()}: "
    inputs = [prefix + full_text[c["start"]:c["end"]] for c in chunks]
    vectors = await embed_texts(inputs, task_type="RETRIEVAL_DOCUMENT")

    async with SessionFactory() as db:
        await db.execute(sql_text(
            "DELETE FROM transcript_chunks WHERE transcript_id = :tid"),
            {"tid": transcript_id})
        for c, v in zip(chunks, vectors):
            await db.execute(sql_text(f"""
                INSERT INTO transcript_chunks
                    (transcript_id, symbol, announced_at, chunk_index,
                     char_start, char_end, section, {EMBEDDING_COLUMN},
                     embedding_version)
                VALUES (:tid, :sym, :ann, :idx, :cs, :ce, :sec,
                        (:vec)::vector, :ver)
            """), {"tid": transcript_id, "sym": symbol, "ann": announced_at,
                   "idx": c["chunk_index"], "cs": c["start"], "ce": c["end"],
                   "sec": c["section"],
                   "vec": "[" + ",".join(f"{x:.6f}" for x in v) + "]",
                   "ver": EMBED_VERSION})
        await db.execute(sql_text(
            "UPDATE concall_transcripts SET embed_version = :ver, "
            "updated_at = now() WHERE id = :tid"),
            {"ver": EMBED_VERSION, "tid": transcript_id})
        await db.commit()
    return len(chunks)
