import { useEffect, useMemo, useState } from 'react';
import { useImpactFeed } from '@/hooks/useImpactFeed';
import { api } from '@/lib/api';
import { ImpactReport } from '@/types';
import { NavSidebar } from '@/components/NavSidebar';
import { FeedList } from '@/components/FeedList';
import { AssessmentPanel } from '@/components/AssessmentPanel';
import { SymbolAutocomplete } from '@/components/SymbolAutocomplete';
import { relTime } from '@/lib/format';

interface Tracked {
  symbol: string;
  price_alert?: number | null;
  impact_threshold?: number;
}

export function Watchlist() {
  const { reports, wsStatus } = useImpactFeed();
  const [tracked, setTracked] = useState<Tracked[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    api
      .getWatchlist()
      .then(({ items }) => setTracked(items as Tracked[]))
      .catch(() => setTracked([]));
  }, []);

  const symbolSet = useMemo(() => new Set(tracked.map((t) => t.symbol.toUpperCase())), [tracked]);

  const filtered = useMemo(
    () =>
      reports.filter((r) =>
        (r.company_impacts ?? []).some((c) => symbolSet.has(c.symbol.toUpperCase())),
      ),
    [reports, symbolSet],
  );

  // Latest event per tracked symbol (for the card note + click target).
  const latestBySymbol = useMemo(() => {
    const m = new Map<string, ImpactReport>();
    for (const r of reports) {
      for (const c of r.company_impacts ?? []) {
        const k = c.symbol.toUpperCase();
        if (symbolSet.has(k) && !m.has(k)) m.set(k, r);
      }
    }
    return m;
  }, [reports, symbolSet]);

  useEffect(() => {
    if (filtered.length && (!selectedId || !filtered.some((r) => r.id === selectedId))) {
      setSelectedId(filtered[0].id);
    }
  }, [filtered, selectedId]);

  const selected = filtered.find((r) => r.id === selectedId) ?? null;
  const connected = wsStatus === 'connected';

  async function persist(next: Tracked[]) {
    setTracked(next);
    try {
      await api.saveWatchlist(next);
    } catch {
      /* surfaced via 401 handler if auth; otherwise keep optimistic */
    }
  }

  function addSymbol(symbol: string) {
    const sym = symbol.trim().toUpperCase();
    if (!sym || symbolSet.has(sym)) return;
    persist([...tracked, { symbol: sym, impact_threshold: 3 }]);
  }

  function remove(sym: string) {
    persist(tracked.filter((t) => t.symbol.toUpperCase() !== sym.toUpperCase()));
  }

  return (
    <div style={{ height: '100vh', display: 'grid', gridTemplateColumns: 'var(--sidebar-w, 184px) minmax(0,1.04fr) minmax(0,0.96fr)', overflow: 'hidden' }}>
      <NavSidebar counts={{ watchlist: tracked.length }} connected={connected}>
        <div style={{ padding: '12px 4px' }}>
          <div className="mono upper" style={{ fontSize: 8.5, letterSpacing: '0.16em', color: 'var(--ink-dim)', marginBottom: 8 }}>
            Add symbol
          </div>
          <div style={{ marginBottom: 18 }}>
            <SymbolAutocomplete onAdd={addSymbol} />
          </div>
          <div className="mono upper" style={{ fontSize: 8.5, letterSpacing: '0.16em', color: 'var(--ink-dim)', marginBottom: 10 }}>
            Tracked · {tracked.length}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 12 }}>
            {tracked.length === 0 && (
              <div style={{ fontSize: 11, color: 'var(--ink-mute)', padding: '6px 4px' }}>No symbols yet.</div>
            )}
            {tracked.map((t) => {
              const ev = latestBySymbol.get(t.symbol.toUpperCase());
              const imp = ev?.company_impacts?.find((c) => c.symbol.toUpperCase() === t.symbol.toUpperCase());
              const dot =
                imp?.sentiment === 'positive' ? 'var(--tail)' : imp?.sentiment === 'negative' ? 'var(--head)' : 'var(--ink-faint)';
              const active = ev && ev.id === selectedId;
              return (
                <div
                  key={t.symbol}
                  onClick={() => ev && setSelectedId(ev.id)}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    padding: '7px 8px',
                    borderRadius: 4,
                    cursor: ev ? 'pointer' : 'default',
                    background: active ? 'var(--accent-dim)' : 'transparent',
                    borderLeft: `2px solid ${active ? 'var(--accent)' : 'transparent'}`,
                  }}
                >
                  <span style={{ width: 7, height: 7, borderRadius: '50%', background: dot, flexShrink: 0 }} />
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div className="mono" style={{ fontSize: 12, fontWeight: 600, color: 'var(--ink-2)' }}>{t.symbol}</div>
                    <div className="mono" style={{ fontSize: 9, color: 'var(--ink-mute)' }}>
                      {ev ? relTime(ev.created_at) : 'Quiet'}
                    </div>
                  </div>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      remove(t.symbol);
                    }}
                    title="Remove"
                    style={{ background: 'transparent', border: 'none', color: 'var(--ink-mute)', cursor: 'pointer', fontSize: 13 }}
                  >
                    ×
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      </NavSidebar>

      <FeedList
        reports={filtered}
        selectedId={selectedId ?? undefined}
        onSelect={(r) => setSelectedId(r.id)}
        title="Watchlist Feed"
        meta={`${filtered.length} EVENTS · ${tracked.length} SYMBOLS`}
        streaming={connected}
        emptyTitle="No events for your tracked symbols"
        emptySubtitle="Add symbols to see their events here."
      />

      <AssessmentPanel report={selected} />
    </div>
  );
}
