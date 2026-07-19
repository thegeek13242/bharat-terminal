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

  getImpactFeed: (limit = 50, relevantOnly = true, before?: string, symbols?: string[]) => {
    const qs = new URLSearchParams({ limit: String(limit), relevant_only: String(relevantOnly) });
    if (before) qs.set('before', before);
    if (symbols && symbols.length > 0) qs.set('symbols', symbols.join(','));
    return request<{ items: any[]; count: number }>(`/impact/feed?${qs.toString()}`);
  },

  getWatchlist: () =>
    request<{ items: any[] }>('/watchlist/'),

  saveWatchlist: (items: any[]) =>
    request('/watchlist/', { method: 'POST', body: JSON.stringify(items) }),

  // ── Starred events (account-level shortlist) ─────────────────────────
  getStarred: () => request<{ items: any[]; count: number }>('/starred/'),

  starEvent: (reportId: string) =>
    request<{ ok: boolean }>(`/starred/${encodeURIComponent(reportId)}`, { method: 'PUT' }),

  unstarEvent: (reportId: string) =>
    request<{ ok: boolean }>(`/starred/${encodeURIComponent(reportId)}`, { method: 'DELETE' }),

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

  // ── Dossier ──────────────────────────────────────────────────────────
  getDossier: (symbol: string) =>
    request<any>(`/dossier/${encodeURIComponent(symbol)}`),

  // ── Earnings surprises (PEAD statistics) ─────────────────────────────
  getRecentSurprises: (days = 7, decile: 'tails' | 'all' = 'tails') =>
    request<{ items: any[]; count: number; historical_context: any }>(
      `/earnings-surprises/recent?days=${days}&decile=${decile}`,
    ),

  // ── Concall transcripts (complete text, never clipped) ───────────────
  getRecentTranscripts: (days = 30, limit = 100) =>
    request<{ items: any[]; count: number }>(`/transcripts/recent?days=${days}&limit=${limit}`),

  getTranscriptsForSymbol: (symbol: string) =>
    request<{ items: any[]; count: number }>(`/transcripts/${encodeURIComponent(symbol)}`),

  getTranscript: (id: number) => request<any>(`/transcripts/id/${id}`),

  askResearchChat: (messages: { role: 'user' | 'assistant'; content: string }[]) =>
    request<{
      answer: string; citations: any[]; status: string;
      model: string; turns: number; latency_ms: number;
      budget: { spent_usd: number; budget_usd: number; remaining_usd: number };
    }>('/research-chat/ask', { method: 'POST', body: JSON.stringify({ messages }) }),

  searchTranscripts: (q: string, symbol?: string, k = 10) => {
    const qs = new URLSearchParams({ q, k: String(k) });
    if (symbol) qs.set('symbol', symbol);
    return request<{ query: string; results: any[] }>(`/transcripts/search?${qs.toString()}`);
  },

  // ── Notable holders / marquee investors ──────────────────────────────
  getHolders: (symbol: string) =>
    request<{ symbol: string; quarter_end: string | null; prior_quarter_end: string | null; holders: any[] }>(
      `/companies/${encodeURIComponent(symbol)}/holders`,
    ),

  getInvestors: (params: { marquee_only?: boolean; min_companies?: number } = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null) qs.set(k, String(v));
    });
    return request<{ count: number; disclaimer: string; investors: any[] }>(
      `/investors${qs.toString() ? `?${qs.toString()}` : ''}`,
    );
  },

  getInvestorPositions: (ident: string) =>
    request<{ id: string; display: string; type: string; n_companies: number; total_value_cr: number | null; disclaimer: string; positions: any[]; recent_deals: any[] }>(
      `/investors/${encodeURIComponent(ident)}`,
    ),

  getDealFlow: (params: { marquee_only?: boolean; include_excluded?: boolean; days?: number } = {}) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null) qs.set(k, String(v));
    });
    return request<{ count: number; disclaimer: string; deals: any[] }>(
      `/deals${qs.toString() ? `?${qs.toString()}` : ''}`,
    );
  },

  // ── Internal-only (403 for regular accounts) ─────────────────────────
  getInternalLtp: (symbol: string) =>
    request<any>(`/internal/ltp/${encodeURIComponent(symbol)}`),
};
