import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { NavSidebar } from '@/components/NavSidebar';
import { api } from '@/lib/api';

interface Citation {
  ref: string;
  tool: string;
  kind: 'transcript' | 'guidance' | 'impact' | 'database' | 'web';
  label: string;
  symbol?: string | null;
  hits?: { transcript_id: number; symbol: string; announced_at: string; char_start: number }[];
  sources?: string[];
}

interface ChatTurn {
  role: 'user' | 'assistant';
  content: string;
  citations?: Citation[];
  meta?: string;
  error?: boolean;
}

const KIND_COLOR: Record<string, string> = {
  transcript: 'var(--accent, #3fa192)',
  guidance: '#a08cc0',
  impact: '#7f9cc6',
  database: 'var(--tail, #5fa98a)',
  web: 'var(--warn, #c2a05e)',
};

/** Render answer text, turning [T1] markers into small citation superscripts. */
function AnswerText({ text, citations }: { text: string; citations: Citation[] }) {
  const byRef = new Map(citations.map((c) => [c.ref, c]));
  const parts = text.split(/(\[T\d+\])/g);
  return (
    <div style={{ fontSize: 13, lineHeight: 1.68, color: 'var(--ink-2)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
      {parts.map((p, i) => {
        const m = p.match(/^\[(T\d+)\]$/);
        if (!m) return <span key={i}>{p}</span>;
        const c = byRef.get(m[1]);
        if (!c) return null;
        return (
          <sup key={i} className="mono" title={c.label} style={{ fontSize: 9, color: KIND_COLOR[c.kind] ?? 'var(--accent)', margin: '0 1px' }}>
            [{m[1].slice(1)}]
          </sup>
        );
      })}
    </div>
  );
}

function CitationChips({ citations }: { citations: Citation[] }) {
  if (!citations.length) return null;
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
      {citations.map((c) => {
        const color = KIND_COLOR[c.kind] ?? 'var(--accent)';
        const firstHit = c.hits?.[0];
        const chip = (
          <span
            className="mono"
            style={{
              fontSize: 10, padding: '3px 8px', borderRadius: 4,
              border: `1px solid ${color}`, color,
              background: 'transparent', cursor: firstHit ? 'pointer' : 'default',
            }}
          >
            {c.ref.slice(1)} · {c.label}
            {firstHit ? ' ↗' : ''}
          </span>
        );
        return firstHit ? (
          <Link key={c.ref} to={`/transcripts?open=${firstHit.transcript_id}&at=${firstHit.char_start}`} style={{ textDecoration: 'none' }}>
            {chip}
          </Link>
        ) : (
          <span key={c.ref}>{chip}</span>
        );
      })}
    </div>
  );
}

export function Research() {
  const [thread, setThread] = useState<ChatTurn[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [thread, busy]);

  async function send() {
    const q = input.trim();
    if (!q || busy) return;
    const next: ChatTurn[] = [...thread, { role: 'user', content: q }];
    setThread(next);
    setInput('');
    setBusy(true);
    try {
      const res = await api.askResearchChat(
        next.map((t) => ({ role: t.role, content: t.content })),
      );
      setThread((cur) => [
        ...cur,
        {
          role: 'assistant',
          content: res.answer || '(no answer)',
          citations: res.citations,
          meta: `${res.turns} tool turn${res.turns === 1 ? '' : 's'} · ${(res.latency_ms / 1000).toFixed(1)}s · ${res.model}`,
        },
      ]);
    } catch {
      setThread((cur) => [
        ...cur,
        { role: 'assistant', content: 'Chat unavailable — the engine errored. The attempt is audit-logged.', error: true },
      ]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ height: '100vh', display: 'grid', gridTemplateColumns: 'var(--sidebar-w, 184px) 1fr', overflow: 'hidden' }}>
      <NavSidebar staticStatus="RESEARCH · INTERNAL">
        <div style={{ padding: '12px 4px' }}>
          <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)', marginBottom: 8 }}>
            Research chat
          </div>
          <div style={{ fontSize: 11.5, lineHeight: 1.55, color: 'var(--ink-mute)' }}>
            Answers come only from Taperead's own data — transcripts, financials,
            prices, guidance, analysed events — every claim cited, every exchange
            audit-logged. If it's not in the data, it says so.
          </div>
          <div className="mono upper" style={{ marginTop: 14, fontSize: 9, letterSpacing: '0.12em', color: 'var(--warn, #c2a05e)' }}>
            Internal dogfood — not customer-facing
          </div>
          {thread.length > 0 && (
            <button
              onClick={() => setThread([])}
              className="mono"
              style={{ marginTop: 14, fontSize: 10, padding: '4px 10px', background: 'transparent', border: '1px solid var(--border)', borderRadius: 4, color: 'var(--ink-dim)', cursor: 'pointer' }}
            >
              New conversation
            </button>
          )}
        </div>
      </NavSidebar>

      <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
        <div style={{ padding: '13px 24px', borderBottom: '1px solid var(--border)' }}>
          <span className="mono upper" style={{ fontSize: 13, letterSpacing: '0.18em', color: 'var(--ink-2)' }}>Research</span>
          <span className="mono upper" style={{ marginLeft: 12, fontSize: 10, letterSpacing: '0.1em', color: 'var(--ink-mute)' }}>
            ask the corpus · citations attached
          </span>
        </div>

        <div ref={scrollRef} style={{ flex: 1, overflow: 'auto', minHeight: 0, padding: '18px 24px' }}>
          <div style={{ maxWidth: 780, margin: '0 auto' }}>
            {thread.length === 0 && (
              <div style={{ marginTop: 60, textAlign: 'center' }}>
                <div className="mono" style={{ fontSize: 12, color: 'var(--ink-mute)' }}>
                  Ask about any covered company.
                </div>
                <div className="mono" style={{ marginTop: 10, fontSize: 11, color: 'var(--ink-dim)', lineHeight: 2 }}>
                  "why did BAJAJCON fall after its results?"<br />
                  "what capex guidance did Hi-Tech Pipes give for FY27?"<br />
                  "what did Dr Reddy's say about the semaglutide supply halt?"
                </div>
              </div>
            )}
            {thread.map((t, i) => (
              <div key={i} style={{ marginBottom: 18 }}>
                {t.role === 'user' ? (
                  <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                    <div style={{ maxWidth: '80%', padding: '8px 12px', background: 'var(--accent-dim)', border: '1px solid var(--accent-line, var(--border))', borderRadius: 6, fontSize: 13, color: 'var(--ink-2)' }}>
                      {t.content}
                    </div>
                  </div>
                ) : (
                  <div style={{ borderLeft: `2px solid ${t.error ? 'var(--head, #c68476)' : 'var(--border)'}`, paddingLeft: 14 }}>
                    <AnswerText text={t.content} citations={t.citations ?? []} />
                    <CitationChips citations={t.citations ?? []} />
                    {t.meta && (
                      <div className="mono" style={{ marginTop: 8, fontSize: 9.5, color: 'var(--ink-faint, var(--ink-mute))' }}>
                        {t.meta}
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
            {busy && (
              <div className="mono" style={{ fontSize: 11, color: 'var(--ink-mute)' }}>
                Consulting the corpus…
              </div>
            )}
          </div>
        </div>

        <div style={{ padding: '12px 24px', borderTop: '1px solid var(--border)' }}>
          <div style={{ maxWidth: 780, margin: '0 auto', display: 'flex', gap: 8 }}>
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && send()}
              placeholder={busy ? 'Working…' : 'Ask about a company, a call, a number…'}
              disabled={busy}
              className="mono"
              style={{
                flex: 1, fontSize: 12.5, padding: '9px 12px', background: 'var(--bg-elev)',
                border: '1px solid var(--border)', borderRadius: 6, color: 'var(--ink-2)', outline: 'none',
              }}
            />
            <button
              onClick={send}
              disabled={busy || input.trim().length === 0}
              className="mono upper"
              style={{
                fontSize: 10, letterSpacing: '0.1em', padding: '0 16px',
                background: 'var(--accent-dim)', border: '1px solid var(--accent-line, var(--accent))',
                borderRadius: 6, color: 'var(--accent)', cursor: busy ? 'default' : 'pointer',
              }}
            >
              Ask
            </button>
          </div>
          <div className="mono" style={{ maxWidth: 780, margin: '6px auto 0', fontSize: 9, color: 'var(--ink-faint, var(--ink-mute))' }}>
            Information and factual analysis only — no recommendations or views. Every exchange is audit-logged.
          </div>
        </div>
      </div>
    </div>
  );
}
