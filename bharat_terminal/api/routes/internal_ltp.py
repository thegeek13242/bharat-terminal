"""Internal-only live LTP (Angel One via kb-service).

LICENSING (do not widen): Angel One market data is licensed for the account
holder's own use — it must not be redistributed to customers. This router is
mounted EXCLUSIVELY behind require_internal_user (INTERNAL_RESEARCH_EMAILS
allowlist, empty by default). Making this public requires an exchange-licensed
data feed instead — see the data-sourcing plan.
"""
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/ltp", tags=["internal-ltp"])

KNOWLEDGE_BASE_URL = os.getenv("KNOWLEDGE_BASE_URL", "http://kb-service:8001")


@router.get("/{symbol}")
async def get_internal_ltp(symbol: str):
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(f"{KNOWLEDGE_BASE_URL}/ltp/{symbol.upper()}")
    except Exception as e:
        logger.warning(f"[internal_ltp] kb fetch failed for {symbol}: {e}")
        raise HTTPException(status_code=503, detail="Price source unavailable")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"No price data for {symbol.upper()}")
    if resp.status_code != 200:
        raise HTTPException(status_code=503, detail="Price source unavailable")
    return resp.json()
