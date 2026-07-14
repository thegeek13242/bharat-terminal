import { useEffect, useMemo, useState } from 'react';
import { NavSidebar } from '@/components/NavSidebar';
import { DossierModal } from '@/components/DossierModal';
import { TextInput, EmptyState } from '@/components/ds';
import { api } from '@/lib/api';
import { num, dash, DASH } from '@/lib/format';

interface Investor {
  id: string;
  display: string;
  type: string;
  marquee: boolean;
  n_companies: number;
  max_pct: number;
  value_cr: number;
}

interface Position {
  symbol: string;
  company_name: string;
  quarter_end: string;
  holding_pct: number | null;
  prev_pct: number | null;
  delta_pct: number | null;
  change: string;
  shares_held: number | null;
  value_cr: number | null;
}

interface Detail {
  id: string;
  display: string;
  type: string;
  n_companies: number;
  total_value_cr: number | null;
  disclaimer: string;
  positions: Position[];
  recent_deals: Deal[];
}

interface Deal {
  deal_date: string;
  exchange: string;
  deal_type: string;
  symbol: string | null;
  company_name: string | null;
  side: string;
  quantity: number | null;
  value_cr: number | null;
}

const GRID = '120px minmax(160px,1.7fr) 96px 92px 78px 104px';

/** Signed color for a QoQ change, matching the tailwind/headwind vocabulary
    used across the other panels. */
function changeColor(c: string): string {
  if (c === 'add' || c === 'new') return 'var(--tail)';
  if (c === 'trim' || c === 'exit') return 'var(--head)';
  return 'var(--ink-dim)';
}

function crShort(v: number | null): string {
  if (v == null) return DASH;
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`;
  return v.toFixed(0);
}

export function Investors() {
  const [investors, setInvestors] = useState<Investor[]>([]);
  const [marqueeOnly, setMarqueeOnly] = useState(true);
  const [filter, setFilter] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [disclaimer, setDisclaimer] = useState('');
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openSymbol, setOpenSymbol] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .getInvestors({ marquee_only: marqueeOnly, min_companies: marqueeOnly ? 1 : 2 })
      .then((res) => {
        if (cancelled) return;
        setInvestors(res.investors);
        setDisclaimer(res.disclaimer);
        setError(null);
        if (res.investors.length && !res.investors.find((i) => i.id === selected)) {
          setSelected(res.investors[0].id);
        }
      })
      .catch(() => !cancelled && setError('Could not load investors.'))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [marqueeOnly]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    api
      .getInvestorPositions(selected)
      .then((d) => !cancelled && setDetail(d))
      .catch(() => !cancelled && setDetail(null))
      .finally(() => !cancelled && setDetailLoading(false));
    return () => {
      cancelled = true;
    };
  }, [selected]);

  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return f ? investors.filter((i) => i.display.toLowerCase().includes(f)) : investors;
  }, [investors, filter]);

  return (
    <div style={{ height: '100vh', display: 'grid', gridTemplateColumns: 'var(--sidebar-w, 184px) 1fr', overflow: 'hidden' }}>
      <NavSidebar counts={{ investors: investors.length }} staticStatus="INVESTORS · SHP">
        <div style={{ padding: '10px 4px', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.16em', color: 'var(--ink-dim)', marginBottom: 8 }}>
            Smart money
          </div>

          {/* Marquee / All toggle — same chip idiom as Universe filters */}
          <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
            {[
              { k: true, l: 'Marquee' },
              { k: false, l: 'All' },
            ].map((o) => (
              <button
                key={String(o.k)}
                onClick={() => setMarqueeOnly(o.k)}
                className="mono upper"
                style={{
                  fontSize: 10,
                  letterSpacing: '0.08em',
                  padding: '4px 9px',
                  borderRadius: 3,
                  cursor: 'pointer',
                  color: o.k === marqueeOnly ? 'var(--accent)' : 'var(--ink-dim)',
                  background: o.k === marqueeOnly ? 'var(--accent-dim)' : 'transparent',
                  border: `1px solid ${o.k === marqueeOnly ? 'var(--accent-line)' : 'var(--border-hi)'}`,
                }}
              >
                {o.l}
              </button>
            ))}
          </div>

          <TextInput placeholder="Filter investors…" value={filter} onChange={setFilter} />

          {/* Investor list */}
          <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 2 }}>
            {loading && <div className="mono" style={{ fontSize: 10.5, color: 'var(--ink-mute)', padding: '6px 4px' }}>Loading…</div>}
            {!loading && error && <div className="mono" style={{ fontSize: 10.5, color: 'var(--head)', padding: '6px 4px' }}>{error}</div>}
            {!loading && !error && shown.length === 0 && (
              <div className="mono" style={{ fontSize: 10.5, color: 'var(--ink-mute)', padding: '6px 4px' }}>No investors.</div>
            )}
            {shown.map((inv) => {
              const active = inv.id === selected;
              return (
                <button
                  key={inv.id}
                  onClick={() => setSelected(inv.id)}
                  style={{
                    display: 'block',
                    width: '100%',
                    textAlign: 'left',
                    padding: '7px 9px',
                    borderRadius: 4,
                    cursor: 'pointer',
                    background: active ? 'var(--bg-elev-2)' : 'transparent',
                    borderLeft: `2px solid ${active ? 'var(--accent)' : 'transparent'}`,
                  }}
                >
                  <div
                    style={{
                      fontSize: 11.5,
                      color: active ? 'var(--ink)' : inv.marquee ? 'var(--accent)' : 'var(--ink-2)',
                      fontWeight: inv.marquee ? 600 : 400,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {inv.display}
                  </div>
                  <div className="mono upper" style={{ marginTop: 2, fontSize: 9, letterSpacing: '0.05em', color: 'var(--ink-mute)', display: 'flex', gap: 6 }}>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{inv.type.replace(/_/g, ' ')}</span>
                    <span style={{ marginLeft: 'auto', flexShrink: 0 }}>{inv.n_companies} cos</span>
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      </NavSidebar>

      <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
        {/* Header bar — mirrors Surprises: name + mono-upper stat line */}
        <div style={{ padding: '13px 24px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'baseline', gap: 12 }}>
          <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--ink)' }}>{detail?.display ?? 'Smart Money Portfolio'}</span>
          <span className="mono upper" style={{ fontSize: 10, letterSpacing: '0.1em', color: 'var(--ink-mute)' }}>
            {detail
              ? `Holdings as of latest filing · ${detail.n_companies} companies${detail.total_value_cr ? ` · ₹${detail.total_value_cr.toLocaleString()} Cr` : ''}`
              : 'SEBI shareholding disclosures'}
          </span>
        </div>

        {/* Positions table */}
        <div style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
          <div style={{ minWidth: 660 }}>
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
                fontSize: 10,
                letterSpacing: '0.1em',
                color: 'var(--ink-dim)',
                zIndex: 1,
              }}
            >
              <span>Symbol</span>
              <span>Company</span>
              <span style={{ textAlign: 'right' }}>Hold %</span>
              <span style={{ textAlign: 'right' }}>Δ QoQ</span>
              <span style={{ textAlign: 'right' }}>Change</span>
              <span style={{ textAlign: 'right' }}>₹ Cr</span>
            </div>

            {detailLoading && (
              <div className="mono" style={{ padding: 24, fontSize: 11, color: 'var(--ink-mute)' }}>Loading…</div>
            )}
            {!detailLoading && detail && detail.positions.length === 0 && (
              <EmptyState title="No disclosed positions" sub="This investor holds no >1% position on record." fill={false} />
            )}
            {!detailLoading &&
              detail &&
              detail.positions.map((p) => (
                <div
                  key={p.symbol}
                  onClick={() => setOpenSymbol(p.symbol)}
                  className="row-hover"
                  style={{ display: 'grid', gridTemplateColumns: GRID, gap: 14, padding: '12px 24px', borderBottom: '1px solid var(--border)', cursor: 'pointer', alignItems: 'center' }}
                >
                  <span className="mono" style={{ fontSize: 11.5, fontWeight: 600, color: 'var(--ink)' }}>{p.symbol}</span>
                  <span style={{ fontSize: 13, color: 'var(--ink-2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {p.company_name}
                  </span>
                  <span className="num" style={{ fontSize: 11.5, color: 'var(--ink-2)', textAlign: 'right' }}>{num(p.holding_pct, 2)}</span>
                  <span className="num" style={{ fontSize: 11.5, textAlign: 'right', color: changeColor(p.change) }}>
                    {p.delta_pct == null ? DASH : `${p.delta_pct > 0 ? '+' : ''}${p.delta_pct.toFixed(2)}`}
                  </span>
                  <span className="mono upper" style={{ fontSize: 9.5, letterSpacing: '0.06em', textAlign: 'right', color: changeColor(p.change) }}>
                    {dash(p.change)}
                  </span>
                  <span className="num" style={{ fontSize: 11.5, color: 'var(--ink-2)', textAlign: 'right' }}>{crShort(p.value_cr)}</span>
                </div>
              ))}

            {/* Recent bulk/block deals — faster, intra-quarter flow */}
            {!detailLoading && detail && detail.recent_deals && detail.recent_deals.length > 0 && (
              <>
                <div style={{ borderTop: '1px solid var(--border)', paddingTop: 8 }}>
                  <div className="mono upper" style={{ padding: '6px 24px 2px', fontSize: 10, letterSpacing: '0.12em', color: 'var(--ink-dim)' }}>
                    Recent activity · bulk &amp; block deals
                  </div>
                  <div className="mono" style={{ padding: '0 24px 6px', fontSize: 9.5, lineHeight: 1.45, color: 'var(--ink-mute)' }}>
                    Individual disclosed transactions — not positions. A single leg may be one side of a trade whose other side is executed off-market over days and never disclosed; confirm against the next filing.
                  </div>
                </div>
                {detail.recent_deals.slice(0, 25).map((d, i) => (
                  <div
                    key={`${d.symbol ?? d.company_name}-${d.deal_date}-${i}`}
                    onClick={() => d.symbol && setOpenSymbol(d.symbol)}
                    className={d.symbol ? 'row-hover' : undefined}
                    style={{ display: 'grid', gridTemplateColumns: '92px 1fr 54px 90px 78px', gap: 12, padding: '9px 24px', borderBottom: '1px solid var(--border)', alignItems: 'center', cursor: d.symbol ? 'pointer' : 'default' }}
                  >
                    <span className="mono" style={{ fontSize: 10.5, color: 'var(--ink-dim)' }}>{d.deal_date}</span>
                    <span style={{ fontSize: 12, color: 'var(--ink-2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {d.symbol ?? d.company_name}
                    </span>
                    <span className="mono upper" style={{ fontSize: 10, fontWeight: 600, textAlign: 'right', color: d.side === 'BUY' ? 'var(--tail)' : 'var(--head)' }}>{d.side}</span>
                    <span className="mono upper" style={{ fontSize: 9, letterSpacing: '0.04em', textAlign: 'right', color: 'var(--ink-mute)' }}>{d.exchange} {d.deal_type}</span>
                    <span className="num" style={{ fontSize: 11, color: 'var(--ink-2)', textAlign: 'right' }}>{crShort(d.value_cr)}</span>
                  </div>
                ))}
              </>
            )}
          </div>
        </div>

        {/* Disclaimer footer — same treatment as Surprises */}
        <div style={{ padding: '10px 24px', borderTop: '1px solid var(--border)' }}>
          <div className="mono" style={{ fontSize: 10, lineHeight: 1.5, color: 'var(--ink-dim)' }}>
            {detail?.disclaimer ?? disclaimer}
          </div>
        </div>
      </div>

      {openSymbol && <DossierModal symbol={openSymbol} onClose={() => setOpenSymbol(null)} />}
    </div>
  );
}
