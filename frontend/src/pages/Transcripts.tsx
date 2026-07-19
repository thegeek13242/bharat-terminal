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

type SpeakerRole = 'management' | 'analyst' | 'moderator' | 'media';

interface Speaker {
  name: string;
  role: SpeakerRole;
  title: string | null;
  firm: string | null;
}

interface Segment {
  speaker: string;
  role: SpeakerRole;
  start: number;
  end: number;
}

interface Highlight {
  category: string;
  start: number;
  end: number;
}

interface Structure {
  pages: { n: number; start: number; end: number }[];
  speakers: Speaker[];
  segments: Segment[];
  qa_start: number | null;
  highlights: Highlight[];
  stats: Record<string, number>;
}

interface TranscriptFull extends TranscriptMeta {
  full_text: string | null;
  structure: Structure | null;
  structure_status: string;
}

// Muted category tints — annotation, not decoration.
const CATEGORY_COLOR: Record<string, string> = {
  guidance: 'var(--accent, #3fa192)',
  outlook: '#7f9cc6',
  financials: 'var(--tail, #5fa98a)',
  one_off: 'var(--head, #c68476)',
  risk: 'var(--head, #c68476)',
  strategy: '#a08cc0',
  capex: 'var(--warn, #c2a05e)',
  demand: '#7f9cc6',
};

const ROLE_STYLE: Record<SpeakerRole, { label: string; color: string }> = {
  management: { label: 'MGMT', color: 'var(--accent, #3fa192)' },
  analyst: { label: 'ANALYST', color: 'var(--warn, #c2a05e)' },
  moderator: { label: 'MODERATOR', color: 'var(--ink-mute)' },
  media: { label: 'MEDIA', color: '#7f9cc6' },
};

function RoleChip({ role }: { role: Segment['role'] }) {
  const s = ROLE_STYLE[role] ?? ROLE_STYLE.analyst;
  return (
    <span
      className="mono upper"
      style={{
        fontSize: 8.5,
        letterSpacing: '0.1em',
        padding: '1px 5px',
        borderRadius: 3,
        color: s.color,
        border: `1px solid ${s.color}`,
        opacity: role === 'moderator' ? 0.6 : 0.9,
      }}
    >
      {s.label}
    </span>
  );
}

/** Structured reader: verbatim text sliced by segment/highlight offsets.
 *  The text itself is NEVER altered — structure only decides where marks,
 *  speaker headers, page chips and section dividers sit around it. */
function StructuredReader({ doc }: { doc: TranscriptFull }) {
  const text = doc.full_text!;
  const s = doc.structure!;
  const pageOf = (off: number) => s.pages.find((p) => off >= p.start && off < p.end)?.n;

  let prevPage: number | undefined;
  let qaShown = false;
  const indexed = s.highlights.map((h, idx) => ({ ...h, idx }));

  return (
    <>
      {s.segments.map((seg, i) => {
        const page = pageOf(seg.start);
        const showPage = page !== undefined && page !== prevPage;
        prevPage = page ?? prevPage;
        const showQa = !qaShown && s.qa_start != null && seg.start >= s.qa_start;
        if (showQa) qaShown = true;

        const hls = indexed.filter((h) => h.start >= seg.start && h.start < seg.end);
        const parts: { t: string; h?: (typeof indexed)[number] }[] = [];
        let pos = seg.start;
        for (const h of hls) {
          if (h.start > pos) parts.push({ t: text.slice(pos, h.start) });
          const end = Math.min(h.end, seg.end);
          parts.push({ t: text.slice(h.start, end), h });
          pos = end;
        }
        if (pos < seg.end) parts.push({ t: text.slice(pos, seg.end) });

        return (
          <div key={i}>
            {showQa && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, margin: '26px 0 14px' }}>
                <span className="mono upper" style={{ fontSize: 10, letterSpacing: '0.18em', color: 'var(--ink-dim)' }}>
                  Question &amp; Answer session
                </span>
                <span style={{ flex: 1, borderTop: '1px solid var(--border)' }} />
              </div>
            )}
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 18 }}>
              <span className="mono" style={{ fontSize: 12, fontWeight: 600, color: 'var(--ink-2)' }}>
                {seg.speaker}
              </span>
              <RoleChip role={seg.role} />
              {showPage && (
                <span className="mono" style={{ marginLeft: 'auto', fontSize: 9.5, color: 'var(--ink-faint, var(--ink-mute))' }}>
                  p. {page}
                </span>
              )}
            </div>
            <div style={{ marginTop: 4, fontSize: 13.5, lineHeight: 1.72, color: 'var(--ink-2)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
              {parts.map((p, j) =>
                p.h ? (
                  <mark
                    key={j}
                    id={`hl-${p.h.idx}`}
                    title={p.h.category.replace('_', '-')}
                    style={{
                      background: 'transparent',
                      color: 'inherit',
                      boxShadow: `inset 0 -1px 0 0 ${CATEGORY_COLOR[p.h.category] ?? 'var(--accent)'}`,
                      backgroundColor: 'color-mix(in srgb, ' + (CATEGORY_COLOR[p.h.category] ?? '#3fa192') + ' 12%, transparent)',
                      borderRadius: 2,
                      padding: '0 1px',
                    }}
                  >
                    {p.t}
                  </mark>
                ) : (
                  <span key={j}>{p.t}</span>
                ),
              )}
            </div>
          </div>
        );
      })}
    </>
  );
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
      (t) => t.symbol.toUpperCase().includes(q) || (t.company_name ?? '').toUpperCase().includes(q),
    );
  }, [items, filter]);

  const structure = doc?.structure ?? null;
  const keyMoments = useMemo(() => {
    if (!doc?.full_text || !structure) return [];
    return structure.highlights.map((h, idx) => ({
      idx,
      category: h.category,
      excerpt: doc.full_text!.slice(h.start, h.end),
    }));
  }, [doc, structure]);

  const mgmt = structure?.speakers.filter((s) => s.role === 'management') ?? [];
  const analysts = structure?.speakers.filter((s) => s.role === 'analyst') ?? [];

  return (
    <div style={{ height: '100vh', display: 'grid', gridTemplateColumns: 'var(--sidebar-w, 184px) minmax(0,0.8fr) minmax(0,1.6fr)', overflow: 'hidden' }}>
      <NavSidebar counts={{ transcripts: items.length }} staticStatus="TRANSCRIPTS · FILINGS">
        <div style={{ padding: '12px 4px' }}>
          <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)', marginBottom: 8 }}>
            Earnings-call transcripts
          </div>
          <div style={{ fontSize: 11.5, lineHeight: 1.55, color: 'var(--ink-mute)' }}>
            Complete transcripts as filed — never clipped. Speaker roles and
            highlighted passages are annotations anchored verbatim to the text.
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
          {structure && (mgmt.length > 0 || analysts.length > 0) && (
            <div style={{ marginTop: 18 }}>
              <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)', marginBottom: 6 }}>
                On this call
              </div>
              {mgmt.map((sp) => (
                <div key={sp.name} style={{ padding: '3px 0', fontSize: 11 }}>
                  <span style={{ color: 'var(--accent)' }}>{sp.name}</span>
                  <span style={{ color: 'var(--ink-mute)' }}>{sp.title ? ` · ${sp.title}` : ''}</span>
                </div>
              ))}
              {analysts.length > 0 && (
                <div style={{ marginTop: 4, fontSize: 10.5, color: 'var(--ink-mute)', lineHeight: 1.6 }}>
                  {analysts.length} analyst{analysts.length > 1 ? 's' : ''}:{' '}
                  {analysts.map((a) => a.firm || a.name).filter(Boolean).slice(0, 6).join(', ')}
                  {analysts.length > 6 ? '…' : ''}
                </div>
              )}
            </div>
          )}
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
            <div className="mono" style={{ padding: 20, fontSize: 11, color: 'var(--ink-mute)' }}>No transcripts match.</div>
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
              <span className="mono" style={{ fontSize: 10, color: 'var(--ink-dim)' }}>
                {doc.page_count != null ? `${doc.page_count} pages` : ''}
              </span>
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

            {keyMoments.length > 0 && (
              <div style={{ padding: '10px 24px', borderBottom: '1px solid var(--border)', maxHeight: 148, overflow: 'auto' }}>
                <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)', marginBottom: 6 }}>
                  Key moments · {keyMoments.length}
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                  {keyMoments.map((m) => (
                    <button
                      key={m.idx}
                      onClick={() =>
                        document.getElementById(`hl-${m.idx}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
                      }
                      title={m.excerpt}
                      style={{
                        maxWidth: 340,
                        textAlign: 'left',
                        fontSize: 10.5,
                        lineHeight: 1.45,
                        padding: '4px 8px',
                        background: 'var(--bg-elev)',
                        border: '1px solid var(--border)',
                        borderLeft: `2px solid ${CATEGORY_COLOR[m.category] ?? 'var(--accent)'}`,
                        borderRadius: 4,
                        color: 'var(--ink-mute)',
                        cursor: 'pointer',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      <span className="mono upper" style={{ fontSize: 8.5, letterSpacing: '0.08em', color: CATEGORY_COLOR[m.category] ?? 'var(--accent)', marginRight: 6 }}>
                        {m.category.replace('_', '-')}
                      </span>
                      {m.excerpt}
                    </button>
                  ))}
                </div>
              </div>
            )}

            <div style={{ flex: 1, overflow: 'auto', minHeight: 0, padding: '8px 24px 20px' }}>
              <div style={{ maxWidth: 760 }}>
                {structure && structure.segments.length > 0 && doc.full_text ? (
                  <StructuredReader doc={doc} />
                ) : (
                  <>
                    {doc.structure_status !== 'ok' && (
                      <div className="mono" style={{ margin: '12px 0', fontSize: 10, color: 'var(--ink-dim)' }}>
                        Annotations {doc.structure_status === 'pending' ? 'pending' : 'unavailable'} — showing the raw transcript.
                      </div>
                    )}
                    <div style={{ fontSize: 13.5, lineHeight: 1.72, color: 'var(--ink-2)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                      {doc.full_text ?? 'Transcript text unavailable — open the source PDF.'}
                    </div>
                  </>
                )}
                <div className="mono" style={{ margin: '28px 0 8px', fontSize: 10, color: 'var(--ink-dim)' }}>
                  — End of transcript · {doc.char_count?.toLocaleString()} characters, as filed —
                </div>
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
