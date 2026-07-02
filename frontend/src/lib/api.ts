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

  getImpactFeed: (limit = 50, relevantOnly = true, before?: string) => {
    const qs = new URLSearchParams({ limit: String(limit), relevant_only: String(relevantOnly) });
    if (before) qs.set('before', before);
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
};
