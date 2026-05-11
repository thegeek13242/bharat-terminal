import { useEffect, useMemo, useState } from 'react';
import { api } from '@/lib/api';
import type {
  CompanySummary,
  CompanyDetail,
  FinancialResult,
  DossierFull,
} from '@/types';

// ── Small formatting helpers ──────────────────────────────────────────────

const fmtCr = (v: number | null | undefined) =>
  v === null || v === undefined
    ? '—'
    : Math.abs(v) >= 100000
      ? `₹${(v / 100000).toFixed(2)} L Cr`
      : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 0 })} Cr`;

const fmtPct = (v: number | null | undefined, dp = 1) =>
  v === null || v === undefined ? '—' : `${v.toFixed(dp)}%`;

const fmtNum = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined ? '—' : v.toFixed(dp);

const fmtDate = (s: string | null | undefined) => {
  if (!s) return '—';
  try { return new Date(s).toLocaleDateString('en-IN', { year: 'numeric', month: 'short', day: 'numeric' }); }
  catch { return s; }
};

// ── Left rail: search + list ──────────────────────────────────────────────

function CompaniesList({
  selected,
  onSelect,
}: { selected: string | null; onSelect: (sym: string) => void }) {
  const [q, setQ] = useState('');
  const [hasDossier, setHasDossier] = useState<boolean | null>(null);
  const [items, setItems] = useState<CompanySummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const t = setTimeout(() => {
      setLoading(true);
      api
        .listCompanies({
          q: q || undefined,
          has_dossier: hasDossier ?? undefined,
          limit: 100,
        })
        .then((d) => {
          if (cancelled) return;
          setItems(d.items as CompanySummary[]);
          setTotal(d.total);
        })
        .catch(() => !cancelled && setItems([]))
        .finally(() => !cancelled && setLoading(false));
    }, 200);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [q, hasDossier]);

  return (
    <div className="flex flex-col h-full border-r border-gray-800 overflow-hidden">
      {/* search */}
      <div className="flex-shrink-0 p-2 border-b border-gray-800 space-y-2">
        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search symbol or name…"
          className="w-full bg-gray-900 text-gray-100 px-2 py-1 text-xs rounded border border-gray-700 focus:outline-none focus:border-yellow-600"
        />
        <div className="flex gap-1 text-[10px]">
          {[null, true, false].map((v) => (
            <button
              key={String(v)}
              onClick={() => setHasDossier(v)}
              className={`px-2 py-0.5 rounded uppercase tracking-wider ${
                hasDossier === v
                  ? 'bg-yellow-500/20 text-yellow-400 border border-yellow-600/40'
                  : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              {v === null ? 'all' : v ? 'has dossier' : 'no dossier'}
            </button>
          ))}
        </div>
        <div className="text-[10px] text-gray-600">
          {loading ? 'Loading…' : `${items.length} of ${total} companies`}
        </div>
      </div>

      {/* list */}
      <div className="flex-1 overflow-y-auto">
        {items.map((c) => {
          const sel = selected === c.symbol;
          return (
            <button
              key={c.symbol}
              onClick={() => onSelect(c.symbol)}
              className={`w-full text-left px-2 py-1.5 border-b border-gray-900 hover:bg-gray-900 transition-colors ${
                sel ? 'bg-yellow-500/10 border-l-2 border-l-yellow-500' : ''
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-bold text-gray-100">{c.symbol}</span>
                <div className="flex gap-1">
                  {c.dossier_at && (
                    <span className="text-[9px] px-1 py-0.5 rounded bg-blue-950/40 text-blue-300 border border-blue-700/30">
                      D
                    </span>
                  )}
                  {c.quarters_loaded > 0 && (
                    <span className="text-[9px] px-1 py-0.5 rounded bg-green-950/40 text-green-300 border border-green-700/30">
                      F{c.quarters_loaded}
                    </span>
                  )}
                </div>
              </div>
              <div className="text-[10px] text-gray-500 truncate">{c.company_name}</div>
              {c.mcap_cr ? (
                <div className="text-[10px] text-gray-600">{fmtCr(c.mcap_cr)}</div>
              ) : null}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ── Right panel sub-tabs ──────────────────────────────────────────────────

type SubTab = 'overview' | 'financials' | 'dossier' | 'linkages';

function OverviewPanel({ symbol }: { symbol: string }) {
  const [detail, setDetail] = useState<CompanyDetail | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api.getCompanyDetail(symbol)
      .then((d) => setDetail(d as CompanyDetail))
      .catch(() => setDetail(null))
      .finally(() => setLoading(false));
  }, [symbol]);

  if (loading) return <div className="p-4 text-xs text-gray-500">Loading…</div>;
  if (!detail) return <div className="p-4 text-xs text-red-400">No record for {symbol}</div>;

  const fields: [string, string | number | null | undefined][] = [
    ['ISIN', detail.isin],
    ['Sector', detail.sector],
    ['Industry', detail.industry],
    ['Market cap', detail.mcap_cr ? fmtCr(detail.mcap_cr) : null],
    ['Market cap tier', detail.market_cap_tier],
    ['Exchange', detail.exchange],
    ['Last price', detail.last_price ? `₹${detail.last_price.toFixed(2)}` : null],
    ['52w high', detail.fifty_two_week_high ? `₹${detail.fifty_two_week_high.toFixed(2)}` : null],
    ['52w low', detail.fifty_two_week_low ? `₹${detail.fifty_two_week_low.toFixed(2)}` : null],
    ['Revenue TTM', detail.revenue_ttm_cr ? fmtCr(detail.revenue_ttm_cr) : null],
    ['PAT TTM', detail.pat_ttm_cr ? fmtCr(detail.pat_ttm_cr) : null],
    ['EBITDA margin', fmtPct(detail.ebitda_margin_pct)],
    ['EPS TTM', detail.eps_ttm ? `₹${detail.eps_ttm.toFixed(2)}` : null],
    ['P/E ratio', fmtNum(detail.pe_ratio)],
    ['P/B ratio', fmtNum(detail.pb_ratio)],
    ['RoE', fmtPct(detail.roe_pct)],
    ['RoCE', fmtPct(detail.roce_pct)],
    ['Dividend yield', fmtPct(detail.dividend_yield_pct, 2)],
    ['Book value', detail.book_value ? `₹${detail.book_value.toFixed(2)}` : null],
    ['Face value', detail.face_value ? `₹${detail.face_value.toFixed(2)}` : null],
    ['DCF fair value', detail.dcf_fair_value ? `₹${detail.dcf_fair_value.toFixed(2)}` : null],
    ['DCF margin of safety', fmtPct(detail.dcf_margin_of_safety_pct, 1)],
    ['Latest quarter', fmtDate(detail.latest_quarter)],
    ['Quarters loaded', detail.quarters_loaded ?? null],
    ['Latest price date', fmtDate(detail.latest_price_date)],
    ['Has dossier', detail.has_dossier ? 'yes' : 'no'],
    ['Updated', fmtDate(detail.updated_at)],
  ];

  return (
    <div className="p-3 overflow-y-auto h-full">
      <div className="mb-3">
        <h2 className="text-yellow-400 text-base font-bold tracking-tight">
          {detail.company_name}
        </h2>
        <div className="text-xs text-gray-500">{detail.symbol}</div>
      </div>
      <table className="w-full text-xs">
        <tbody>
          {fields.map(([k, v]) =>
            v === null || v === undefined ? null : (
              <tr key={k} className="border-b border-gray-900">
                <td className="py-1 pr-3 text-gray-500 w-1/2">{k}</td>
                <td className="py-1 text-gray-200 font-mono">{String(v)}</td>
              </tr>
            ),
          )}
        </tbody>
      </table>
    </div>
  );
}

function FinancialsPanel({ symbol }: { symbol: string }) {
  const [items, setItems] = useState<FinancialResult[]>([]);
  const [loading, setLoading] = useState(true);
  const [period, setPeriod] = useState<'quarterly' | 'annual'>('quarterly');

  useEffect(() => {
    setLoading(true);
    api.getCompanyFinancials(symbol, period, 40)
      .then((d) => setItems(d.items as FinancialResult[]))
      .catch(() => setItems([]))
      .finally(() => setLoading(false));
  }, [symbol, period]);

  // Show only consolidated rows if both present
  const display = useMemo(() => {
    const byPeriod = new Map<string, FinancialResult>();
    for (const r of items) {
      const k = r.period_end;
      const existing = byPeriod.get(k);
      if (!existing || (!existing.consolidated && r.consolidated)) {
        byPeriod.set(k, r);
      }
    }
    return Array.from(byPeriod.values()).sort((a, b) => b.period_end.localeCompare(a.period_end));
  }, [items]);

  return (
    <div className="flex flex-col h-full">
      <div className="flex-shrink-0 p-2 border-b border-gray-800 flex items-center gap-2">
        {(['quarterly', 'annual'] as const).map((p) => (
          <button
            key={p}
            onClick={() => setPeriod(p)}
            className={`px-2 py-0.5 text-[10px] rounded uppercase tracking-wider ${
              period === p
                ? 'bg-yellow-500/20 text-yellow-400 border border-yellow-600/40'
                : 'text-gray-500 hover:text-gray-300'
            }`}
          >
            {p}
          </button>
        ))}
        <span className="text-[10px] text-gray-600 ml-auto">
          {loading ? 'Loading…' : `${display.length} periods (consolidated preferred)`}
        </span>
      </div>
      <div className="flex-1 overflow-auto">
        <table className="w-full text-[11px] font-mono">
          <thead className="sticky top-0 bg-gray-950 border-b border-gray-800">
            <tr className="text-gray-500 text-left">
              <th className="px-2 py-1 font-normal">Period</th>
              <th className="px-2 py-1 font-normal text-right">Revenue ₹Cr</th>
              <th className="px-2 py-1 font-normal text-right">EBITDA ₹Cr</th>
              <th className="px-2 py-1 font-normal text-right">EBITDA %</th>
              <th className="px-2 py-1 font-normal text-right">PBT ₹Cr</th>
              <th className="px-2 py-1 font-normal text-right">PAT ₹Cr</th>
              <th className="px-2 py-1 font-normal text-right">EPS basic</th>
              <th className="px-2 py-1 font-normal">Type</th>
            </tr>
          </thead>
          <tbody>
            {display.map((r) => (
              <tr key={r.id} className="border-b border-gray-900 hover:bg-gray-900/40">
                <td className="px-2 py-1 text-gray-300">{fmtDate(r.period_end)}</td>
                <td className="px-2 py-1 text-right text-gray-200">{r.revenue_cr?.toLocaleString('en-IN') ?? '—'}</td>
                <td className="px-2 py-1 text-right text-gray-200">{r.ebitda_cr?.toLocaleString('en-IN') ?? '—'}</td>
                <td className="px-2 py-1 text-right text-gray-200">{fmtPct(r.ebitda_margin_pct, 1)}</td>
                <td className="px-2 py-1 text-right text-gray-200">{r.pbt_cr?.toLocaleString('en-IN') ?? '—'}</td>
                <td className="px-2 py-1 text-right text-gray-200">{r.pat_cr?.toLocaleString('en-IN') ?? '—'}</td>
                <td className="px-2 py-1 text-right text-gray-200">{fmtNum(r.eps_basic)}</td>
                <td className="px-2 py-1 text-gray-500">
                  {r.consolidated ? 'C' : 'S'}/{r.period_type[0].toUpperCase()}
                </td>
              </tr>
            ))}
            {!loading && !display.length && (
              <tr>
                <td colSpan={8} className="px-2 py-8 text-center text-gray-500">
                  No financial records for {symbol}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function JSONBlock({ data }: { data: unknown }) {
  return (
    <pre className="text-[11px] text-gray-300 bg-gray-950 border border-gray-800 rounded p-2 overflow-auto whitespace-pre-wrap break-words">
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}

function DossierPanel({ symbol }: { symbol: string }) {
  const [d, setD] = useState<DossierFull | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setErr(null);
    api.getDossier(symbol)
      .then((x) => setD(x as DossierFull))
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, [symbol]);

  if (loading) return <div className="p-4 text-xs text-gray-500">Loading dossier…</div>;
  if (err) return (
    <div className="p-4 text-xs text-gray-500">
      <p className="text-amber-400 mb-1">No dossier yet for {symbol}.</p>
      <p className="text-gray-600">
        Generate one:&nbsp;
        <code className="text-gray-400 bg-gray-900 px-1 rounded">
          docker compose --profile research run --rm research-runner dossier --symbols {symbol}
        </code>
      </p>
    </div>
  );
  if (!d) return null;

  return (
    <div className="overflow-y-auto h-full p-3 space-y-4">
      {/* Header */}
      <div className="border-b border-gray-800 pb-2">
        <div className="flex items-baseline justify-between">
          <h2 className="text-yellow-400 text-base font-bold">{d.company_name ?? d.symbol}</h2>
          <div className="text-[10px] text-gray-500 font-mono">
            {d.model_used} · {d.tokens_used?.toLocaleString()} toks · {fmtDate(d.generated_at)}
          </div>
        </div>
        <div className="flex gap-3 mt-1 text-[10px] text-gray-500">
          <span>quality <span className="text-gray-200">{d.data_quality?.toFixed(2)}</span></span>
          <span>source <span className="text-gray-200">{fmtDate(d.source_quarter)}</span></span>
          <span>kind <span className="text-gray-200">{d.refresh_kind}</span></span>
          <span>v<span className="text-gray-200">{d.version}</span></span>
        </div>
      </div>

      {/* Sensitivities */}
      <section>
        <h3 className="text-yellow-400 text-xs font-bold uppercase tracking-wider mb-1">
          Sensitivities ({d.sensitivities.length})
        </h3>
        <p className="text-[10px] text-gray-600 mb-2">
          Signed elasticity: +1% trigger move → β% fundamental impact. Channel + lag tell you
          how the impact propagates.
        </p>
        <table className="w-full text-[11px] font-mono">
          <thead className="border-b border-gray-800">
            <tr className="text-gray-500 text-left">
              <th className="px-2 py-1 font-normal">Trigger</th>
              <th className="px-2 py-1 font-normal">Channel</th>
              <th className="px-2 py-1 font-normal text-right">β</th>
              <th className="px-2 py-1 font-normal text-right">Lag</th>
              <th className="px-2 py-1 font-normal">Conf</th>
              <th className="px-2 py-1 font-normal">Rationale</th>
            </tr>
          </thead>
          <tbody>
            {d.sensitivities.map((s, i) => (
              <tr key={i} className="border-b border-gray-900 align-top">
                <td className="px-2 py-1 text-yellow-400 whitespace-nowrap">{s.trigger_id}</td>
                <td className="px-2 py-1 text-gray-400 whitespace-nowrap">{s.channel}</td>
                <td className={`px-2 py-1 text-right ${s.beta >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {s.beta >= 0 ? '+' : ''}{s.beta.toFixed(3)}
                </td>
                <td className="px-2 py-1 text-right text-gray-300">{s.lag_days}d</td>
                <td className="px-2 py-1 text-gray-300">{s.confidence}</td>
                <td className="px-2 py-1 text-gray-300 max-w-[40ch]">
                  <div className="line-clamp-2" title={s.rationale ?? ''}>{s.rationale}</div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {/* Linkages */}
      <section>
        <h3 className="text-yellow-400 text-xs font-bold uppercase tracking-wider mb-1">
          Linkages ({d.linkages.length})
        </h3>
        <p className="text-[10px] text-gray-600 mb-2">
          Outbound edges used by Stage 4 to find 2nd-degree exposed names.
        </p>
        <table className="w-full text-[11px] font-mono">
          <thead className="border-b border-gray-800">
            <tr className="text-gray-500 text-left">
              <th className="px-2 py-1 font-normal">Target</th>
              <th className="px-2 py-1 font-normal">Kind</th>
              <th className="px-2 py-1 font-normal text-right">Strength</th>
              <th className="px-2 py-1 font-normal">Dir</th>
              <th className="px-2 py-1 font-normal text-right">Lag</th>
              <th className="px-2 py-1 font-normal">Rationale</th>
            </tr>
          </thead>
          <tbody>
            {d.linkages.map((l, i) => (
              <tr key={i} className="border-b border-gray-900 align-top">
                <td className="px-2 py-1 text-yellow-400">{l.target_symbol}</td>
                <td className="px-2 py-1 text-gray-400">{l.kind}</td>
                <td className="px-2 py-1 text-right text-gray-300">{l.strength.toFixed(2)}</td>
                <td className={`px-2 py-1 ${
                  l.direction === 'positive' ? 'text-green-400' :
                  l.direction === 'negative' ? 'text-red-400' : 'text-amber-400'
                }`}>{l.direction}</td>
                <td className="px-2 py-1 text-right text-gray-300">{l.propagation_lag_days}d</td>
                <td className="px-2 py-1 text-gray-300 max-w-[40ch]">
                  <div className="line-clamp-2" title={l.rationale ?? ''}>{l.rationale}</div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {/* Business / Financials / Risks / Catalysts / Mgmt as collapsibles */}
      {(['business', 'financials', 'risks', 'catalysts', 'mgmt_commentary', 'citations'] as const).map((key) => {
        const value = d[key];
        if (!value) return null;
        return (
          <details key={key} className="border border-gray-800 rounded">
            <summary className="cursor-pointer px-2 py-1 text-xs text-yellow-400 hover:bg-gray-900 select-none uppercase tracking-wider">
              {key.replace(/_/g, ' ')}
            </summary>
            <div className="p-2">
              <JSONBlock data={value} />
            </div>
          </details>
        );
      })}
    </div>
  );
}

function LinkagesPanel({ symbol }: { symbol: string }) {
  const [data, setData] = useState<{ outbound: any[]; inbound: any[] } | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api.getCompanyLinkages(symbol)
      .then((d) => setData({ outbound: d.outbound, inbound: d.inbound }))
      .catch(() => setData({ outbound: [], inbound: [] }))
      .finally(() => setLoading(false));
  }, [symbol]);

  if (loading) return <div className="p-4 text-xs text-gray-500">Loading…</div>;
  if (!data) return null;

  const renderTable = (rows: any[], cap: string) => (
    <section className="space-y-1">
      <h3 className="text-yellow-400 text-xs font-bold uppercase tracking-wider">{cap} ({rows.length})</h3>
      <table className="w-full text-[11px] font-mono">
        <thead className="border-b border-gray-800">
          <tr className="text-gray-500 text-left">
            <th className="px-2 py-1 font-normal">Symbol</th>
            <th className="px-2 py-1 font-normal">Kind</th>
            <th className="px-2 py-1 font-normal text-right">Strength</th>
            <th className="px-2 py-1 font-normal">Dir</th>
            <th className="px-2 py-1 font-normal text-right">Lag</th>
            <th className="px-2 py-1 font-normal">Rationale</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-gray-900 align-top">
              <td className="px-2 py-1 text-yellow-400">{r.other}</td>
              <td className="px-2 py-1 text-gray-400">{r.kind}</td>
              <td className="px-2 py-1 text-right text-gray-300">{Number(r.strength).toFixed(2)}</td>
              <td className={`px-2 py-1 ${
                r.direction === 'positive' ? 'text-green-400' :
                r.direction === 'negative' ? 'text-red-400' : 'text-amber-400'
              }`}>{r.direction}</td>
              <td className="px-2 py-1 text-right text-gray-300">{r.propagation_lag_days}d</td>
              <td className="px-2 py-1 text-gray-300 max-w-[40ch]">
                <div className="line-clamp-2" title={r.rationale ?? ''}>{r.rationale}</div>
              </td>
            </tr>
          ))}
          {!rows.length && (
            <tr><td colSpan={6} className="px-2 py-4 text-center text-gray-600">none</td></tr>
          )}
        </tbody>
      </table>
    </section>
  );

  return (
    <div className="overflow-y-auto h-full p-3 space-y-4">
      {renderTable(data.outbound, 'Outbound (this co → others)')}
      {renderTable(data.inbound, 'Inbound (others → this co)')}
    </div>
  );
}

// ── Main browser ──────────────────────────────────────────────────────────

export function CompaniesBrowser() {
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<SubTab>('overview');

  return (
    <div className="h-full grid grid-cols-12 gap-0">
      <div className="col-span-3 min-h-0">
        <CompaniesList selected={selected} onSelect={setSelected} />
      </div>
      <div className="col-span-9 min-h-0 flex flex-col bg-gray-950">
        {!selected ? (
          <div className="flex-1 flex items-center justify-center text-xs text-gray-500">
            Select a company from the list
          </div>
        ) : (
          <>
            {/* Sub-tab bar */}
            <div className="flex border-b border-gray-800 bg-gray-900/40 flex-shrink-0">
              {(['overview', 'financials', 'dossier', 'linkages'] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`px-4 py-2 text-xs uppercase tracking-wider border-b-2 transition-colors ${
                    tab === t
                      ? 'border-yellow-400 text-yellow-400'
                      : 'border-transparent text-gray-500 hover:text-gray-300'
                  }`}
                >
                  {t}
                </button>
              ))}
              <div className="ml-auto px-3 py-2 text-xs text-gray-500 font-mono">
                {selected}
              </div>
            </div>
            <div className="flex-1 min-h-0">
              {tab === 'overview' && <OverviewPanel symbol={selected} />}
              {tab === 'financials' && <FinancialsPanel symbol={selected} />}
              {tab === 'dossier' && <DossierPanel symbol={selected} />}
              {tab === 'linkages' && <LinkagesPanel symbol={selected} />}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
