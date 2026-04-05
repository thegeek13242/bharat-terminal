export interface NewsItem {
  id: string;
  source: string;
  timestamp_utc: string;
  headline: string;
  body?: string;
  url: string;
  ingest_latency_ms: number;
  category?: string;
  symbols_mentioned: string[];
}

export interface CompanyImpact {
  symbol: string;
  company_name: string;
  sentiment: 'positive' | 'negative' | 'neutral';
  magnitude: 1 | 2 | 3 | 4 | 5;
  time_horizon: 'immediate' | 'short_term' | 'medium_term' | 'long_term';
  affected_line_items: string[];
  explanation: string;
  hop_distance: number;
  decay_factor: number;
}

export interface TradeSignal {
  symbol: string;
  direction: 'long' | 'short' | 'neutral';
  instrument_type: 'equity' | 'futures' | 'options_call' | 'options_put' | 'avoid';
  suggested_strike_rationale?: string;
  position_size_pct_of_portfolio: number;
  stop_loss_rationale: string;
  conviction: 'low' | 'medium' | 'high';
  reasoning: string;
}

export interface ImpactReport {
  id: string;
  news_id: string;
  news_item: NewsItem;
  relevant: boolean;
  confidence: number;
  macro_theme?: string;
  affected_sectors: string[];
  company_impacts: CompanyImpact[];
  trade_signals: TradeSignal[];
  processing_latency_ms: number;
  created_at: string;
}

export interface RevenueSegment {
  name: string;
  pct_of_revenue: number;
  yoy_growth: number;
}

export interface CompanyProfile {
  identity: {
    symbol: string;
    exchange: string;
    company_name: string;
    sector_nse?: string;
    mcap_cr?: number;
  };
  business: {
    description_200w?: string;
    revenue_segments: RevenueSegment[];
  };
  financials: {
    revenue_ttm_cr?: number;
    ebitda_margin_pct?: number;
    eps_ttm?: number;
    pe_ratio?: number;
  };
  dcf_model: {
    fair_value_per_share?: number;
    margin_of_safety_pct?: number;
    bull_value?: number;
    bear_value?: number;
    wacc_pct?: number;
  };
  analyst_consensus: {
    median_target_price?: number;
    buy_pct?: number;
    hold_pct?: number;
    sell_pct?: number;
  };
}

export interface GraphNode {
  symbol: string;
  name: string;
  hop: number;
}

export interface GraphEdge {
  source_symbol: string;
  target_symbol: string;
  target_name?: string;
  relationship_type: string;
  weight: number;
  hop: number;
}
