/** Admin action audit log (admin_audit_log — one row per admin write). */
import { useCallback, useEffect, useState } from 'react';
import { ApiError } from '@/lib/api';
import { relTime } from '@/lib/format';
import { apiFetch, ErrorNote, GridHeader, Hint, SectionTitle } from './shared';
import type { TabProps } from './AdminPage';

interface AuditRow {
  id: number; ts: string; actor_email: string; action: string;
  target: string | null; payload: Record<string, unknown> | null; success: boolean;
}

const GRID = '120px minmax(160px,1fr) 140px minmax(160px,1fr) 60px';

export function AuditTab({ onDenied }: TabProps) {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await apiFetch<{ entries: AuditRow[] }>('/admin/audit-log?limit=200');
      setRows(res.entries);
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) return onDenied();
      setError(String((e as Error).message));
    }
  }, [onDenied]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div>
      <SectionTitle first>Admin actions ({rows.length})</SectionTitle>
      <ErrorNote error={error} />
      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <GridHeader grid={GRID} cols={['When', 'Actor', 'Action', 'Target', 'OK']} />
        {rows.map((r) => (
          <div key={r.id}>
            <div
              className="row-hover"
              onClick={() => setExpanded(expanded === r.id ? null : r.id)}
              style={{ display: 'grid', gridTemplateColumns: GRID, gap: 10, padding: '8px 12px', fontSize: 11, alignItems: 'center', cursor: 'pointer', borderBottom: '1px solid var(--border)' }}
            >
              <span className="mono" style={{ color: 'var(--ink-mute)', fontSize: 10.5 }}>{relTime(r.ts)}</span>
              <span className="mono" style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.actor_email}</span>
              <span className="mono" style={{ color: 'var(--accent)' }}>{r.action}</span>
              <span className="mono" style={{ color: 'var(--ink-mute)', overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.target ?? '—'}</span>
              <span className="mono" style={{ color: r.success ? 'var(--tail)' : 'var(--head)' }}>{r.success ? 'yes' : 'no'}</span>
            </div>
            {expanded === r.id && r.payload && (
              <pre className="mono" style={{ margin: 0, padding: '10px 14px', fontSize: 10.5, whiteSpace: 'pre-wrap', color: 'var(--ink-2)', background: 'var(--bg-elev-2)', borderBottom: '1px solid var(--border)' }}>
                {JSON.stringify(r.payload, null, 2)}
              </pre>
            )}
          </div>
        ))}
        {rows.length === 0 && (
          <div style={{ padding: 12 }}><Hint>no admin actions yet</Hint></div>
        )}
      </div>
    </div>
  );
}
