from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Literal
import uuid


class NewsItem(BaseModel):
    model_config = {"frozen": True}
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str  # "NSE_FILINGS" | "BSE_FILINGS" | "ECONOMIC_TIMES" | etc
    timestamp_utc: datetime
    headline: str
    body: Optional[str] = None
    url: str
    raw_html: Optional[str] = None
    ingest_latency_ms: float
    category: Optional[str] = None   # "CORPORATE_FILING" | "NEWS" | "ANNOUNCEMENT"
    symbols_mentioned: List[str] = Field(default_factory=list)


class CompanyImpact(BaseModel):
    symbol: str
    company_name: str
    sentiment: Literal["positive", "negative", "neutral"]
    magnitude: int = Field(ge=1, le=5)
    time_horizon: Literal["immediate", "short_term", "medium_term", "long_term"]
    affected_line_items: List[str] = Field(default_factory=list)
    explanation: str
    hop_distance: int = 0
    decay_factor: float = 1.0


class TradeSignal(BaseModel):
    symbol: str
    direction: Literal["long", "short", "neutral"]
    instrument_type: Literal["equity", "futures", "options_call", "options_put", "avoid"]
    suggested_strike_rationale: Optional[str] = None
    position_size_pct_of_portfolio: float = Field(ge=0, le=10)
    stop_loss_rationale: str
    conviction: Literal["low", "medium", "high"]
    reasoning: str


class ImpactReport(BaseModel):
    model_config = {"frozen": True}
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    news_id: str
    news_item: "NewsItem"
    relevant: bool
    confidence: float
    macro_theme: Optional[str] = None
    affected_sectors: List[str] = Field(default_factory=list)
    company_impacts: List["CompanyImpact"] = Field(default_factory=list)
    trade_signals: List["TradeSignal"] = Field(default_factory=list)
    processing_latency_ms: float
    created_at: datetime
