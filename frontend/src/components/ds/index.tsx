/**
 * Taperead Terminal design-system components — rebuilt in TS from the handoff
 * (design-system/taperead-ds-bundle.js + README prop APIs). Colors/typography
 * come from the CSS variables in index.css.
 */
import { CSSProperties, ReactNode } from 'react';

// ── Logo ──────────────────────────────────────────────────────────────────
// Matches the landing page brand lockup (taperead-landing-page/src/ds/Logo.jsx):
// inline tape SVG (currentColor bars + one accent bar) + mono "taperead." wordmark
// + optional TERMINAL tag. Renders as a link when `href` is provided.
export function Logo({
  size = 22,
  showTag = true,
  href,
}: {
  size?: number;
  showTag?: boolean;
  href?: string;
}) {
  const wrap: CSSProperties = {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 10,
    textDecoration: 'none',
    color: 'var(--ink)',
  };
  const inner = (
    <>
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-label="Taperead">
        <rect x="2" y="3.5" width="5" height="2" fill="currentColor" opacity="0.7" />
        <rect x="9" y="3.5" width="7" height="2" fill="currentColor" opacity="0.45" />
        <rect x="18" y="3.5" width="4" height="2" fill="currentColor" opacity="0.7" />
        <rect x="2" y="8.5" width="3" height="2" fill="currentColor" opacity="0.45" />
        <rect x="7" y="8.5" width="9" height="2" fill="var(--accent)" />
        <rect x="18" y="8.5" width="4" height="2" fill="currentColor" opacity="0.45" />
        <rect x="4" y="13.5" width="6" height="2" fill="currentColor" opacity="0.6" />
        <rect x="12" y="13.5" width="8" height="2" fill="currentColor" opacity="0.45" />
        <rect x="2" y="18.5" width="10" height="2" fill="currentColor" opacity="0.35" />
        <rect x="14" y="18.5" width="5" height="2" fill="currentColor" opacity="0.35" />
      </svg>
      <span className="mono" style={{ fontSize: 15, fontWeight: 600, letterSpacing: '0.01em', color: 'var(--ink)' }}>
        taperead<span style={{ color: 'var(--accent)' }}>.</span>
      </span>
      {showTag && (
        <span
          className="mono upper"
          style={{
            fontSize: 9,
            letterSpacing: '0.22em',
            color: 'var(--ink-mute)',
            borderLeft: '1px solid var(--border-hi)',
            paddingLeft: 9,
            marginLeft: 1,
            alignSelf: 'center',
          }}
        >
          TERMINAL
        </span>
      )}
    </>
  );
  return href ? (
    <a href={href} style={wrap}>
      {inner}
    </a>
  ) : (
    <span style={wrap}>{inner}</span>
  );
}

// ── Button ────────────────────────────────────────────────────────────────
export function Button({
  variant = 'primary',
  href,
  disabled,
  onClick,
  type = 'button',
  style,
  className,
  children,
}: {
  variant?: 'primary' | 'ghost';
  href?: string;
  disabled?: boolean;
  onClick?: () => void;
  type?: 'button' | 'submit';
  style?: CSSProperties;
  className?: string;
  children: ReactNode;
}) {
  const cls = `btn ${variant === 'primary' ? 'btn-primary' : 'btn-ghost'} ${className ?? ''}`;
  if (href && !disabled) {
    return (
      <a href={href} className={cls} style={style} onClick={onClick}>
        {children}
      </a>
    );
  }
  return (
    <button type={type} className={cls} style={style} disabled={disabled} onClick={onClick}>
      {children}
    </button>
  );
}

// ── Card ──────────────────────────────────────────────────────────────────
export function Card({
  level = 1,
  style,
  className,
  children,
}: {
  level?: 1 | 2 | 3;
  style?: CSSProperties;
  className?: string;
  children: ReactNode;
}) {
  const surface = level === 3 ? 'var(--bg-elev-3)' : level === 2 ? 'var(--bg-elev-2)' : 'var(--bg-elev)';
  return (
    <div
      className={className}
      style={{ background: surface, border: '1px solid var(--border)', borderRadius: 8, ...style }}
    >
      {children}
    </div>
  );
}

// ── Tag ───────────────────────────────────────────────────────────────────
type TagVariant = 'muted' | 'accent' | 'tail' | 'head' | 'warn';
const TAG_COLOR: Record<TagVariant, string> = {
  muted: 'var(--ink-dim)',
  accent: 'var(--accent)',
  tail: 'var(--tail)',
  head: 'var(--head)',
  warn: 'var(--warn)',
};
export function Tag({
  variant = 'muted',
  size = 'md',
  dot = false,
  style,
  children,
}: {
  variant?: TagVariant;
  size?: 'sm' | 'md';
  dot?: boolean;
  style?: CSSProperties;
  children: ReactNode;
}) {
  const color = TAG_COLOR[variant];
  const isAccentish = variant !== 'muted';
  return (
    <span
      className="mono upper"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        fontSize: size === 'sm' ? 8.5 : 10,
        letterSpacing: '0.1em',
        color,
        border: `1px solid ${isAccentish ? 'var(--accent-line)' : 'var(--border-hi)'}`,
        background: variant === 'accent' ? 'var(--accent-dim)' : 'transparent',
        borderRadius: 3,
        padding: size === 'sm' ? '2px 5px' : '3px 7px',
        whiteSpace: 'nowrap',
        ...style,
      }}
    >
      {dot && (
        <span style={{ width: 5, height: 5, borderRadius: '50%', background: color, flexShrink: 0 }} />
      )}
      {children}
    </span>
  );
}

// ── LiveDot ───────────────────────────────────────────────────────────────
export function LiveDot({ color = 'accent' }: { color?: 'accent' | 'tail' | 'head' | 'warn' }) {
  const c =
    color === 'tail' ? 'var(--tail)' : color === 'head' ? 'var(--head)' : color === 'warn' ? 'var(--warn)' : 'var(--accent)';
  return <span className="live-dot" style={{ background: c }} />;
}

// ── TextInput ─────────────────────────────────────────────────────────────
export function TextInput({
  type = 'text',
  placeholder,
  value,
  onChange,
  name,
  autoComplete,
  style,
  onKeyDown,
}: {
  type?: string;
  placeholder?: string;
  value?: string;
  onChange?: (v: string) => void;
  name?: string;
  autoComplete?: string;
  style?: CSSProperties;
  onKeyDown?: (e: React.KeyboardEvent<HTMLInputElement>) => void;
}) {
  return (
    <input
      type={type}
      name={name}
      autoComplete={autoComplete}
      placeholder={placeholder}
      value={value}
      onChange={(e) => onChange?.(e.target.value)}
      onKeyDown={onKeyDown}
      className="mono ds-input"
      style={{
        width: '100%',
        background: 'var(--bg-elev-2)',
        border: '1px solid var(--border-hi)',
        borderRadius: 4,
        color: 'var(--ink)',
        fontSize: 13,
        padding: '11px 13px',
        outline: 'none',
        ...style,
      }}
    />
  );
}

// ── FormField ─────────────────────────────────────────────────────────────
export function FormField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label style={{ display: 'block' }}>
      <span
        className="mono upper"
        style={{ display: 'block', fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)', marginBottom: 9 }}
      >
        {label}
      </span>
      {children}
    </label>
  );
}

// ── InfoBox ───────────────────────────────────────────────────────────────
export function InfoBox({ label, children }: { label?: string; children: ReactNode }) {
  return (
    <div
      style={{
        background: 'var(--bg-elev-2)',
        border: '1px solid var(--border)',
        borderRadius: 6,
        padding: '12px 14px',
      }}
    >
      {label && (
        <div
          className="mono upper"
          style={{ fontSize: 9, letterSpacing: '0.16em', color: 'var(--accent)', marginBottom: 6 }}
        >
          {label}
        </div>
      )}
      <div style={{ fontSize: 12.5, lineHeight: 1.5, color: 'var(--ink-2)' }}>{children}</div>
    </div>
  );
}

// ── SectionLabel ──────────────────────────────────────────────────────────
export function SectionLabel({ children, style }: { children: ReactNode; style?: CSSProperties }) {
  return (
    <div
      className="mono upper"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 9,
        color: 'var(--ink-dim)',
        fontSize: 10.5,
        letterSpacing: '0.2em',
        paddingBottom: 10,
        borderBottom: '1px solid var(--border)',
        ...style,
      }}
    >
      <span style={{ width: 6, height: 6, borderRadius: 1, background: 'var(--accent)' }} />
      {children}
    </div>
  );
}

// ── FeatureCard ───────────────────────────────────────────────────────────
export function FeatureCard({ index, title, body }: { index: number; title: string; body: string }) {
  return (
    <Card style={{ padding: '20px 22px' }}>
      <div className="mono" style={{ fontSize: 11, color: 'var(--accent)', marginBottom: 12 }}>
        {String(index).padStart(2, '0')}.
      </div>
      <h3 style={{ fontFamily: 'var(--sans)', fontSize: 15, fontWeight: 600, margin: '0 0 8px', color: 'var(--ink)' }}>
        {title}
      </h3>
      <p style={{ fontSize: 13, lineHeight: 1.55, color: 'var(--ink-dim)', margin: 0 }}>{body}</p>
    </Card>
  );
}
