import { useEffect, useMemo, useRef, useState } from 'react';
import { useImpactFeed } from '@/hooks/useImpactFeed';
import { useStarred } from '@/hooks/useStarred';
import { ImpactReport } from '@/types';
import { NavSidebar } from '@/components/NavSidebar';
import { FeedList } from '@/components/FeedList';
import { AssessmentPanel } from '@/components/AssessmentPanel';
import { feedRangeMeta } from '@/lib/format';
import { useFeedKeyboardNav } from '@/hooks/useFeedKeyboardNav';

type RangeKey = '90m' | '6h' | '24h' | 'all';
const RANGE_MS: Record<Exclude<RangeKey, 'all'>, number> = {
  '90m': 90 * 60_000,
  '6h': 6 * 3_600_000,
  '24h': 24 * 3_600_000,
};
const RANGE_TEXT: Record<Exclude<RangeKey, 'all'>, string> = {
  '90m': 'last 90 min',
  '6h': 'last 6 hours',
  '24h': 'last 24 hours',
};
const RANGE_CHIP: Record<RangeKey, string> = { '90m': '90M', '6h': '6H', '24h': '24H', all: 'ALL' };

export function Terminal() {
  const { reports, wsStatus, loadOlder, loadingOlder, hasMore } = useImpactFeed();
  const { isStarred, toggleStar, count: starredCount } = useStarred();
  // selectedId + userPinned: when the user explicitly clicks a row we pin it,
  // even if it later scrolls out of the capped in-memory window. Only when
  // nothing is explicitly pinned do we auto-follow the newest event.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [userPinned, setUserPinned] = useState(false);
  const [sector, setSector] = useState<string | null>(null);
  const [range, setRange] = useState<RangeKey>('all');
  // Retain the last explicitly-selected report object so the panel keeps
  // showing it even after eviction from `reports`.
  const pinnedReport = useRef<ImpactReport | null>(null);

  // The range cutoff moves with the clock; re-evaluate once a minute so a
  // "last 90 min" view doesn't silently go stale while the user reads.
  const [nowTick, setNowTick] = useState(() => Date.now());
  useEffect(() => {
    if (range === 'all') return;
    const t = setInterval(() => setNowTick(Date.now()), 60_000);
    return () => clearInterval(t);
  }, [range]);
  const cutoffMs = range === 'all' ? null : nowTick - RANGE_MS[range];

  const rangeFiltered = useMemo(
    () => (cutoffMs == null ? reports : reports.filter((r) => Date.parse(r.created_at) >= cutoffMs)),
    [reports, cutoffMs],
  );

  const sectors = useMemo(() => {
    const counts = new Map<string, number>();
    rangeFiltered.forEach((r) => (r.affected_sectors ?? []).forEach((s) => counts.set(s, (counts.get(s) ?? 0) + 1)));
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([s]) => s);
  }, [rangeFiltered]);

  const filtered = useMemo(
    () => (sector ? rangeFiltered.filter((r) => (r.affected_sectors ?? []).includes(sector)) : rangeFiltered),
    [rangeFiltered, sector],
  );

  // When a time window is selected, page older events in until the window is
  // covered. Stops on: window covered, or no more history.
  useEffect(() => {
    if (cutoffMs == null || !hasMore || loadingOlder || reports.length === 0) return;
    const oldestMs = Date.parse(reports[reports.length - 1].created_at);
    if (oldestMs > cutoffMs) loadOlder();
  }, [cutoffMs, reports, hasMore, loadingOlder, loadOlder]);

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

  // Auto-follow newest ONLY when the user has not explicitly pinned a report.
  useEffect(() => {
    if (userPinned) return;
    if (filtered.length === 0) return;
    if (!selectedId || !filtered.some((r) => r.id === selectedId)) {
      setSelectedId(filtered[0].id);
    }
  }, [filtered, selectedId, userPinned]);

  useFeedKeyboardNav({
    reports: filtered,
    selectedId,
    onSelect: select,
    onEscape: clearSelection,
  });

  // Resolve the panel's report: prefer the live copy from the list, fall back
  // to the retained pinned object if it has been evicted from the window.
  const selected: ImpactReport | null =
    filtered.find((r) => r.id === selectedId) ??
    (userPinned && pinnedReport.current?.id === selectedId ? pinnedReport.current : null);

  const connected = wsStatus === 'connected';
  const oldest = filtered.length ? filtered[filtered.length - 1].created_at : null;

  // Honest meta: computed from what is actually shown, never a fixed window.
  const windowUncovered =
    cutoffMs != null && reports.length > 0 && Date.parse(reports[reports.length - 1].created_at) > cutoffMs;
  const meta =
    range === 'all'
      ? feedRangeMeta(filtered.length, oldest)
      : `${filtered.length} events · ${RANGE_TEXT[range]}${windowUncovered ? ' · loading…' : ''}`;

  const rangeChips = (
    <span style={{ display: 'inline-flex', gap: 4 }}>
      {(Object.keys(RANGE_CHIP) as RangeKey[]).map((k) => {
        const active = range === k;
        return (
          <button
            key={k}
            onClick={() => setRange(k)}
            className="mono upper"
            title={k === 'all' ? 'All loaded events' : `Show the ${RANGE_TEXT[k as Exclude<RangeKey, 'all'>]}`}
            style={{
              fontSize: 10,
              letterSpacing: '0.06em',
              padding: '3px 7px',
              borderRadius: 3,
              cursor: 'pointer',
              color: active ? 'var(--accent)' : 'var(--ink-dim)',
              background: active ? 'var(--accent-dim)' : 'transparent',
              border: `1px solid ${active ? 'var(--accent-line)' : 'var(--border-hi)'}`,
            }}
          >
            {RANGE_CHIP[k]}
          </button>
        );
      })}
    </span>
  );

  return (
    <div style={{ height: '100vh', display: 'grid', gridTemplateColumns: 'var(--sidebar-w, 184px) minmax(0,1.04fr) minmax(0,0.96fr)', overflow: 'hidden' }}>
      <NavSidebar counts={{ feed: reports.length, starred: starredCount }} connected={connected}>
        <div style={{ padding: '12px 4px' }}>
          <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)', marginBottom: 10 }}>
            Sectors
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {sectors.map((s) => {
              const active = sector === s;
              return (
                <button
                  key={s}
                  onClick={() => setSector(active ? null : s)}
                  className="mono upper"
                  style={{
                    fontSize: 10,
                    letterSpacing: '0.06em',
                    padding: '3px 7px',
                    borderRadius: 3,
                    cursor: 'pointer',
                    color: active ? 'var(--accent)' : 'var(--ink-dim)',
                    background: active ? 'var(--accent-dim)' : 'transparent',
                    border: `1px solid ${active ? 'var(--accent-line)' : 'var(--border-hi)'}`,
                  }}
                >
                  {s}
                </button>
              );
            })}
          </div>
        </div>
      </NavSidebar>

      <FeedList
        reports={filtered}
        selectedId={selectedId ?? undefined}
        onSelect={select}
        title="Event Feed"
        meta={meta}
        streaming={connected}
        headerExtra={rangeChips}
        emptyTitle={range === 'all' ? 'No events loaded yet' : `No events in the ${RANGE_TEXT[range]}`}
        emptySubtitle={
          range === 'all'
            ? 'Market events will stream in here as they are analysed.'
            : 'Try a wider time range, or clear the sector filter.'
        }
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
