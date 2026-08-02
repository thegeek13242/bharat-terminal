/** Per-user engagement: visits (sessionized), time on platform, feature
 * usage, page views; drill-in adds daily series, watchlist, starred count,
 * research-chat spend. Empty until the beacon + middleware deploy. */
import { useCallback, useEffect, useState } from 'react';
import { ApiError } from '@/lib/api';
import { relTime } from '@/lib/format';
import { apiFetch, ErrorNote, GridHeader, Hint, hms, SectionTitle, Segmented, Tile, TileGrid, usd } from './shared';
import type { TabProps } from './AdminPage';

interface UserRow {
  id: number; email: string; name: string | null; role: string; is_active: boolean;
  last_login_at: string | null; visits: number; total_seconds: number;
  last_visit: string | null; page_views: number;
  top_features: { feature: string; n: number }[];
}

interface Detail {
  user: { id: number; email: string; role: string };
  visits: number; total_seconds: number;
  daily: { day: string; api_calls: number; page_views: number; heartbeats: number }[];
  features: { feature: string; n: number; avg_ms: number | null }[];
  pages: { path: string; n: number; last_seen: string }[];
  watchlist: { symbol: string; price_alert?: number | null; impact_threshold?: number }[];
  starred_count: number;
  research_chat: { asks: number; usd: number } | null;
}

const GRID = 'minmax(200px,1.8fr) 70px 90px 110px 90px minmax(180px,1.4fr)';
const DAYS = [7, 30, 90] as const;

export function AnalyticsTab({ onDenied }: TabProps) {
  const [days, setDays] = useState<number>(30);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await apiFetch<{ users: UserRow[]; error?: string }>(`/admin/analytics/users?days=${days}`);
      setUsers(res.users);
      setNote(res.error ?? null);
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) return onDenied();
      setError(String((e as Error).message));
    }
  }, [days, onDenied]);

  useEffect(() => {
    void load();
  }, [load]);

  const openDetail = async (id: number) => {
    try {
      setDetail(await apiFetch<Detail>(`/admin/analytics/users/${id}?days=${days}`));
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) return onDenied();
      setError(String((e as Error).message));
    }
  };

  return (
    <div>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        <Segmented
          value={String(days)}
          onChange={(v) => { setDays(Number(v)); setDetail(null); }}
          options={DAYS.map((d) => ({ value: String(d), label: `${d}d` }))}
        />
        {note && <Hint warn>{note}</Hint>}
      </div>
      <ErrorNote error={error} />

      <SectionTitle>Users</SectionTitle>
      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <GridHeader grid={GRID} cols={['Email', 'Visits', 'Time', 'Last seen', 'Views', 'Top features']} />
        {users.map((u) => (
          <div
            key={u.id}
            className="row-hover"
            onClick={() => void openDetail(u.id)}
            style={{
              display: 'grid', gridTemplateColumns: GRID, gap: 10, padding: '9px 12px',
              fontSize: 11.5, alignItems: 'center', cursor: 'pointer',
              borderBottom: '1px solid var(--border)',
              background: detail?.user.id === u.id ? 'var(--bg-elev-2)' : undefined,
              opacity: u.is_active ? 1 : 0.55,
            }}
          >
            <span className="mono" style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{u.email}</span>
            <span className="num">{u.visits}</span>
            <span className="num">{hms(u.total_seconds)}</span>
            <span className="mono" style={{ color: 'var(--ink-mute)', fontSize: 10.5 }}>
              {relTime(u.last_visit ?? u.last_login_at)}
            </span>
            <span className="num">{u.page_views}</span>
            <span className="mono" style={{ fontSize: 10, color: 'var(--ink-mute)', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {u.top_features.map((f) => `${f.feature} (${f.n})`).join(' · ') || '—'}
            </span>
          </div>
        ))}
        {users.length === 0 && (
          <div style={{ padding: 12 }}><Hint>no users</Hint></div>
        )}
      </div>

      {detail && (
        <>
          <SectionTitle>{detail.user.email} — last {days}d</SectionTitle>
          <TileGrid>
            <Tile label="Visits" value={detail.visits} />
            <Tile label="Time on platform" value={hms(detail.total_seconds)} />
            <Tile label="Watchlist symbols" value={detail.watchlist.length} />
            <Tile label="Starred events" value={detail.starred_count} />
            {detail.research_chat && (
              <Tile label="Research chat" value={usd(detail.research_chat.usd)} sub={`${detail.research_chat.asks} asks`} />
            )}
          </TileGrid>

          {detail.watchlist.length > 0 && (
            <>
              <SectionTitle>Watchlist</SectionTitle>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {detail.watchlist.map((w) => (
                  <span key={w.symbol} className="tag mono" title={w.price_alert ? `alert @ ${w.price_alert}` : undefined}>
                    {w.symbol}
                  </span>
                ))}
              </div>
            </>
          )}

          <SectionTitle>Feature usage (API calls)</SectionTitle>
          <div className="card" style={{ padding: '8px 14px' }}>
            {detail.features.length === 0 && <Hint>no API activity recorded</Hint>}
            {detail.features.map((f) => {
              const max = detail.features[0]?.n || 1;
              return (
                <div key={f.feature} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '3px 0' }}>
                  <span className="mono" style={{ width: 130, fontSize: 11 }}>{f.feature}</span>
                  <div style={{ flex: 1, height: 7, background: 'var(--bg-elev-2)', borderRadius: 2 }}>
                    <div style={{ width: `${(f.n / max) * 100}%`, height: '100%', background: 'var(--accent)', borderRadius: 2, opacity: 0.75 }} />
                  </div>
                  <span className="num" style={{ width: 60, textAlign: 'right', fontSize: 11 }}>{f.n}</span>
                </div>
              );
            })}
          </div>

          <SectionTitle>Pages</SectionTitle>
          <div className="card" style={{ padding: '8px 14px' }}>
            {detail.pages.length === 0 && <Hint>no page views recorded yet</Hint>}
            {detail.pages.map((p) => (
              <div key={p.path} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, padding: '3px 0' }}>
                <span className="mono">{p.path}</span>
                <span className="num" style={{ color: 'var(--ink-mute)' }}>{p.n} · {relTime(p.last_seen)}</span>
              </div>
            ))}
          </div>

          <SectionTitle>Daily activity (IST days)</SectionTitle>
          <div className="card" style={{ padding: '8px 14px' }}>
            {detail.daily.length === 0 && <Hint>none</Hint>}
            {detail.daily.map((d) => (
              <div key={d.day} style={{ display: 'flex', gap: 14, fontSize: 11, padding: '2px 0' }}>
                <span className="mono" style={{ width: 90 }}>{d.day}</span>
                <span className="num" style={{ color: 'var(--ink-mute)' }}>api {d.api_calls}</span>
                <span className="num" style={{ color: 'var(--ink-mute)' }}>views {d.page_views}</span>
                <span className="num" style={{ color: 'var(--ink-mute)' }}>beats {d.heartbeats}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
