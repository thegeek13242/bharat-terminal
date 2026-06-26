import { useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';
import { CompanySummary } from '@/types';
import { NavSidebar } from '@/components/NavSidebar';
import { DossierModal } from '@/components/DossierModal';
import { TextInput } from '@/components/ds';
import { num, dash, DASH } from '@/lib/format';

function Chip({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="mono upper"
      style={{
        fontSize: 9,
        letterSpacing: '0.08em',
        padding: '4px 9px',
        borderRadius: 3,
        cursor: 'pointer',
        color: active ? 'var(--accent)' : 'var(--ink-dim)',
        background: active ? 'var(--accent-dim)' : 'transparent',
        border: `1px solid ${active ? 'var(--accent-line)' : 'var(--border-hi)'}`,
        boxShadow: active ? '0 0 0 1.5px var(--accent)' : 'none',
      }}
    >
      {label}
    </button>
  );
}

const GRID = '128px minmax(170px,1.7fr) 132px 96px 84px 70px';

export function Universe() {
  const [q, setQ] = useState('');
  const [sector, setSector] = useState<string | null>(null);
  const [items, setItems] = useState<CompanySummary[]>([]);
  const [total, setTotal] = useState(0);
  const [sectorOptions, setSectorOptions] = useState<string[]>([]);
  const [coverage, setCoverage] = useState<{ withDossier: number; total: number } | null>(null);
  const [openSymbol, setOpenSymbol] = useState<string | null>(null);
  const debounce = useRef<ReturnType<typeof setTimeout>>();

  // One-time: stable sector chip options + coverage counts.
  useEffect(() => {
    api.listCompanies({ limit: 500 }).then(({ items, total }) => {
      const secs = [...new Set(items.map((i: CompanySummary) => i.sector).filter(Boolean))] as string[];
      setSectorOptions(secs.sort().slice(0, 8));
      setTotal(total);
    });
    api.listCompanies({ has_dossier: true, limit: 1 }).then(({ total: withDossier }) =>
      api.listCompanies({ limit: 1 }).then(({ total }) => setCoverage({ withDossier, total })),
    );
  }, []);

  // Search / sector filter (server-side, debounced on q).
  useEffect(() => {
    clearTimeout(debounce.current);
    debounce.current = setTimeout(() => {
      api
        .listCompanies({ q: q || undefined, sector: sector || undefined, limit: 200 })
        .then(({ items, total }) => {
          setItems(items as CompanySummary[]);
          setTotal(total);
        })
        .catch(() => setItems([]));
    }, q ? 220 : 0);
    return () => clearTimeout(debounce.current);
  }, [q, sector]);

  const displayed = items;

  return (
    <div style={{ height: '100vh', display: 'grid', gridTemplateColumns: 'var(--sidebar-w, 184px) 1fr', overflow: 'hidden' }}>
      <NavSidebar connected statusLabel="CONNECTED · NSE">
        <div style={{ padding: '12px 4px' }}>
          <div className="mono upper" style={{ fontSize: 8.5, letterSpacing: '0.16em', color: 'var(--ink-dim)', marginBottom: 8 }}>
            Coverage
          </div>
          <div className="mono" style={{ fontSize: 10.5, color: 'var(--ink-dim)', lineHeight: 1.6 }}>
            NSE listed
            <br />
            Dossiers pre-computed for{' '}
            <span style={{ color: 'var(--ink-2)' }}>
              {coverage ? `${coverage.withDossier} of ${coverage.total}` : '…'}
            </span>
          </div>
          <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 7 }}>
            <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--accent)' }} />
              <span className="mono" style={{ fontSize: 10, color: 'var(--ink-dim)' }}>Dossier ready</span>
            </span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ width: 7, height: 7, borderRadius: '50%', border: '1.5px solid var(--ink-faint)' }} />
              <span className="mono" style={{ fontSize: 10, color: 'var(--ink-dim)' }}>Not yet generated</span>
            </span>
          </div>
        </div>
      </NavSidebar>

      <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
        {/* Search + counter */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '14px 24px', borderBottom: '1px solid var(--border)' }}>
          <div style={{ flex: 1, maxWidth: 520 }}>
            <TextInput placeholder="Search by symbol or company name…" value={q} onChange={setQ} />
          </div>
          <span className="mono" style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--ink-mute)' }}>
            {displayed.length} / {total} companies
          </span>
        </div>

        {/* Filter chips */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 24px', borderBottom: '1px solid var(--border)', flexWrap: 'wrap' }}>
          <span className="mono upper" style={{ fontSize: 8.5, letterSpacing: '0.1em', color: 'var(--ink-dim)' }}>Sector</span>
          {sectorOptions.map((s) => (
            <Chip key={s} label={s} active={sector === s} onClick={() => setSector(sector === s ? null : s)} />
          ))}
        </div>

        {/* Table */}
        <div style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
          <div style={{ minWidth: 720 }}>
            <div
              className="mono upper"
              style={{
                display: 'grid',
                gridTemplateColumns: GRID,
                gap: 14,
                padding: '10px 24px',
                position: 'sticky',
                top: 0,
                background: 'var(--bg-elev)',
                borderBottom: '1px solid var(--border)',
                fontSize: 8.5,
                letterSpacing: '0.1em',
                color: 'var(--ink-dim)',
                zIndex: 1,
              }}
            >
              <span>Symbol</span>
              <span>Company</span>
              <span>Sector</span>
              <span style={{ textAlign: 'right' }}>LTP (₹)</span>
              <span style={{ textAlign: 'right' }}>1M %</span>
              <span style={{ textAlign: 'right' }}>P/E</span>
            </div>

            {displayed.length === 0 ? (
              <div style={{ padding: '60px 24px', textAlign: 'center', color: 'var(--ink-mute)' }}>
                <div className="mono" style={{ fontSize: 13, color: 'var(--ink-dim)', marginBottom: 8 }}>
                  No companies match these filters
                </div>
                <div style={{ fontSize: 12 }}>Clear the search or filter chips.</div>
              </div>
            ) : (
              displayed.map((c) => (
                <div
                  key={c.symbol}
                  onClick={() => setOpenSymbol(c.symbol)}
                  style={{ display: 'grid', gridTemplateColumns: GRID, gap: 14, padding: '12px 24px', borderBottom: '1px solid var(--border)', cursor: 'pointer', alignItems: 'center' }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-elev-2)')}
                  onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                >
                  <span style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
                    <span
                      style={{
                        width: 7,
                        height: 7,
                        borderRadius: '50%',
                        flexShrink: 0,
                        background: c.dossier_at ? 'var(--accent)' : 'transparent',
                        border: c.dossier_at ? 'none' : '1.5px solid var(--ink-faint)',
                      }}
                    />
                    <span className="mono" style={{ fontSize: 12, fontWeight: 600, color: 'var(--ink)' }}>{c.symbol}</span>
                  </span>
                  <span style={{ fontSize: 12.5, color: 'var(--ink-2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {c.company_name}
                  </span>
                  <span className="mono" style={{ fontSize: 11, color: 'var(--ink-dim)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {dash(c.sector)}
                  </span>
                  <span className="num" style={{ fontSize: 11.5, color: 'var(--ink-2)', textAlign: 'right' }}>{num(c.ltp, 1)}</span>
                  <span
                    className="num"
                    style={{
                      fontSize: 11.5,
                      textAlign: 'right',
                      color:
                        c.chg_1m_pct == null
                          ? 'var(--ink-mute)'
                          : c.chg_1m_pct > 0
                          ? 'var(--tail)'
                          : c.chg_1m_pct < 0
                          ? 'var(--head)'
                          : 'var(--ink-dim)',
                    }}
                  >
                    {c.chg_1m_pct == null ? DASH : `${c.chg_1m_pct > 0 ? '+' : ''}${c.chg_1m_pct.toFixed(1)}%`}
                  </span>
                  <span className="num" style={{ fontSize: 11.5, color: 'var(--ink-2)', textAlign: 'right' }}>{num(c.pe_ratio, 1)}</span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      {openSymbol && <DossierModal symbol={openSymbol} onClose={() => setOpenSymbol(null)} />}
    </div>
  );
}
