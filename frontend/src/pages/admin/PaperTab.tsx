/** Paper-ledger reset: dry-run first (counts), then type-RESET confirm.
 * Archive-first server-side — rows move to *_archive stamped with batch id,
 * reason, and actor before the live tables are cleared. */
import { useCallback, useEffect, useState } from 'react';
import { ApiError } from '@/lib/api';
import {
  apiFetch, Btn, ErrorNote, Hint, Input, SectionTitle, Tile, TileGrid,
} from './shared';
import type { TabProps } from './AdminPage';

interface DryRun {
  dry_run: true;
  counts: Record<string, number>;
  paper_trades_by_status: Record<string, number>;
  note: string;
}

interface ResetResult {
  dry_run: false;
  batch_id: string;
  archived: Record<string, number>;
  restart_hint: string;
}

export function PaperTab({ onDenied }: TabProps) {
  const [dry, setDry] = useState<DryRun | null>(null);
  const [reason, setReason] = useState('');
  const [confirmText, setConfirmText] = useState('');
  const [result, setResult] = useState<ResetResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadDryRun = useCallback(async () => {
    try {
      setDry(await apiFetch<DryRun>('/admin/paper/reset', {
        method: 'POST',
        body: JSON.stringify({ execute: false }),
      }));
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) return onDenied();
      setError(String((e as Error).message));
    }
  }, [onDenied]);

  useEffect(() => {
    void loadDryRun();
  }, [loadDryRun]);

  const armed = confirmText === 'RESET' && !!reason.trim();

  const execute = async () => {
    if (busy || !armed) return;
    if (!window.confirm('Archive and wipe the ENTIRE paper ledger? The pilot dataset moves to the archive tables.')) return;
    setBusy(true);
    try {
      setResult(await apiFetch<ResetResult>('/admin/paper/reset', {
        method: 'POST',
        body: JSON.stringify({ execute: true, reason: reason.trim() }),
      }));
      setConfirmText('');
      setReason('');
      await loadDryRun();
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) return onDenied();
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <SectionTitle first>Current ledger</SectionTitle>
      {dry ? (
        <TileGrid>
          <Tile label="paper_trades" value={dry.counts.paper_trades ?? 0}
                sub={Object.entries(dry.paper_trades_by_status).map(([s, n]) => `${s} ${n}`).join(' · ') || 'empty'} />
          <Tile label="equity snapshots" value={dry.counts.paper_equity_snapshots ?? 0} />
          <Tile label="signal marks" value={dry.counts.paper_signal_marks ?? 0} />
        </TileGrid>
      ) : (
        <Hint>{error ?? 'Loading…'}</Hint>
      )}
      <ErrorNote error={error} />

      <SectionTitle>Reset (archive-first)</SectionTitle>
      <div className="card" style={{ padding: '16px 18px', maxWidth: 560 }}>
        <div style={{ fontSize: 12.5, color: 'var(--ink-2)', lineHeight: 1.55 }}>
          Every row is copied to the <span className="mono">*_archive</span> tables with a batch id,
          your email, and the reason below — then the live tables are cleared. The engine holds no
          in-memory positions, so this is safe while the papertrade service runs; restart the
          container afterwards to clear its in-memory mark queue (cosmetic).
          <span style={{ color: 'var(--warn)' }}> This ends the current pre-registered pilot dataset.</span>
        </div>
        <div style={{ marginTop: 14, display: 'flex', flexDirection: 'column', gap: 8 }}>
          <Input
            value={reason}
            onChange={setReason}
            placeholder="Reason (required — lands in the archive stamp + audit log)"
          />
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <Input
              value={confirmText}
              onChange={setConfirmText}
              placeholder='Type "RESET" to arm'
              width={160}
            />
            <Btn variant="danger" disabled={busy || !armed} onClick={() => void execute()}>
              {busy ? 'Archiving…' : 'Archive + wipe ledger'}
            </Btn>
          </div>
        </div>
      </div>

      {result && (
        <>
          <SectionTitle>Last reset (this session)</SectionTitle>
          <div className="card" style={{ padding: '12px 16px', maxWidth: 560 }}>
            <div className="mono" style={{ fontSize: 11, color: 'var(--tail)' }}>batch {result.batch_id}</div>
            <div className="mono" style={{ fontSize: 11, marginTop: 6, color: 'var(--ink-2)' }}>
              {Object.entries(result.archived).map(([t, n]) => `${t}: ${n} archived`).join(' · ')}
            </div>
            <div className="mono" style={{ fontSize: 10.5, marginTop: 8, color: 'var(--warn)' }}>{result.restart_hint}</div>
          </div>
        </>
      )}
    </div>
  );
}
