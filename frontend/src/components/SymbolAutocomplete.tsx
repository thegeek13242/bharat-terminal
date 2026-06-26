import { useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';
import { CompanySummary } from '@/types';
import { TextInput, Button } from './ds';

/** Symbol typeahead backed by /companies search. Calls onAdd(symbol) on commit. */
export function SymbolAutocomplete({ onAdd }: { onAdd: (symbol: string) => void }) {
  const [q, setQ] = useState('');
  const [sug, setSug] = useState<CompanySummary[]>([]);
  const [open, setOpen] = useState(false);
  const deb = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => {
    clearTimeout(deb.current);
    const term = q.trim();
    if (term.length < 1) {
      setSug([]);
      return;
    }
    deb.current = setTimeout(() => {
      api
        .listCompanies({ q: term, limit: 8 })
        .then(({ items }) => {
          setSug(items as CompanySummary[]);
          setOpen(true);
        })
        .catch(() => setSug([]));
    }, 180);
    return () => clearTimeout(deb.current);
  }, [q]);

  function commit(symbol: string) {
    const s = symbol.trim().toUpperCase();
    if (!s) return;
    onAdd(s);
    setQ('');
    setSug([]);
    setOpen(false);
  }

  return (
    <div style={{ position: 'relative' }}>
      <div style={{ display: 'flex', gap: 6 }}>
        <TextInput
          placeholder="e.g. RELIANCE"
          value={q}
          onChange={(v) => {
            setQ(v);
            setOpen(true);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') commit(sug[0]?.symbol ?? q);
            if (e.key === 'Escape') setOpen(false);
          }}
          style={{ fontSize: 11, padding: '8px 9px' }}
        />
        <Button variant="primary" onClick={() => commit(sug[0]?.symbol ?? q)} style={{ padding: '8px 14px' }}>
          Add
        </Button>
      </div>

      {open && sug.length > 0 && (
        <div
          style={{
            position: 'absolute',
            top: 'calc(100% + 4px)',
            left: 0,
            zIndex: 30,
            minWidth: 248,
            maxWidth: 340,
            maxHeight: 264,
            overflow: 'auto',
            background: 'var(--bg-elev-3)',
            border: '1px solid var(--border-hi)',
            borderRadius: 6,
            boxShadow: '0 10px 28px rgba(0,0,0,0.5)',
          }}
        >
          {sug.map((c) => (
            <div
              key={c.symbol}
              // mousedown (not click) so it fires before the input blur closes the list
              onMouseDown={(e) => {
                e.preventDefault();
                commit(c.symbol);
              }}
              style={{
                display: 'flex',
                alignItems: 'baseline',
                gap: 8,
                padding: '8px 11px',
                cursor: 'pointer',
                borderBottom: '1px solid var(--border)',
              }}
              onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-elev-2)')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
            >
              <span className="mono" style={{ fontSize: 12, fontWeight: 600, color: 'var(--ink)' }}>
                {c.symbol}
              </span>
              <span
                style={{ fontSize: 11, color: 'var(--ink-dim)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
              >
                {c.company_name}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
