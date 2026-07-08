import { ReactNode } from 'react';

/**
 * Semantic highlighting for analysis prose. Detection only — this layer NEVER
 * invents information: it extracts markers the pipeline wrote (the priced-in
 * bracket tag) and visually lifts figures that already exist in the text.
 */

export interface PricedIn {
  /** Signed move, e.g. "+2.0" / "-6.8" (percent). */
  movePct: string;
  magFrom: string;
  magTo: string;
  /** Raw tag body, for formats that drift from the canonical pattern. */
  raw: string;
}

const PRICED_IN_FULL =
  /\s*\[priced-in:\s*stock already moved ([+-]?[\d.]+)%[^\]]*magnitude\s*(\d)\s*->\s*(\d)\]/i;
const PRICED_IN_ANY = /\s*\[priced-in:([^\]]*)\]/i;

/** Split a pipeline explanation into clean prose + structured priced-in tag. */
export function extractPricedIn(text: string): { clean: string; pricedIn: PricedIn | null } {
  if (!text) return { clean: '', pricedIn: null };
  const full = text.match(PRICED_IN_FULL);
  if (full) {
    return {
      clean: text.replace(PRICED_IN_FULL, '').trim(),
      pricedIn: { movePct: full[1], magFrom: full[2], magTo: full[3], raw: full[0] },
    };
  }
  const any = text.match(PRICED_IN_ANY);
  if (any) {
    return {
      clean: text.replace(PRICED_IN_ANY, '').trim(),
      pricedIn: { movePct: '', magFrom: '', magTo: '', raw: any[1].trim() },
    };
  }
  return { clean: text, pricedIn: null };
}

export function hasPricedIn(text: string | undefined | null): boolean {
  return !!text && PRICED_IN_ANY.test(text);
}

// Money (₹/Rs/$ with crore/lakh/mn units), signed + unsigned percentages,
// magnitude arrows, and Indian-comma-grouped figures.
const TOKEN_SOURCE = [
  '₹\\s?[\\d,]+(?:\\.\\d+)?(?:\\s?(?:crores?|cr|lakhs?)\\b)?',
  'Rs\\.?\\s?[\\d,]+(?:\\.\\d+)?(?:\\s?(?:crores?|cr|lakhs?)\\b)?',
  '(?:USD|US\\$|\\$)\\s?[\\d,]+(?:\\.\\d+)?(?:\\s?(?:million|billion|mn|bn)\\b)?',
  '[+-]\\d+(?:\\.\\d+)?%',
  '\\d+(?:\\.\\d+)?%',
  'magnitude\\s?\\d\\s?->\\s?\\d',
  '\\b\\d{1,3}(?:,\\d{2,3})+(?:\\.\\d+)?\\b',
].join('|');

/** Prose with figures lifted: money/percent/magnitude tokens render in bright
 *  mono; explicitly signed percentages get tailwind/headwind colors. */
export function Hi({ text }: { text: string | undefined | null }) {
  if (!text) return null;
  const re = new RegExp(TOKEN_SOURCE, 'g');
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    const cls = tok.startsWith('+') ? 'hl-pos' : tok.startsWith('-') ? 'hl-neg' : 'hl-num';
    out.push(
      <span key={`${m.index}-${tok}`} className={cls}>
        {tok}
      </span>,
    );
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return <>{out}</>;
}

/** The coral priced-in warning pill. */
export function PricedInPill({ pricedIn }: { pricedIn: PricedIn }) {
  const label = pricedIn.movePct
    ? `PRICED IN · MOVED ${pricedIn.movePct}% · MAG ${pricedIn.magFrom}→${pricedIn.magTo}`
    : 'PRICED IN';
  return (
    <span className="mono upper pill-priced-in" title={pricedIn.raw}>
      {label}
    </span>
  );
}
