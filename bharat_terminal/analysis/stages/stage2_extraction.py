"""
Stage 2: Sector/entity extraction using Claude Haiku with function calling.
SLA: ≤800ms
"""
import re
import time
import logging
import os
import json
import uuid
from typing import TypedDict, List, Optional, Tuple
import anthropic
from bharat_terminal.types import NewsItem
from bharat_terminal.analysis.llm_logger import get_llm_logger, LLMCallRecord, compute_cost

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL = os.getenv("EXTRACTION_MODEL", "claude-haiku-4-5-20251001")

# BSE sector codes (official)
BSE_SECTORS = [
    "BANKING", "NBFC", "IT_TECHNOLOGY", "PHARMA_HEALTHCARE", "AUTO_ANCILLARIES",
    "OIL_GAS", "METALS_MINING", "FMCG", "TELECOM", "POWER_UTILITIES",
    "REAL_ESTATE", "CEMENT", "CHEMICALS", "TEXTILES", "MEDIA_ENTERTAINMENT",
    "AVIATION", "SHIPPING_LOGISTICS", "AGRICULTURE", "INSURANCE", "CAPITAL_GOODS",
    "CONSUMER_DURABLES", "DIVERSIFIED", "MACRO_ECONOMY", "GLOBAL_MARKETS",
]

EXTRACTION_TOOL = {
    "name": "extract_market_entities",
    "description": "Extract structured market intelligence from Indian financial news",
    "input_schema": {
        "type": "object",
        "properties": {
            "macro_theme": {
                "type": "string",
                "description": "Primary macro theme: MONETARY_POLICY, EARNINGS, REGULATORY, GEOPOLITICAL, COMMODITY, CREDIT, M_AND_A, IPO, SECTORAL, GLOBAL_MACRO"
            },
            "affected_bse_sectors": {
                "type": "array",
                "items": {"type": "string", "enum": BSE_SECTORS},
                "description": "BSE sector codes directly affected by this news"
            },
            "named_companies": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "mention_type": {"type": "string", "enum": ["direct", "indirect"]},
                        "probable_symbol": {"type": "string", "description": "NSE symbol if known, e.g. RELIANCE, TCS, INFY"}
                    },
                    "required": ["name", "mention_type"]
                }
            },
            "named_regulators": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Regulators or government bodies mentioned: RBI, SEBI, MCA, DPIIT, etc."
            },
            "economic_indicators": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Economic indicators mentioned: GDP, CPI, WPI, IIP, CAD, etc."
            },
            "sentiment_direction": {
                "type": "string",
                "enum": ["positive", "negative", "neutral", "mixed"],
                "description": "Overall sentiment direction for Indian equity markets"
            }
        },
        "required": ["macro_theme", "affected_bse_sectors", "named_companies", "sentiment_direction"]
    }
}


_SECTOR_KEYWORDS: List[Tuple[str, str]] = [
    # keyword (lower)          → BSE sector code
    ("bank", "BANKING"), ("hdfc", "BANKING"), ("icici", "BANKING"),
    ("sbi", "BANKING"), ("kotak", "BANKING"), ("axis bank", "BANKING"),
    ("nbfc", "NBFC"), ("microfinance", "NBFC"),
    ("pharma", "PHARMA_HEALTHCARE"), ("drug", "PHARMA_HEALTHCARE"),
    ("hospital", "PHARMA_HEALTHCARE"), ("healthcare", "PHARMA_HEALTHCARE"),
    ("medicine", "PHARMA_HEALTHCARE"), ("cipla", "PHARMA_HEALTHCARE"),
    ("sun pharma", "PHARMA_HEALTHCARE"),
    ("software", "IT_TECHNOLOGY"), ("infosys", "IT_TECHNOLOGY"),
    ("tcs", "IT_TECHNOLOGY"), ("wipro", "IT_TECHNOLOGY"),
    ("hcl tech", "IT_TECHNOLOGY"), ("tech mahindra", "IT_TECHNOLOGY"),
    ("it sector", "IT_TECHNOLOGY"),
    ("oil", "OIL_GAS"), ("gas", "OIL_GAS"), ("refin", "OIL_GAS"),
    ("petroleum", "OIL_GAS"), ("reliance", "OIL_GAS"),
    ("automobile", "AUTO_ANCILLARIES"), ("vehicle", "AUTO_ANCILLARIES"),
    ("maruti", "AUTO_ANCILLARIES"), ("tata motor", "AUTO_ANCILLARIES"),
    ("bajaj auto", "AUTO_ANCILLARIES"),
    ("fmcg", "FMCG"), ("hindustan unilever", "FMCG"), ("itc", "FMCG"),
    ("nestle", "FMCG"), ("dabur", "FMCG"),
    ("telecom", "TELECOM"), ("airtel", "TELECOM"), ("jio", "TELECOM"),
    ("vodafone", "TELECOM"), ("5g", "TELECOM"),
    ("metal", "METALS_MINING"), ("steel", "METALS_MINING"),
    ("mining", "METALS_MINING"), ("tata steel", "METALS_MINING"),
    ("jsw", "METALS_MINING"),
    ("power", "POWER_UTILITIES"), ("electricity", "POWER_UTILITIES"),
    ("renewable", "POWER_UTILITIES"), ("adani green", "POWER_UTILITIES"),
    ("real estate", "REAL_ESTATE"), ("housing", "REAL_ESTATE"),
    ("dlf", "REAL_ESTATE"), ("godrej prop", "REAL_ESTATE"),
    ("cement", "CEMENT"), ("ultratech", "CEMENT"), ("acc cement", "CEMENT"),
    ("chemical", "CHEMICALS"), ("pidilite", "CHEMICALS"),
    ("insurance", "INSURANCE"), ("lic", "INSURANCE"), ("hdfc life", "INSURANCE"),
    ("rbi", "BANKING"), ("repo rate", "BANKING"), ("credit policy", "BANKING"),
    ("sebi", "MACRO_ECONOMY"), ("gdp", "MACRO_ECONOMY"),
    ("inflation", "MACRO_ECONOMY"), ("cpi", "MACRO_ECONOMY"),
    ("budget", "MACRO_ECONOMY"), ("fiscal", "MACRO_ECONOMY"),
    ("nifty", "MACRO_ECONOMY"), ("sensex", "MACRO_ECONOMY"),
    ("fed", "GLOBAL_MARKETS"), ("us market", "GLOBAL_MARKETS"),
    ("crude", "OIL_GAS"), ("brent", "OIL_GAS"),
]

_SYMBOL_STOPWORDS = {
    "NSE", "BSE", "IPO", "GDP", "RBI", "SEBI", "FII", "DII", "ETF",
    "MF", "NPS", "FD", "NFO", "SIP", "NAV", "NIFTY", "Q1", "Q2",
    "Q3", "Q4", "YOY", "QOQ", "US", "UK", "UN", "UPI", "MCA",
    "CEO", "CFO", "CTO", "COO", "MD", "AGM", "EGM", "QIP", "OFS",
    "PAT", "EBITDA", "PBT", "EPS", "PE", "PB", "ROE", "WACC",
}

_MACRO_FROM_SECTOR = {
    "BANKING": "MONETARY_POLICY", "NBFC": "MONETARY_POLICY",
    "IT_TECHNOLOGY": "SECTORAL", "PHARMA_HEALTHCARE": "REGULATORY",
    "OIL_GAS": "COMMODITY", "AUTO_ANCILLARIES": "SECTORAL",
    "METALS_MINING": "COMMODITY", "MACRO_ECONOMY": "MONETARY_POLICY",
    "GLOBAL_MARKETS": "GLOBAL_MACRO",
}


def _heuristic_extract(news_item: NewsItem) -> Tuple[List[str], str, List[str]]:
    """
    Keyword-based sector detection + symbol extraction for use when no API key is set.
    Returns (affected_sectors, macro_theme, resolved_symbols).
    """
    text = f"{news_item.headline} {news_item.body or ''}".lower()

    # Sector detection (ordered, deduped)
    seen_sectors: set = set()
    sectors: List[str] = []
    for keyword, sector in _SECTOR_KEYWORDS:
        if keyword in text and sector not in seen_sectors:
            sectors.append(sector)
            seen_sectors.add(sector)
        if len(sectors) >= 3:
            break

    # Symbol extraction from headline — find 2-10 char all-caps words
    potential = re.findall(r'\b[A-Z]{2,10}\b', news_item.headline)
    extracted = [s for s in potential if s not in _SYMBOL_STOPWORDS][:4]
    resolved = list(dict.fromkeys(list(news_item.symbols_mentioned) + extracted))

    # Macro theme: derive from leading sector, else SECTORAL
    macro_theme = "SECTORAL"
    if sectors:
        macro_theme = _MACRO_FROM_SECTOR.get(sectors[0], "SECTORAL")

    return sectors, macro_theme, resolved


class ExtractionResult(TypedDict):
    macro_theme: str
    affected_sectors: List[str]
    named_companies: List[dict]
    named_regulators: List[str]
    economic_indicators: List[str]
    sentiment_direction: str
    latency_ms: float
    resolved_symbols: List[str]


def extract_entities(news_item: NewsItem) -> ExtractionResult:
    """
    Extract sector and entity information using Claude Haiku function calling.
    SLA: ≤800ms
    """
    start_time = time.time()
    llm_logger = get_llm_logger()

    if not ANTHROPIC_API_KEY:
        logger.warning("No ANTHROPIC_API_KEY — using heuristic extraction")
        sectors, macro_theme, resolved = _heuristic_extract(news_item)
        return ExtractionResult(
            macro_theme=macro_theme,
            affected_sectors=sectors,
            named_companies=[],
            named_regulators=[],
            economic_indicators=[],
            sentiment_direction="neutral",
            latency_ms=(time.time() - start_time) * 1000,
            resolved_symbols=resolved,
        )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""Analyze this Indian financial news and extract structured market intelligence.

Headline: {news_item.headline}
Body: {(news_item.body or '')[:800]}
Source: {news_item.source}

Extract all relevant market entities, sectors, and the macro theme."""

    llm_start = time.time()
    try:
        response = client.messages.create(
            model=LLM_MODEL,
            max_tokens=1024,
            tools=[EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": "extract_market_entities"},
            messages=[{"role": "user", "content": prompt}],
        )

        llm_latency = (time.time() - llm_start) * 1000

        # Log the call
        record = LLMCallRecord(
            call_id=str(uuid.uuid4()),
            model=LLM_MODEL,
            stage="stage2_extraction",
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            latency_ms=llm_latency,
            cost_usd=compute_cost(LLM_MODEL, response.usage.input_tokens, response.usage.output_tokens),
            success=True,
        )
        llm_logger.log(record)

        # Parse tool call result
        tool_result = None
        for block in response.content:
            if block.type == "tool_use" and block.name == "extract_market_entities":
                tool_result = block.input
                break

        if not tool_result:
            raise ValueError("No tool_use block in response")

        total_latency = (time.time() - start_time) * 1000
        if total_latency > 800:
            logger.warning(f"Stage 2 SLA breach: {total_latency:.0f}ms > 800ms for {news_item.id}")

        # Combine explicit symbols from news_item with LLM-extracted ones
        resolved_symbols = list(news_item.symbols_mentioned)
        for co in tool_result.get("named_companies", []):
            if co.get("probable_symbol"):
                sym = co["probable_symbol"].upper().strip()
                if sym and sym not in resolved_symbols:
                    resolved_symbols.append(sym)

        return ExtractionResult(
            macro_theme=tool_result.get("macro_theme", "SECTORAL"),
            affected_sectors=tool_result.get("affected_bse_sectors", []),
            named_companies=tool_result.get("named_companies", []),
            named_regulators=tool_result.get("named_regulators", []),
            economic_indicators=tool_result.get("economic_indicators", []),
            sentiment_direction=tool_result.get("sentiment_direction", "neutral"),
            latency_ms=total_latency,
            resolved_symbols=resolved_symbols,
        )

    except Exception as e:
        llm_latency = (time.time() - llm_start) * 1000
        record = LLMCallRecord(
            call_id=str(uuid.uuid4()),
            model=LLM_MODEL,
            stage="stage2_extraction",
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=llm_latency,
            cost_usd=0.0,
            success=False,
            error=str(e),
        )
        llm_logger.log(record)
        logger.error(f"Stage 2 error for {news_item.id}: {e}")

        return ExtractionResult(
            macro_theme="SECTORAL",
            affected_sectors=[],
            named_companies=[],
            named_regulators=[],
            economic_indicators=[],
            sentiment_direction="neutral",
            latency_ms=(time.time() - start_time) * 1000,
            resolved_symbols=list(news_item.symbols_mentioned),
        )
