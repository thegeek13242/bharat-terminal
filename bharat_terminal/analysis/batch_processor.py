"""
Off-market-hours batch processor.

Collects news items and processes them with a single LLM call (vs 3× per-item
calls in real-time mode), reducing Claude API cost by ~10× during off-hours.

Stage 1 (relevance filter) still runs locally per-item.
Stage 2+3+5 are fused into one batch call.
Stage 4 (graph propagation) runs per-item but is KB-only (no LLM).
"""
import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import List

import anthropic

from bharat_terminal.types import NewsItem, ImpactReport, CompanyImpact, TradeSignal
from bharat_terminal.analysis.stages.stage1_relevance import classify_relevance
from bharat_terminal.analysis.stages.stage4_propagation import propagate_impacts
from bharat_terminal.analysis.llm_logger import get_llm_logger, LLMCallRecord, compute_cost
from bharat_terminal.analysis.stages.stage2_extraction import BSE_SECTORS

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL = os.getenv("EXTRACTION_MODEL", "claude-haiku-4-5-20251001")

# Batch limits
BATCH_MAX_SIZE  = int(os.getenv("BATCH_MAX_SIZE",  "20"))   # articles per call
BATCH_WINDOW_S  = int(os.getenv("BATCH_WINDOW_S",  "300"))  # 5-min window
# Body truncation per article to keep prompt small
_BODY_CHARS = 300

_BATCH_TOOL = {
    "name": "batch_analyze_news",
    "description": (
        "Analyze multiple Indian financial news articles. "
        "Return one result object per article in order."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "description": "One entry per article, same order as input.",
                "items": {
                    "type": "object",
                    "properties": {
                        "article_index": {
                            "type": "integer",
                            "description": "0-based index matching the input list."
                        },
                        "macro_theme": {
                            "type": "string",
                            "description": (
                                "MONETARY_POLICY, EARNINGS, REGULATORY, GEOPOLITICAL, "
                                "COMMODITY, CREDIT, M_AND_A, IPO, SECTORAL, GLOBAL_MACRO"
                            ),
                        },
                        "affected_sectors": {
                            "type": "array",
                            "items": {"type": "string", "enum": BSE_SECTORS},
                        },
                        "companies": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "symbol":    {"type": "string", "description": "NSE ticker e.g. SBIN, TCS"},
                                    "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral"]},
                                    "magnitude": {"type": "integer", "description": "Impact strength 1 (low) to 5 (high)"},
                                    "confidence":{"type": "number",  "description": "Confidence 0.0–1.0"},
                                    "explanation":{"type": "string"},
                                },
                                "required": ["symbol", "sentiment", "magnitude", "confidence"],
                            },
                        },
                        "trade_direction": {
                            "type": "string",
                            "enum": ["long", "short", "neutral", "none"],
                            "description": "Suggested trade direction for the primary company.",
                        },
                        "conviction": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                        "sentiment": {
                            "type": "string",
                            "enum": ["positive", "negative", "neutral", "mixed"],
                        },
                        "overall_confidence": {
                            "type": "number",
                            "description": "0.0–1.0 confidence in this assessment.",
                        },
                    },
                    "required": [
                        "article_index", "macro_theme", "affected_sectors",
                        "companies", "trade_direction", "conviction", "sentiment", "overall_confidence",
                    ],
                },
            }
        },
        "required": ["results"],
    },
}


def _build_prompt(items: List[NewsItem]) -> str:
    lines = [
        "Analyze the following Indian financial news articles for equity market impact. "
        "Return one result per article in the same order.\n"
    ]
    for i, item in enumerate(items):
        body_snippet = (item.body or "")[:_BODY_CHARS]
        lines.append(
            f"--- Article {i} ---\n"
            f"Headline: {item.headline}\n"
            f"Source: {item.source}\n"
            f"Body: {body_snippet}\n"
        )
    return "\n".join(lines)


async def _call_batch_llm(items: List[NewsItem]) -> list:
    """Single Claude call for N articles. Returns raw 'results' list or []."""
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    llm_logger = get_llm_logger()
    t0 = time.time()

    try:
        response = await client.messages.create(
            model=LLM_MODEL,
            max_tokens=4096,
            tools=[_BATCH_TOOL],
            tool_choice={"type": "tool", "name": "batch_analyze_news"},
            messages=[{"role": "user", "content": _build_prompt(items)}],
        )
        latency_ms = (time.time() - t0) * 1000

        record = LLMCallRecord(
            call_id=str(uuid.uuid4()),
            model=LLM_MODEL,
            stage="batch_analysis",
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            latency_ms=latency_ms,
            cost_usd=compute_cost(
                LLM_MODEL,
                response.usage.input_tokens,
                response.usage.output_tokens,
            ),
            success=True,
        )
        llm_logger.log(record)
        logger.info(
            f"Batch LLM: {len(items)} articles | "
            f"tokens={response.usage.input_tokens}+{response.usage.output_tokens} | "
            f"cost=${record.cost_usd:.4f} | latency={latency_ms:.0f}ms"
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == "batch_analyze_news":
                return block.input.get("results", [])

    except Exception as e:
        latency_ms = (time.time() - t0) * 1000
        record = LLMCallRecord(
            call_id=str(uuid.uuid4()),
            model=LLM_MODEL,
            stage="batch_analysis",
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=latency_ms,
            cost_usd=0.0,
            success=False,
            error=str(e),
        )
        llm_logger.log(record)
        logger.error(f"Batch LLM call failed: {e}")

    return []


def _make_irrelevant_report(item: NewsItem, latency_ms: float) -> ImpactReport:
    return ImpactReport(
        id=str(uuid.uuid4()),
        news_id=item.id,
        news_item=item,
        relevant=False,
        confidence=0.0,
        macro_theme=None,
        affected_sectors=[],
        company_impacts=[],
        trade_signals=[],
        processing_latency_ms=latency_ms,
        created_at=datetime.now(timezone.utc),
    )


async def process_batch(items: List[NewsItem]) -> List[ImpactReport]:
    """
    Process a batch of news items with a single LLM call.
    Returns one ImpactReport per input item (same order).
    """
    batch_start = time.time()
    reports: List[ImpactReport | None] = [None] * len(items)

    # Stage 1: local relevance filter (no LLM)
    relevant_indices = []
    for i, item in enumerate(items):
        result = classify_relevance(item)
        if not result["relevant"]:
            reports[i] = _make_irrelevant_report(
                item, (time.time() - batch_start) * 1000
            )
        else:
            relevant_indices.append(i)

    if not relevant_indices:
        logger.info(f"Batch: all {len(items)} items filtered by Stage 1")
        return reports  # type: ignore[return-value]

    relevant_items = [items[i] for i in relevant_indices]
    logger.info(
        f"Batch: {len(relevant_items)}/{len(items)} relevant — calling LLM once"
    )

    # Stage 2+3+5 fused: single LLM call
    llm_results = await _call_batch_llm(relevant_items)

    # Index by article_index for quick lookup
    llm_by_idx = {r["article_index"]: r for r in llm_results}

    # Stage 4 + assemble reports
    stage4_tasks = []
    for batch_pos, original_idx in enumerate(relevant_indices):
        item   = items[original_idx]
        llm_r  = llm_by_idx.get(batch_pos, {})

        # Build CompanyImpacts from batch result
        direct_impacts: List[CompanyImpact] = []
        for co in llm_r.get("companies", []):
            symbol = co.get("symbol", "").upper().strip()
            if not symbol:
                continue
            magnitude = max(1, min(5, int(co.get("magnitude", 2))))
            direct_impacts.append(
                CompanyImpact(
                    symbol=symbol,
                    company_name=symbol,
                    sentiment=co.get("sentiment", "neutral"),
                    magnitude=magnitude,
                    time_horizon="short_term",
                    affected_line_items=[],
                    explanation=co.get("explanation", ""),
                    hop_distance=0,
                    decay_factor=1.0,
                )
            )

        stage4_tasks.append((original_idx, item, llm_r, direct_impacts))

    # Run stage 4 propagation concurrently for all relevant items
    async def _propagate(original_idx, item, llm_r, direct_impacts):
        try:
            all_impacts = await propagate_impacts(direct_impacts)
        except Exception as e:
            logger.warning(f"Stage 4 propagation failed for {item.id}: {e}")
            all_impacts = direct_impacts

        latency_ms = (time.time() - batch_start) * 1000

        # Build trade signal if LLM suggested a directional trade
        trade_signals: List[TradeSignal] = []
        direction = llm_r.get("trade_direction", "none")
        if direction in ("long", "short") and all_impacts:
            top = all_impacts[0]
            trade_signals.append(
                TradeSignal(
                    symbol=top.symbol,
                    direction=direction,
                    instrument_type="equity",
                    conviction=llm_r.get("conviction", "low"),
                    reasoning=f"Batch analysis: {llm_r.get('macro_theme', '')}",
                    stop_loss_rationale="Off-hours batch signal — use tight stop",
                    position_size_pct_of_portfolio=1.0,
                )
            )

        reports[original_idx] = ImpactReport(
            id=str(uuid.uuid4()),
            news_id=item.id,
            news_item=item,
            relevant=True,
            confidence=float(llm_r.get("overall_confidence", 0.5)),
            macro_theme=llm_r.get("macro_theme"),
            affected_sectors=llm_r.get("affected_sectors", []),
            company_impacts=all_impacts,
            trade_signals=trade_signals,
            processing_latency_ms=latency_ms,
            created_at=datetime.now(timezone.utc),
        )

    await asyncio.gather(*[
        _propagate(oi, item, llm_r, di)
        for oi, item, llm_r, di in stage4_tasks
    ])

    elapsed = (time.time() - batch_start) * 1000
    logger.info(
        f"Batch complete: {len(items)} items in {elapsed:.0f}ms "
        f"({len(relevant_indices)} relevant)"
    )
    return reports  # type: ignore[return-value]
