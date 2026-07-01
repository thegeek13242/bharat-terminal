import { useEffect, useState, type ReactNode } from 'react';
import { api } from '@/lib/api';
import { NavSidebar } from '@/components/NavSidebar';
import { num, dash, DASH, clockTime } from '@/lib/format';

// Trade-signal direction → colour (long = tailwind/green, short = headwind/red).
const DIR_VAR: Record<string, string> = { long: 'var(--tail)', short: 'var(--head)' };
const DIR_GLYPH: Record<string, string> = { long: '▲', short: '▼' };
const CONV_VAR: Record<string, string> = {
  high: 'var(--accent)', medium: 'var(--ink-2)', low: 'var(--ink-mute)',
};

const GRID = '120px minmax(150px,1.5fr) 92px 78px 96px 60px 64px minmax(120px,1fr)';

interface ReportDate {
  trading_day: string; n_signals: number; n_long: number; n_short: number; n_high_conviction: number;
}

function Stat({ label, value, color }: { label: string; value: ReactNode; color?: string }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <span className="mono upper" style={{ fontSize: 8, letterSpacing: '0.12em', color: 'var(--ink-dim)' }}>{label}</span>
      <span className="num" style={{ fontSize: 15, color: color || 'var(--ink)' }}>{value}</span>
    </div>
  );
}

function SignalRow({ s }: { s: any }) {
  const [open, setOpen] = useState(false);
  const dirVar = DIR_VAR[s.direction] || 'var(--ink-dim)';
  return (
    <>
      <div
        onClick={() => setOpen((o) => !o)}
        style={{ display: 'grid', gridTemplateColumns: GRID, gap: 12, padding: '11px 24px', borderBottom: '1px solid var(--border)', cursor: 'pointer', alignItems: 'center' }}
        onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-elev-2)')}
        onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
      >
        <span className="mono" style={{ fontSize: 12, fontWeight: 600, color: 'var(--ink)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.symbol}</span>
        <span style={{ fontSize: 12, color: 'var(--ink-2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{dash(s.company_name)}</span>
        <span className="mono upper" style={{ fontSize: 10.5, fontWeight: 600, color: dirVar, letterSpacing: '0.04em' }}>
          {DIR_GLYPH[s.direction] || ''} {dash(s.direction)}
        </span>
        <span className="mono upper" style={{ fontSize: 9.5, letterSpacing: '0.05em', color: CONV_VAR[s.conviction] || 'var(--ink-dim)' }}>{dash(s.conviction)}</span>
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--ink-dim)' }}>{dash(s.instrument_type)}</span>
        <span className="num" style={{ fontSize: 11, color: 'var(--ink-2)', textAlign: 'right' }}>{s.position_size_pct != null ? `${num(s.position_size_pct, 1)}%` : DASH}</span>
        <span className="num" style={{ fontSize: 11, color: 'var(--ink-dim)', textAlign: 'center' }}>{s.impact_magnitude != null ? `${s.impact_magnitude}/5` : DASH}</span>
        <span className="mono" style={{ fontSize: 10, color: 'var(--ink-dim)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{dash(s.news_source)}</span>
      </div>
      {open && (
        <div style={{ padding: '14px 24px 18px 24px', borderBottom: '1px solid var(--border)', background: 'var(--bg-elev)', display: 'grid', gap: 12 }}>
          <Field label="Signal thesis">{dash(s.reasoning)}</Field>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            <Field label="Stop-loss rationale">{dash(s.stop_loss_rationale)}</Field>
            <Field label="Strike rationale">{dash(s.suggested_strike_rationale)}</Field>
          </div>
          <Field label={`Impact analysis · ${dash(s.impact_sentiment)} · magnitude ${s.impact_magnitude ?? '–'}/5 · ${dash(s.time_horizon)} · implied move ${s.implied_move_pct != null ? `${s.implied_move_pct}%` : '–'}`}>
            {dash(s.impact_explanation)}
          </Field>
          {Array.isArray(s.affected_line_items) && s.affected_line_items.length > 0 && (
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
              <span className="mono upper" style={{ fontSize: 8, letterSpacing: '0.12em', color: 'var(--ink-dim)' }}>Affected lines</span>
              {s.affected_line_items.map((li: string) => (
                <span key={li} className="mono" style={{ fontSize: 9.5, padding: '2px 7px', borderRadius: 3, background: 'var(--bg-elev-2)', border: '1px solid var(--border-hi)', color: 'var(--ink-dim)' }}>{li}</span>
              ))}
            </div>
          )}
          <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap', alignItems: 'baseline' }}>
            <span className="mono" style={{ fontSize: 10, color: 'var(--ink-dim)' }}>
              Sector: <span style={{ color: 'var(--ink-2)' }}>{dash(s.sector)}</span>
            </span>
            <span className="mono" style={{ fontSize: 10, color: 'var(--ink-dim)' }}>
              Theme: <span style={{ color: 'var(--ink-2)' }}>{dash(s.macro_theme)}</span>
            </span>
            <span className="mono" style={{ fontSize: 10, color: 'var(--ink-dim)' }}>
              Generated: <span style={{ color: 'var(--ink-2)' }}>{clockTime(s.generated_at)}</span>
            </span>
          </div>
          <Field label={`Source · ${dash(s.news_source)}`}>
            {s.news_url ? (
              <a href={s.news_url} target="_blank" rel="noreferrer" style={{ color: 'var(--accent)', textDecoration: 'none' }}>
                {dash(s.news_headline)} ↗
              </a>
            ) : dash(s.news_headline)}
          </Field>
        </div>
      )}
    </>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <span className="mono upper" style={{ fontSize: 8, letterSpacing: '0.12em', color: 'var(--ink-dim)' }}>{label}</span>
      <span style={{ fontSize: 12, color: 'var(--ink-2)', lineHeight: 1.55 }}>{children}</span>
    </div>
  );
}

export function Signals() {
  const [dates, setDates] = useState<ReportDate[]>([]);
  const [selected, setSelected] = useState<string | null>(null); // null = latest (live)
  const [report, setReport] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  // Load the report whenever the selected day changes.
  useEffect(() => {
    setLoading(true);
    setErr(null);
    api.getSignalReport(selected || undefined)
      .then((r) => setReport(r))
      .catch((e) => setErr(e?.detail || 'Failed to load report'))
      .finally(() => setLoading(false));
  }, [selected]);

  // Load the list of available trading-day reports for the sidebar picker.
  useEffect(() => {
    api.getSignalReportDates(90).then(({ dates }) => setDates(dates)).catch(() => setDates([]));
  }, [report?.trading_day]);

  const signals: any[] = report?.signals || [];

  return (
    <div style={{ height: '100vh', display: 'grid', gridTemplateColumns: 'var(--sidebar-w, 184px) 1fr', overflow: 'hidden' }}>
      <NavSidebar connected statusLabel="CONNECTED · NSE">
        <div style={{ padding: '12px 4px' }}>
          <div className="mono upper" style={{ fontSize: 8.5, letterSpacing: '0.16em', color: 'var(--ink-dim)', marginBottom: 10 }}>
            Trading day
          </div>
          <button
            onClick={() => setSelected(null)}
            className="mono"
            style={{
              width: '100%', textAlign: 'left', fontSize: 10.5, padding: '6px 8px', marginBottom: 6, borderRadius: 3, cursor: 'pointer',
              color: selected === null ? 'var(--accent)' : 'var(--ink-2)',
              background: selected === null ? 'var(--accent-dim)' : 'transparent',
              border: `1px solid ${selected === null ? 'var(--accent-line)' : 'var(--border-hi)'}`,
            }}
          >
            ● Latest · live
          </button>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2, maxHeight: 'calc(100vh - 200px)', overflow: 'auto' }}>
            {dates.map((d) => {
              const active = selected === d.trading_day;
              return (
                <button
                  key={d.trading_day}
                  onClick={() => setSelected(d.trading_day)}
                  className="mono"
                  style={{
                    display: 'flex', justifyContent: 'space-between', alignItems: 'center', width: '100%',
                    fontSize: 10.5, padding: '5px 8px', borderRadius: 3, cursor: 'pointer',
                    color: active ? 'var(--accent)' : 'var(--ink-dim)',
                    background: active ? 'var(--accent-dim)' : 'transparent',
                    border: `1px solid ${active ? 'var(--accent-line)' : 'transparent'}`,
                  }}
                >
                  <span>{d.trading_day}</span>
                  <span style={{ color: 'var(--ink-mute)' }}>{d.n_signals}</span>
                </button>
              );
            })}
          </div>
        </div>
      </NavSidebar>

      <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
        {/* Summary bar */}
        <div style={{ padding: '16px 24px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 28, flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            <span className="mono upper" style={{ fontSize: 8.5, letterSpacing: '0.14em', color: 'var(--ink-dim)' }}>Actionable signals for</span>
            <span className="mono" style={{ fontSize: 18, fontWeight: 600, color: 'var(--ink)' }}>{report?.trading_day || '…'}</span>
            {report && (
              <span className="mono" style={{ fontSize: 9.5, color: 'var(--ink-mute)' }}>
                generated after {report.prev_trading_day} · 15:30 IST cutoff
              </span>
            )}
          </div>
          {report && (
            <>
              <Stat label="Signals" value={report.n_signals} />
              <Stat label="Long" value={report.n_long} color="var(--tail)" />
              <Stat label="Short" value={report.n_short} color="var(--head)" />
              <Stat label="High conv." value={report.n_high_conviction} color="var(--accent)" />
              <Stat label="Symbols" value={report.distinct_symbols} />
              <Stat label="Avg conf." value={report.avg_relevance_confidence != null ? `${(report.avg_relevance_confidence * 100).toFixed(0)}%` : DASH} />
            </>
          )}
        </div>

        {/* Table */}
        <div style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
          <div style={{ minWidth: 880 }}>
            <div
              className="mono upper"
              style={{ display: 'grid', gridTemplateColumns: GRID, gap: 12, padding: '10px 24px', position: 'sticky', top: 0, background: 'var(--bg-elev)', borderBottom: '1px solid var(--border)', fontSize: 8.5, letterSpacing: '0.1em', color: 'var(--ink-dim)', zIndex: 1 }}
            >
              <span>Symbol</span><span>Company</span><span>Direction</span><span>Conviction</span>
              <span>Instrument</span><span style={{ textAlign: 'right' }}>Size</span>
              <span style={{ textAlign: 'center' }}>Mag</span><span>Source</span>
            </div>

            {loading ? (
              <div style={{ padding: '60px 24px', textAlign: 'center', color: 'var(--ink-mute)', fontSize: 13 }}>Loading report…</div>
            ) : err ? (
              <div style={{ padding: '60px 24px', textAlign: 'center', color: 'var(--head)', fontSize: 13 }}>{err}</div>
            ) : signals.length === 0 ? (
              <div style={{ padding: '60px 24px', textAlign: 'center', color: 'var(--ink-mute)' }}>
                <div className="mono" style={{ fontSize: 13, color: 'var(--ink-dim)', marginBottom: 8 }}>No actionable signals for this trading day</div>
                <div style={{ fontSize: 12 }}>Signals generated after 15:30 IST roll into the next session.</div>
              </div>
            ) : (
              signals.map((s) => <SignalRow key={s.signal_id} s={s} />)
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
