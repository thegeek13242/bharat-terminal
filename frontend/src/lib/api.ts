const BASE_URL = '/api';

/** Notified on 401 so the auth layer can clear state + redirect to /login. */
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(`API error ${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    let detail = text;
    try {
      detail = JSON.parse(text).detail ?? text;
    } catch {
      /* not JSON */
    }
    if (response.status === 401) onUnauthorized?.();
    throw new ApiError(response.status, detail);
  }
  return response.json();
}

export const api = {
  // ── Auth ─────────────────────────────────────────────────────────────
  authGoogle: (idToken: string) =>
    request<{ user: any }>('/auth/google', {
      method: 'POST',
      body: JSON.stringify({ id_token: idToken }),
    }),

  me: () => request<{ user: any }>('/auth/me'),

  logout: () => request<{ ok: boolean }>('/auth/logout', { method: 'POST' }),

  getNewsFeed: (limit = 50, sector?: string) =>
    request<{ items: any[]; count: number }>(
      `/news/feed?limit=${limit}${sector ? `&sector=${sector}` : ''}`
    ),

  getImpactFeed: (limit = 50, relevantOnly = true) =>
    request<{ items: any[]; count: number }>(
      `/impact/feed?limit=${limit}&relevant_only=${relevantOnly}`
    ),

  getImpact: (newsId: string) =>
    request<any>(`/impact/${newsId}`),

  getCompany: (symbol: string) =>
    request<any>(`/company/${symbol}`),

  getGraph: (symbol: string, hops = 2) =>
    request<{ nodes: any[]; edges: any[] }>(`/graph/${symbol}?hops=${hops}`),

  getWatchlist: () =>
    request<{ items: any[] }>('/watchlist/'),

  saveWatchlist: (items: any[]) =>
    request('/watchlist/', { method: 'POST', body: JSON.stringify(items) }),

  getSentimentLatest: (theme?: string, direction?: string, limit = 100) => {
    const qs = new URLSearchParams({ limit: String(limit) });
    if (theme) qs.set('theme', theme);
    if (direction) qs.set('direction', direction);
    return request<{ items: any[]; count: number }>(`/sentiment/latest?${qs}`);
  },

  getSentimentRecent: (limit = 100, sinceMinutes = 720) =>
    request<{ items: any[]; count: number }>(
      `/sentiment/recent?limit=${limit}&since_minutes=${sinceMinutes}`
    ),

  getSentimentThemes: (sinceMinutes = 720) =>
    request<{ themes: any[]; count: number }>(
      `/sentiment/themes?since_minutes=${sinceMinutes}`
    ),

  // ── Companies browser ────────────────────────────────────────────────
  listCompanies: (params: {
    q?: string;
    sector?: string;
    has_dossier?: boolean;
    has_financials?: boolean;
    limit?: number;
    offset?: number;
  } = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') qs.set(k, String(v));
    });
    return request<{ total: number; limit: number; offset: number; items: any[] }>(
      `/companies?${qs.toString()}`,
    );
  },

  getCompanyDetail: (symbol: string) =>
    request<any>(`/companies/${encodeURIComponent(symbol)}`),

  getCompanyFinancials: (symbol: string, period: 'quarterly' | 'annual' | 'all' = 'quarterly', limit = 40) =>
    request<{ symbol: string; count: number; items: any[] }>(
      `/companies/${encodeURIComponent(symbol)}/financials?period=${period}&limit=${limit}`,
    ),

  getCompanyPrices: (symbol: string, days = 365) =>
    request<{ symbol: string; count: number; items: any[] }>(
      `/companies/${encodeURIComponent(symbol)}/prices?days=${days}`,
    ),

  getCompanyLinkages: (symbol: string) =>
    request<{ symbol: string; outbound: any[]; inbound: any[] }>(
      `/companies/${encodeURIComponent(symbol)}/linkages`,
    ),

  // ── Dossier ──────────────────────────────────────────────────────────
  listDossiers: (params: { min_quality?: number; limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null) qs.set(k, String(v));
    });
    return request<{ total: number; limit: number; offset: number; items: any[] }>(
      `/dossier/list?${qs.toString()}`,
    );
  },

  getDossier: (symbol: string) =>
    request<any>(`/dossier/${encodeURIComponent(symbol)}`),

  getDossierExposed: (trigger: string, minAbsBeta = 0.05, limit = 50) =>
    request<{ trigger: string; count: number; items: any[] }>(
      `/dossier/exposed/?trigger=${encodeURIComponent(trigger)}&min_abs_beta=${minAbsBeta}&limit=${limit}`,
    ),

  getDossierImpactGraph: (
    trigger: string,
    opts: { max_hops?: number; min_abs_beta?: number; limit_first_degree?: number; limit_per_node?: number } = {},
  ) => {
    const qs = new URLSearchParams({ trigger });
    Object.entries(opts).forEach(([k, v]) => {
      if (v !== undefined) qs.set(k, String(v));
    });
    return request<any>(`/dossier/impact-graph/?${qs.toString()}`);
  },

  getDossierTriggers: () =>
    request<{ count: number; items: any[] }>(`/dossier/triggers/`),

  // ── Daily signal reports ─────────────────────────────────────────────
  /** Current actionable trading day (live), or a specific date's report. */
  getSignalReport: (date?: string) =>
    request<any>(date ? `/signals/daily-report/${date}` : `/signals/daily-report`),

  getSignalReportDates: (limit = 120) =>
    request<{ dates: any[] }>(`/signals/report-dates?limit=${limit}`),

  // ── DB Explorer ──────────────────────────────────────────────────────
  listDbTables: () => request<{ tables: any[] }>(`/db/tables`),

  getDbTable: (table: string, params: { q?: string; limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') qs.set(k, String(v));
    });
    return request<any>(`/db/${encodeURIComponent(table)}?${qs.toString()}`);
  },
};
