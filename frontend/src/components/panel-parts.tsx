import { CSSProperties, ReactNode } from 'react';
import { Card } from './ds';
import { Direction, DIRECTION_VAR, DIRECTION_LABEL } from '@/lib/format';

/** Section header strip used between assessment sections. */
export function SectionHeader({ children, style }: { children: ReactNode; style?: CSSProperties }) {
  return (
    <div
      className="mono upper"
      style={{
        background: 'var(--bg-elev)',
        borderTop: '1px solid var(--border)',
        borderBottom: '1px solid var(--border)',
        padding: '8px 24px',
        fontSize: 9.5,
        letterSpacing: '0.18em',
        color: 'var(--ink-dim)',
        ...style,
      }}
    >
      {children}
    </div>
  );
}

/** A single KPI card in the Fundamentals 2×2 grid. */
export function KpiCard({
  label,
  value,
  sub,
  subColor,
}: {
  label: string;
  value: string;
  sub?: string;
  subColor?: string;
}) {
  return (
    <Card style={{ padding: '13px 15px' }}>
      <div className="mono upper" style={{ fontSize: 8.5, letterSpacing: '0.12em', color: 'var(--ink-dim)' }}>
        {label}
      </div>
      <div className="num" style={{ fontSize: 18, fontWeight: 600, color: 'var(--ink)', marginTop: 6 }}>
        {value}
      </div>
      {sub && (
        <div className="mono" style={{ fontSize: 10.5, color: subColor ?? 'var(--ink-mute)', marginTop: 4 }}>
          {sub}
        </div>
      )}
    </Card>
  );
}

/** Direction pill: dot + TAILWIND/HEADWIND/MIXED. */
export function DirectionPill({ direction }: { direction: Direction }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap' }}>
      <span style={{ width: 6, height: 6, borderRadius: '50%', background: DIRECTION_VAR[direction] }} />
      <span className="mono upper" style={{ fontSize: 9, letterSpacing: '0.1em', color: DIRECTION_VAR[direction] }}>
        {DIRECTION_LABEL[direction]}
      </span>
    </span>
  );
}

/** Numbered reasoning list (01. / 02. …). */
export function ReasoningList({ items }: { items: string[] }) {
  if (items.length === 0) {
    return (
      <div style={{ padding: '14px 24px', fontSize: 12.5, color: 'var(--ink-mute)' }}>
        No reasoning recorded for this event.
      </div>
    );
  }
  return (
    <div style={{ padding: '14px 24px', display: 'flex', flexDirection: 'column', gap: 12 }}>
      {items.map((text, i) => (
        <div key={i} style={{ display: 'grid', gridTemplateColumns: '26px 1fr', gap: 8 }}>
          <span className="mono" style={{ fontSize: 12, color: 'var(--accent)' }}>
            {String(i + 1).padStart(2, '0')}.
          </span>
          <span style={{ fontSize: 13, lineHeight: 1.5, color: 'var(--ink-2)' }}>{text}</span>
        </div>
      ))}
    </div>
  );
}
