import { ReactNode, useEffect, useMemo, useState } from 'react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { NavSidebar } from '@/components/NavSidebar';
import { ApiError } from '@/lib/api';
import { DASH, clockTime, dayLabel, num, pct } from '@/lib/format';

// ── Types (mirror migrations/versions/030_paper_trading.py + the summary API) ──

interface EquityPoint {
  ts: string;
  equity: number;
}

interface StrategyStats {
  strategy: string;
  n_trades?: number | null;
  n_open?: number | null;
  n_closed?: number | null;
  n_rejected?: number | null;
  realized_pnl?: number | null;
  unrealized_pnl?: number | null;
  total_pnl?: number | null;
  hit_rate?: number | null;
  avg_pnl?: number | null;
}

interface Summary {
  equity?: number | null;
  starting_capital?: number | null;
  total_pnl?: number | null;
  realized_pnl?: number | null;
  unrealized_pnl?: number | null;
  max_drawdown_pct?: number | null;
  sharpe?: number | null;
  hit_rate?: number | null;
  n_open?: number | null;
  gross_exposure?: number | null;
  equity_curve?: EquityPoint[];
  per_strategy?: StrategyStats[];
}

type TradeStatus = 'PENDING' | 'OPEN' | 'CLOSED' | 'REJECTED';

interface Position {
  id: number;
  created_at: string;
  strategy: string;
  impact_id: string | null;
  news_id: string | null;
  symbol: string;
  side: 'LONG' | 'SHORT';
  instrument: 'CASH' | 'FUT';
  qty: number;
  entry_price: number | null;
  entry_at: string | null;
  exit_price: number | null;
  exit_at: string | null;
  exit_reason: string | null;
  status: TradeStatus;
  mtm_price: number | null;
  mtm_at: string | null;
  pnl: number | null;
  provenance: Record<string, unknown> | null;
}

// ── Local fetch (same semantics as lib/api request(); endpoints are new and
//    internal-only, so they live here until the shapes settle) ────────────────

async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
  });
  if (!res.ok) {
    const text = await res.text();
    let detail = text;
    try {
      detail = JSON.parse(text).detail ?? text;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

// ── Indian-locale money formatting ────────────────────────────────────────────

function inr(v: number | null | undefined, digits = 0): string {
  if (v == null || Number.isNaN(v)) return DASH;
  const sign = v < 0 ? '-' : '';
  return `${sign}₹${Math.abs(v).toLocaleString('en-IN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function inrSigned(v: number | null | undefined, digits = 0): string {
  if (v == null || Number.isNaN(v)) return DASH;
  return v > 0 ? `+${inr(v, digits)}` : inr(v, digits);
}

/** Compact ₹ for the chart y-axis: ₹1.05Cr / ₹8.2L / ₹45K. */
function inrCompact(v: number): string {
  const sign = v < 0 ? '-' : '';
  const a = Math.abs(v);
  if (a >= 1e7) return `${sign}₹${(a / 1e7).toFixed(2)}Cr`;
  if (a >= 1e5) return `${sign}₹${(a / 1e5).toFixed(1)}L`;
  if (a >= 1e3) return `${sign}₹${(a / 1e3).toFixed(0)}K`;
  return `${sign}₹${a.toFixed(0)}`;
}

function signedPct(v: number | null | undefined, digits = 1): string {
  if (v == null || Number.isNaN(v)) return DASH;
  return `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`;
}

function pnlColor(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v) || v === 0) return 'var(--ink-mute)';
  return v > 0 ? 'var(--tail)' : 'var(--head)';
}

/** Tolerate hit-rate as a 0–1 fraction or an already-scaled percentage. */
function normPct(v: number | null | undefined): number | null {
  if (v == null || Number.isNaN(v)) return null;
  return v <= 1 ? v * 100 : v;
}

function pnlPct(p: Position): number | null {
  if (p.pnl == null || p.entry_price == null || !p.qty) return null;
  const notional = Math.abs(p.entry_price * p.qty);
  return notional ? (p.pnl / notional) * 100 : null;
}

// recharts renders SVG presentation attributes, where CSS var() does not
// resolve — so the chart uses the design-token hex values directly.
const CHART = {
  accent: '#3FA192', // --accent
  border: '#242B36', // --border
  inkMute: '#5E6775', // --ink-mute
};

// ── Small presentational pieces ───────────────────────────────────────────────

function Tile({ label, value, color, sub, size = 17 }: {
  label: string;
  value: ReactNode;
  color?: string;
  sub?: ReactNode;
  size?: number;
}) {
  return (
    <div className="card" style={{ padding: '11px 14px', minWidth: 0 }}>
      <div
        className="mono upper"
        style={{ fontSize: 9, letterSpacing: '0.12em', color: 'var(--ink-dim)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
      >
        {label}
      </div>
      <div className="num" style={{ fontSize: size, marginTop: 5, color: color ?? 'var(--ink)' }}>{value}</div>
      {sub != null && (
        <div className="mono" style={{ marginTop: 3, fontSize: 9.5, color: 'var(--ink-mute)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {sub}
        </div>
      )}
    </div>
  );
}

function SideChip({ side }: { side: Position['side'] }) {
  const color = side === 'LONG' ? 'var(--tail)' : 'var(--head)';
  return (
    <span
      className="mono upper"
      style={{ fontSize: 8.5, letterSpacing: '0.1em', padding: '1px 5px', borderRadius: 3, color, border: `1px solid ${color}` }}
    >
      {side}
    </span>
  );
}

function InstrumentBadge({ instrument }: { instrument: Position['instrument'] }) {
  const fut = instrument === 'FUT';
  return (
    <span
      className="mono upper"
      style={{
        fontSize: 8.5,
        letterSpacing: '0.1em',
        padding: '1px 5px',
        borderRadius: 3,
        color: fut ? 'var(--warn)' : 'var(--ink-dim)',
        border: `1px solid ${fut ? 'var(--warn)' : 'var(--border-hi)'}`,
      }}
    >
      {instrument}
    </span>
  );
}

function StrategyChip({ strategy }: { strategy: string }) {
  return (
    <span
      className="mono"
      style={{
        fontSize: 9.5,
        padding: '2px 7px',
        borderRadius: 3,
        color: 'var(--ink-2)',
        background: 'var(--bg-elev-2)',
        border: '1px solid var(--border-hi)',
        whiteSpace: 'nowrap',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        maxWidth: '100%',
        display: 'inline-block',
      }}
    >
      {strategy}
    </span>
  );
}

function EquityTip({ active, payload }: {
  active?: boolean;
  payload?: { payload: { t: number; equity: number } }[];
}) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="mono" style={{ background: 'var(--bg-elev-2)', border: '1px solid var(--border-hi)', borderRadius: 6, padding: '8px 10px', fontSize: 11 }}>
      <div style={{ fontSize: 9.5, color: 'var(--ink-mute)' }}>
        {new Date(p.t).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false })}
      </div>
      <div className="num" style={{ marginTop: 3, color: 'var(--ink)' }}>{inr(p.equity)}</div>
    </div>
  );
}

/** Provenance audit view: scalar highlights up top, the full JSON verbatim. */
function ProvenancePanel({ p }: { p: Position }) {
  const prov = p.provenance ?? {};
  const highlights: { k: string; v: string }[] = [];
  for (const k of ['magnitude', 'sentiment', 'materiality_p', 'latency_ms', 'latency_s']) {
    const v = (prov as Record<string, unknown>)[k];
    if (v != null && typeof v !== 'object') highlights.push({ k, v: String(v) });
  }
  return (
    <div style={{ padding: '10px 20px 16px', borderBottom: '1px solid var(--border)', background: 'var(--bg)' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, flexWrap: 'wrap', marginBottom: 8 }}>
        <span className="mono upper" style={{ fontSize: 9, letterSpacing: '0.14em', color: 'var(--ink-dim)' }}>
          Decision provenance
        </span>
        <span className="mono" style={{ fontSize: 9.5, color: 'var(--ink-mute)' }}>
          impact {p.impact_id ?? DASH} · news {p.news_id ?? DASH} · trade #{p.id}
        </span>
      </div>
      {highlights.length > 0 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 8 }}>
          {highlights.map(({ k, v }) => (
            <span key={k} className="mono" style={{ fontSize: 9.5, padding: '2px 7px', borderRadius: 3, border: '1px solid var(--border-hi)', color: 'var(--ink-dim)' }}>
              {k}: <span style={{ color: 'var(--ink-2)' }}>{v}</span>
            </span>
          ))}
        </div>
      )}
      <pre
        className="mono"
        style={{
          margin: 0,
          padding: 12,
          fontSize: 10.5,
          lineHeight: 1.6,
          color: 'var(--ink-2)',
          background: 'var(--bg-elev)',
          border: '1px solid var(--border)',
          borderRadius: 6,
          maxHeight: 320,
          overflow: 'auto',
        }}
      >
        {JSON.stringify(prov, null, 2)}
      </pre>
    </div>
  );
}

function StatRow({ k, v, color }: { k: string; v: ReactNode; color?: string }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 10 }}>
      <span className="mono upper" style={{ fontSize: 9, letterSpacing: '0.1em', color: 'var(--ink-mute)' }}>{k}</span>
      <span className="num" style={{ fontSize: 11.5, color: color ?? 'var(--ink-2)' }}>{v}</span>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

const POLL_MS = 15_000;
const TABS: TradeStatus[] = ['PENDING', 'OPEN', 'CLOSED', 'REJECTED'];

const EMPTY_TAB_TEXT: Record<TradeStatus, string> = {
  PENDING: 'No queued signals. Signals arriving while the market is closed (evening results) queue here and fill at the next open.',
  OPEN: 'No open positions. The engine opens simulated trades automatically when an impact passes its gates.',
  CLOSED: 'No closed trades yet.',
  REJECTED: 'No rejected orders — rejections land here with the gate that blocked them.',
};

export function PaperTrading() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [positions, setPositions] = useState<Position[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [tab, setTab] = useState<TradeStatus>('OPEN');
  const [expanded, setExpanded] = useState<number | null>(null);

  // 15s polling, visibility-aware: the timer is not rescheduled while the tab
  // is hidden; coming back to the tab refreshes immediately and resumes.
  useEffect(() => {
    let cancelled = false;
    let stopped = false; // 403 → internal-only, stop polling for good
    let inflight = false;
    let timer: number | undefined;

    const schedule = () => {
      if (cancelled || stopped) return;
      if (timer !== undefined) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        timer = undefined;
        if (document.hidden) return; // paused — visibilitychange resumes
        void load();
      }, POLL_MS);
    };

    const load = async () => {
      if (inflight) return;
      inflight = true;
      try {
        const [s, p] = await Promise.all([
          apiGet<Summary>('/papertrading/summary'),
          apiGet<{ items?: Position[] } | Position[]>('/papertrading/positions'),
        ]);
        if (cancelled) return;
        setSummary(s ?? null);
        setPositions(Array.isArray(p) ? p : p?.items ?? []);
        setError(null);
        setUpdatedAt(new Date());
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 403) {
          stopped = true;
          setDenied(true);
        } else {
          setError(e instanceof ApiError ? `${e.status} ${e.detail}` : 'network error');
        }
      } finally {
        inflight = false;
        if (!cancelled) {
          setLoading(false);
          schedule();
        }
      }
    };

    const onVisibility = () => {
      if (!document.hidden && !stopped) void load();
    };

    document.addEventListener('visibilitychange', onVisibility);
    void load();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  // ── Derived ────────────────────────────────────────────────────────────────

  const start = summary?.starting_capital ?? null;
  const totalPnl = summary?.total_pnl
    ?? (summary?.realized_pnl != null && summary?.unrealized_pnl != null
      ? summary.realized_pnl + summary.unrealized_pnl
      : null);
  const totalReturnPct = totalPnl != null && start ? (totalPnl / start) * 100 : null;
  const hitRatePct = normPct(summary?.hit_rate);
  const openCount = summary?.n_open
    ?? (positions ? positions.filter((p) => p.status === 'OPEN').length : null);
  const grossPctOfEquity = summary?.gross_exposure != null && summary?.equity
    ? (summary.gross_exposure / summary.equity) * 100
    : null;

  const chartData = useMemo(() => {
    const curve = summary?.equity_curve ?? [];
    return curve
      .map((p) => ({ t: new Date(p.ts).getTime(), equity: p.equity }))
      .filter((d) => Number.isFinite(d.t) && Number.isFinite(d.equity))
      .sort((a, b) => a.t - b.t);
  }, [summary]);

  const yDomain = useMemo<[number, number] | undefined>(() => {
    if (!chartData.length) return undefined;
    let lo = chartData[0].equity;
    let hi = chartData[0].equity;
    for (const d of chartData) {
      if (d.equity < lo) lo = d.equity;
      if (d.equity > hi) hi = d.equity;
    }
    if (start != null) {
      lo = Math.min(lo, start);
      hi = Math.max(hi, start);
    }
    const pad = Math.max((hi - lo) * 0.08, Math.abs(hi) * 0.002, 1);
    return [lo - pad, hi + pad];
  }, [chartData, start]);

  const intraday = chartData.length >= 2
    && chartData[chartData.length - 1].t - chartData[0].t <= 48 * 3600 * 1000;
  const xTick = (t: number) =>
    intraday
      ? new Date(t).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false })
      : new Date(t).toLocaleDateString('en-IN', { day: '2-digit', month: 'short' });

  const tabCounts = useMemo(() => {
    const c: Record<TradeStatus, number> = {
      PENDING: 0, OPEN: 0, CLOSED: 0, REJECTED: 0,
    };
    for (const p of positions ?? []) if (c[p.status] !== undefined) c[p.status] += 1;
    return c;
  }, [positions]);

  const rows = useMemo(() => {
    const list = (positions ?? []).filter((p) => p.status === tab);
    const key = (p: Position) =>
      tab === 'OPEN' ? p.entry_at ?? p.created_at
        : tab === 'CLOSED' ? p.exit_at ?? p.created_at
          : p.created_at;
    return [...list].sort((a, b) => new Date(key(b) ?? 0).getTime() - new Date(key(a) ?? 0).getTime());
  }, [positions, tab]);

  const showReason = tab !== 'OPEN';
  const GRID = showReason
    ? 'minmax(96px, 1fr) 130px 58px 52px 64px 118px 118px 132px 104px'
    : 'minmax(96px, 1fr) 130px 58px 52px 64px 118px 118px 132px';

  const strategies = summary?.per_strategy ?? [];

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <div style={{ height: '100vh', display: 'grid', gridTemplateColumns: 'var(--sidebar-w, 184px) 1fr', overflow: 'hidden' }}>
      <NavSidebar staticStatus="PAPER · INTERNAL">
        <div style={{ padding: '12px 4px' }}>
          <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)', marginBottom: 8 }}>
            Paper trading
          </div>
          <div style={{ fontSize: 11.5, lineHeight: 1.55, color: 'var(--ink-mute)' }}>
            Simulated execution of the pipeline's own signals — fills, marks and
            exits are computed, never sent to a broker. Every trade carries the
            full decision record (magnitude, sentiment, materiality, gates,
            latency); click a row to see it.
          </div>
          <div className="mono upper" style={{ marginTop: 14, fontSize: 9, letterSpacing: '0.12em', color: 'var(--warn)' }}>
            Internal · simulated — not customer-facing
          </div>
          <div className="mono" style={{ marginTop: 8, fontSize: 10, color: 'var(--ink-dim)' }}>
            refreshes every 15s while visible
          </div>
        </div>
      </NavSidebar>

      <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
        <div style={{ padding: '13px 20px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'baseline', gap: 12 }}>
          <span className="mono upper" style={{ fontSize: 13, letterSpacing: '0.18em', color: 'var(--ink-2)' }}>
            Paper trading
          </span>
          <span className="mono upper" style={{ fontSize: 10, letterSpacing: '0.1em', color: 'var(--ink-mute)' }}>
            simulated fills · no live orders
          </span>
          <span className="mono" style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--ink-mute)' }}>
            {updatedAt ? `updated ${clockTime(updatedAt.toISOString())}` : ''}
          </span>
        </div>

        {error && (
          <div className="mono" style={{ padding: '7px 20px', borderBottom: '1px solid var(--border)', fontSize: 10.5, color: 'var(--head)' }}>
            Refresh failed ({error}) — showing last loaded data. Retrying every 15s.
          </div>
        )}

        <div style={{ flex: 1, overflow: 'auto', minHeight: 0, padding: '16px 20px 28px' }}>
          {denied && (
            <div className="mono" style={{ marginTop: 60, textAlign: 'center', fontSize: 11.5, color: 'var(--ink-mute)', lineHeight: 1.8 }}>
              Internal-only. This account is not on the internal allowlist,
              <br />
              so the paper-trading API returns 403.
            </div>
          )}
          {!denied && loading && (
            <div className="mono" style={{ marginTop: 60, textAlign: 'center', fontSize: 11, color: 'var(--ink-mute)' }}>
              Loading paper desk…
            </div>
          )}
          {!denied && !loading && summary == null && positions == null && (
            <div className="mono" style={{ marginTop: 60, textAlign: 'center', fontSize: 11.5, color: 'var(--ink-mute)', lineHeight: 1.8 }}>
              Could not reach the paper-trading API yet.
              <br />
              Retrying every 15 seconds while this tab is visible.
            </div>
          )}

          {!denied && !loading && (summary != null || positions != null) && (
            <>
              {/* 1 · Metric tiles */}
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(148px, 1fr))', gap: 10 }}>
                <Tile
                  label="Total P&L"
                  value={inrSigned(totalPnl)}
                  color={pnlColor(totalPnl)}
                  sub={totalReturnPct != null ? `${signedPct(totalReturnPct)} on start` : 'no trades marked yet'}
                />
                <Tile
                  label="Realized / Unrealized"
                  size={13.5}
                  value={
                    <span style={{ display: 'inline-flex', gap: 6, flexWrap: 'wrap', alignItems: 'baseline' }}>
                      <span style={{ color: pnlColor(summary?.realized_pnl) }}>{inrSigned(summary?.realized_pnl)}</span>
                      <span style={{ color: 'var(--ink-faint)' }}>/</span>
                      <span style={{ color: pnlColor(summary?.unrealized_pnl) }}>{inrSigned(summary?.unrealized_pnl)}</span>
                    </span>
                  }
                />
                <Tile
                  label="Equity"
                  value={inr(summary?.equity)}
                  sub={start != null ? `start ${inr(start)}` : undefined}
                />
                <Tile
                  label="Max drawdown"
                  value={pct(summary?.max_drawdown_pct, 1)}
                  color={summary?.max_drawdown_pct ? 'var(--head)' : undefined}
                  sub="peak-to-trough on equity"
                />
                <Tile
                  label="Sharpe"
                  value={num(summary?.sharpe, 2)}
                  sub={summary?.sharpe == null ? 'needs more history' : 'annualised'}
                />
                <Tile
                  label="Hit rate"
                  value={pct(hitRatePct, 1)}
                  sub="closed trades only"
                />
                <Tile
                  label="Open positions"
                  value={openCount != null ? num(openCount, 0) : DASH}
                />
                <Tile
                  label="Gross exposure"
                  value={inr(summary?.gross_exposure)}
                  sub={grossPctOfEquity != null ? `${num(grossPctOfEquity, 0)}% of equity` : undefined}
                />
              </div>

              {/* 2 · Equity curve */}
              <div className="card" style={{ marginTop: 14, padding: '14px 16px' }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
                  <span className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)' }}>
                    Equity curve
                  </span>
                  <span className="mono" style={{ fontSize: 9.5, color: 'var(--ink-mute)' }}>
                    {chartData.length > 0
                      ? `${chartData.length} snapshots${start != null ? ` · baseline ${inr(start)}` : ''}`
                      : ''}
                  </span>
                </div>
                {chartData.length === 0 ? (
                  <div className="mono" style={{ padding: '48px 0', textAlign: 'center', fontSize: 11, color: 'var(--ink-mute)' }}>
                    No equity snapshots yet — the curve appears after the first portfolio mark.
                  </div>
                ) : (
                  <div style={{ marginTop: 10 }}>
                    <ResponsiveContainer width="100%" height={220}>
                      <AreaChart data={chartData} margin={{ top: 6, right: 10, bottom: 0, left: 0 }}>
                        <defs>
                          <linearGradient id="ptEquityFill" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="0%" stopColor={CHART.accent} stopOpacity={0.22} />
                            <stop offset="100%" stopColor={CHART.accent} stopOpacity={0} />
                          </linearGradient>
                        </defs>
                        <CartesianGrid stroke={CHART.border} strokeDasharray="3 3" vertical={false} />
                        <XAxis
                          dataKey="t"
                          type="number"
                          domain={['dataMin', 'dataMax']}
                          tickFormatter={xTick}
                          tick={{ fill: CHART.inkMute, fontSize: 10, fontFamily: 'IBM Plex Mono, monospace' }}
                          stroke={CHART.border}
                          tickLine={false}
                          minTickGap={48}
                        />
                        <YAxis
                          domain={yDomain}
                          tickFormatter={inrCompact}
                          tick={{ fill: CHART.inkMute, fontSize: 10, fontFamily: 'IBM Plex Mono, monospace' }}
                          stroke={CHART.border}
                          tickLine={false}
                          width={64}
                        />
                        <Tooltip content={<EquityTip />} cursor={{ stroke: CHART.inkMute, strokeDasharray: '3 3' }} />
                        {start != null && (
                          <ReferenceLine
                            y={start}
                            stroke={CHART.inkMute}
                            strokeDasharray="4 4"
                            label={{ value: 'START', position: 'insideBottomRight', fill: CHART.inkMute, fontSize: 9, fontFamily: 'IBM Plex Mono, monospace' }}
                          />
                        )}
                        <Area
                          type="monotone"
                          dataKey="equity"
                          stroke={CHART.accent}
                          strokeWidth={2}
                          fill="url(#ptEquityFill)"
                          dot={false}
                          isAnimationActive={false}
                        />
                      </AreaChart>
                    </ResponsiveContainer>
                  </div>
                )}
              </div>

              {/* 3 · Positions table */}
              <div className="card" style={{ marginTop: 14, overflow: 'hidden' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '10px 16px', borderBottom: '1px solid var(--border)' }}>
                  <span className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)', marginRight: 8 }}>
                    Positions
                  </span>
                  {TABS.map((t) => (
                    <button
                      key={t}
                      onClick={() => { setTab(t); setExpanded(null); }}
                      className="mono upper"
                      style={{
                        fontSize: 9.5,
                        letterSpacing: '0.1em',
                        padding: '4px 10px',
                        background: t === tab ? 'var(--bg-elev-2)' : 'transparent',
                        border: `1px solid ${t === tab ? 'var(--border-hi)' : 'var(--border)'}`,
                        borderRadius: 4,
                        color: t === tab ? 'var(--ink-2)' : 'var(--ink-dim)',
                        cursor: 'pointer',
                      }}
                    >
                      {t} <span className="num" style={{ color: 'var(--ink-mute)' }}>{tabCounts[t]}</span>
                    </button>
                  ))}
                </div>

                <div
                  className="mono upper"
                  style={{
                    display: 'grid',
                    gridTemplateColumns: GRID,
                    gap: 12,
                    padding: '7px 20px',
                    background: 'var(--bg-elev-2)',
                    borderBottom: '1px solid var(--border)',
                    fontSize: 9.5,
                    letterSpacing: '0.1em',
                    color: 'var(--ink-dim)',
                  }}
                >
                  <span>Symbol</span>
                  <span>Strategy</span>
                  <span>Side</span>
                  <span>Instr</span>
                  <span style={{ textAlign: 'right' }}>Qty</span>
                  <span style={{ textAlign: 'right' }}>Entry</span>
                  <span style={{ textAlign: 'right' }}>{tab === 'OPEN' ? 'Mark' : 'Exit'}</span>
                  <span style={{ textAlign: 'right' }}>P&L</span>
                  {showReason && <span>Reason</span>}
                </div>

                {rows.length === 0 && (
                  <div className="mono" style={{ padding: '28px 20px', fontSize: 11, color: 'var(--ink-mute)' }}>
                    {EMPTY_TAB_TEXT[tab]}
                  </div>
                )}
                {rows.map((p) => {
                  const isOpen = p.status === 'OPEN';
                  const markPrice = isOpen ? p.mtm_price : p.exit_price;
                  const markAt = isOpen ? p.mtm_at : p.exit_at;
                  const rowPct = pnlPct(p);
                  const isExpanded = expanded === p.id;
                  return (
                    <div key={p.id}>
                      <div
                        className="row-hover"
                        onClick={() => setExpanded(isExpanded ? null : p.id)}
                        title="Click for decision provenance"
                        style={{
                          display: 'grid',
                          gridTemplateColumns: GRID,
                          gap: 12,
                          padding: '10px 20px',
                          borderBottom: '1px solid var(--border)',
                          alignItems: 'center',
                          cursor: 'pointer',
                          opacity: p.status === 'REJECTED' ? 0.8 : 1,
                        }}
                      >
                        <span style={{ display: 'flex', alignItems: 'center', gap: 7, minWidth: 0 }}>
                          <span className="mono" style={{ fontSize: 9, color: 'var(--ink-faint)' }}>{isExpanded ? '▾' : '▸'}</span>
                          <span className="mono" style={{ fontSize: 12, color: 'var(--ink)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {p.symbol}
                          </span>
                        </span>
                        <span style={{ minWidth: 0 }}><StrategyChip strategy={p.strategy} /></span>
                        <span><SideChip side={p.side} /></span>
                        <span><InstrumentBadge instrument={p.instrument} /></span>
                        <span className="num" style={{ textAlign: 'right', fontSize: 12, color: 'var(--ink-2)' }}>
                          {num(p.qty, 0)}
                        </span>
                        <span style={{ textAlign: 'right' }}>
                          <span className="num" style={{ fontSize: 12, color: 'var(--ink-2)' }}>{inr(p.entry_price, 2)}</span>
                          <div className="mono" style={{ fontSize: 9, color: 'var(--ink-mute)' }}>
                            {p.entry_at ? `${dayLabel(p.entry_at)} ${clockTime(p.entry_at)}` : DASH}
                          </div>
                        </span>
                        <span style={{ textAlign: 'right' }}>
                          <span className="num" style={{ fontSize: 12, color: 'var(--ink-2)' }}>{inr(markPrice, 2)}</span>
                          <div className="mono" style={{ fontSize: 9, color: 'var(--ink-mute)' }}>
                            {markAt ? `${dayLabel(markAt)} ${clockTime(markAt)}` : DASH}
                          </div>
                        </span>
                        <span style={{ textAlign: 'right' }}>
                          <span className="num" style={{ fontSize: 12, color: pnlColor(p.pnl) }}>{inrSigned(p.pnl)}</span>
                          <div className="num" style={{ fontSize: 9.5, color: pnlColor(p.pnl) }}>{signedPct(rowPct)}</div>
                        </span>
                        {showReason && (
                          <span className="mono upper" style={{ fontSize: 9, letterSpacing: '0.08em', color: 'var(--ink-dim)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {p.exit_reason ?? DASH}
                          </span>
                        )}
                      </div>
                      {isExpanded && <ProvenancePanel p={p} />}
                    </div>
                  );
                })}
              </div>

              {/* 4 · Per-strategy stat cards */}
              <div style={{ marginTop: 18 }}>
                <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)', marginBottom: 8 }}>
                  Per strategy
                </div>
                {strategies.length === 0 ? (
                  <div className="mono" style={{ fontSize: 11, color: 'var(--ink-mute)' }}>
                    No strategy activity yet.
                  </div>
                ) : (
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(230px, 1fr))', gap: 10 }}>
                    {strategies.map((s) => {
                      const stratTotal = s.total_pnl
                        ?? (s.realized_pnl != null || s.unrealized_pnl != null
                          ? (s.realized_pnl ?? 0) + (s.unrealized_pnl ?? 0)
                          : null);
                      return (
                        <div key={s.strategy} className="card" style={{ padding: '12px 14px' }}>
                          <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 10, marginBottom: 9 }}>
                            <span className="mono upper" style={{ fontSize: 10.5, letterSpacing: '0.1em', color: 'var(--ink-2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {s.strategy}
                            </span>
                            <span className="num" style={{ fontSize: 13, color: pnlColor(stratTotal) }}>
                              {inrSigned(stratTotal)}
                            </span>
                          </div>
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                            <StatRow
                              k="Trades"
                              v={s.n_trades != null ? num(s.n_trades, 0) : DASH}
                            />
                            <StatRow
                              k="Open / Closed"
                              v={`${s.n_open != null ? num(s.n_open, 0) : DASH} / ${s.n_closed != null ? num(s.n_closed, 0) : DASH}`}
                            />
                            <StatRow k="Hit rate" v={pct(normPct(s.hit_rate), 1)} />
                            <StatRow
                              k="Realized"
                              v={inrSigned(s.realized_pnl)}
                              color={pnlColor(s.realized_pnl)}
                            />
                            <StatRow
                              k="Unrealized"
                              v={inrSigned(s.unrealized_pnl)}
                              color={pnlColor(s.unrealized_pnl)}
                            />
                            <StatRow
                              k="Avg P&L / trade"
                              v={inrSigned(s.avg_pnl)}
                              color={pnlColor(s.avg_pnl)}
                            />
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
