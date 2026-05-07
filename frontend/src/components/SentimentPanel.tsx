import { useMemo, useState } from 'react';
import { SentimentSnapshot, ThemeAggregate, SentimentDirection } from '@/types';
import { useSentimentFeed } from '@/hooks/useSentimentFeed';

const DIRECTION_LABEL: Record<SentimentDirection, string> = {
  bullish_in: 'BULLISH IN',
  bearish_in: 'BEARISH IN',
  neutral: 'NEUTRAL',
  ambiguous: 'MIXED',
};

const DIRECTION_COLOR: Record<SentimentDirection, string> = {
  bullish_in: 'text-green-400 border-green-700/50 bg-green-950/30',
  bearish_in: 'text-red-400 border-red-700/50 bg-red-950/30',
  neutral: 'text-gray-400 border-gray-700/50 bg-gray-900/30',
  ambiguous: 'text-amber-300 border-amber-700/50 bg-amber-950/30',
};

function fmtPct(p?: number | null): string {
  if (p === null || p === undefined) return '—';
  return `${(p * 100).toFixed(1)}%`;
}

function fmtDelta(d?: number | null): { text: string; cls: string } {
  if (d === null || d === undefined) return { text: '—', cls: 'text-gray-500' };
  const pp = d * 100;
  const sign = pp >= 0 ? '+' : '';
  const cls = pp > 0 ? 'text-green-400' : pp < 0 ? 'text-red-400' : 'text-gray-400';
  return { text: `${sign}${pp.toFixed(1)}pp`, cls };
}

function fmtVolume(v?: number | null): string {
  if (!v) return '—';
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(0)}K`;
  return `$${v.toFixed(0)}`;
}

function fmtTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return iso;
  }
}

function ThemeStrip({ themes }: { themes: ThemeAggregate[] }) {
  if (themes.length === 0) {
    return (
      <div className="text-xs text-gray-500 px-3 py-2 border-b border-gray-800">
        No theme aggregates yet — waiting for sentiment events.
      </div>
    );
  }

  return (
    <div className="flex gap-2 overflow-x-auto px-3 py-2 border-b border-gray-800 bg-gray-900/40">
      {themes.slice(0, 12).map((t) => {
        const isBull = t.weighted_score > 0;
        const isBear = t.weighted_score < 0;
        const cls = isBull
          ? 'border-green-700/50 bg-green-950/30 text-green-300'
          : isBear
          ? 'border-red-700/50 bg-red-950/30 text-red-300'
          : 'border-gray-700/50 bg-gray-900/30 text-gray-400';
        return (
          <div
            key={t.theme}
            className={`flex-shrink-0 px-3 py-1.5 border rounded text-xs ${cls}`}
            title={t.last_question}
          >
            <div className="font-bold tracking-wide">{t.theme}</div>
            <div className="flex gap-2 mt-0.5 text-[10px] opacity-80">
              <span>↑{t.bullish_in}</span>
              <span>↓{t.bearish_in}</span>
              <span className="font-mono">
                {t.weighted_score >= 0 ? '+' : ''}
                {t.weighted_score}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function SnapshotRow({ snap }: { snap: SentimentSnapshot }) {
  const delta = fmtDelta(snap.delta_1h);
  const dirCls = DIRECTION_COLOR[snap.direction];
  const magBars = '▮'.repeat(snap.magnitude) + '▯'.repeat(5 - snap.magnitude);

  return (
    <div className="border-b border-gray-800 px-3 py-2 hover:bg-gray-900/40 transition-colors">
      <div className="flex items-start gap-3">
        <div className="flex-shrink-0 text-[10px] text-gray-500 font-mono w-12 mt-0.5">
          {fmtTime(snap.created_at)}
        </div>

        <div className={`flex-shrink-0 px-2 py-0.5 border rounded text-[10px] font-bold tracking-wide ${dirCls}`}>
          {DIRECTION_LABEL[snap.direction]}
        </div>

        <div className="flex-shrink-0 text-[10px] text-gray-500 font-mono w-20">
          {snap.macro_theme}
        </div>

        <div className="flex-1 min-w-0">
          <div className="text-xs text-gray-200 truncate" title={snap.question}>
            {snap.question}
          </div>
          <div className="flex gap-3 mt-0.5 text-[10px] text-gray-500">
            <span>
              prob <span className="text-gray-300 font-mono">{fmtPct(snap.probability)}</span>
            </span>
            <span>
              Δ1h <span className={`font-mono ${delta.cls}`}>{delta.text}</span>
            </span>
            <span>
              vol24 <span className="text-gray-300 font-mono">{fmtVolume(snap.volume_24h)}</span>
            </span>
            {snap.affected_sectors.length > 0 && (
              <span className="truncate">
                sectors{' '}
                <span className="text-gray-300">
                  {snap.affected_sectors.slice(0, 3).join(', ')}
                </span>
              </span>
            )}
            {snap.watchlist_symbols.length > 0 && (
              <span className="truncate">
                watch{' '}
                <span className="text-yellow-400">
                  {snap.watchlist_symbols.slice(0, 4).join(' ')}
                </span>
              </span>
            )}
          </div>
        </div>

        <div className="flex-shrink-0 text-[10px] font-mono text-gray-600" title={`magnitude ${snap.magnitude}/5`}>
          {magBars}
        </div>

        {snap.market_url && (
          <a
            href={snap.market_url}
            target="_blank"
            rel="noreferrer"
            className="flex-shrink-0 text-[10px] text-blue-400 hover:text-blue-300 mt-0.5"
          >
            ↗
          </a>
        )}
      </div>
    </div>
  );
}

export function SentimentPanel() {
  const { snapshots, themes, wsStatus } = useSentimentFeed();
  const [filter, setFilter] = useState<SentimentDirection | 'all'>('all');

  const filtered = useMemo(() => {
    if (filter === 'all') return snapshots;
    return snapshots.filter((s) => s.direction === filter);
  }, [snapshots, filter]);

  return (
    <div className="flex flex-col h-full overflow-hidden bg-gray-950">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-800 bg-gray-900/40 flex-shrink-0">
        <div className="flex items-center gap-3 text-xs">
          <span className="text-yellow-400 font-bold tracking-wide">SENTIMENT</span>
          <span className="text-gray-600">Polymarket → India equity bias</span>
        </div>
        <div className="flex items-center gap-1">
          {(['all', 'bullish_in', 'bearish_in', 'ambiguous'] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-2 py-0.5 rounded text-[10px] uppercase tracking-wider ${
                filter === f
                  ? 'bg-yellow-500/20 text-yellow-400 border border-yellow-600/40'
                  : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              {f === 'all' ? 'all' : f.replace('_in', '')}
            </button>
          ))}
        </div>
      </div>

      {/* Theme aggregates strip */}
      <ThemeStrip themes={themes} />

      {/* Snapshot list */}
      <div className="flex-1 overflow-y-auto">
        {filtered.length === 0 ? (
          <div className="text-xs text-gray-500 px-3 py-8 text-center">
            {wsStatus === 'connected'
              ? 'Waiting for sentiment events…'
              : `WebSocket ${wsStatus} — reconnecting`}
          </div>
        ) : (
          filtered.map((snap) => <SnapshotRow key={snap.id} snap={snap} />)
        )}
      </div>

      {/* Footer */}
      <div className="flex-shrink-0 px-3 py-1 border-t border-gray-800 bg-gray-900/40 text-[10px] text-gray-600 font-mono flex justify-between">
        <span>{filtered.length} events shown</span>
        <span>{themes.length} themes tracked</span>
      </div>
    </div>
  );
}
