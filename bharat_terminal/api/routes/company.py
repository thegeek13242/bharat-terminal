"""Company profile proxy endpoints (proxies Agent 3 KB API)."""
import logging
import os
import httpx
from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/company", tags=["company"])

KB_SERVICE_URL = os.getenv("KB_SERVICE_URL", "http://kb-service:8001")


@router.get("/search/")
async def search_companies(q: str = Query(..., min_length=2), limit: int = 10):
    """Search companies by name or symbol."""
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            resp = await client.get(f"{KB_SERVICE_URL}/search", params={"q": q, "limit": limit})
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"KB search proxy error: {e}")
            raise HTTPException(status_code=503, detail="Knowledge base unavailable")


@router.get("/{symbol}")
async def get_company(symbol: str):
    """Get company profile + DCF. Proxies KB service."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{KB_SERVICE_URL}/company/{symbol.upper()}")
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Company {symbol} not found")
            resp.raise_for_status()
            return resp.json()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"KB proxy error for {symbol}: {e}")
            raise HTTPException(status_code=503, detail="Knowledge base unavailable")
