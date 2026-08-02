/** LLM spend: MTD total, per-stage/model/service breakdown, daily series,
 * per-filing and per-dossier economics. Data source: llm_call_log +
 * research_chat_log — history starts at the Phase-2 deploy (the ephemeral
 * JSONL era is unrecoverable), so the first month is partial. */
import { useCallback, useEffect, useState } from 'react';
import {
  Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { ApiError } from '@/lib/api';
import { apiFetch, ErrorNote, GridHeader, Hint, SectionTitle, Segmented, Tile, TileGrid, usd } from './shared';
import type { TabProps } from './AdminPage';

// recharts renders SVG presentation attributes where CSS var() does not
// resolve — design-token hex used directly (PaperTrading.tsx precedent).
const CHART = { accent: '#3FA192', border: '#242B36', inkMute: '#5E6775' };

interface Costs {
  window: string;
  total_usd?: number;
  llm?: { usd: number; calls: number; failed_usd: number; failed_calls: number; web_search_calls: number; data_since: string | null };
  research_chat?: { usd: number; asks: number };
  by_stage?: { k: string; calls: number; usd: number; tok_in: number; tok_out: number }[];
  by_model?: { k: string; calls: number; usd: number; tok_in: number; tok_out: number }[];
  by_service?: { k: string; calls: number; usd: number; tok_in: number; tok_out: number }[];
  daily?: { day: string; usd: number; calls: number }[];
  per_filing?: { filings: number; mean_usd: number | null; median_usd: number | null; p90_usd: number | null } | null;
  per_dossier?: { dossiers: number; mean_usd: number | null; median_usd: number | null } | null;
  error?: string;
}

const WINDOWS = [
  { key: 'mtd', label: 'Month to date' },
  { key: '7d', label: '7d' },
  { key: '30d', label: '30d' },
] as const;

const B_GRID = 'minmax(160px,1.4fr) 80px 100px 110px 110px';

function Breakdown({ title, rows }: { title: string; rows?: Costs['by_stage'] }) {
  if (!rows) return null;
  return (
    <>
      <SectionTitle>{title}</SectionTitle>
      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <GridHeader grid={B_GRID} cols={['Key', 'Calls', 'Cost', 'Tokens in', 'Tokens out']} />
        {rows.map((r) => (
          <div key={r.k ?? '(none)'} style={{ display: 'grid', gridTemplateColumns: B_GRID, gap: 10, padding: '7px 12px', fontSize: 11, alignItems: 'center', borderBottom: '1px solid var(--border)' }}>
            <span className="mono" style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.k || '(unset)'}</span>
            <span className="num">{r.calls}</span>
            <span className="num">{usd(Number(r.usd), 4)}</span>
            <span className="num" style={{ color: 'var(--ink-mute)' }}>{Number(r.tok_in).toLocaleString('en-IN')}</span>
            <span className="num" style={{ color: 'var(--ink-mute)' }}>{Number(r.tok_out).toLocaleString('en-IN')}</span>
          </div>
        ))}
        {rows.length === 0 && (
          <div style={{ padding: 12 }}><Hint>no rows in window</Hint></div>
        )}
      </div>
    </>
  );
}

export function CostsTab({ onDenied }: TabProps) {
  const [win, setWin] = useState<string>('mtd');
  const [data, setData] = useState<Costs | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await apiFetch<Costs>(`/admin/costs?window=${win}`));
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) return onDenied();
      setError(String((e as Error).message));
    }
  }, [win, onDenied]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div>
      <Segmented
        value={win}
        onChange={setWin}
        options={WINDOWS.map((w) => ({ value: w.key, label: w.label }))}
      />
      <ErrorNote error={error} />
      {data?.error && <ErrorNote error={data.error} />}

      {data && !data.error && (
        <>
          <SectionTitle>Totals ({data.window})</SectionTitle>
          <TileGrid>
            <Tile label="Total spend" value={usd(data.total_usd)} size={20}
                  sub={data.llm?.data_since ? `LLM data since ${String(data.llm.data_since).slice(0, 10)} — first month partial` : 'no LLM rows yet'} />
            <Tile label="LLM (pipeline+dossier)" value={usd(data.llm?.usd)} sub={`${data.llm?.calls ?? 0} calls`} />
            <Tile label="Research chat" value={usd(data.research_chat?.usd)} sub={`${data.research_chat?.asks ?? 0} asks`} />
            <Tile label="Failed-run spend" value={usd(data.llm?.failed_usd)} color={data.llm?.failed_usd ? 'var(--warn)' : undefined}
                  sub={`${data.llm?.failed_calls ?? 0} failed calls — tokens burned, no output`} />
            <Tile label="Web searches" value={data.llm?.web_search_calls ?? 0}
                  sub="per-query grounding fee unpriced (U6)" />
          </TileGrid>

          <SectionTitle>Per-unit economics</SectionTitle>
          <TileGrid>
            <Tile label="Cost / filing (median)" value={usd(data.per_filing?.median_usd, 4)}
                  sub={`mean ${usd(data.per_filing?.mean_usd, 4)} · p90 ${usd(data.per_filing?.p90_usd, 4)} · n=${data.per_filing?.filings ?? 0}`} />
            <Tile label="Cost / dossier (median)" value={usd(data.per_dossier?.median_usd, 3)}
                  sub={`mean ${usd(data.per_dossier?.mean_usd, 3)} · n=${data.per_dossier?.dossiers ?? 0} (grounding priced separately)`} />
          </TileGrid>

          {data.daily && data.daily.length > 1 && (
            <>
              <SectionTitle>Daily spend (IST days)</SectionTitle>
              <div className="card" style={{ padding: '12px 8px 4px', height: 180 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={data.daily} margin={{ top: 4, right: 12, bottom: 0, left: 0 }}>
                    <defs>
                      <linearGradient id="costFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor={CHART.accent} stopOpacity={0.22} />
                        <stop offset="100%" stopColor={CHART.accent} stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke={CHART.border} strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="day" tick={{ fill: CHART.inkMute, fontSize: 10, fontFamily: 'IBM Plex Mono, monospace' }} stroke={CHART.border} />
                    <YAxis tick={{ fill: CHART.inkMute, fontSize: 10, fontFamily: 'IBM Plex Mono, monospace' }} stroke={CHART.border}
                           tickFormatter={(v: number) => `$${v}`} width={54} />
                    <Tooltip
                      formatter={(v: number) => [usd(v, 4), 'spend']}
                      contentStyle={{ background: '#161B22', border: `1px solid ${CHART.border}`, fontSize: 11, fontFamily: 'IBM Plex Mono, monospace' }}
                    />
                    <Area type="monotone" dataKey="usd" stroke={CHART.accent} strokeWidth={1.5} fill="url(#costFill)" />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </>
          )}

          <Breakdown title="By stage" rows={data.by_stage} />
          <Breakdown title="By model" rows={data.by_model} />
          <Breakdown title="By service" rows={data.by_service} />
        </>
      )}
    </div>
  );
}
