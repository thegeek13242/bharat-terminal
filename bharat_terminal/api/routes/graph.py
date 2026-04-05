"""Company relationship graph endpoints."""
import logging
import os
import httpx
from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/graph", tags=["graph"])

KB_SERVICE_URL = os.getenv("KB_SERVICE_URL", "http://kb-service:8001")


@router.get("/{symbol}")
async def get_company_graph(
    symbol: str,
    hops: int = Query(default=2, ge=1, le=2),
):
    """Get company relationship graph up to N hops. Proxies KB service."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(
                f"{KB_SERVICE_URL}/graph/{symbol.upper()}",
                params={"hops": hops},
            )
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Company {symbol} not found")
            resp.raise_for_status()
            return resp.json()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"KB graph proxy error for {symbol}: {e}")
            raise HTTPException(status_code=503, detail="Knowledge base unavailable")
