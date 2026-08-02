/** Health: containers (docker-proxy) + loop heartbeats + kafka consumer lag.
 * 10s visibility-aware polling. "Container Up but loop stalled" is the
 * project's characteristic failure mode — the two views are shown side by
 * side on purpose. */
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '@/lib/api';
import { relTime } from '@/lib/format';
import { apiFetch, ErrorNote, GridHeader, Hint, SectionTitle, StateChip, Tile, TileGrid } from './shared';
import type { TabProps } from './AdminPage';

const POLL_MS = 10_000;

interface Health {
  containers: { name: string; image: string; state: string; status: string; health: string | null }[];
  heartbeats: { service: string; alive: boolean; last_beat: string | null; age_seconds: number | null }[];
  kafka_lag: Record<string, number>;
  stalled_services: string[];
  generated_at: string;
  note?: string;
}

const C_GRID = 'minmax(180px,1.4fr) 90px minmax(200px,1.6fr) 90px';
const H_GRID = 'minmax(180px,1fr) 90px 140px';

export function HealthTab({ onDenied }: TabProps) {
  const [data, setData] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inflight = useRef(false);

  const load = useCallback(async () => {
    if (inflight.current || document.hidden) return;
    inflight.current = true;
    try {
      setData(await apiFetch<Health>('/admin/health'));
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) return onDenied();
      setError(String((e as Error).message));
    } finally {
      inflight.current = false;
    }
  }, [onDenied]);

  useEffect(() => {
    void load();
    const t = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(t);
  }, [load]);

  if (!data) return <Hint>{error ?? 'Loading…'}</Hint>;

  const up = data.containers.filter((c) => c.state === 'running').length;
  const unhealthy = data.containers.filter((c) => c.health === 'unhealthy').length;
  const stalled = data.stalled_services.length;
  const lagTotal = Object.values(data.kafka_lag).reduce((a, b) => a + b, 0);

  return (
    <div>
      <TileGrid>
        <Tile label="Containers up" value={`${up}/${data.containers.length}`}
              color={up === data.containers.length ? 'var(--tail)' : 'var(--head)'} />
        <Tile label="Unhealthy" value={unhealthy} color={unhealthy ? 'var(--head)' : 'var(--tail)'} />
        <Tile label="Stalled loops" value={stalled} color={stalled ? 'var(--head)' : 'var(--tail)'}
              sub={stalled ? data.stalled_services.join(', ') : 'all loops beating'} />
        <Tile label="Kafka lag (sum)" value={lagTotal} color={lagTotal > 100 ? 'var(--warn)' : 'var(--ink)'} />
      </TileGrid>
      <ErrorNote error={error} />
      {data.note && stalled > 0 && (
        <div className="mono" style={{ marginTop: 8, fontSize: 10, color: 'var(--ink-mute)' }}>{data.note}</div>
      )}

      <SectionTitle>Worker loops (Redis heartbeats)</SectionTitle>
      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <GridHeader grid={H_GRID} cols={['Service', 'Loop', 'Last beat']} />
        {data.heartbeats.map((h) => (
          <div key={h.service} style={{ display: 'grid', gridTemplateColumns: H_GRID, gap: 10, padding: '8px 12px', fontSize: 11.5, alignItems: 'center', borderBottom: '1px solid var(--border)' }}>
            <span className="mono">{h.service}</span>
            <span><StateChip ok={h.alive} label={h.alive ? 'alive' : 'stalled'} /></span>
            <span className="mono num" style={{ color: 'var(--ink-mute)', fontSize: 10.5 }}>
              {h.age_seconds != null ? `${h.age_seconds}s ago` : '—'}
            </span>
          </div>
        ))}
      </div>

      <SectionTitle>Containers (docker)</SectionTitle>
      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <GridHeader grid={C_GRID} cols={['Name', 'State', 'Status', 'Health']} />
        {data.containers.map((c) => (
          <div key={c.name} style={{ display: 'grid', gridTemplateColumns: C_GRID, gap: 10, padding: '8px 12px', fontSize: 11.5, alignItems: 'center', borderBottom: '1px solid var(--border)' }}>
            <span className="mono" style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{c.name}</span>
            <span><StateChip ok={c.state === 'running'} warn={c.state === 'restarting'} label={c.state} /></span>
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--ink-mute)', overflow: 'hidden', textOverflow: 'ellipsis' }}>{c.status}</span>
            <span>
              {c.health
                ? <StateChip ok={c.health === 'healthy'} label={c.health} />
                : <Hint>no check</Hint>}
            </span>
          </div>
        ))}
        {data.containers.length === 0 && (
          <div style={{ padding: 12 }}>
            <Hint warn>docker-proxy unreachable — is the docker-proxy service up?</Hint>
          </div>
        )}
      </div>

      <SectionTitle>Kafka consumer lag (redpanda public_metrics)</SectionTitle>
      <div className="card" style={{ padding: '10px 14px' }}>
        {Object.keys(data.kafka_lag).length === 0 ? (
          <Hint>no group-lag series exposed</Hint>
        ) : (
          Object.entries(data.kafka_lag).map(([g, lag]) => (
            <div key={g} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11.5, padding: '3px 0' }}>
              <span className="mono">{g}</span>
              <span className="num" style={{ color: lag > 100 ? 'var(--warn)' : 'var(--ink-2)' }}>{lag}</span>
            </div>
          ))
        )}
      </div>

      <div className="mono" style={{ marginTop: 10, fontSize: 9.5, color: 'var(--ink-faint)' }}>
        refreshed {relTime(data.generated_at)} · polls every {POLL_MS / 1000}s while visible
      </div>
    </div>
  );
}
