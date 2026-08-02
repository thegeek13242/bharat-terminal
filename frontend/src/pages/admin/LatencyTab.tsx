/** Latency percentiles per module + LLM stages + sync-job durations. */
import { useCallback, useEffect, useState } from 'react';
import { ApiError } from '@/lib/api';
import { relTime } from '@/lib/format';
import { apiFetch, ErrorNote, GridHeader, Hint, SectionTitle, Segmented, Tile, TileGrid } from './shared';
import type { TabProps } from './AdminPage';

type Pct = { n: number; p50_ms: number; p90_ms: number; p99_ms: number } | { error: string } | null;

interface Latency {
  window: string;
  modules: Record<string, Pct>;
  llm_stages: ({ stage: string; mean_ms: number } & Record<string, unknown>)[] | { error: string };
  sync_jobs: { sync_type: string; source: string | null; runs: number; failed: number; mean_s: number | null; last_run: string }[] | { error: string };
}

const WINDOWS = ['1h', '24h', '7d'] as const;
const MODULE_LABELS: Record<string, string> = {
  pipeline_end_to_end: 'Pipeline e2e',
  analysis_audited: 'Analysis (audited)',
  ingestion: 'Ingestion',
  research_chat: 'Research chat',
  api_requests: 'API requests',
};
const S_GRID = 'minmax(160px,1.4fr) 110px 70px 70px 90px 110px';

function fmtMs(v: number | undefined): string {
  if (v == null) return '—';
  return v >= 10_000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`;
}

export function LatencyTab({ onDenied }: TabProps) {
  const [win, setWin] = useState<string>('24h');
  const [data, setData] = useState<Latency | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await apiFetch<Latency>(`/admin/latency?window=${win}`));
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) return onDenied();
      setError(String((e as Error).message));
    }
  }, [win, onDenied]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div>
      <Segmented value={win} onChange={setWin} options={WINDOWS} />
      <ErrorNote error={error} />

      {data && (
        <>
          <SectionTitle>Modules (p50 / p90 / p99)</SectionTitle>
          <TileGrid>
            {Object.entries(data.modules).map(([key, m]) => {
              if (!m) {
                return <Tile key={key} label={MODULE_LABELS[key] ?? key} value="—" sub="no data in window" />;
              }
              if ('error' in m) {
                return <Tile key={key} label={MODULE_LABELS[key] ?? key} value="—" sub={m.error} color="var(--head)" />;
              }
              return (
                <Tile
                  key={key}
                  label={MODULE_LABELS[key] ?? key}
                  value={fmtMs(m.p50_ms)}
                  sub={`p90 ${fmtMs(m.p90_ms)} · p99 ${fmtMs(m.p99_ms)} · n=${m.n}`}
                />
              );
            })}
          </TileGrid>

          <SectionTitle>LLM stages (llm_call_log)</SectionTitle>
          {Array.isArray(data.llm_stages) ? (
            data.llm_stages.length === 0 ? (
              <Hint>empty — populates as the pipeline processes filings</Hint>
            ) : (
              <TileGrid>
                {data.llm_stages.map((s) => (
                  <Tile
                    key={s.stage}
                    label={s.stage}
                    value={fmtMs(s.p50_ms as number | undefined)}
                    sub={`p90 ${fmtMs(s.p90_ms as number | undefined)} · mean ${fmtMs(s.mean_ms)} · n=${s.n as number}`}
                  />
                ))}
              </TileGrid>
            )
          ) : (
            <div className="mono" style={{ fontSize: 11, color: 'var(--head)' }}>{data.llm_stages.error}</div>
          )}

          <SectionTitle>Sync jobs (sync_log — partial coverage by design)</SectionTitle>
          {Array.isArray(data.sync_jobs) ? (
            <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
              <GridHeader grid={S_GRID} cols={['Job', 'Source', 'Runs', 'Failed', 'Mean dur', 'Last run']} />
              {data.sync_jobs.map((j, i) => (
                <div key={i} style={{ display: 'grid', gridTemplateColumns: S_GRID, gap: 10, padding: '8px 12px', fontSize: 11, alignItems: 'center', borderBottom: '1px solid var(--border)' }}>
                  <span className="mono" style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{j.sync_type}</span>
                  <span className="mono" style={{ color: 'var(--ink-mute)', fontSize: 10 }}>{j.source ?? '—'}</span>
                  <span className="num">{j.runs}</span>
                  <span className="num" style={{ color: j.failed ? 'var(--head)' : 'var(--ink-2)' }}>{j.failed}</span>
                  <span className="num">{j.mean_s != null ? `${j.mean_s}s` : '—'}</span>
                  <span className="mono" style={{ color: 'var(--ink-mute)', fontSize: 10 }}>{relTime(j.last_run)}</span>
                </div>
              ))}
              {data.sync_jobs.length === 0 && (
                <div className="mono" style={{ padding: 12, fontSize: 11, color: 'var(--ink-mute)' }}>no sync_log rows in window</div>
              )}
            </div>
          ) : (
            <div className="mono" style={{ fontSize: 11, color: 'var(--head)' }}>{data.sync_jobs.error}</div>
          )}
        </>
      )}
    </div>
  );
}
