"""
Main LangGraph analysis pipeline DAG.
News → Relevance → Extraction → Impact → Propagation → Signals → Publish
End-to-end SLA: ≤5s at P95
"""
import asyncio
import time
import logging
import os
import uuid
from typing import TypedDict, List, Optional
from datetime import datetime, timezone

from langgraph.graph import StateGraph, END

from bharat_terminal.types import NewsItem, ImpactReport, CompanyImpact, TradeSignal
from bharat_terminal.analysis.stages.stage1_relevance import classify_relevance
from bharat_terminal.analysis.stages.stage2_extraction import extract_entities
from bharat_terminal.analysis.stages.stage3_impact import score_impacts
from bharat_terminal.analysis.stages.stage4_propagation import propagate_impacts
from bharat_terminal.analysis.stages.stage5_signals import generate_signals
from bharat_terminal.analysis.llm_logger import get_llm_logger

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

    # Stage 4 outputs
    all_impacts: List[CompanyImpact]

    # Stage 5 outputs
    trade_signals: List[TradeSignal]

    # Final
    impact_report: Optional[ImpactReport]
    error: Optional[str]


# ─── Stage nodes ─────────────────────────────────────────────────────────────

def stage1_node(state: PipelineState) -> PipelineState:
    """Stage 1: Relevance classification (sync)."""
    result = classify_relevance(state["news_item"])
    return {
        **state,
        "relevant": result["relevant"],
        "relevance_confidence": result["confidence"],
        "relevance_reason": result["reason"],
    }


def stage2_node(state: PipelineState) -> PipelineState:
    """Stage 2: Entity/sector extraction."""
    result = extract_entities(state["news_item"])
    return {
        **state,
        "macro_theme": result["macro_theme"],
        "affected_sectors": result["affected_sectors"],
        "resolved_symbols": result["resolved_symbols"],
        "extraction_sentiment": result["sentiment_direction"],
    }


async def stage3_node(state: PipelineState) -> PipelineState:
    """Stage 3: Impact scoring."""
    impacts = await score_impacts(
        state["news_item"],
        state["resolved_symbols"],
        state["macro_theme"],
    )
    return {**state, "direct_impacts": impacts}


async def stage4_node(state: PipelineState) -> PipelineState:
    """Stage 4: Graph propagation."""
    all_impacts = await propagate_impacts(state["direct_impacts"])
    return {**state, "all_impacts": all_impacts}


def stage5_node(state: PipelineState) -> PipelineState:
    """Stage 5: Trade signal generation."""
    signals = generate_signals(
        state["news_item"],
        state["all_impacts"],
        state["macro_theme"],
    )
    return {**state, "trade_signals": signals}


def finalize_node(state: PipelineState) -> PipelineState:
    """Assemble final ImpactReport."""
    elapsed_ms = (time.time() - state["pipeline_start_time"]) * 1000

    if elapsed_ms > 5000:
        logger.warning(f"E2E pipeline SLA breach: {elapsed_ms:.0f}ms > 5000ms for {state['news_item'].id}")
    else:
        logger.info(f"Pipeline completed in {elapsed_ms:.0f}ms for {state['news_item'].id}")

    report = ImpactReport(
        id=str(uuid.uuid4()),
        news_id=state["news_item"].id,
        news_item=state["news_item"],
        relevant=state["relevant"],
        confidence=state["relevance_confidence"],
        macro_theme=state.get("macro_theme"),
        affected_sectors=state.get("affected_sectors", []),
        company_impacts=state.get("all_impacts", []),
        trade_signals=state.get("trade_signals", []),
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
    graph.add_node("stage2", stage2_node)
    graph.add_node("stage3", stage3_node)
    graph.add_node("stage4", stage4_node)
    graph.add_node("stage5", stage5_node)
    graph.add_node("finalize", finalize_node)
    graph.add_node("skip", skip_node)

    graph.set_entry_point("stage1")

    graph.add_conditional_edges("stage1", should_proceed, {
        "stage2": "stage2",
        "skip": "skip",
    })

    graph.add_edge("stage2", "stage3")
    graph.add_edge("stage3", "stage4")
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
        all_impacts=[],
        trade_signals=[],
        impact_report=None,
        error=None,
    )

    result = await pipeline.ainvoke(initial_state)

    if result.get("impact_report") is None:
        raise RuntimeError(f"Pipeline failed for {news_item.id}: no impact_report in state")

    return result["impact_report"]
