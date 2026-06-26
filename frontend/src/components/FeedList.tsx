import { ImpactReport } from '@/types';
import { Tag, LiveDot } from './ds';
import {
  clockTime,
  primaryImpact,
  sentimentToDirection,
  magnitudeBand,
  DIRECTION_GLYPH,
  DIRECTION_VAR,
  DIRECTION_LABEL,
} from '@/lib/format';

interface Props {
  reports: ImpactReport[];
  selectedId?: string;
  onSelect: (r: ImpactReport) => void;
  title: string;
  meta?: string;
  streaming?: boolean;
  emptyTitle: string;
  emptySubtitle: string;
}

export function FeedList({ reports, selectedId, onSelect, title, meta, streaming, emptyTitle, emptySubtitle }: Props) {
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
        <span className="mono upper" style={{ fontSize: 11, letterSpacing: '0.2em', color: 'var(--ink-2)' }}>
          {title}
        </span>
        {meta && (
          <span className="mono upper" style={{ fontSize: 10, letterSpacing: '0.12em', color: 'var(--ink-mute)' }}>
            {meta}
          </span>
        )}
        <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 7 }}>
          {streaming ? <LiveDot color="accent" /> : <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--ink-faint)' }} />}
          <span className="mono upper" style={{ fontSize: 9, letterSpacing: '0.14em', color: 'var(--ink-dim)' }}>
            {streaming ? 'Streaming' : 'Idle'}
          </span>
        </span>
      </div>

      {/* Column header strip */}
      <div
        className="mono upper"
        style={{
          display: 'grid',
          gridTemplateColumns: '46px 56px 1fr auto',
          gap: 12,
          padding: '7px 20px',
          background: 'var(--bg-elev)',
          borderBottom: '1px solid var(--border)',
          fontSize: 8.5,
          letterSpacing: '0.12em',
          color: 'var(--ink-dim)',
        }}
      >
        <span>Time</span>
        <span>Src</span>
        <span>Event</span>
        <span style={{ textAlign: 'right' }}>Impact</span>
      </div>

      {/* Rows */}
      <div style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
        {reports.length === 0 ? (
          <div
            style={{
              height: '100%',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 14,
              color: 'var(--ink-mute)',
              padding: 24,
              textAlign: 'center',
            }}
          >
            <span style={{ width: 34, height: 34, border: '1px dashed var(--ink-faint)', borderRadius: 4 }} />
            <div className="mono" style={{ fontSize: 12, color: 'var(--ink-dim)' }}>{emptyTitle}</div>
            <div style={{ fontSize: 12, color: 'var(--ink-mute)' }}>{emptySubtitle}</div>
          </div>
        ) : (
          reports.map((r) => {
            const imp = primaryImpact(r);
            const dir = imp ? sentimentToDirection(imp.sentiment) : 'neutral';
            const selected = selectedId === r.id;
            return (
              <div
                key={r.id}
                onClick={() => onSelect(r)}
                style={{
                  position: 'relative',
                  display: 'grid',
                  gridTemplateColumns: '46px 56px 1fr auto',
                  gap: 12,
                  padding: '14px 20px',
                  borderBottom: '1px solid var(--border)',
                  cursor: 'pointer',
                  background: selected ? 'var(--accent-dim)' : 'transparent',
                  borderLeft: `2px solid ${selected ? 'var(--accent)' : 'transparent'}`,
                }}
                onMouseEnter={(e) => {
                  if (!selected) e.currentTarget.style.background = 'var(--bg-elev-2)';
                }}
                onMouseLeave={(e) => {
                  if (!selected) e.currentTarget.style.background = 'transparent';
                }}
              >
                <span className="num" style={{ fontSize: 11.5, color: 'var(--ink-dim)' }}>
                  {clockTime(r.created_at)}
                </span>
                <span>
                  <span
                    className="mono upper"
                    style={{
                      fontSize: 8.5,
                      letterSpacing: '0.08em',
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
                  <div className="mono upper" style={{ fontSize: 8, letterSpacing: '0.1em', color: 'var(--ink-dim)', marginTop: 3 }}>
                    {DIRECTION_LABEL[dir]}
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
