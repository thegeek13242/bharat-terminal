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

/** Short date for the FeedList day-divider: "TODAY" or "01 JUL". */
export function dayLabel(iso: string | null | undefined): string {
  if (!iso) return DASH;
  try {
    const d = new Date(iso);
    const now = new Date();
    const sameDay =
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate();
    if (sameDay) return 'TODAY';
    return d
      .toLocaleDateString('en-IN', { day: '2-digit', month: 'short' })
      .toUpperCase();
  } catch {
    return DASH;
  }
}

/** Stable per-day key ("2026-07-01") for grouping feed rows. */
export function dayKey(iso: string | null | undefined): string {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleDateString('en-CA'); // YYYY-MM-DD, local
  } catch {
    return '';
  }
}

/** "312 events · since 09:15" derived from the oldest loaded event. Honest — no
 * fabricated windows. */
export function feedRangeMeta(count: number, oldestIso: string | null | undefined): string {
  const noun = count === 1 ? 'EVENT' : 'EVENTS';
  if (!count || !oldestIso) return `${count} ${noun}`;
  const t = clockTime(oldestIso);
  return t === DASH ? `${count} ${noun}` : `${count} ${noun} · SINCE ${t}`;
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

/** Event-level impact band for a feed row. A commodity shock only reaches the
 *  feed after the detector's strict ≥3σ / ≥1.2% gate, so the EVENT is material
 *  by construction and is flagged HIGH regardless of individual company betas
 *  (per-company magnitudes stay data-backed). All other reports fall back to
 *  the top affected company's magnitude. */
export function reportImpactBand(report: ImpactReport, primaryMagnitude?: number): string {
  if (report.news_item?.category === 'COMMODITY_SHOCK') return 'HIGH';
  return primaryMagnitude != null ? magnitudeBand(primaryMagnitude) : DASH;
}
