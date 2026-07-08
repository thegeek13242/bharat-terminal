import { Fragment, ReactNode, useEffect, useRef, useState } from 'react';
import { ImpactReport } from '@/types';
import { Tag, LiveDot, EmptyState } from './ds';
import {
  clockTime,
  dayKey,
  dayLabel,
  primaryImpact,
  sentimentToDirection,
  magnitudeBand,
  DIRECTION_GLYPH,
  DIRECTION_VAR,
  DIRECTION_LABEL,
} from '@/lib/format';
import { hasPricedIn } from '@/lib/richtext';

interface Props {
  reports: ImpactReport[];
  selectedId?: string;
  onSelect: (r: ImpactReport) => void;
  title: string;
  meta?: string;
  /** Omit entirely on pages without a live stream (no Streaming/Idle chrome). */
  streaming?: boolean;
  emptyTitle: string;
  emptySubtitle: string;
  onLoadOlder?: () => void;
  loadingOlder?: boolean;
  hasMore?: boolean;
  /** Shown instead of "End of history" when paging stops for another reason. */
  endLabel?: string;
  /** Extra header controls (e.g. time-range chips), rendered after the meta. */
  headerExtra?: ReactNode;
  isStarred?: (id: string) => boolean;
  onToggleStar?: (r: ImpactReport) => void;
}

export function FeedList({
  reports,
  selectedId,
  onSelect,
  title,
  meta,
  streaming,
  emptyTitle,
  emptySubtitle,
  onLoadOlder,
  loadingOlder,
  hasMore,
  endLabel,
  headerExtra,
  isStarred,
  onToggleStar,
}: Props) {
  const grid = onToggleStar ? '46px 56px 1fr auto 20px' : '46px 56px 1fr auto';

  // Infinite scroll: when the load-older sentinel at the list's bottom becomes
  // visible inside the scroll container, fetch the next (older) page. The
  // button stays clickable as an explicit fallback.
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const sentinelRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !onLoadOlder || !hasMore) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting) && !loadingOlder) onLoadOlder();
      },
      { root: scrollRef.current, rootMargin: '200px' },
    );
    io.observe(sentinel);
    return () => io.disconnect();
  }, [onLoadOlder, hasMore, loadingOlder, reports.length]);

  // During an auto-load chain the observer fires page after page, toggling
  // loadingOlder on every fetch — which made the sentinel text flicker
  // "Loading… / Load older events" rapidly. Keep the loading presentation
  // sticky across the brief gap between consecutive fetches.
  const [showLoading, setShowLoading] = useState(false);
  useEffect(() => {
    if (loadingOlder) {
      setShowLoading(true);
      return;
    }
    const t = setTimeout(() => setShowLoading(false), 400);
    return () => clearTimeout(t);
  }, [loadingOlder]);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, height: '100%', borderRight: '1px solid var(--border)' }}>
      {/* Header bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          padding: '13px 20px',
          borderBottom: '1px solid var(--border)',
        }}
      >
        <span className="mono upper" style={{ fontSize: 13, letterSpacing: '0.18em', color: 'var(--ink-2)' }}>
          {title}
        </span>
        {meta && (
          <span className="mono upper" style={{ fontSize: 10, letterSpacing: '0.1em', color: 'var(--ink-mute)' }}>
            {meta}
          </span>
        )}
        <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 12 }}>
          {headerExtra}
          {streaming !== undefined && (
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7 }}>
              {streaming ? <LiveDot color="accent" /> : <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--ink-faint)' }} />}
              <span className="mono upper" style={{ fontSize: 10, letterSpacing: '0.12em', color: 'var(--ink-dim)' }}>
                {streaming ? 'Streaming' : 'Idle'}
              </span>
            </span>
          )}
        </span>
      </div>

      {/* Column header strip */}
      <div
        className="mono upper"
        style={{
          display: 'grid',
          gridTemplateColumns: grid,
          gap: 12,
          padding: '7px 20px',
          background: 'var(--bg-elev)',
          borderBottom: '1px solid var(--border)',
          fontSize: 10,
          letterSpacing: '0.1em',
          color: 'var(--ink-dim)',
        }}
      >
        <span>Time</span>
        <span>Src</span>
        <span>Event</span>
        <span style={{ textAlign: 'right' }}>Impact</span>
        {onToggleStar && <span aria-hidden="true" />}
      </div>

      {/* Rows */}
      <div ref={scrollRef} style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
        {reports.length === 0 ? (
          <EmptyState title={emptyTitle} sub={emptySubtitle} />
        ) : (
          <>
            {reports.map((r, i) => {
              const imp = primaryImpact(r);
              const dir = imp ? sentimentToDirection(imp.sentiment) : 'neutral';
              const selected = selectedId === r.id;
              const showDivider = i === 0 || dayKey(r.created_at) !== dayKey(reports[i - 1].created_at);
              return (
                <Fragment key={r.id}>
                  {showDivider && (
                    <div
                      className="mono upper"
                      style={{
                        position: 'sticky',
                        top: 0,
                        zIndex: 2,
                        padding: '5px 20px',
                        background: 'var(--bg-elev)',
                        borderBottom: '1px solid var(--border)',
                        fontSize: 10,
                        letterSpacing: '0.14em',
                        color: 'var(--ink-mute)',
                      }}
                    >
                      {dayLabel(r.created_at)}
                    </div>
                  )}
                  <div
                    onClick={() => onSelect(r)}
                    className={`feed-row row-hover${selected ? ' is-selected' : ''}`}
                    style={{
                      position: 'relative',
                      display: 'grid',
                      gridTemplateColumns: grid,
                      gap: 12,
                      padding: '14px 20px',
                      borderBottom: '1px solid var(--border)',
                      cursor: 'pointer',
                      background: selected ? 'var(--accent-dim)' : 'transparent',
                      borderLeft: `2px solid ${selected ? 'var(--accent)' : 'transparent'}`,
                    }}
                  >
                    <span className="num" style={{ fontSize: 11.5, color: 'var(--ink-dim)' }}>
                      {clockTime(r.created_at)}
                    </span>
                    <span>
                      <span
                        className="mono upper"
                        style={{
                          fontSize: 10,
                          letterSpacing: '0.06em',
                          color: 'var(--ink-dim)',
                          border: '1px solid var(--border-hi)',
                          borderRadius: 2,
                          padding: '2px 5px',
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {(r.news_item?.source ?? '').replace(/_/g, ' ').slice(0, 6) || 'NSE'}
                      </span>
                    </span>
                    <div style={{ minWidth: 0 }}>
                      <div
                        className="line-clamp-2"
                        style={{ fontFamily: 'var(--sans)', fontSize: 13, lineHeight: 1.42, color: 'var(--ink-2)' }}
                      >
                        {r.news_item?.headline}
                      </div>
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, marginTop: 7 }}>
                        {(r.company_impacts ?? []).slice(0, 3).map((c) => (
                          <Tag key={c.symbol} size="sm">
                            {c.symbol}
                          </Tag>
                        ))}
                      </div>
                    </div>
                    <div style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                      <div className="mono" style={{ fontSize: 11.5, color: DIRECTION_VAR[dir], fontWeight: 600 }}>
                        {DIRECTION_GLYPH[dir]} {imp ? magnitudeBand(imp.magnitude) : '—'}
                      </div>
                      <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.08em', color: 'var(--ink-dim)', marginTop: 3 }}>
                        {DIRECTION_LABEL[dir]}
                      </div>
                      {hasPricedIn(imp?.explanation) && (
                        <div className="mono upper" style={{ fontSize: 8.5, letterSpacing: '0.08em', color: 'var(--head)', marginTop: 3 }}>
                          Priced in
                        </div>
                      )}
                    </div>
                    {onToggleStar && (
                      <button
                        className={`star-btn${isStarred?.(r.id) ? ' on' : ''}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          onToggleStar(r);
                        }}
                        title={isStarred?.(r.id) ? 'Remove star' : 'Star this event'}
                        aria-label={isStarred?.(r.id) ? 'Remove star' : 'Star this event'}
                      >
                        {isStarred?.(r.id) ? '★' : '☆'}
                      </button>
                    )}
                  </div>
                </Fragment>
              );
            })}

            {/* Load-older pagination: auto-fires via the IntersectionObserver
                sentinel when scrolled into view; still clickable directly. */}
            {onLoadOlder && hasMore && (
              <button
                ref={sentinelRef}
                onClick={onLoadOlder}
                disabled={showLoading}
                className="mono upper"
                style={{
                  width: '100%',
                  padding: '13px 20px',
                  background: 'transparent',
                  border: 'none',
                  borderBottom: '1px solid var(--border)',
                  color: showLoading ? 'var(--ink-mute)' : 'var(--ink-dim)',
                  fontSize: 10,
                  letterSpacing: '0.14em',
                  cursor: showLoading ? 'default' : 'pointer',
                }}
              >
                {showLoading ? 'Loading…' : 'Load older events'}
              </button>
            )}
            {onLoadOlder && !hasMore && reports.length > 0 && (
              <div
                className="mono upper"
                style={{ padding: '13px 20px', textAlign: 'center', fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-mute)' }}
              >
                {endLabel ?? 'End of history'}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
