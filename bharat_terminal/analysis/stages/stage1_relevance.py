"""
Stage 1: Relevance classification using sentence-transformers.
Classifies whether a news item is relevant to Indian equity markets.
SLA: ≤300ms

Three-tier filter:
  1. Hard-block pattern list  — kills obvious personal-finance / lifestyle /
                                explainer / AI-prompt noise before any
                                embedding compute (fast + deterministic).
  2. Positive cosine similarity ≥ RELEVANCE_THRESHOLD against market-news
                                reference corpus.
  3. Positive-vs-negative MARGIN — text must be closer to market-news refs
                                than to personal-finance/explainer refs by at
                                least RELEVANCE_NEGATIVE_MARGIN.

The negative-reference tier is what catches articles like:
  • "I asked ChatGPT to calculate monthly savings to build education fund…"
  • "What happens if you forget to extend your PPF account after maturity?"
These otherwise score >0.35 against generic market refs because they share
India+finance+money vocabulary, but they're noticeably closer to the
personal-finance/explainer negative refs.
"""
import re
import time
import logging
import os
from typing import TypedDict
import numpy as np
from functools import lru_cache
from bharat_terminal.types import NewsItem

logger = logging.getLogger(__name__)

RELEVANCE_THRESHOLD = float(os.getenv("RELEVANCE_THRESHOLD", "0.35"))
# How much closer to positive refs vs negative refs we require. Set to 0.0
# to disable the margin check (positive-only behaviour, original behaviour).
RELEVANCE_NEGATIVE_MARGIN = float(os.getenv("RELEVANCE_NEGATIVE_MARGIN", "0.05"))
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# ── Tier 1: hard-block patterns ─────────────────────────────────────────
# Headline/body substrings (case-insensitive, word-boundary where applicable)
# that mark obvious noise. Drop on match — never reaches embedding compute.
# Add new patterns here when you spot a class of noise that's slipping
# through; cheap to extend.
_HARD_BLOCK_PATTERNS = (
    # AI-prompt / how-I-used-X articles
    r"\b(chatgpt|asked\s+(?:ai|chatgpt|gemini|claude)|gemini|i\s+asked)\b",
    r"\bi\s+(?:tried|used|asked|consulted)\b",
    # Personal finance explainers
    r"\bppf\s+account\b",
    r"\b(?:epf|nps|ulip|sip)\s+(?:withdrawal|calculator|account|maturity)\b",
    r"\btax[- ]?saving\b",
    r"\bhow\s+to\s+(?:save|invest|plan|budget|retire|buy|choose)\b",
    r"\b(?:5|6|7|8|10)\s+(?:ways|tips|steps|things|reasons|mistakes)\b",
    r"\b(?:rules\s+explained|explainer|explained\s*[:|])\b",
    r"\bwhat\s+happens\s+if\b",
    r"\b(?:education|retirement|emergency)\s+(?:fund|corpus)\b",
    r"\bbuild\s+(?:a\s+)?(?:fund|corpus|portfolio\s+for\s+children)\b",
    r"\bpersonal\s+finance\b",
    r"\byour\s+money\s*[:|]",
    # Pure lifestyle / entertainment / sports
    r"\b(?:horoscope|astrology|zodiac|tarot)\b",
    r"\b(?:lifestyle|fitness|wellness|weight\s+loss|diet\s+plan)\b",
    r"\b(?:recipe|cooking|kitchen|food)\b",
    r"\b(?:bollywood|hollywood|movie\s+review|trailer|box\s+office)\b",
    r"\b(?:ipl|cricket|football|tennis)\s+(?:match|score|highlights|live)\b",
    r"\b(?:viral\s+video|watch\s*[:|]|caught\s+on\s+camera)\b",
    r"\btravel\s+(?:guide|destination|tips)\b",
)
HARD_BLOCK_RE = re.compile("|".join(_HARD_BLOCK_PATTERNS), re.IGNORECASE)

# ── Tier 2: positive references — "this IS market news" ─────────────────
REFERENCE_SENTENCES = [
    "NSE BSE stock market India equity trading session",
    "company quarterly results earnings revenue profit beat miss",
    "RBI repo rate monetary policy committee meeting decision",
    "SEBI regulation enforcement action securities exchange board",
    "Nifty Sensex index closed higher lower intraday rally",
    "merger acquisition deal stake sale promoter holding",
    "IPO initial public offering listing subscription price band",
    "sector banking IT pharma auto metals capex industrial",
    "foreign institutional investor FII DII flows net buyers sellers",
    "crude oil OPEC production cut impact import bill",
    "budget fiscal deficit borrowing capex tax government",
    "rupee dollar exchange rate USDINR forex reserves",
    "promoter pledge insider trading block deal bulk deal",
    "credit rating upgrade downgrade outlook revised",
    "earnings guidance management commentary outlook FY",
    "regulatory approval product launch capacity expansion announcement",
    "IT services global deal contract digital transformation outsourcing client win",
    "pharma drug approval USFDA observation Form 483 plant inspection",
    "auto sales volume monthly dispatch wholesale retail passenger commercial",
    "cement steel metals demand production capex expansion capacity utilisation",
    "telecom subscriber addition ARPU spectrum 5G tariff hike",
    "NPA gross net interest margin NIM CASA loan book growth",
]

# ── Tier 3: negative references — "this is NOT market news" ─────────────
NEGATIVE_REFERENCE_SENTENCES = [
    "personal finance tips for individuals how to save money",
    "PPF EPF tax saving investment retirement planning calculator",
    "education fund for children college planning monthly savings",
    "credit card loan EMI home loan personal banking guide",
    "ChatGPT AI artificial intelligence used to plan finances",
    "explainer rules explained what happens if step-by-step guide",
    "5 ways tips mistakes things you should know before",
    "lifestyle health wellness fitness diet weight loss food recipe",
    "horoscope astrology zodiac predictions weekly daily",
    "movie film bollywood celebrity entertainment trailer review",
    "cricket IPL football sports match score highlights player",
    "viral video watch caught on camera trending social media",
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
    """Pre-compute positive reference embeddings (cached)."""
    model = _get_model()
    return model.encode(list(REFERENCE_SENTENCES), convert_to_numpy=True)


@lru_cache(maxsize=1)
def _get_negative_reference_embeddings():
    """Pre-compute negative reference embeddings (cached)."""
    model = _get_model()
    return model.encode(list(NEGATIVE_REFERENCE_SENTENCES), convert_to_numpy=True)


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

    Three-tier filter (described in module docstring):
      1. Hard-block pattern match → drop immediately, skip embeddings.
      2. Positive cosine ≥ RELEVANCE_THRESHOLD.
      3. (Positive − Negative) margin ≥ RELEVANCE_NEGATIVE_MARGIN.

    SLA target: ≤300ms
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

    # ── Tier 1: hard-block patterns ──────────────────────────────────────
    # Cheap regex sweep before any embedding compute. Catches the obvious
    # personal-finance / lifestyle / AI-prompt noise that otherwise embeds
    # close enough to market-news refs to slip through.
    block_match = HARD_BLOCK_RE.search(news_item.headline or "")
    if block_match:
        latency_ms = (time.time() - start_time) * 1000
        return RelevanceResult(
            relevant=False,
            confidence=0.95,
            reason=f"Hard-block pattern matched in headline: {block_match.group(0)!r}",
            latency_ms=latency_ms,
        )

    try:
        model = _get_model()
        pos_ref = _get_reference_embeddings()
        neg_ref = _get_negative_reference_embeddings()

        text_embedding = model.encode([text], convert_to_numpy=True)[0]
        max_pos = max_similarity_to_references(text_embedding, pos_ref)
        max_neg = max_similarity_to_references(text_embedding, neg_ref)
        margin = max_pos - max_neg

        # ── Tier 2 + 3: both must pass ────────────────────────────────────
        passes_threshold = max_pos >= RELEVANCE_THRESHOLD
        passes_margin = margin >= RELEVANCE_NEGATIVE_MARGIN
        relevant = passes_threshold and passes_margin

        latency_ms = (time.time() - start_time) * 1000

        if latency_ms > 300:
            logger.warning(
                f"Stage 1 SLA breach: {latency_ms:.0f}ms > 300ms for {news_item.id}"
            )

        if relevant:
            reason = (
                f"pos={max_pos:.3f}≥{RELEVANCE_THRESHOLD} "
                f"margin={margin:+.3f}≥{RELEVANCE_NEGATIVE_MARGIN}"
            )
        elif not passes_threshold:
            reason = f"pos={max_pos:.3f}<{RELEVANCE_THRESHOLD}"
        else:
            reason = (
                f"margin={margin:+.3f}<{RELEVANCE_NEGATIVE_MARGIN} "
                f"(pos={max_pos:.3f} neg={max_neg:.3f}) — closer to noise corpus"
            )

        return RelevanceResult(
            relevant=relevant,
            confidence=min(1.0, max_pos / RELEVANCE_THRESHOLD) if relevant
                       else max(0.0, max_pos / RELEVANCE_THRESHOLD),
            reason=reason,
            latency_ms=latency_ms,
        )

    except Exception as e:
        logger.error(f"Stage 1 error for {news_item.id}: {e}", exc_info=True)
        latency_ms = (time.time() - start_time) * 1000
        # Fail open: if classifier fails, treat as relevant so we don't
        # silently drop news during a model load failure.
        return RelevanceResult(
            relevant=True,
            confidence=0.5,
            reason=f"Classifier error, defaulting to relevant: {e}",
            latency_ms=latency_ms,
        )
