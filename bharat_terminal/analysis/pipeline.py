"""
Main LangGraph analysis pipeline DAG.
News → Relevance → Extraction → Impact → Propagation → Signals → Publish
End-to-end SLA: ≤5s at P95
"""
import asyncio
import time
import logging
import uuid
from typing import Any, TypedDict, List, Optional
from datetime import datetime, timezone

from langgraph.graph import StateGraph, END

from bharat_terminal.types import NewsItem, ImpactReport, CompanyImpact, TradeSignal
from bharat_terminal.analysis.stages.stage1_relevance import classify_relevance
from bharat_terminal.analysis.stages.stage2_extraction import extract_entities
from bharat_terminal.analysis.stages.stage3_impact import (consume_router_meta,
                                                           score_impacts)
from bharat_terminal.analysis.stages.stage4_propagation import propagate_impacts
from bharat_terminal.analysis.stages.stage5_signals import generate_signals
from bharat_terminal.analysis.priced_in import apply_priced_in
from bharat_terminal.analysis.filing_summary import summarize_filing, FILING_SOURCES

logger = logging.getLogger(__name__)


class PipelineState(TypedDict):
    """State passed between pipeline stages."""
    news_item: NewsItem
    pipeline_start_time: float

    # Stage 1 outputs
    relevant: bool
    relevance_confidence: float
    relevance_reason: str

    # Stage 2 outputs
    macro_theme: str
    affected_sectors: List[str]
    resolved_symbols: List[str]
    extraction_sentiment: str

    # Stage 3 outputs
    direct_impacts: List[CompanyImpact]
    # How the event transmits through the graph (gates stage-4 propagation).
    event_scope: str

    # In-flight filing-summary task (runs in parallel with stages 2-3;
    # awaited in finalize). None for non-filing items.
    summary_task: Optional[Any]

    # Priced-in adjustments (observability; see priced_in.py)
    priced_in_adjustments: List[dict]

    # Stage 4 outputs
    all_impacts: List[CompanyImpact]

    # Stage 5 outputs
    trade_signals: List[TradeSignal]

    # Final
    impact_report: Optional[ImpactReport]
    error: Optional[str]


# ─── Stage nodes ─────────────────────────────────────────────────────────────

async def stage1_node(state: PipelineState) -> PipelineState:
    """Stage 1: Relevance classification.

    classify_relevance is synchronous and CPU/IO-bound (zero-shot model on
    CPU + sync Redis cache). Run it in a worker thread so it can never block
    the event loop — with concurrent items in flight, a blocked loop would
    stall every other pipeline task and the Kafka consumer."""
    result = await asyncio.to_thread(classify_relevance, state["news_item"])
    return {
        **state,
        "relevant": result["relevant"],
        "relevance_confidence": result["confidence"],
        "relevance_reason": result["reason"],
    }


async def filing_summary_node(state: PipelineState) -> PipelineState:
    """Kick off the grounded filing summary WITHOUT awaiting it — the LLM call
    (1-3s+) runs in parallel with stages 2-3 and is joined in finalize.

    Fail-soft: any rejection/error yields summary None — a missing summary is
    fine, a wrong one is not (accuracy gate inside summarize_filing)."""
    ni = state["news_item"]
    if ni.source not in FILING_SOURCES or ni.summary:
        return state
    task = asyncio.create_task(
        summarize_filing(ni.headline, ni.full_body or ni.body, ni.source)
    )
    return {**state, "summary_task": task}


# Never let a slow summary hold the report back longer than this — by finalize
# time the summary has already had the whole stage-2..5 window to complete.
SUMMARY_JOIN_TIMEOUT_S = 30.0


async def stage2_node(state: PipelineState) -> PipelineState:
    """Stage 2: Entity/sector extraction."""
    result = await extract_entities(state["news_item"])
    return {
        **state,
        "macro_theme": result["macro_theme"],
        "affected_sectors": result["affected_sectors"],
        "resolved_symbols": result["resolved_symbols"],
        "extraction_sentiment": result["sentiment_direction"],
    }


async def stage3_node(state: PipelineState) -> PipelineState:
    """Stage 3: Impact scoring + event-scope classification."""
    impacts, event_scope = await score_impacts(
        state["news_item"],
        state["resolved_symbols"],
        state["macro_theme"],
        affected_sectors=state.get("affected_sectors", []),
    )
    return {**state, "direct_impacts": impacts, "event_scope": event_scope}


async def priced_in_node(state: PipelineState) -> PipelineState:
    """Downgrade direct impacts the market has already priced in (Feature 1).

    Runs before stage4 so propagation decays from the corrected base. Fail-open:
    a price-lookup error simply leaves impacts unchanged.
    """
    try:
        adjusted, adjustments = await apply_priced_in(state["direct_impacts"])
    except Exception as e:  # never block the pipeline on the priced-in check
        logger.warning(f"[priced_in] node error (skipping downgrade): {e}")
        return {**state, "priced_in_adjustments": []}
    return {**state, "direct_impacts": adjusted, "priced_in_adjustments": adjustments}


async def stage4_node(state: PipelineState) -> PipelineState:
    """Stage 4: Graph propagation, gated by the event's transmission scope."""
    all_impacts = await propagate_impacts(
        state["direct_impacts"],
        event_scope=state.get("event_scope", "company_specific"),
    )
    return {**state, "all_impacts": all_impacts}


async def stage5_node(state: PipelineState) -> PipelineState:
    """Stage 5: Trade signal generation."""
    signals = await generate_signals(
        state["news_item"],
        state["all_impacts"],
        state["macro_theme"],
        affected_sectors=state.get("affected_sectors", []),
    )
    return {**state, "trade_signals": signals}


async def finalize_node(state: PipelineState) -> PipelineState:
    """Assemble final ImpactReport (joining the parallel summary task first)."""
    news_item = state["news_item"]
    task = state.get("summary_task")
    if task is not None:
        try:
            summary = await asyncio.wait_for(task, timeout=SUMMARY_JOIN_TIMEOUT_S)
            if summary:
                news_item = news_item.model_copy(update={"summary": summary})
        except Exception as e:
            # Includes TimeoutError (task cancelled by wait_for) — no summary.
            logger.info(f"[filing_summary] join failed/timed out (no summary): {e}")

    elapsed_ms = (time.time() - state["pipeline_start_time"]) * 1000

    if elapsed_ms > 5000:
        logger.warning(f"E2E pipeline SLA breach: {elapsed_ms:.0f}ms > 5000ms for {state['news_item'].id}")
    else:
        logger.info(f"Pipeline completed in {elapsed_ms:.0f}ms for {state['news_item'].id}")

    report = ImpactReport(
        id=str(uuid.uuid4()),
        news_id=news_item.id,
        news_item=news_item,
        relevant=state["relevant"],
        confidence=state["relevance_confidence"],
        macro_theme=state.get("macro_theme"),
        affected_sectors=state.get("affected_sectors", []),
        company_impacts=state.get("all_impacts", []),
        trade_signals=state.get("trade_signals", []),
        event_scope=state.get("event_scope"),
        # Router-v2 provenance stashed by stage-3 (bucket, Para-A leaf/floor,
        # company type, market-state flags, suppressions) — audit trail for
        # every classification decision.
        router_meta=consume_router_meta(news_item.id),
        processing_latency_ms=elapsed_ms,
        created_at=datetime.now(timezone.utc),
    )

    return {**state, "impact_report": report}


def skip_node(state: PipelineState) -> PipelineState:
    """For irrelevant news: build minimal ImpactReport and skip LLM stages."""
    elapsed_ms = (time.time() - state["pipeline_start_time"]) * 1000
    report = ImpactReport(
        id=str(uuid.uuid4()),
        news_id=state["news_item"].id,
        news_item=state["news_item"],
        relevant=False,
        confidence=state["relevance_confidence"],
        macro_theme=None,
        affected_sectors=[],
        company_impacts=[],
        trade_signals=[],
        processing_latency_ms=elapsed_ms,
        created_at=datetime.now(timezone.utc),
    )
    return {**state, "impact_report": report}


def should_proceed(state: PipelineState) -> str:
    """Route: skip LLM stages if news is irrelevant."""
    return "stage2" if state["relevant"] else "skip"


# ─── Build graph ──────────────────────────────────────────────────────────────

def build_pipeline() -> StateGraph:
    graph = StateGraph(PipelineState)

    graph.add_node("stage1", stage1_node)
    graph.add_node("filing_summary", filing_summary_node)
    graph.add_node("stage2", stage2_node)
    graph.add_node("stage3", stage3_node)
    graph.add_node("priced_in", priced_in_node)
    graph.add_node("stage4", stage4_node)
    graph.add_node("stage5", stage5_node)
    graph.add_node("finalize", finalize_node)
    graph.add_node("skip", skip_node)

    graph.set_entry_point("stage1")

    graph.add_conditional_edges("stage1", should_proceed, {
        "stage2": "filing_summary",
        "skip": "skip",
    })

    graph.add_edge("filing_summary", "stage2")
    graph.add_edge("stage2", "stage3")
    graph.add_edge("stage3", "priced_in")
    graph.add_edge("priced_in", "stage4")
    graph.add_edge("stage4", "stage5")
    graph.add_edge("stage5", "finalize")
    graph.add_edge("finalize", END)
    graph.add_edge("skip", END)

    return graph.compile()


# Singleton compiled pipeline
_pipeline = None

def get_pipeline():
    global _pipeline
    if _pipeline is None:
        _pipeline = build_pipeline()
    return _pipeline


async def process_news_item(news_item: NewsItem) -> ImpactReport:
    """Process a single news item through the full pipeline."""
    pipeline = get_pipeline()

    initial_state = PipelineState(
        news_item=news_item,
        pipeline_start_time=time.time(),
        relevant=False,
        relevance_confidence=0.0,
        relevance_reason="",
        macro_theme="SECTORAL",
        affected_sectors=[],
        resolved_symbols=[],
        extraction_sentiment="neutral",
        direct_impacts=[],
        event_scope="company_specific",
        summary_task=None,
        priced_in_adjustments=[],
        all_impacts=[],
        trade_signals=[],
        impact_report=None,
        error=None,
    )

    result = await pipeline.ainvoke(initial_state)

    if result.get("impact_report") is None:
        raise RuntimeError(f"Pipeline failed for {news_item.id}: no impact_report in state")

    return result["impact_report"]
