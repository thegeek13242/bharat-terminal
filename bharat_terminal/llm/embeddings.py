"""Text embeddings for the event-memory index + transcript retrieval.

Two providers, selected by EMBEDDING_PROVIDER:

  vertex  : text-embedding-004 (768-dim) via google-genai — the original setup.
            Requires GCP ADC, which does NOT exist on the AWS host.
  bedrock : Cohere Embed v4 (1024-dim) via the `global.` cross-region profile —
            AWS IAM auth (instance role). The Vertex task types map 1:1 onto
            Cohere input types (RETRIEVAL_DOCUMENT→search_document,
            RETRIEVAL_QUERY→search_query), preserving the asymmetric
            query/document space the retrieval code was built on.

The dimension is pinned to EMBEDDING_DIM to match the pgvector column and its
HNSW cosine index — a mismatch raises loudly rather than silently corrupting
the index. Because the two providers emit different dimensions, each dimension
gets its OWN column: EMBEDDING_COLUMN names it, and every reader/writer must go
through it (memory index/retrieve, transcript embed/search). 768 ↔ `embedding`,
anything else ↔ `embedding_<dim>` (migration 036 added embedding_1024).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import List, Optional, Sequence

from bharat_terminal.llm.wrapper import (
    VERTEX_PROJECT,
    VERTEX_GEMINI_REGION,
    LLM_CALL_TIMEOUT_S,
    BEDROCK_REGION,
    _bedrock_pace_sync,
)

logger = logging.getLogger(__name__)

EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "vertex").lower()

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-004")
# Cohere Embed v4 is served from ap-south-1 ONLY via the global cross-region
# profile (verified live 2026-08-12); quotas are the "Global cross-region ...
# Cohere Embed V4" family (20 req/min, 300k tokens/min).
BEDROCK_EMBEDDING_MODEL = os.getenv("BEDROCK_EMBEDDING_MODEL", "global.cohere.embed-v4:0")

EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM",
                              "1024" if EMBEDDING_PROVIDER == "bedrock" else "768"))
# The pgvector column this dimension lives in. Readers and writers MUST use
# this name — mixing columns across dimensions is exactly the silent-corruption
# failure the dim check below exists to prevent.
EMBEDDING_COLUMN = os.getenv(
    "EMBEDDING_COLUMN", "embedding" if EMBEDDING_DIM == 768 else f"embedding_{EMBEDDING_DIM}")

# Vertex caps instances/request at ~250; Cohere Embed v4 caps texts at 96.
_BATCH = int(os.getenv("EMBEDDING_BATCH",
                       "96" if EMBEDDING_PROVIDER == "bedrock" else "100"))
# Memory summaries are short; bound defensively so a stray long body can't blow
# the per-instance token limit.
_MAX_CHARS = int(os.getenv("EMBEDDING_MAX_CHARS", "2000"))

# Cohere input types for the Vertex task-type names used across the codebase.
_COHERE_INPUT_TYPE = {
    "RETRIEVAL_DOCUMENT": "search_document",
    "RETRIEVAL_QUERY": "search_query",
}


def _client():
    from google import genai
    from google.genai import types
    return genai.Client(
        vertexai=True, project=VERTEX_PROJECT, location=VERTEX_GEMINI_REGION,
        http_options=types.HttpOptions(timeout=int(LLM_CALL_TIMEOUT_S * 1000)),
    )


def _check_dim(v: List[float]) -> List[float]:
    if len(v) != EMBEDDING_DIM:
        raise ValueError(
            f"embedding dim {len(v)} != {EMBEDDING_DIM} "
            f"(provider={EMBEDDING_PROVIDER}); the {EMBEDDING_COLUMN} HNSW "
            f"index requires {EMBEDDING_DIM}"
        )
    return v


def _embed_sync_vertex(texts: List[str], task_type: str) -> List[List[float]]:
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
            out.append(_check_dim(list(e.values)))
    return out


def _embed_sync_bedrock(texts: List[str], task_type: str) -> List[List[float]]:
    import boto3
    from botocore.config import Config
    client = boto3.client(
        "bedrock-runtime", region_name=BEDROCK_REGION,
        config=Config(read_timeout=LLM_CALL_TIMEOUT_S, connect_timeout=10),
    )
    input_type = _COHERE_INPUT_TYPE.get(task_type)
    if input_type is None:
        raise ValueError(f"unmapped embedding task_type: {task_type!r}")
    out: List[List[float]] = []
    for i in range(0, len(texts), _BATCH):
        chunk = [((t or "")[:_MAX_CHARS] or " ") for t in texts[i:i + _BATCH]]
        # Same sliding-window pacer as the Claude calls — Embed V4's global
        # quota is 20 req/min; override via BEDROCK_RPM_LIMIT_GLOBAL_COHERE_...
        _bedrock_pace_sync(BEDROCK_EMBEDDING_MODEL)
        resp = client.invoke_model(
            modelId=BEDROCK_EMBEDDING_MODEL,
            body=json.dumps({
                "texts": chunk,
                "input_type": input_type,
                "embedding_types": ["float"],
                "output_dimension": EMBEDDING_DIM,
            }),
        )
        body = json.loads(resp["body"].read())
        embs = body.get("embeddings")
        vecs = embs.get("float") if isinstance(embs, dict) else embs
        if vecs is None or len(vecs) != len(chunk):
            raise ValueError(
                f"bedrock embed returned {0 if not vecs else len(vecs)} vectors "
                f"for {len(chunk)} texts (model={BEDROCK_EMBEDDING_MODEL})")
        for v in vecs:
            out.append(_check_dim(list(v)))
    return out


def _embed_sync(texts: List[str], task_type: str) -> List[List[float]]:
    if EMBEDDING_PROVIDER == "bedrock":
        return _embed_sync_bedrock(texts, task_type)
    return _embed_sync_vertex(texts, task_type)


async def embed_texts(
    texts: Sequence[str], task_type: str = "RETRIEVAL_DOCUMENT"
) -> List[List[float]]:
    """Embed a batch of texts → list of EMBEDDING_DIM-float vectors (order
    preserved). Runs the sync client in a thread so it doesn't block the loop."""
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
