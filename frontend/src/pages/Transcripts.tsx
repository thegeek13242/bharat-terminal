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

interface GuidanceItem {
  id: number;
  metric: string;
  period: string | null;
  value_low: number | null;
  value_high: number | null;
  unit: string | null;
  quote_start: number;
  quote_end: number;
  change_verdict: 'new' | 'raised' | 'lowered' | 'maintained' | 'changed';
  prior_period: string | null;
  prior_low: number | null;
  prior_high: number | null;
  prior_unit: string | null;
}

interface TranscriptFull extends TranscriptMeta {
  full_text: string | null;
  structure: Structure | null;
  structure_status: string;
  guidance: GuidanceItem[];
}

const VERDICT_STYLE: Record<GuidanceItem['change_verdict'], { label: string; color: string }> = {
  new: { label: 'NEW', color: 'var(--ink-dim)' },
  raised: { label: 'RAISED', color: 'var(--tail, #5fa98a)' },
  lowered: { label: 'LOWERED', color: 'var(--head, #c68476)' },
  maintained: { label: 'MAINTAINED', color: 'var(--ink-mute)' },
  changed: { label: 'CHANGED', color: 'var(--warn, #c2a05e)' },
};

function fmtValue(lo: number | null, hi: number | null, unit: string | null): string {
  if (lo == null) return '—';
  const range = hi != null && hi !== lo ? `${lo}–${hi}` : `${lo}`;
  return unit ? `${range} ${unit}` : range;
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
          <div key={i} id={`seg-${i}`}>
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

interface SearchResult {
  transcript_id: number;
  symbol: string;
  company_name: string | null;
  announced_at: string;
  char_start: number;
  char_end: number;
  section: string | null;
  relevance: number;
  excerpt: string;
}

export function Transcripts() {
  const [items, setItems] = useState<TranscriptMeta[]>([]);
  const [filter, setFilter] = useState('');
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [doc, setDoc] = useState<TranscriptFull | null>(null);
  const [loading, setLoading] = useState(true);
  const [docLoading, setDocLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searchQ, setSearchQ] = useState('');
  const [searchResults, setSearchResults] = useState<SearchResult[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [pendingScroll, setPendingScroll] = useState<number | null>(null);

  async function runSearch() {
    const q = searchQ.trim();
    if (q.length < 3) return;
    setSearching(true);
    try {
      const res = await api.searchTranscripts(q);
      setSearchResults(res.results);
    } catch {
      setSearchResults([]);
    } finally {
      setSearching(false);
    }
  }

  function openResult(r: SearchResult) {
    setPendingScroll(r.char_start);
    setSelectedId(r.transcript_id);
  }

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

  // After opening a search result: scroll to the segment containing the hit
  // once the document (and its structure) has rendered.
  useEffect(() => {
    if (pendingScroll == null || !doc || docLoading) return;
    const segs = doc.structure?.segments;
    if (segs && segs.length > 0) {
      const idx = segs.findIndex((s) => pendingScroll >= s.start && pendingScroll < s.end);
      if (idx >= 0) {
        setTimeout(() =>
          document.getElementById(`seg-${idx}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 120);
      }
    }
    setPendingScroll(null);
  }, [doc, docLoading, pendingScroll]);

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
            {searchResults !== null ? `${searchResults.length} matches` : `${filtered.length} · last 60 days`}
          </span>
        </div>
        <div style={{ padding: '8px 16px', borderBottom: '1px solid var(--border)', display: 'flex', gap: 6 }}>
          <input
            value={searchQ}
            onChange={(e) => setSearchQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') runSearch();
              if (e.key === 'Escape') { setSearchQ(''); setSearchResults(null); }
            }}
            placeholder="Search across all calls… (Enter)"
            className="mono"
            style={{
              flex: 1, fontSize: 11, padding: '6px 8px', background: 'var(--bg-elev)',
              border: '1px solid var(--border)', borderRadius: 4, color: 'var(--ink-2)', outline: 'none',
            }}
          />
          {searchResults !== null && (
            <button
              onClick={() => { setSearchQ(''); setSearchResults(null); }}
              className="mono"
              style={{ fontSize: 10, padding: '4px 8px', background: 'transparent', border: '1px solid var(--border)', borderRadius: 4, color: 'var(--ink-dim)', cursor: 'pointer' }}
            >
              ×
            </button>
          )}
        </div>
        <div style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
          {searching && <div className="mono" style={{ padding: 20, fontSize: 11, color: 'var(--ink-mute)' }}>Searching…</div>}
          {!searching && searchResults !== null && searchResults.length === 0 && (
            <div className="mono" style={{ padding: 20, fontSize: 11, color: 'var(--ink-mute)' }}>No matches in the corpus.</div>
          )}
          {!searching && searchResults !== null &&
            searchResults.map((r, i) => (
              <div
                key={`${r.transcript_id}-${i}`}
                onClick={() => openResult(r)}
                style={{
                  padding: '10px 16px', borderBottom: '1px solid var(--border)', cursor: 'pointer',
                  borderLeft: `2px solid ${r.transcript_id === selectedId ? 'var(--accent)' : 'transparent'}`,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                  <span className="mono" style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--ink-2)' }}>{r.symbol}</span>
                  <span className="mono" style={{ fontSize: 10, color: 'var(--ink-dim)' }}>
                    {r.announced_at.slice(0, 10)}{r.section ? ` · ${r.section === 'qa' ? 'Q&A' : 'remarks'}` : ''}
                  </span>
                  <span className="mono" style={{ marginLeft: 'auto', fontSize: 9.5, color: 'var(--ink-faint, var(--ink-mute))' }}>
                    {(r.relevance * 100).toFixed(0)}%
                  </span>
                </div>
                <div style={{ marginTop: 4, fontSize: 11, lineHeight: 1.5, color: 'var(--ink-mute)', display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
                  {r.excerpt}
                </div>
              </div>
            ))}
          {searchResults === null && (<>

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
          </>)}
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

            {(doc.guidance?.length ?? 0) > 0 && (
              <div style={{ padding: '10px 24px', borderBottom: '1px solid var(--border)', maxHeight: 170, overflow: 'auto' }}>
                <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)', marginBottom: 6 }}>
                  Management guidance · {doc.guidance.length}
                  <span style={{ marginLeft: 8, letterSpacing: '0.06em', textTransform: 'none', color: 'var(--ink-mute)' }}>
                    as stated on the call · change vs previous call
                  </span>
                </div>
                <table className="mono" style={{ width: '100%', fontSize: 11, borderCollapse: 'collapse' }}>
                  <tbody>
                    {doc.guidance.map((g) => {
                      const v = VERDICT_STYLE[g.change_verdict] ?? VERDICT_STYLE.new;
                      const segIdx = structure?.segments.findIndex(
                        (s) => g.quote_start >= s.start && g.quote_start < s.end,
                      );
                      const prior =
                        g.prior_low != null
                          ? `was ${fmtValue(g.prior_low, g.prior_high, g.prior_unit)}${g.prior_period ? ` (${g.prior_period})` : ''}`
                          : undefined;
                      return (
                        <tr
                          key={g.id}
                          onClick={() =>
                            segIdx != null && segIdx >= 0 &&
                            document.getElementById(`seg-${segIdx}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                          }
                          title={prior}
                          style={{ cursor: segIdx != null && segIdx >= 0 ? 'pointer' : 'default', borderTop: '1px solid var(--border)' }}
                        >
                          <td style={{ padding: '4px 8px 4px 0', color: 'var(--ink-2)' }}>{g.metric}</td>
                          <td style={{ padding: '4px 8px', color: 'var(--ink-dim)' }}>{g.period ?? '—'}</td>
                          <td style={{ padding: '4px 8px', color: 'var(--ink-2)', textAlign: 'right', whiteSpace: 'nowrap' }}>
                            {fmtValue(g.value_low, g.value_high, g.unit)}
                          </td>
                          <td style={{ padding: '4px 0 4px 8px', textAlign: 'right' }}>
                            <span
                              className="mono upper"
                              style={{
                                fontSize: 8.5,
                                letterSpacing: '0.1em',
                                padding: '1px 5px',
                                borderRadius: 3,
                                color: v.color,
                                border: `1px solid ${v.color}`,
                              }}
                            >
                              {v.label}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}

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
