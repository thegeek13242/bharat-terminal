import { useEffect, useState } from 'react';
import { NavSidebar } from '@/components/NavSidebar';
import { api } from '@/lib/api';
import { clockTime, dayLabel } from '@/lib/format';

interface SurpriseRow {
  symbol: string;
  company_name: string;
  period_end: string;
  announced_at: string | null;
  sue_eps: number | null;
  sue_revenue: number | null;
  sue_composite: number | null;
  percentile: number | null;
  decile: number | null;
  liquidity_pass: boolean | null;
  study_version: string;
}

interface HistoricalContext {
  study_version: string;
  summary: string;
  disclaimer: string;
}

const fmt = (v: number | null, digits = 2) => (v == null ? '—' : v.toFixed(digits));

export function Surprises() {
  const [items, setItems] = useState<SurpriseRow[]>([]);
  const [context, setContext] = useState<HistoricalContext | null>(null);
  const [days, setDays] = useState(7);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .getRecentSurprises(days, 'tails')
      .then((res) => {
        if (cancelled) return;
        setItems(res.items);
        setContext(res.historical_context);
        setError(null);
      })
      .catch(() => !cancelled && setError('Could not load earnings surprises.'))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [days]);

  return (
    <div style={{ height: '100vh', display: 'grid', gridTemplateColumns: 'var(--sidebar-w, 184px) 1fr', overflow: 'hidden' }}>
      <NavSidebar counts={{ surprises: items.length }} staticStatus="SURPRISES · STATISTICS">
        <div style={{ padding: '12px 4px' }}>
          <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)', marginBottom: 8 }}>
            Earnings surprises
          </div>
          <div style={{ fontSize: 11.5, lineHeight: 1.55, color: 'var(--ink-mute)' }}>
            Results whose combined earnings-and-revenue surprise ranks in the top or
            bottom decile of the trailing year, computed against each company's own
            8-quarter history.
          </div>
          <div style={{ marginTop: 14, display: 'flex', gap: 4 }}>
            {[7, 14, 30, 90].map((d) => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className="mono"
                style={{
                  fontSize: 10,
                  padding: '3px 8px',
                  background: d === days ? 'var(--bg-elev)' : 'transparent',
                  border: '1px solid var(--border)',
                  borderRadius: 4,
                  color: d === days ? 'var(--ink-2)' : 'var(--ink-dim)',
                  cursor: 'pointer',
                }}
              >
                {d}d
              </button>
            ))}
          </div>
        </div>
      </NavSidebar>

      <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
        <div style={{ padding: '13px 20px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'baseline', gap: 12 }}>
          <span className="mono upper" style={{ fontSize: 13, letterSpacing: '0.18em', color: 'var(--ink-2)' }}>
            Earnings surprises
          </span>
          <span className="mono upper" style={{ fontSize: 10, letterSpacing: '0.1em', color: 'var(--ink-mute)' }}>
            {items.length} tail events · last {days} days · {context?.study_version ?? ''}
          </span>
        </div>

        <div
          className="mono upper"
          style={{
            display: 'grid',
            gridTemplateColumns: '110px 1fr 90px 80px 90px 90px 80px',
            gap: 12,
            padding: '7px 20px',
            background: 'var(--bg-elev)',
            borderBottom: '1px solid var(--border)',
            fontSize: 10,
            letterSpacing: '0.1em',
            color: 'var(--ink-dim)',
          }}
        >
          <span>Announced</span>
          <span>Company</span>
          <span>Quarter</span>
          <span style={{ textAlign: 'right' }}>Decile</span>
          <span style={{ textAlign: 'right' }}>SUE eps</span>
          <span style={{ textAlign: 'right' }}>SUE rev</span>
          <span style={{ textAlign: 'right' }}>Liquid</span>
        </div>

        <div style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
          {loading && (
            <div className="mono" style={{ padding: 24, fontSize: 11, color: 'var(--ink-mute)' }}>Loading…</div>
          )}
          {!loading && error && (
            <div className="mono" style={{ padding: 24, fontSize: 11, color: 'var(--head)' }}>{error}</div>
          )}
          {!loading && !error && items.length === 0 && (
            <div className="mono" style={{ padding: 24, fontSize: 11, color: 'var(--ink-mute)' }}>
              No tail-decile surprises in this window.
            </div>
          )}
          {items.map((r) => {
            const top = r.decile === 10;
            return (
              <div
                key={`${r.symbol}-${r.period_end}`}
                style={{
                  display: 'grid',
                  gridTemplateColumns: '110px 1fr 90px 80px 90px 90px 80px',
                  gap: 12,
                  padding: '9px 20px',
                  borderBottom: '1px solid var(--border)',
                  fontSize: 12,
                  alignItems: 'baseline',
                }}
              >
                <span className="mono" style={{ fontSize: 11, color: 'var(--ink-dim)' }}>
                  {r.announced_at ? `${dayLabel(r.announced_at)} ${clockTime(r.announced_at)}` : '—'}
                </span>
                <span>
                  <span className="mono" style={{ color: 'var(--ink-2)' }}>{r.symbol}</span>
                  <span style={{ color: 'var(--ink-mute)', marginLeft: 8, fontSize: 11 }}>
                    {r.company_name}
                  </span>
                </span>
                <span className="mono" style={{ fontSize: 11, color: 'var(--ink-dim)' }}>{r.period_end}</span>
                <span
                  className="mono upper"
                  style={{
                    textAlign: 'right',
                    fontSize: 10,
                    letterSpacing: '0.08em',
                    color: top ? 'var(--pos, #7fb069)' : 'var(--neg, #c68476)',
                  }}
                >
                  {top ? 'Top 10%' : 'Bottom 10%'}
                </span>
                <span className="mono" style={{ textAlign: 'right', color: 'var(--ink-2)' }}>{fmt(r.sue_eps)}</span>
                <span className="mono" style={{ textAlign: 'right', color: 'var(--ink-2)' }}>{fmt(r.sue_revenue)}</span>
                <span className="mono" style={{ textAlign: 'right', fontSize: 11, color: 'var(--ink-dim)' }}>
                  {r.liquidity_pass ? 'yes' : 'thin'}
                </span>
              </div>
            );
          })}
        </div>

        <div style={{ padding: '10px 20px', borderTop: '1px solid var(--border)' }}>
          {context && (
            <div style={{ fontSize: 11, lineHeight: 1.55, color: 'var(--ink-mute)' }}>
              {context.summary}
            </div>
          )}
          <div className="mono" style={{ marginTop: 6, fontSize: 10, lineHeight: 1.5, color: 'var(--ink-dim)' }}>
            {context?.disclaimer ??
              'Historical statistics from Taperead’s own backtest. Not investment advice or a recommendation.'}
          </div>
        </div>
      </div>
    </div>
  );
}
