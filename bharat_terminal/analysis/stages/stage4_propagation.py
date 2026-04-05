"""
Stage 4: Graph propagation through company relationship graph.
Traverses up to 2 hops with decay: 0.6x at hop 1, 0.35x at hop 2.
SLA: ≤500ms
"""
import time
import logging
import asyncio
import httpx
from typing import List, Dict
from bharat_terminal.types import CompanyImpact

logger = logging.getLogger(__name__)

DECAY_HOP1 = 0.6
DECAY_HOP2 = 0.35
MAX_HOPS = 2
KNOWLEDGE_BASE_URL = "http://kb-service:8001"


async def fetch_company_relationships(symbol: str) -> List[dict]:
    """Fetch company relationship edges from knowledge base."""
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            resp = await client.get(f"{KNOWLEDGE_BASE_URL}/graph/{symbol}?hops=1")
            if resp.status_code == 200:
                return resp.json().get("edges", [])
    except Exception as e:
        logger.debug(f"Graph fetch failed for {symbol}: {e}")
    return []


def apply_decay(impact: CompanyImpact, hop: int) -> CompanyImpact:
    """Apply decay factor for graph propagation."""
    decay = DECAY_HOP1 if hop == 1 else DECAY_HOP2

    # Decay the magnitude (but keep minimum at 1)
    new_magnitude = max(1, round(impact.magnitude * decay))

    return CompanyImpact(
        symbol=impact.symbol,
        company_name=impact.company_name,
        sentiment=impact.sentiment,
        magnitude=new_magnitude,
        time_horizon="medium_term" if hop == 1 else "long_term",
        affected_line_items=impact.affected_line_items,
        explanation=f"[Hop {hop} indirect exposure, decay {decay}x] {impact.explanation}",
        hop_distance=hop,
        decay_factor=decay,
    )


async def propagate_impacts(
    direct_impacts: List[CompanyImpact],
) -> List[CompanyImpact]:
    """
    Propagate impact scores through company relationship graph up to 2 hops.
    Returns combined list of direct + indirect impacts (deduped, keeping highest magnitude).
    SLA: ≤500ms
    """
    start_time = time.time()

    if not direct_impacts:
        return []

    all_impacts: Dict[str, CompanyImpact] = {
        imp.symbol: imp for imp in direct_impacts
    }

    # Hop 1: fetch neighbors of direct companies
    hop1_tasks = [
        fetch_company_relationships(imp.symbol)
        for imp in direct_impacts
    ]
    hop1_results = await asyncio.gather(*hop1_tasks, return_exceptions=True)

    hop1_companies: Dict[str, List[CompanyImpact]] = {}  # symbol → source impacts

    for impact, edges in zip(direct_impacts, hop1_results):
        if isinstance(edges, Exception) or not edges:
            continue
        for edge in edges[:10]:  # Max 10 neighbors per company
            neighbor_symbol = edge.get("target_symbol") or edge.get("symbol")
            if not neighbor_symbol or neighbor_symbol in all_impacts:
                continue

            indirect = apply_decay(impact, hop=1)
            # Override symbol/name for the neighbor
            propagated = CompanyImpact(
                symbol=neighbor_symbol,
                company_name=edge.get("target_name", neighbor_symbol),
                sentiment=indirect.sentiment,
                magnitude=indirect.magnitude,
                time_horizon=indirect.time_horizon,
                affected_line_items=indirect.affected_line_items,
                explanation=indirect.explanation,
                hop_distance=1,
                decay_factor=DECAY_HOP1,
            )

            if neighbor_symbol not in hop1_companies:
                hop1_companies[neighbor_symbol] = []
            hop1_companies[neighbor_symbol].append(propagated)

    # Deduplicate hop 1: keep highest magnitude
    hop1_deduped: Dict[str, CompanyImpact] = {}
    for symbol, impacts in hop1_companies.items():
        best = max(impacts, key=lambda x: x.magnitude)
        hop1_deduped[symbol] = best

    all_impacts.update(hop1_deduped)

    # Hop 2: fetch neighbors of hop-1 companies (sample top 5 by magnitude)
    top_hop1 = sorted(hop1_deduped.values(), key=lambda x: x.magnitude, reverse=True)[:5]

    if top_hop1:
        hop2_tasks = [fetch_company_relationships(imp.symbol) for imp in top_hop1]
        hop2_results = await asyncio.gather(*hop2_tasks, return_exceptions=True)

        for hop1_impact, edges in zip(top_hop1, hop2_results):
            if isinstance(edges, Exception) or not edges:
                continue
            for edge in edges[:5]:  # Max 5 neighbors per hop-1 company
                neighbor_symbol = edge.get("target_symbol") or edge.get("symbol")
                if not neighbor_symbol or neighbor_symbol in all_impacts:
                    continue

                indirect = apply_decay(hop1_impact, hop=2)
                propagated = CompanyImpact(
                    symbol=neighbor_symbol,
                    company_name=edge.get("target_name", neighbor_symbol),
                    sentiment=indirect.sentiment,
                    magnitude=indirect.magnitude,
                    time_horizon="long_term",
                    affected_line_items=indirect.affected_line_items,
                    explanation=indirect.explanation,
                    hop_distance=2,
                    decay_factor=DECAY_HOP2,
                )
                all_impacts[neighbor_symbol] = propagated

    latency_ms = (time.time() - start_time) * 1000
    if latency_ms > 500:
        logger.warning(f"Stage 4 SLA breach: {latency_ms:.0f}ms > 500ms")

    return list(all_impacts.values())
