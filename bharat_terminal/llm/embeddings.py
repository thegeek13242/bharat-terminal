"""Vertex text embeddings (768-dim) for the event-memory index.

Reuses the same Vertex / google-genai setup as the LLM wrapper. The dimension is
pinned to EMBEDDING_DIM (768) to match the `memory_items.embedding` column and its
HNSW cosine index — a mismatch raises loudly rather than silently corrupting the
index. `text-embedding-004` and `gemini-embedding-001` both emit 768 dims at the
`global` Vertex region (verified live).

Task types follow Vertex conventions: index the memory items as
RETRIEVAL_DOCUMENT, embed the incoming news as RETRIEVAL_QUERY, so the query and
document spaces line up for cosine retrieval.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Optional, Sequence

from bharat_terminal.llm.wrapper import (
    VERTEX_PROJECT,
    VERTEX_GEMINI_REGION,
    LLM_CALL_TIMEOUT_S,
)

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-004")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "768"))
# Vertex text-embedding endpoints cap instances/request; keep batches modest.
_BATCH = int(os.getenv("EMBEDDING_BATCH", "100"))
# Memory summaries are short; bound defensively so a stray long body can't blow
# the per-instance token limit.
_MAX_CHARS = int(os.getenv("EMBEDDING_MAX_CHARS", "2000"))


def _client():
    from google import genai
    from google.genai import types
    return genai.Client(
        vertexai=True, project=VERTEX_PROJECT, location=VERTEX_GEMINI_REGION,
        http_options=types.HttpOptions(timeout=int(LLM_CALL_TIMEOUT_S * 1000)),
    )


def _embed_sync(texts: List[str], task_type: str) -> List[List[float]]:
    from google.genai import types
    client = _client()
    cfg = types.EmbedContentConfig(
        output_dimensionality=EMBEDDING_DIM, task_type=task_type
    )
    out: List[List[float]] = []
    for i in range(0, len(texts), _BATCH):
        # Empty strings are rejected by the API — substitute a single space.
        chunk = [((t or "")[:_MAX_CHARS] or " ") for t in texts[i:i + _BATCH]]
        resp = client.models.embed_content(
            model=EMBEDDING_MODEL, contents=chunk, config=cfg
        )
        for e in resp.embeddings:
            v = list(e.values)
            if len(v) != EMBEDDING_DIM:
                raise ValueError(
                    f"embedding dim {len(v)} != {EMBEDDING_DIM} "
                    f"(model={EMBEDDING_MODEL}); the memory_items HNSW index "
                    f"requires {EMBEDDING_DIM}"
                )
            out.append(v)
    return out


async def embed_texts(
    texts: Sequence[str], task_type: str = "RETRIEVAL_DOCUMENT"
) -> List[List[float]]:
    """Embed a batch of texts → list of 768-float vectors (order preserved).
    Runs the sync Vertex client in a thread so it doesn't block the event loop."""
    items = list(texts)
    if not items:
        return []
    return await asyncio.to_thread(_embed_sync, items, task_type)


async def embed_query(text: str) -> Optional[List[float]]:
    """Embed a single query string (the incoming news context). None on empty."""
    if not (text or "").strip():
        return None
    vecs = await embed_texts([text], task_type="RETRIEVAL_QUERY")
    return vecs[0] if vecs else None
