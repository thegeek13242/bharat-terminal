/** Alert history (alert_events via guardrails → Redis → flusher). */
import { useCallback, useEffect, useState } from 'react';
import { ApiError } from '@/lib/api';
import { relTime } from '@/lib/format';
import { apiFetch, ErrorNote, FilterChip, GridHeader, Hint, SectionTitle, StateChip } from './shared';
import type { TabProps } from './AdminPage';

interface AlertRow {
  id: number; ts: string; service: string; kind: string;
  subject: string; body: string | null; delivered: boolean | null;
}

const GRID = '120px 140px minmax(140px,1fr) minmax(240px,2fr) 80px';

export function AlertsTab({ onDenied }: TabProps) {
  const [alerts, setAlerts] = useState<AlertRow[]>([]);
  const [byKind, setByKind] = useState<{ kind: string; n: number; latest: string }[]>([]);
  const [kind, setKind] = useState<string>('');
  const [expanded, setExpanded] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const qs = new URLSearchParams({ since_hours: '168' });
      if (kind) qs.set('kind', kind);
      const res = await apiFetch<{ alerts: AlertRow[]; by_kind: typeof byKind }>(`/admin/alerts?${qs}`);
      setAlerts(res.alerts);
      setByKind(res.by_kind);
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) return onDenied();
      setError(String((e as Error).message));
    }
  }, [kind, onDenied]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div>
      <SectionTitle first>Last 7 days by kind</SectionTitle>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        <FilterChip active={kind === ''} onClick={() => setKind('')}>all</FilterChip>
        {byKind.map((k) => (
          <FilterChip
            key={k.kind}
            active={kind === k.kind}
            onClick={() => setKind(kind === k.kind ? '' : k.kind)}
            title={`latest ${relTime(k.latest)}`}
          >
            {k.kind} <span className="num">{k.n}</span>
          </FilterChip>
        ))}
        {byKind.length === 0 && (
          <Hint>no alerts recorded — history accrues from the Phase-2 deploy onward</Hint>
        )}
      </div>

      <ErrorNote error={error} />

      <SectionTitle>Alerts ({alerts.length})</SectionTitle>
      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <GridHeader grid={GRID} cols={['When', 'Kind', 'Service', 'Subject', 'Sent']} />
        {alerts.map((a) => (
          <div key={a.id}>
            <div
              className="row-hover"
              onClick={() => setExpanded(expanded === a.id ? null : a.id)}
              style={{ display: 'grid', gridTemplateColumns: GRID, gap: 10, padding: '8px 12px', fontSize: 11, alignItems: 'center', cursor: 'pointer', borderBottom: '1px solid var(--border)' }}
            >
              <span className="mono" style={{ color: 'var(--ink-mute)', fontSize: 10.5 }}>{relTime(a.ts)}</span>
              <span className="mono" style={{ color: 'var(--warn)', fontSize: 10, overflow: 'hidden', textOverflow: 'ellipsis' }}>{a.kind}</span>
              <span className="mono" style={{ color: 'var(--ink-mute)', fontSize: 10.5, overflow: 'hidden', textOverflow: 'ellipsis' }}>{a.service}</span>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.subject}</span>
              <span>
                {a.delivered == null
                  ? <span className="mono" style={{ fontSize: 10, color: 'var(--ink-faint)' }}>?</span>
                  : <StateChip ok={a.delivered} label={a.delivered ? 'yes' : 'no'} />}
              </span>
            </div>
            {expanded === a.id && a.body && (
              <pre className="mono" style={{ margin: 0, padding: '10px 14px', fontSize: 10.5, whiteSpace: 'pre-wrap', color: 'var(--ink-2)', background: 'var(--bg-elev-2)', borderBottom: '1px solid var(--border)' }}>
                {a.body}
              </pre>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
