import { useEffect, useMemo, useState } from 'react';
import { useImpactFeed } from '@/hooks/useImpactFeed';
import { ImpactReport } from '@/types';
import { NavSidebar } from '@/components/NavSidebar';
import { FeedList } from '@/components/FeedList';
import { AssessmentPanel } from '@/components/AssessmentPanel';

export function Terminal() {
  const { reports, wsStatus } = useImpactFeed();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [sector, setSector] = useState<string | null>(null);

  const sectors = useMemo(() => {
    const counts = new Map<string, number>();
    reports.forEach((r) => (r.affected_sectors ?? []).forEach((s) => counts.set(s, (counts.get(s) ?? 0) + 1)));
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([s]) => s);
  }, [reports]);

  const filtered = useMemo(
    () => (sector ? reports.filter((r) => (r.affected_sectors ?? []).includes(sector)) : reports),
    [reports, sector],
  );

  // Auto-select the newest event when nothing is selected (or selection drops out of view).
  useEffect(() => {
    if (filtered.length === 0) return;
    if (!selectedId || !filtered.some((r) => r.id === selectedId)) {
      setSelectedId(filtered[0].id);
    }
  }, [filtered, selectedId]);

  const selected: ImpactReport | null = filtered.find((r) => r.id === selectedId) ?? null;
  const connected = wsStatus === 'connected';

  return (
    <div style={{ height: '100vh', display: 'grid', gridTemplateColumns: 'var(--sidebar-w, 184px) minmax(0,1.04fr) minmax(0,0.96fr)', overflow: 'hidden' }}>
      <NavSidebar counts={{ feed: reports.length }} connected={connected}>
        <div style={{ padding: '12px 4px' }}>
          <div className="mono upper" style={{ fontSize: 8.5, letterSpacing: '0.16em', color: 'var(--ink-dim)', marginBottom: 10 }}>
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
                    fontSize: 8.5,
                    letterSpacing: '0.08em',
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
        onSelect={(r) => setSelectedId(r.id)}
        title="Event Feed"
        meta={`${filtered.length} EVENTS · LAST 90 MIN`}
        streaming={connected}
        emptyTitle="No events in the last 90 minutes"
        emptySubtitle="Live market events will stream in here."
      />

      <AssessmentPanel report={selected} />
    </div>
  );
}
