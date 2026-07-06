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

from bharat_terminal import llm

from bharat_terminal.types import NewsItem, ImpactReport, CompanyImpact, TradeSignal
from bharat_terminal.analysis.stages.stage1_relevance import classify_relevance
from bharat_terminal.analysis.stages.stage4_propagation import propagate_impacts
from bharat_terminal.analysis.llm_logger import get_llm_logger, LLMCallRecord, compute_cost
from bharat_terminal.analysis.stages.stage2_extraction import BSE_SECTORS
from bharat_terminal.analysis.signal_gates import is_derivative_headline
from bharat_terminal.analysis.pipeline import process_news_item

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL = os.getenv("EXTRACTION_MODEL", "gemini-2.5-flash")

# Batch limits
BATCH_MAX_SIZE  = int(os.getenv("BATCH_MAX_SIZE",  "20"))   # articles per call
BATCH_WINDOW_S  = int(os.getenv("BATCH_WINDOW_S",  "300"))  # 5-min window
# Max strong items escalated from batch triage to the full pipeline per batch.
BATCH_ESCALATE_MAX = int(os.getenv("BATCH_ESCALATE_MAX", "5"))
# Off-hours batch TRIAGE snippet. This path packs up to BATCH_MAX_SIZE items
# into ONE LLM call, so the per-item budget is multiplied by the batch size —
# a large value here (e.g. a full article) produces a multi-hundred-K-char
# prompt that runs 85-110s and can hang the consumer. Triage only needs enough
# context to decide relevance + which items to escalate; the escalated items go
# through the full RT pipeline, which DOES read the complete full_body (120k).
# So keep this small. (Was briefly raised to 12000 and overloaded the batch
# call — do not feed full_body here.)
_BODY_CHARS = int(os.getenv("BATCH_BODY_CHARS", "3000"))

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
        "You are triaging a raw news wire for an Indian equity fund's trading desk. "
        "Assess each article for tradeable information content. Most articles carry "
        "none — be strict: reserve high conviction for clear, material, company- or "
        "sector-moving surprises; mark recaps, derivative coverage, and opinion "
        "pieces low. Return one result per article in the same order.\n"
    ]
    for i, item in enumerate(items):
        body_snippet = (item.full_body or item.body or "")[:_BODY_CHARS]
        lines.append(
            f"--- Article {i} ---\n"
            f"Headline: {item.headline}\n"
            f"Source: {item.source}\n"
            f"Body: {body_snippet}\n"
        )
    return "\n".join(lines)


async def _call_batch_llm(items: List[NewsItem]) -> list:
    """Single LLM call for N articles. Returns raw 'results' list or []."""
    llm_logger = get_llm_logger()
    t0 = time.time()

    try:
        response = await llm.acomplete(
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
        # Re-raise so the caller can fail loudly instead of silently writing
        # relevant-but-empty reports that are indistinguishable from "analysed,
        # nothing found". A *successful* call returning an empty results array is
        # legitimate and handled by the normal return above.
        raise

    # tool_choice forces the batch tool, so a missing tool_use block is a
    # malformed/failed response — surface it rather than returning [].
    raise RuntimeError("Batch LLM response had no batch_analyze_news tool_use block")


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

    # Stage 2+3+5 fused: single LLM call.
    # If the LLM is unavailable (over-limit/quota/network), DO NOT fabricate
    # relevant-but-empty reports — those look "analysed, nothing found" and are
    # indistinguishable from a real null result (the project's characteristic
    # failure mode). Instead drop the relevant items unpublished and log loudly
    # so the gap is visible and the items can be reprocessed.
    try:
        llm_results = await _call_batch_llm(relevant_items)
    except Exception as e:
        logger.error(
            f"[BATCH] LLM unavailable — NOT publishing {len(relevant_indices)} "
            f"relevant items as empty stubs. Dropped news_ids="
            f"{[items[i].id for i in relevant_indices]} | cause: {e}"
        )
        for i in relevant_indices:
            reports[i] = None
        return reports  # type: ignore[return-value]

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

        # Batch triage NEVER fabricates trade signals. The shallow batch call
        # (300-char bodies, forced per-article direction enum) produced template
        # signals with unvalidated symbols — unacceptable output. Strong items
        # are instead escalated to the full per-item pipeline below, where real
        # stage-5 generation + quality gates apply.
        trade_signals: List[TradeSignal] = []

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

    # ── Escalation: strong batch items get the FULL per-item pipeline ──────
    # Batch triage is for cheap classification; signals require the real
    # stage 2→3→5 analysis with quality gates. Escalate only items where the
    # triage saw a high-conviction directional event on a non-derivative
    # headline, capped per batch to bound cost.
    candidates = []
    for original_idx, item, llm_r, direct_impacts in stage4_tasks:
        if (
            llm_r.get("trade_direction") in ("long", "short")
            and llm_r.get("conviction") == "high"
            and direct_impacts
            and not is_derivative_headline(item.headline)
        ):
            candidates.append(
                (float(llm_r.get("overall_confidence", 0.0)), original_idx, item)
            )
    candidates.sort(reverse=True)

    for _conf, original_idx, item in candidates[:BATCH_ESCALATE_MAX]:
        try:
            logger.info(f"[BATCH→FULL] escalating: {item.headline[:80]!r}")
            reports[original_idx] = await process_news_item(item)
        except Exception as e:
            # Keep the triage report (impacts, no signals) on escalation failure.
            logger.warning(f"[BATCH→FULL] escalation failed for {item.id}: {e}")

    elapsed = (time.time() - batch_start) * 1000
    logger.info(
        f"Batch complete: {len(items)} items in {elapsed:.0f}ms "
        f"({len(relevant_indices)} relevant, "
        f"{min(len(candidates), BATCH_ESCALATE_MAX)} escalated)"
    )
    return reports  # type: ignore[return-value]
