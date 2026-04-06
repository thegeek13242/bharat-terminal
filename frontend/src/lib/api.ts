const BASE_URL = '/api';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    throw new Error(`API error ${response.status}: ${await response.text()}`);
  }
  return response.json();
}

export const api = {
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
};
