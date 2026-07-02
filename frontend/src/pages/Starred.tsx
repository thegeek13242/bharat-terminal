import { useEffect, useState } from 'react';
import { ImpactReport } from '@/types';
import { NavSidebar } from '@/components/NavSidebar';
import { FeedList } from '@/components/FeedList';
import { AssessmentPanel } from '@/components/AssessmentPanel';
import { useStarred } from '@/hooks/useStarred';
import { useFeedKeyboardNav } from '@/hooks/useFeedKeyboardNav';

export function Starred() {
  const { starred, isStarred, toggleStar, count, error } = useStarred();
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Keep a valid selection as items are starred/unstarred.
  useEffect(() => {
    if (starred.length === 0) {
      setSelectedId(null);
      return;
    }
    if (!selectedId || !starred.some((r) => r.id === selectedId)) {
      setSelectedId(starred[0].id);
    }
  }, [starred, selectedId]);

  const select = (r: ImpactReport) => setSelectedId(r.id);

  useFeedKeyboardNav({
    reports: starred,
    selectedId,
    onSelect: select,
    onEscape: () => setSelectedId(starred[0]?.id ?? null),
  });

  const selected = starred.find((r) => r.id === selectedId) ?? null;

  return (
    <div style={{ height: '100vh', display: 'grid', gridTemplateColumns: 'var(--sidebar-w, 184px) minmax(0,1.04fr) minmax(0,0.96fr)', overflow: 'hidden' }}>
      <NavSidebar counts={{ starred: count }} staticStatus="STARRED · ACCOUNT">
        <div style={{ padding: '12px 4px' }}>
          <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)', marginBottom: 8 }}>
            Shortlist
          </div>
          <div style={{ fontSize: 11.5, lineHeight: 1.55, color: 'var(--ink-mute)' }}>
            Events you star in the feed collect here, saved to your account.
          </div>
          {error && (
            <div
              className="mono"
              style={{
                marginTop: 12,
                fontSize: 10,
                color: 'var(--head)',
                background: 'rgba(198,132,118,0.10)',
                border: '1px solid rgba(198,132,118,0.35)',
                borderRadius: 4,
                padding: '6px 8px',
                lineHeight: 1.4,
              }}
            >
              {error}
            </div>
          )}
        </div>
      </NavSidebar>

      <FeedList
        reports={starred}
        selectedId={selectedId ?? undefined}
        onSelect={select}
        title="Starred"
        meta={`${count} starred`}
        emptyTitle="No starred events"
        emptySubtitle="Star events in the feed to shortlist them here."
        isStarred={isStarred}
        onToggleStar={toggleStar}
      />

      <AssessmentPanel report={selected} />
    </div>
  );
}
