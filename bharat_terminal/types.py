from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Literal
import uuid


NewsTier = Literal["tier1", "tier2", "tier3", "sentiment"]
BodyFetchStatus = Literal["pending", "success", "failed", "skipped", "cached"]


class NewsItem(BaseModel):
    model_config = {"frozen": True}
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str  # "NSE_FILINGS" | "BSE_FILINGS" | "ECONOMIC_TIMES" | etc
    timestamp_utc: datetime
    headline: str
    body: Optional[str] = None          # RSS summary / short description
    url: str
    raw_html: Optional[str] = None
    ingest_latency_ms: float
    category: Optional[str] = None   # "CORPORATE_FILING" | "NEWS" | "ANNOUNCEMENT"
    symbols_mentioned: List[str] = Field(default_factory=list)

    # ── Pillar 2 extensions ───────────────────────────────────────────────
    full_body: Optional[str] = None                # Full article text (post-fetch)
    body_word_count: Optional[int] = None
    body_fetch_status: BodyFetchStatus = "pending"
    body_extraction_method: Optional[str] = None   # "trafilatura" | "readability" | "rss"
    dedup_hash: Optional[str] = None               # sha256 of normalized URL
    tier: NewsTier = "tier2"                       # Latency tier


class SentimentItem(BaseModel):
    """
    Sentiment/probability signal from prediction markets (e.g. Polymarket).

    Not a news article — a point-in-time probability observation for a binary
    market, intended for macro-sentiment analysis (rate decisions, geopolitics,
    commodity prices, elections).
    """
    model_config = {"frozen": True}
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str = "POLYMARKET"
    timestamp_utc: datetime
    market_id: str
    question: str
    category: str = "PREDICTION_MARKET"
    probability: float                            # 0.0 – 1.0 (YES price)
    prev_probability: Optional[float] = None      # Last observation before this one
    delta_1h: Optional[float] = None              # Shift vs ~1h ago
    volume_24h: Optional[float] = None
    liquidity: Optional[float] = None
    end_date: Optional[datetime] = None           # Market resolution deadline
    tags: List[str] = Field(default_factory=list)
    market_url: Optional[str] = None
    ingest_latency_ms: float = 0.0
    tier: Literal["sentiment"] = "sentiment"


class SentimentSnapshot(BaseModel):
    """
    Classified prediction-market signal — the output of the sentiment-worker.

    Maps a raw SentimentItem (point-in-time Polymarket probability) onto the
    Indian-equity worldview: which macro theme does this market track, which
    NSE sectors / symbols are exposed, and which direction does the latest
    delta push them.

    A snapshot is produced ONLY when the underlying market is classifiable
    AND the move is meaningful (|delta_1h| ≥ noise floor configured per theme,
    OR first-observation of an unseen market).
    """
    model_config = {"frozen": True}
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sentiment_id: str                       # source SentimentItem.id
    market_id: str                          # source SentimentItem.market_id
    source: str = "POLYMARKET"
    timestamp_utc: datetime
    question: str
    market_url: Optional[str] = None

    # Classification
    macro_theme: str                        # e.g. "FED_RATES", "OIL_PRICES"
    theme_confidence: float = Field(ge=0.0, le=1.0)
    direction: Literal["bullish_in", "bearish_in", "neutral", "ambiguous"]
    magnitude: int = Field(ge=1, le=5)      # how big the implied India-equity move is
    affected_sectors: List[str] = Field(default_factory=list)
    watchlist_symbols: List[str] = Field(default_factory=list)
    explanation: str

    # Carry-through observables
    probability: float                      # latest YES prob (0-1)
    prev_probability: Optional[float] = None
    delta_1h: Optional[float] = None
    volume_24h: Optional[float] = None

    processing_latency_ms: float = 0.0
    created_at: datetime = Field(default_factory=lambda: datetime.utcnow())


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


class CommodityTick(BaseModel):
    """
    Single MCX tick — last-traded-price observation for a commodity future.

    Emitted by the Angel One SmartWebSocketV2 adapter onto raw.commodity.in.
    Price is INR per the contract's quote unit (e.g. ₹/barrel for CRUDEOIL,
    ₹/10g for GOLD). Volume is cumulative trade count for the day.
    """
    model_config = {"frozen": True}
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str = "ANGELONE_MCX"
    timestamp_utc: datetime
    exchange_timestamp_ms: Optional[int] = None    # Exchange-side ts if provided

    commodity: str                                  # Normalised: CRUDEOIL, GOLD, SILVER, COPPER, ALUMINIUM, NATURALGAS
    contract_symbol: str                            # e.g. "CRUDEOIL25NOV24FUT"
    token: str                                      # Angel One scrip token
    expiry: Optional[str] = None                    # "DDMMMYYYY"

    last_price: float                               # ₹ in quote unit
    open_price: Optional[float] = None
    high_price: Optional[float] = None
    low_price: Optional[float] = None
    prev_close: Optional[float] = None

    last_quantity: Optional[int] = None
    avg_price: Optional[float] = None
    volume_day: Optional[int] = None
    total_buy_qty: Optional[float] = None
    total_sell_qty: Optional[float] = None

    ingest_latency_ms: float = 0.0
    tier: Literal["commodity"] = "commodity"


class DerivativeTick(BaseModel):
    """
    Single F&O tick — SnapQuote-mode observation for an NSE F&O contract
    (stock future, index future, or option) from Angel One SmartWebSocketV2.

    Emitted onto raw.fno.in. The fno-engine consumer downstream filters
    these to "OI-significant" events (|Δ OI| >= threshold) plus periodic
    heartbeats before persisting — raw tick rate is ~5M/day across 60+
    contracts, too high to store unfiltered.

    Channel semantics:
      Open interest CHANGE is the real signal (informed flow direction).
      Absolute OI matters only as a sanity check on liquidity.
    """
    model_config = {"frozen": True}
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str = "ANGELONE_NFO"
    timestamp_utc: datetime
    exchange_timestamp_ms: Optional[int] = None

    symbol: str                                     # Underlying: RELIANCE, NIFTY, BANKNIFTY, …
    instrument_type: Literal["FUT", "CE", "PE"]
    contract_symbol: str                            # e.g. "RELIANCE25MAY26FUT" / "NIFTY15MAY2625000CE"
    token: str                                      # Angel One scrip token
    expiry: Optional[str] = None                    # "DDMMMYYYY"
    strike: Optional[float] = None                  # None for futures

    last_price: float
    open_price: Optional[float] = None
    high_price: Optional[float] = None
    low_price: Optional[float] = None
    prev_close: Optional[float] = None

    last_quantity: Optional[int] = None
    avg_price: Optional[float] = None

    # The OI fields are the whole point of subscribing in SnapQuote mode
    open_interest: Optional[int] = None
    change_in_oi: Optional[int] = None              # vs previous day's close
    volume_day: Optional[int] = None

    total_buy_qty: Optional[float] = None
    total_sell_qty: Optional[float] = None

    iv: Optional[float] = None                      # Implied vol for options

    ingest_latency_ms: float = 0.0
    tier: Literal["derivative"] = "derivative"


class CommodityShock(BaseModel):
    """
    Anomaly event: commodity price moved enough vs. its rolling baseline that
    we believe the move encodes information not yet in the news topic.

    Emitted by the commodity-engine when:
      • |return| over `window_minutes` exceeds `pct_move_threshold`, AND
      • z-score of return relative to rolling realised vol exceeds
        `z_threshold`.
    """
    model_config = {"frozen": True}
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp_utc: datetime

    commodity: str                                  # CRUDEOIL, GOLD, …
    contract_symbol: str
    direction: Literal["up", "down"]

    pct_move: float                                 # signed (-0.031 = -3.1%)
    z_score: float                                  # signed
    window_minutes: int

    spot_price: float                               # current LTP that triggered
    baseline_price: float                           # price `window_minutes` ago

    detection_latency_ms: float = 0.0
