/**
 * Admin-panel control kit — data-dense variants of the Taperead design
 * system (components/ds, tokens in index.css).
 *
 * One control scale, everywhere: CONTROL_H (30px) boxes, 11.5px mono text,
 * radius 4, surface --bg-elev-2 with --border-hi chrome. In-row actions use
 * the `small` size (24px). No call site sets its own padding/font on a
 * control — if a size isn't here, it doesn't exist.
 *
 * Local apiFetch follows the PaperTrading.tsx precedent: admin endpoints
 * live here until the shapes settle rather than in lib/api.ts.
 */
import { CSSProperties, ReactNode } from 'react';
import { SectionLabel, Tag } from '@/components/ds';
import { ApiError } from '@/lib/api';
import { DASH } from '@/lib/format';

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    let detail = text;
    try {
      detail = JSON.parse(text).detail ?? text;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

// ── Formatters ─────────────────────────────────────────────────────────────
export function usd(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return DASH;
  return `$${v.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function hms(totalSeconds: number | null | undefined): string {
  if (totalSeconds == null || Number.isNaN(totalSeconds)) return DASH;
  const s = Math.round(totalSeconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${s}s`;
}

// ── Control scale ──────────────────────────────────────────────────────────
export const CONTROL_H = 30;
const CONTROL_H_SM = 24;
const CONTROL_FONT = 11.5;
const CONTROL_FONT_SM = 10;

const boxChrome: CSSProperties = {
  background: 'var(--bg-elev-2)',
  border: '1px solid var(--border-hi)',
  borderRadius: 4,
  color: 'var(--ink)',
  fontSize: CONTROL_FONT,
  outline: 'none',
};

// ── Input ──────────────────────────────────────────────────────────────────
export function Input({
  value, onChange, placeholder, width, type = 'text', onKeyDown,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  width?: number | string;
  type?: string;
  onKeyDown?: (e: React.KeyboardEvent<HTMLInputElement>) => void;
}) {
  return (
    <input
      type={type}
      className="mono ds-input"
      placeholder={placeholder}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={onKeyDown}
      style={{
        ...boxChrome,
        height: CONTROL_H,
        padding: '0 10px',
        width: width ?? '100%',
      }}
    />
  );
}

// ── Select ─────────────────────────────────────────────────────────────────
export function Select({
  value, onChange, options, width, small, disabled,
}: {
  value: string;
  onChange: (v: string) => void;
  options: readonly string[] | { value: string; label: string }[];
  width?: number | string;
  small?: boolean;
  disabled?: boolean;
}) {
  const opts = options.map((o) =>
    typeof o === 'string' ? { value: o, label: o } : o);
  return (
    <select
      className="mono ds-input"
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      style={{
        ...boxChrome,
        height: small ? CONTROL_H_SM : CONTROL_H,
        fontSize: small ? CONTROL_FONT_SM : CONTROL_FONT,
        padding: small ? '0 6px' : '0 8px',
        width,
        colorScheme: 'dark',
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.5 : 1,
      }}
    >
      {opts.map((o) => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  );
}

// ── Button ─────────────────────────────────────────────────────────────────
// The global .btn is landing-page CTA scale — deliberately NOT used here.
type BtnVariant = 'primary' | 'ghost' | 'danger';

export function Btn({
  variant = 'ghost', small, disabled, onClick, title, children,
}: {
  variant?: BtnVariant;
  small?: boolean;
  disabled?: boolean;
  onClick?: () => void;
  title?: string;
  children: ReactNode;
}) {
  const palette: Record<BtnVariant, CSSProperties> = {
    primary: {
      background: 'var(--accent)', color: '#06120F',
      border: '1px solid var(--accent)',
    },
    ghost: {
      background: 'transparent', color: 'var(--ink-2)',
      border: '1px solid var(--border-hi)',
    },
    danger: {
      background: 'transparent', color: 'var(--head)',
      border: '1px solid rgba(198,132,118,0.45)',
    },
  };
  return (
    <button
      type="button"
      className="mono"
      title={title}
      disabled={disabled}
      onClick={onClick}
      style={{
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        gap: 6,
        height: small ? CONTROL_H_SM : CONTROL_H,
        padding: small ? '0 9px' : '0 14px',
        borderRadius: 4,
        fontSize: small ? CONTROL_FONT_SM : CONTROL_FONT,
        fontWeight: 500,
        letterSpacing: '0.04em',
        whiteSpace: 'nowrap',
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.45 : 1,
        transition: 'border-color .15s ease, color .15s ease, background .15s ease',
        ...palette[variant],
      }}
    >
      {children}
    </button>
  );
}

// ── Segmented (window/period pickers — ONE implementation for all tabs) ────
export function Segmented({
  value, onChange, options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: readonly { value: string; label: string }[] | readonly string[];
}) {
  const opts = options.map((o) =>
    typeof o === 'string' ? { value: o, label: o } : o);
  return (
    <div
      style={{
        display: 'inline-flex',
        height: CONTROL_H,
        border: '1px solid var(--border-hi)',
        borderRadius: 4,
        overflow: 'hidden',
        background: 'var(--bg-elev-2)',
      }}
    >
      {opts.map((o, i) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            className="mono upper"
            onClick={() => onChange(o.value)}
            style={{
              border: 'none',
              borderLeft: i > 0 ? '1px solid var(--border-hi)' : 'none',
              background: active ? 'var(--accent-dim)' : 'transparent',
              color: active ? 'var(--accent)' : 'var(--ink-dim)',
              fontSize: 10,
              letterSpacing: '0.1em',
              padding: '0 12px',
              cursor: 'pointer',
              transition: 'color .12s ease, background .12s ease',
            }}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

// ── FilterChip (toggleable facet chips, e.g. alert kinds) ──────────────────
export function FilterChip({
  active, onClick, title, children,
}: {
  active: boolean;
  onClick: () => void;
  title?: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      className="mono upper"
      title={title}
      onClick={onClick}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 6,
        height: CONTROL_H_SM,
        padding: '0 9px',
        borderRadius: 3,
        fontSize: 9.5,
        letterSpacing: '0.08em',
        cursor: 'pointer',
        background: active ? 'var(--accent-dim)' : 'transparent',
        border: `1px solid ${active ? 'var(--accent-line)' : 'var(--border-hi)'}`,
        color: active ? 'var(--accent)' : 'var(--ink-dim)',
        transition: 'color .12s ease, border-color .12s ease',
        whiteSpace: 'nowrap',
      }}
    >
      {children}
    </button>
  );
}

// ── Layout / display pieces ────────────────────────────────────────────────
export function Tile({ label, value, color, sub, size = 17 }: {
  label: string;
  value: ReactNode;
  color?: string;
  sub?: ReactNode;
  size?: number;
}) {
  return (
    <div className="card" style={{ padding: '11px 14px', minWidth: 0 }}>
      <div
        className="mono upper"
        style={{ fontSize: 9, letterSpacing: '0.12em', color: 'var(--ink-dim)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
      >
        {label}
      </div>
      <div className="num" style={{ fontSize: size, marginTop: 5, color: color ?? 'var(--ink)' }}>{value}</div>
      {sub != null && (
        <div className="mono" style={{ marginTop: 3, fontSize: 9.5, color: 'var(--ink-mute)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {sub}
        </div>
      )}
    </div>
  );
}

export function TileGrid({ children }: { children: ReactNode }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(148px, 1fr))', gap: 8 }}>
      {children}
    </div>
  );
}

/** Section heading — the ds SectionLabel (accent square + hairline) with the
 * panel's vertical rhythm baked in, so tabs never set their own margins. */
export function SectionTitle({ children, first }: { children: ReactNode; first?: boolean }) {
  return (
    <SectionLabel style={{ margin: first ? '0 0 12px' : '26px 0 12px' }}>
      {children}
    </SectionLabel>
  );
}

export function GridHeader({ grid, cols }: { grid: string; cols: (string | ReactNode)[] }) {
  return (
    <div
      className="mono upper"
      style={{
        display: 'grid', gridTemplateColumns: grid, gap: 10, padding: '8px 12px',
        fontSize: 9, letterSpacing: '0.1em', color: 'var(--ink-dim)',
        borderBottom: '1px solid var(--border)', position: 'sticky', top: 0,
        background: 'var(--bg-elev)', zIndex: 1,
      }}
    >
      {cols.map((c, i) => <span key={i}>{c}</span>)}
    </div>
  );
}

/** Status chip — the ds Tag with ok/warn semantics mapped onto it. */
export function StateChip({ ok, warn, label }: { ok?: boolean; warn?: boolean; label: string }) {
  return <Tag size="sm" variant={ok ? 'tail' : warn ? 'warn' : 'head'}>{label}</Tag>;
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div className="mono" style={{ margin: '12px 0 0', fontSize: 11, color: 'var(--head)' }}>
      {error}
    </div>
  );
}

/** Muted inline note (empty-data explanations, hints). */
export function Hint({ children, warn }: { children: ReactNode; warn?: boolean }) {
  return (
    <span className="mono" style={{ fontSize: 10.5, color: warn ? 'var(--warn)' : 'var(--ink-mute)' }}>
      {children}
    </span>
  );
}

/** Standard denied panel for a 403 (not on the admin allowlist). */
export function DeniedPanel() {
  return (
    <div style={{ display: 'flex', flex: 1, alignItems: 'center', justifyContent: 'center' }}>
      <div className="card" style={{ padding: '22px 28px', maxWidth: 420 }}>
        <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--warn)' }}>
          Admin only
        </div>
        <div style={{ marginTop: 8, fontSize: 13, color: 'var(--ink-2)', lineHeight: 1.5 }}>
          This account is not an admin. The panel is available to the
          platform owner only; server-side enforcement returned 403.
        </div>
      </div>
    </div>
  );
}
