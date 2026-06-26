export interface NewsItem {
  id: string;
  source: string;
  timestamp_utc: string;
  headline: string;
  body?: string;
  url: string;
  raw_html?: string;
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

export type SentimentDirection = 'bullish_in' | 'bearish_in' | 'neutral' | 'ambiguous';

export interface SentimentSnapshot {
  id: string;
  sentiment_id: string;
  market_id: string;
  source: string;
  timestamp_utc: string;
  question: string;
  market_url?: string;
  macro_theme: string;
  theme_confidence: number;
  direction: SentimentDirection;
  magnitude: 1 | 2 | 3 | 4 | 5;
  affected_sectors: string[];
  watchlist_symbols: string[];
  explanation: string;
  probability: number;
  prev_probability?: number | null;
  delta_1h?: number | null;
  volume_24h?: number | null;
  processing_latency_ms: number;
  created_at: string;
}

export interface ThemeAggregate {
  theme: string;
  bullish_in: number;
  bearish_in: number;
  neutral: number;
  ambiguous: number;
  weighted_score: number;
  events: number;
  last_question: string;
  last_delta_1h: number | null;
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

// ── Companies browser ──────────────────────────────────────────────────

export interface CompanySummary {
  symbol: string;
  company_name: string;
  sector?: string | null;
  ltp?: number | null;
  chg_1m_pct?: number | null;
  pe_ratio?: number | null;
  is_active: boolean;
  latest_quarter?: string | null;
  quarters_loaded: number;
  dossier_at?: string | null;
  dossier_quality?: number | null;
}

export interface CompanyDetail {
  symbol: string;
  company_name: string;
  isin?: string;
  sector?: string | null;
  industry?: string | null;
  mcap_cr?: number | null;
  exchange?: string | null;
  is_active: boolean;
  description_200w?: string | null;
  moat_classification?: string | null;
  revenue_ttm_cr?: number | null;
  pat_cr?: number | null;
  ebitda_margin_pct?: number | null;
  pe_ratio?: number | null;
  pb_ratio?: number | null;
  roe_pct?: number | null;
  eps_ttm?: number | null;
  net_debt_cr?: number | null;
  interest_coverage?: number | null;
  fcf_yield_pct?: number | null;
  fair_value_per_share?: number | null;
  margin_of_safety_pct?: number | null;
  dcf_bull_value?: number | null;
  dcf_bear_value?: number | null;
  dcf_confidence?: string | null;
  median_target_price?: number | null;
  num_analysts?: number | null;
  buy_pct?: number | null;
  hold_pct?: number | null;
  sell_pct?: number | null;
  revenue_segments?: unknown;
  geography_split?: unknown;
  created_at?: string;
  updated_at?: string;
  latest_quarter?: string | null;
  quarters_loaded?: number;
  latest_price_date?: string | null;
  has_dossier: boolean;
}

export interface AuthUser {
  id: number;
  email: string;
  name?: string | null;
  picture?: string | null;
}

export interface FinancialResult {
  id: number;
  symbol: string;
  period_end: string;
  period_type: string;
  consolidated: boolean;
  revenue_cr?: number | null;
  other_income_cr?: number | null;
  cost_of_materials_cr?: number | null;
  employee_expense_cr?: number | null;
  finance_costs_cr?: number | null;
  depreciation_cr?: number | null;
  other_expenses_cr?: number | null;
  total_expenses_cr?: number | null;
  pbt_cr?: number | null;
  tax_cr?: number | null;
  pat_cr?: number | null;
  pat_attributable_cr?: number | null;
  eps_basic?: number | null;
  eps_diluted?: number | null;
  ebitda_cr?: number | null;
  ebitda_margin_pct?: number | null;
  segment_data?: unknown;
  source: string;
}

export interface PricePoint {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
}

export interface DossierSensitivity {
  trigger_id: string;
  beta: number;
  channel: string;
  lag_days: number;
  confidence: 'high' | 'med' | 'low';
  rationale?: string | null;
  evidence_url?: string | null;
}

export interface DossierLinkage {
  target_symbol: string;
  kind: 'competitor' | 'customer' | 'supplier' | 'subsidiary' | 'jv' | 'sector_peer';
  strength: number;
  direction: 'positive' | 'negative' | 'mixed';
  propagation_lag_days: number;
  rationale?: string | null;
  evidence_url?: string | null;
}

export interface DossierFull {
  symbol: string;
  company_name?: string | null;
  version: string;
  source_quarter?: string | null;
  refresh_kind: string;
  data_quality?: number | null;
  model_used?: string | null;
  tokens_used?: number | null;
  generated_at: string;
  business?: unknown;
  financials?: unknown;
  risks?: unknown;
  catalysts?: unknown;
  mgmt_commentary?: unknown;
  citations?: unknown;
  sensitivities: DossierSensitivity[];
  linkages: DossierLinkage[];
}

export interface DossierListItem {
  symbol: string;
  company_name?: string | null;
  version: string;
  source_quarter?: string | null;
  refresh_kind: string;
  data_quality?: number | null;
  model_used?: string | null;
  tokens_used?: number | null;
  generated_at: string;
  sens_count: number;
  link_count: number;
}

// ── 3D Impact Graph ────────────────────────────────────────────────────

export interface ImpactGraph3DNode {
  id: string;
  type: 'trigger' | 'company';
  label: string;
  company_name?: string | null;
  hop: 0 | 1 | 2;
  beta?: number;
  channel?: string;
  lag_days?: number;
  confidence?: string;
}

export interface ImpactGraph3DLink {
  source: string;
  target: string;
  kind: string;
  channel?: string;
  beta?: number;
  strength?: number;
  direction?: string;
  lag_days?: number;
  value: number;
}

export interface ImpactGraph3DPayload {
  trigger: string;
  nodes: ImpactGraph3DNode[];
  links: ImpactGraph3DLink[];
  stats: {
    n_first_degree: number;
    n_second_degree: number;
    n_links: number;
  };
}

export interface TriggerInfo {
  id: string;
  label: string;
  kind: string;
  unit?: string | null;
  data_source?: string | null;
  coverage_count: number;
  max_abs_beta?: number | null;
}

// ── DB Explorer ────────────────────────────────────────────────────────

export interface DbTableInfo {
  table: string;
  rows: number | null;
  columns: string[];
}

export interface DbTableRows {
  table: string;
  total: number;
  limit: number;
  offset: number;
  columns: string[];
  rows: Record<string, unknown>[];
}
