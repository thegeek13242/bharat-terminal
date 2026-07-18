import { useEffect, useMemo, useState } from 'react';
import { NavSidebar } from '@/components/NavSidebar';
import { api } from '@/lib/api';
import { dayLabel, clockTime } from '@/lib/format';

interface TranscriptMeta {
  id: number;
  symbol: string;
  company_name: string | null;
  headline: string | null;
  announced_at: string | null;
  pdf_url: string;
  page_count: number | null;
  char_count: number | null;
  parse_status: string;
}

interface TranscriptFull extends TranscriptMeta {
  full_text: string | null;
}

export function Transcripts() {
  const [items, setItems] = useState<TranscriptMeta[]>([]);
  const [filter, setFilter] = useState('');
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [doc, setDoc] = useState<TranscriptFull | null>(null);
  const [loading, setLoading] = useState(true);
  const [docLoading, setDocLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .getRecentTranscripts(60, 200)
      .then((res) => {
        if (cancelled) return;
        setItems(res.items);
        setError(null);
      })
      .catch(() => !cancelled && setError('Could not load transcripts.'))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (selectedId == null) return;
    let cancelled = false;
    setDocLoading(true);
    api
      .getTranscript(selectedId)
      .then((res) => !cancelled && setDoc(res))
      .catch(() => !cancelled && setDoc(null))
      .finally(() => !cancelled && setDocLoading(false));
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const filtered = useMemo(() => {
    const q = filter.trim().toUpperCase();
    if (!q) return items;
    return items.filter(
      (t) =>
        t.symbol.toUpperCase().includes(q) ||
        (t.company_name ?? '').toUpperCase().includes(q),
    );
  }, [items, filter]);

  return (
    <div style={{ height: '100vh', display: 'grid', gridTemplateColumns: 'var(--sidebar-w, 184px) minmax(0,0.9fr) minmax(0,1.5fr)', overflow: 'hidden' }}>
      <NavSidebar counts={{ transcripts: items.length }} staticStatus="TRANSCRIPTS · FILINGS">
        <div style={{ padding: '12px 4px' }}>
          <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)', marginBottom: 8 }}>
            Earnings-call transcripts
          </div>
          <div style={{ fontSize: 11.5, lineHeight: 1.55, color: 'var(--ink-mute)' }}>
            Complete transcripts as filed with the exchange — the full document,
            never summarised or clipped. Source PDF linked on every record.
          </div>
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter symbol / company…"
            className="mono"
            style={{
              marginTop: 14,
              width: '100%',
              fontSize: 11,
              padding: '6px 8px',
              background: 'var(--bg-elev)',
              border: '1px solid var(--border)',
              borderRadius: 4,
              color: 'var(--ink-2)',
              outline: 'none',
            }}
          />
        </div>
      </NavSidebar>

      {/* ── List pane ─────────────────────────────────────────────── */}
      <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, borderRight: '1px solid var(--border)' }}>
        <div style={{ padding: '13px 16px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <span className="mono upper" style={{ fontSize: 13, letterSpacing: '0.18em', color: 'var(--ink-2)' }}>
            Transcripts
          </span>
          <span className="mono upper" style={{ fontSize: 10, letterSpacing: '0.1em', color: 'var(--ink-mute)' }}>
            {filtered.length} · last 60 days
          </span>
        </div>
        <div style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
          {loading && <div className="mono" style={{ padding: 20, fontSize: 11, color: 'var(--ink-mute)' }}>Loading…</div>}
          {!loading && error && <div className="mono" style={{ padding: 20, fontSize: 11, color: 'var(--head)' }}>{error}</div>}
          {!loading && !error && filtered.length === 0 && (
            <div className="mono" style={{ padding: 20, fontSize: 11, color: 'var(--ink-mute)' }}>
              No transcripts match.
            </div>
          )}
          {filtered.map((t) => {
            const active = t.id === selectedId;
            return (
              <div
                key={t.id}
                onClick={() => setSelectedId(t.id)}
                style={{
                  padding: '10px 16px',
                  borderBottom: '1px solid var(--border)',
                  borderLeft: `2px solid ${active ? 'var(--accent)' : 'transparent'}`,
                  background: active ? 'var(--accent-dim)' : 'transparent',
                  cursor: 'pointer',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                  <span className="mono" style={{ fontSize: 13, fontWeight: 600, color: active ? 'var(--accent)' : 'var(--ink-2)' }}>
                    {t.symbol}
                  </span>
                  <span style={{ fontSize: 11, color: 'var(--ink-mute)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {t.company_name ?? ''}
                  </span>
                </div>
                <div className="mono" style={{ marginTop: 3, fontSize: 10, color: 'var(--ink-dim)' }}>
                  {t.announced_at ? `${dayLabel(t.announced_at)} ${clockTime(t.announced_at)}` : '—'}
                  {t.page_count != null && ` · ${t.page_count}p`}
                  {t.char_count != null && ` · ${(t.char_count / 1000).toFixed(0)}k chars`}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* ── Reader pane ───────────────────────────────────────────── */}
      <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
        {doc && !docLoading ? (
          <>
            <div style={{ padding: '13px 24px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'baseline', gap: 12 }}>
              <span className="mono" style={{ fontSize: 13, fontWeight: 600, color: 'var(--ink-2)' }}>{doc.symbol}</span>
              <span style={{ fontSize: 12, color: 'var(--ink-mute)' }}>{doc.company_name}</span>
              <a
                href={doc.pdf_url}
                target="_blank"
                rel="noreferrer"
                className="mono upper"
                style={{ marginLeft: 'auto', fontSize: 10, letterSpacing: '0.1em', color: 'var(--accent)', textDecoration: 'none' }}
              >
                Source PDF ↗
              </a>
            </div>
            <div style={{ flex: 1, overflow: 'auto', minHeight: 0, padding: '20px 24px' }}>
              <div
                style={{
                  maxWidth: 760,
                  fontSize: 13.5,
                  lineHeight: 1.72,
                  color: 'var(--ink-2)',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {doc.full_text ?? 'Transcript text unavailable — open the source PDF.'}
              </div>
              <div className="mono" style={{ margin: '28px 0 8px', fontSize: 10, color: 'var(--ink-dim)' }}>
                — End of transcript · {doc.char_count?.toLocaleString()} characters, as filed —
              </div>
            </div>
          </>
        ) : (
          <div className="mono" style={{ margin: 'auto', fontSize: 11, color: 'var(--ink-mute)' }}>
            {docLoading ? 'Loading transcript…' : 'Select a transcript to read it in full.'}
          </div>
        )}
      </div>
    </div>
  );
}
