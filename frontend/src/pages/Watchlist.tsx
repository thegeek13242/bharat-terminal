import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useImpactFeed } from '@/hooks/useImpactFeed';
import { useStarred } from '@/hooks/useStarred';
import { api } from '@/lib/api';
import { CompanyImpact, ImpactReport } from '@/types';
import { NavSidebar } from '@/components/NavSidebar';
import { FeedList } from '@/components/FeedList';
import { AssessmentPanel } from '@/components/AssessmentPanel';
import { SymbolAutocomplete } from '@/components/SymbolAutocomplete';
import { relTime } from '@/lib/format';
import { useFeedKeyboardNav } from '@/hooks/useFeedKeyboardNav';

interface Tracked {
  symbol: string;
  price_alert?: number | null;
  impact_threshold?: number;
}

export function Watchlist() {
  const { reports, wsStatus, loadOlder, loadingOlder, hasMore } = useImpactFeed();
  const { isStarred, toggleStar, count: starredCount } = useStarred();
  const [tracked, setTracked] = useState<Tracked[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [userPinned, setUserPinned] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const pinnedReport = useRef<ImpactReport | null>(null);

  useEffect(() => {
    api
      .getWatchlist()
      .then(({ items }) => setTracked(items as Tracked[]))
      .catch(() => setTracked([]));
  }, []);

  const symbolSet = useMemo(() => new Set(tracked.map((t) => t.symbol.toUpperCase())), [tracked]);
  const trackedBySymbol = useMemo(
    () => new Map(tracked.map((t) => [t.symbol.toUpperCase(), t])),
    [tracked],
  );

  // Match rule: DIRECT impacts on a tracked symbol always count; INDIRECT
  // (graph-propagated) impacts count only at magnitude >= the symbol's
  // impact_threshold (default 3). Without this, low-magnitude propagated
  // impacts flooded the feed with events that read as unrelated — measured:
  // 70 of 87 SBIN matches in a week were indirect and all below magnitude 3.
  const matchesTracked = useCallback(
    (c: CompanyImpact) => {
      const t = trackedBySymbol.get(c.symbol.toUpperCase());
      if (!t) return false;
      if ((c.hop_distance ?? 0) === 0) return true;
      return (c.magnitude ?? 0) >= (t.impact_threshold ?? 3);
    },
    [trackedBySymbol],
  );

  const filtered = useMemo(
    () =>
      reports
        .filter((r) => (r.company_impacts ?? []).some(matchesTracked))
        .map((r) => ({
          ...r,
          // Display order only: the tracked symbols that caused the match come
          // first, so every row shows WHY it is in the watchlist feed.
          company_impacts: [...(r.company_impacts ?? [])].sort(
            (a, b) => Number(matchesTracked(b)) - Number(matchesTracked(a)),
          ),
        })),
    [reports, matchesTracked],
  );

  // Latest event per tracked symbol (for the card note + click target).
  const latestBySymbol = useMemo(() => {
    const m = new Map<string, ImpactReport>();
    for (const r of reports) {
      for (const c of r.company_impacts ?? []) {
        if (!matchesTracked(c)) continue;
        const k = c.symbol.toUpperCase();
        if (!m.has(k)) m.set(k, r);
      }
    }
    return m;
  }, [reports, matchesTracked]);

  const select = (r: ImpactReport) => {
    setSelectedId(r.id);
    setUserPinned(true);
    pinnedReport.current = r;
  };

  const clearSelection = () => {
    setUserPinned(false);
    pinnedReport.current = null;
    setSelectedId(filtered[0]?.id ?? null);
  };

  useEffect(() => {
    if (userPinned) return;
    if (filtered.length && (!selectedId || !filtered.some((r) => r.id === selectedId))) {
      setSelectedId(filtered[0].id);
    }
  }, [filtered, selectedId, userPinned]);

  useFeedKeyboardNav({ reports: filtered, selectedId, onSelect: select, onEscape: clearSelection });

  const selected =
    filtered.find((r) => r.id === selectedId) ??
    (userPinned && pinnedReport.current?.id === selectedId ? pinnedReport.current : null);
  const connected = wsStatus === 'connected';

  // Optimistic save with rollback + inline error surfacing.
  async function persist(next: Tracked[]) {
    const prev = tracked;
    setTracked(next);
    setSaveError(null);
    try {
      await api.saveWatchlist(next);
    } catch {
      setTracked(prev); // roll back the optimistic update
      setSaveError('Could not save — changes reverted. Retry.');
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
      <NavSidebar counts={{ watchlist: tracked.length, starred: starredCount }} connected={connected}>
        <div style={{ padding: '12px 4px' }}>
          <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)', marginBottom: 8 }}>
            Add symbol
          </div>
          <div style={{ marginBottom: 18 }}>
            <SymbolAutocomplete onAdd={addSymbol} />
          </div>

          {saveError && (
            <div
              className="mono"
              style={{
                fontSize: 10,
                color: 'var(--head)',
                background: 'rgba(198,132,118,0.10)',
                border: '1px solid rgba(198,132,118,0.35)',
                borderRadius: 4,
                padding: '6px 8px',
                marginBottom: 12,
                lineHeight: 1.4,
              }}
            >
              {saveError}
            </div>
          )}

          <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)', marginBottom: 10 }}>
            Tracked · {tracked.length}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 12 }}>
            {tracked.length === 0 && (
              <div style={{ fontSize: 11.5, color: 'var(--ink-mute)', padding: '6px 4px' }}>No symbols yet.</div>
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
                  onClick={() => ev && select(ev)}
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
                    <div className="mono" style={{ fontSize: 13, fontWeight: 600, color: 'var(--ink-2)' }}>{t.symbol}</div>
                    <div className="mono" style={{ fontSize: 10, color: 'var(--ink-mute)' }}>
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
        onSelect={select}
        title="Watchlist Feed"
        meta={`${filtered.length} MATCHES · DIRECT + MATERIAL INDIRECT`}
        streaming={connected}
        emptyTitle="No events for your tracked symbols"
        emptySubtitle="Direct events always show; indirect exposure only at magnitude 3+. Load older events or add symbols."
        onLoadOlder={loadOlder}
        loadingOlder={loadingOlder}
        hasMore={hasMore}
        isStarred={isStarred}
        onToggleStar={toggleStar}
      />

      <AssessmentPanel report={selected} />
    </div>
  );
}
