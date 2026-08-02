export interface NewsItem {
  id: string;
  source: string;
  timestamp_utc: string;
  headline: string;
  body?: string;
  /** Grounded extractive summary of a filing attachment (filings only; absent when the accuracy gate rejected it). */
  summary?: string | null;
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

export interface ImpactReport {
  id: string;
  news_id: string;
  news_item: NewsItem;
  relevant: boolean;
  confidence: number;
  macro_theme?: string;
  affected_sectors: string[];
  company_impacts: CompanyImpact[];
  processing_latency_ms: number;
  created_at: string;
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
  /** Internal-research allowlist member — gates internal-only UI (research chat). */
  internal?: boolean;
  /** DB-backed role (app_users.role). Server enforces; UI only shapes nav. */
  role?: 'admin' | 'internal' | 'external';
  /** Feature keys the admin disabled for this user (wire format, snake_case). */
  disabled_features?: string[];
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
