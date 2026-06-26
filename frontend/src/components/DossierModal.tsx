import { useEffect, useState } from 'react';
import { CompanyDetail, DossierFull } from '@/types';
import { api, ApiError } from '@/lib/api';
import { SectionHeader, KpiCard, DirectionPill, ReasoningList } from './panel-parts';
import { crore, num, pct, dash, Direction } from '@/lib/format';

function linkDirection(d: string): Direction {
  return d === 'positive' ? 'tailwind' : d === 'negative' ? 'headwind' : 'neutral';
}

export function DossierModal({ symbol, onClose }: { symbol: string; onClose: () => void }) {
  const [dossier, setDossier] = useState<DossierFull | null>(null);
  const [detail, setDetail] = useState<CompanyDetail | null>(null);
  const [state, setState] = useState<'loading' | 'ready' | 'none'>('loading');

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;
    setState('loading');
    api.getCompanyDetail(symbol).then((d) => !cancelled && setDetail(d)).catch(() => {});
    api
      .getDossier(symbol)
      .then((d) => {
        if (cancelled) return;
        setDossier(d);
        setState('ready');
      })
      .catch((e) => {
        if (cancelled) return;
        // 404 == not generated yet (the common ~80% case).
        if (e instanceof ApiError && e.status === 404) setState('none');
        else setState('none');
      });
    return () => {
      cancelled = true;
    };
  }, [symbol]);

  const reasoning = dossier
    ? (dossier.sensitivities ?? [])
        .slice(0, 6)
        .map((s) =>
          `${s.trigger_id}: β ${s.beta > 0 ? '+' : ''}${num(s.beta, 2)} via ${s.channel}${s.rationale ? ` — ${s.rationale}` : ''}`,
        )
    : [];

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 50,
        background: 'rgba(8,11,15,0.72)',
        backdropFilter: 'blur(3px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
        animation: 'backdrop-in 0.2s ease',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="card"
        style={{
          width: '100%',
          maxWidth: 640,
          maxHeight: '88vh',
          overflow: 'auto',
          background: 'var(--bg-elev-2)',
          display: 'flex',
          flexDirection: 'column',
          minHeight: 0,
        }}
      >
        {/* Sticky header */}
        <div
          style={{
            position: 'sticky',
            top: 0,
            background: 'var(--bg-elev-2)',
            borderBottom: '1px solid var(--border)',
            padding: '16px 22px',
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            zIndex: 1,
          }}
        >
          <span className="mono" style={{ fontSize: 18, fontWeight: 600, color: 'var(--ink)' }}>{symbol}</span>
          <span className="mono upper" style={{ fontSize: 9, letterSpacing: '0.14em', color: 'var(--ink-mute)' }}>NSE · EQ</span>
          <span
            className="mono upper"
            style={{ fontSize: 8.5, letterSpacing: '0.14em', color: 'var(--accent)', border: '1px solid var(--accent-line)', borderRadius: 2, padding: '2px 6px' }}
          >
            Dossier
          </span>
          <button
            onClick={onClose}
            style={{ marginLeft: 'auto', background: 'transparent', border: '1px solid var(--border-hi)', color: 'var(--ink-dim)', borderRadius: 4, width: 26, height: 26, cursor: 'pointer', fontSize: 15 }}
          >
            ×
          </button>
        </div>

        {state === 'loading' && (
          <div className="mono upper" style={{ padding: 40, textAlign: 'center', color: 'var(--ink-mute)', fontSize: 11, letterSpacing: '0.1em' }}>
            Loading…
          </div>
        )}

        {state === 'none' && (
          <div style={{ padding: '48px 24px', textAlign: 'center', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 14 }}>
            <span style={{ width: 44, height: 44, border: '1px dashed var(--ink-faint)', borderRadius: 6 }} />
            <div className="mono" style={{ fontSize: 13, color: 'var(--ink-dim)' }}>Dossier not yet generated</div>
            <div style={{ fontSize: 12, color: 'var(--ink-mute)' }}>Queued for analysis · typically &lt; 2 min</div>
          </div>
        )}

        {state === 'ready' && dossier && (
          <div>
            {/* Fundamentals */}
            <SectionHeader style={{ borderTop: 'none' }}>Fundamentals</SectionHeader>
            <div style={{ padding: '14px 24px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              <KpiCard label="Mcap" value={crore(detail?.mcap_cr)} sub="₹ market cap" />
              <KpiCard label="P/E (TTM)" value={num(detail?.pe_ratio, 1)} sub="price / earnings" />
              <KpiCard label="RoE" value={pct(detail?.roe_pct)} sub="return on equity" subColor="var(--tail)" />
              <KpiCard label="Rev TTM" value={crore(detail?.revenue_ttm_cr)} sub="trailing 12M" />
            </div>

            {/* Companies Affected (linkages) */}
            <SectionHeader>Companies Affected · Linkages</SectionHeader>
            {(dossier.linkages ?? []).length === 0 ? (
              <div style={{ padding: '14px 24px', fontSize: 12, color: 'var(--ink-mute)' }}>No linkages recorded.</div>
            ) : (
              (dossier.linkages ?? []).map((l) => (
                <div
                  key={l.target_symbol + l.kind}
                  style={{ display: 'grid', gridTemplateColumns: '88px 1fr 110px', gap: 10, padding: '9px 24px', borderTop: '1px solid var(--border)', alignItems: 'center' }}
                >
                  <span className="mono" style={{ fontSize: 12, color: 'var(--ink)' }}>{l.target_symbol}</span>
                  <span className="mono upper" style={{ fontSize: 9, letterSpacing: '0.08em', color: 'var(--ink-dim)' }}>{dash(l.kind)}</span>
                  <DirectionPill direction={linkDirection(l.direction)} />
                </div>
              ))
            )}

            {/* Reasoning (sensitivities) */}
            <SectionHeader>Reasoning · Sensitivities</SectionHeader>
            <ReasoningList items={reasoning} />

            {/* Dossier meta (honest verdict surrogate — no fabricated buy/sell) */}
            <div style={{ background: 'var(--accent-dim)', borderTop: '1px solid var(--accent-line)', padding: '14px 24px' }}>
              <div className="mono upper" style={{ fontSize: 9, letterSpacing: '0.18em', color: 'var(--accent)' }}>Dossier</div>
              <div className="mono" style={{ fontSize: 11.5, color: 'var(--ink-2)', marginTop: 6 }}>
                v{dossier.version} · quality {num((dossier.data_quality ?? 0) * 100, 0)}% · {dash(dossier.model_used)}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
