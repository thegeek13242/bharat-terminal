import { formatDistanceToNow } from 'date-fns';
import { CompanyImpact, ImpactReport } from '@/types';

/** Em-dash fallback for null/undefined — we never fabricate numbers. */
export const DASH = '—';

export function dash(v: unknown): string {
  return v === null || v === undefined || v === '' ? DASH : String(v);
}

export function num(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return DASH;
  return v.toLocaleString('en-IN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function pct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return DASH;
  return `${v.toFixed(digits)}%`;
}

/** ₹cr, compact: 12,345 cr or 1.2L cr for very large. */
export function crore(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return DASH;
  if (v >= 1_00_000) return `${(v / 1_00_000).toFixed(2)}L cr`;
  return `${Math.round(v).toLocaleString('en-IN')} cr`;
}

export function relTime(iso: string | null | undefined): string {
  if (!iso) return DASH;
  try {
    return formatDistanceToNow(new Date(iso), { addSuffix: true });
  } catch {
    return DASH;
  }
}

/** HH:MM clock for the feed TIME column. */
export function clockTime(iso: string | null | undefined): string {
  if (!iso) return DASH;
  try {
    return new Date(iso).toLocaleTimeString('en-IN', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  } catch {
    return DASH;
  }
}

// ── Fundamental-effect direction (color + glyph, double-encoded) ───────────
export type Direction = 'tailwind' | 'headwind' | 'neutral';

export const DIRECTION_GLYPH: Record<Direction, string> = {
  tailwind: '▲',
  headwind: '▼',
  neutral: '◆',
};
export const DIRECTION_VAR: Record<Direction, string> = {
  tailwind: 'var(--tail)',
  headwind: 'var(--head)',
  neutral: 'var(--neutral)',
};
export const DIRECTION_LABEL: Record<Direction, string> = {
  tailwind: 'TAILWIND',
  headwind: 'HEADWIND',
  neutral: 'NEUTRAL',
};

export function sentimentToDirection(sentiment: CompanyImpact['sentiment']): Direction {
  return sentiment === 'positive' ? 'tailwind' : sentiment === 'negative' ? 'headwind' : 'neutral';
}

/** Magnitude (1–5) → HIGH / MED / LOW band shown next to the glyph. */
export function magnitudeBand(magnitude: number): string {
  if (magnitude >= 4) return 'HIGH';
  if (magnitude === 3) return 'MED';
  return 'LOW';
}

const HORIZON_LABEL: Record<string, string> = {
  immediate: '0–1D',
  short_term: '1–5D',
  medium_term: '1–4W',
  long_term: '1M+',
};
export function horizonLabel(h: string | undefined): string {
  return h ? HORIZON_LABEL[h] ?? h : DASH;
}

/** The headline company impact for a report (first-degree, highest magnitude). */
export function primaryImpact(report: ImpactReport): CompanyImpact | undefined {
  const impacts = report.company_impacts ?? [];
  if (impacts.length === 0) return undefined;
  const firstDegree = impacts.filter((i) => (i.hop_distance ?? 0) === 0);
  const pool = firstDegree.length ? firstDegree : impacts;
  return [...pool].sort((a, b) => (b.magnitude ?? 0) - (a.magnitude ?? 0))[0];
}
