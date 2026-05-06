"""
Redis-backed deduplication for cross-adapter news items.

Two layers:
  1. URL dedup  — sha256(normalized URL) in a Redis SET with 48h TTL
  2. Content dedup — SimHash of (headline + first 200 chars of body);
                     Hamming distance < 3 between two hashes means duplicate.

Used by the Kafka producer *before* publishing, and by adapters before
fetching the full article body (to skip redundant HTTP fetches).
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Optional, Tuple
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import redis.asyncio as aioredis
from simhash import Simhash

from bharat_terminal.types import NewsItem

logger = logging.getLogger(__name__)

# Redis key prefixes
URL_KEY_PREFIX = "news:dedup:url:"
SIMHASH_KEY_PREFIX = "news:dedup:simhash:"
SIMHASH_INDEX_KEY = "news:dedup:simhash:index"  # ZSET for nearby-hash lookup

URL_TTL_SECONDS = 48 * 3600
SIMHASH_TTL_SECONDS = 48 * 3600
SIMHASH_HAMMING_THRESHOLD = 3  # distances below this are considered duplicates

# Tracking params to strip from URLs for canonicalization
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source", "amp",
}


def normalize_url(url: str) -> str:
    """Return a canonical representation of a URL for dedup."""
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip().lower())
        # Drop tracking params
        query = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False)
            if k not in _TRACKING_PARAMS
        ]
        query.sort()
        # Strip trailing slash from path
        path = parsed.path.rstrip("/")
        return urlunparse((parsed.scheme, parsed.netloc, path, "", urlencode(query), ""))
    except Exception:
        return url.strip().lower()


def url_fingerprint(url: str) -> str:
    """sha256 fingerprint of the normalized URL."""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def _content_tokens(item: NewsItem) -> str:
    """Tokens used for SimHash content fingerprint."""
    text = (item.headline or "") + " " + (item.body or "")[:200]
    # Strip punctuation, collapse whitespace
    text = re.sub(r"[^\w\s]", " ", text).lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def content_simhash(item: NewsItem) -> int:
    """64-bit SimHash of the item's content tokens."""
    tokens = _content_tokens(item)
    if not tokens:
        return 0
    return Simhash(tokens).value


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class DedupFilter:
    """Redis-backed URL + content dedup for news items."""

    def __init__(self, redis_client: aioredis.Redis):
        self._redis = redis_client

    async def is_duplicate(self, item: NewsItem) -> Tuple[bool, Optional[str]]:
        """
        Return (is_duplicate, reason). Reason is one of: "url", "simhash", None.
        Does NOT mark the item as seen — caller must call mark_seen separately
        after a successful publish.
        """
        # URL layer
        fp = url_fingerprint(item.url)
        if fp:
            exists = await self._redis.exists(f"{URL_KEY_PREFIX}{fp}")
            if exists:
                return True, "url"

        # SimHash layer — cheap near-duplicate check within a bucket
        sh = content_simhash(item)
        if sh:
            # Bucket by upper 16 bits so we only compare hashes that share a prefix.
            # This keeps the comparison set small enough to scan in-memory.
            bucket = (sh >> 48) & 0xFFFF
            bucket_key = f"{SIMHASH_KEY_PREFIX}bucket:{bucket:04x}"
            candidates = await self._redis.smembers(bucket_key)
            for cand in candidates:
                try:
                    other = int(cand)
                    if hamming_distance(sh, other) < SIMHASH_HAMMING_THRESHOLD:
                        return True, "simhash"
                except (ValueError, TypeError):
                    continue

        return False, None

    async def mark_seen(self, item: NewsItem) -> str:
        """Mark the item as seen in Redis. Returns the URL fingerprint (dedup_hash)."""
        fp = url_fingerprint(item.url)
        if fp:
            await self._redis.set(
                f"{URL_KEY_PREFIX}{fp}", "1", ex=URL_TTL_SECONDS
            )

        sh = content_simhash(item)
        if sh:
            bucket = (sh >> 48) & 0xFFFF
            bucket_key = f"{SIMHASH_KEY_PREFIX}bucket:{bucket:04x}"
            await self._redis.sadd(bucket_key, str(sh))
            await self._redis.expire(bucket_key, SIMHASH_TTL_SECONDS)

        return fp

    async def has_url(self, url: str) -> bool:
        """Fast URL-only presence check (no SimHash)."""
        fp = url_fingerprint(url)
        if not fp:
            return False
        return bool(await self._redis.exists(f"{URL_KEY_PREFIX}{fp}"))
