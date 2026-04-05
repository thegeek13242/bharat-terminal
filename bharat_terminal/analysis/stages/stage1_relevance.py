"""
Stage 1: Relevance classification using sentence-transformers.
Classifies whether a news item is relevant to Indian equity markets.
SLA: ≤300ms
"""
import time
import logging
import os
from typing import TypedDict
import numpy as np
from functools import lru_cache
from bharat_terminal.types import NewsItem

logger = logging.getLogger(__name__)

RELEVANCE_THRESHOLD = float(os.getenv("RELEVANCE_THRESHOLD", "0.35"))
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Reference sentences that define "relevant Indian equity market news"
REFERENCE_SENTENCES = [
    "NSE BSE stock market India equity trading",
    "corporate earnings quarterly results revenue profit",
    "RBI interest rate monetary policy inflation India",
    "SEBI regulation securities exchange board India",
    "Nifty Sensex index movement market rally",
    "company merger acquisition deal India",
    "IPO initial public offering listing India",
    "sector performance banking IT pharma auto India",
    "foreign institutional investor FII DII flows India",
    "crude oil commodity price impact India economy",
    "budget fiscal policy government spending India",
    "rupee dollar exchange rate currency India",
]


@lru_cache(maxsize=1)
def _get_model():
    """Lazy load the sentence transformer model (cached after first load)."""
    from sentence_transformers import SentenceTransformer
    logger.info(f"Loading sentence transformer model: {MODEL_NAME}")
    model = SentenceTransformer(MODEL_NAME)
    return model


@lru_cache(maxsize=1)
def _get_reference_embeddings():
    """Pre-compute reference embeddings (cached)."""
    model = _get_model()
    embeddings = model.encode(REFERENCE_SENTENCES, convert_to_numpy=True)
    # Average the reference embeddings to create a single reference vector
    return embeddings


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors."""
    a_norm = np.linalg.norm(a)
    b_norm = np.linalg.norm(b)
    if a_norm == 0 or b_norm == 0:
        return 0.0
    return float(np.dot(a, b) / (a_norm * b_norm))


def max_similarity_to_references(text_embedding: np.ndarray, reference_embeddings: np.ndarray) -> float:
    """Return the maximum cosine similarity to any reference sentence."""
    similarities = [
        cosine_similarity(text_embedding, ref_emb)
        for ref_emb in reference_embeddings
    ]
    return max(similarities)


class RelevanceResult(TypedDict):
    relevant: bool
    confidence: float
    reason: str
    latency_ms: float


def classify_relevance(news_item: NewsItem) -> RelevanceResult:
    """
    Classify whether a news item is relevant to Indian equity markets.
    Uses cosine similarity with pre-computed reference embeddings.

    Returns RelevanceResult with SLA target: ≤300ms
    """
    start_time = time.time()

    # NSE/BSE filings are always relevant — skip embedding check
    if news_item.source in ("NSE_FILINGS", "BSE_FILINGS"):
        latency_ms = (time.time() - start_time) * 1000
        return RelevanceResult(
            relevant=True,
            confidence=1.0,
            reason=f"Corporate filing from {news_item.source} — always relevant",
            latency_ms=latency_ms,
        )

    text = f"{news_item.headline} {news_item.body or ''}".strip()[:512]

    try:
        model = _get_model()
        ref_embeddings = _get_reference_embeddings()

        text_embedding = model.encode([text], convert_to_numpy=True)[0]
        max_sim = max_similarity_to_references(text_embedding, ref_embeddings)

        relevant = max_sim >= RELEVANCE_THRESHOLD
        latency_ms = (time.time() - start_time) * 1000

        if latency_ms > 300:
            logger.warning(f"Stage 1 SLA breach: {latency_ms:.0f}ms > 300ms for {news_item.id}")

        reason = (
            f"Max cosine similarity {max_sim:.3f} {'≥' if relevant else '<'} "
            f"threshold {RELEVANCE_THRESHOLD}"
        )

        return RelevanceResult(
            relevant=relevant,
            confidence=min(1.0, max_sim / RELEVANCE_THRESHOLD) if relevant else max_sim / RELEVANCE_THRESHOLD,
            reason=reason,
            latency_ms=latency_ms,
        )

    except Exception as e:
        logger.error(f"Stage 1 error for {news_item.id}: {e}", exc_info=True)
        latency_ms = (time.time() - start_time) * 1000
        # Fail open: if classifier fails, treat as relevant
        return RelevanceResult(
            relevant=True,
            confidence=0.5,
            reason=f"Classifier error, defaulting to relevant: {e}",
            latency_ms=latency_ms,
        )
