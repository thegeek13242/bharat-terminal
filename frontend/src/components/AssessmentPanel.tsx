import { useEffect, useState } from 'react';
import { CompanyDetail, ImpactReport } from '@/types';
import { api } from '@/lib/api';
import { EmptyState } from './ds';
import { SectionHeader, KpiCard, DirectionPill, ReasoningList } from './panel-parts';
import { useInternalLtp } from '@/hooks/useInternalLtp';
import { Hi, extractPricedIn } from '@/lib/richtext';
import {
  primaryImpact,
  sentimentToDirection,
  horizonLabel,
  crore,
  num,
  pct,
  dash,
} from '@/lib/format';

const SENTIMENT_DOT: Record<string, string> = {
  positive: 'var(--tail)',
  negative: 'var(--head)',
  neutral: 'var(--neutral)',
};
const SENTIMENT_WORD: Record<string, string> = {
  positive: 'Positive',
  negative: 'Negative',
  neutral: 'Neutral',
};

interface LtpWs {
  send: (data: object) => void;
  lastMessage: any;
  connected: boolean;
}

export function AssessmentPanel({ report, ltpWs }: { report: ImpactReport | null; ltpWs?: LtpWs }) {
  const [detail, setDetail] = useState<CompanyDetail | null>(null);
  const imp = report ? primaryImpact(report) : undefined;
  const symbol = imp?.symbol;
  // Renders only for internal-allowlist accounts (endpoint 403s otherwise).
  const ltp = useInternalLtp(symbol);

  // Live tick stream over the existing feed WS (internal accounts only —
  // the server silently ignores subscribe_ltp from anyone else). The REST
  // hook stays as bootstrap + EOD fallback; a fresh tick wins the display.
  const [tick, setTick] = useState<{ ltp: number; at: number } | null>(null);
  const internalOk = ltp != null; // REST succeeded → account is internal
  const ltpSend = ltpWs?.send;
  const ltpConnected = ltpWs?.connected ?? false;

  useEffect(() => {
    setTick(null);
  }, [symbol]);

  useEffect(() => {
    if (!ltpSend || !ltpConnected || !symbol || !internalOk) return;
    ltpSend({ action: 'subscribe_ltp', symbol });
    return () => ltpSend({ action: 'unsubscribe_ltp' });
  }, [ltpSend, ltpConnected, symbol, internalOk]);

  useEffect(() => {
    const m = ltpWs?.lastMessage;
    if (m?.type === 'ltp' && m.symbol === symbol && typeof m.ltp === 'number') {
      setTick({ ltp: m.ltp, at: Date.now() });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ltpWs?.lastMessage, symbol]);

  // Prefer a recent live tick; fall back to the REST value between ticks.
  const liveLtp = tick && Date.now() - tick.at < 15000 ? tick.ltp : ltp?.ltp;

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    if (!symbol) return;
    api
      .getCompanyDetail(symbol)
      .then((d) => !cancelled && setDetail(d))
      .catch(() => !cancelled && setDetail(null));
    return () => {
      cancelled = true;
    };
  }, [symbol]);

  if (!report || !imp) {
    return <EmptyState title="Select an event" sub="Its structured assessment appears here." />;
  }

  // One entry per distinct explanation, attributed to its company. Legacy
  // propagated impacts (pre linkage-aware Stage 4) duplicated the source
  // company's text verbatim — the dedupe collapses those to a single entry.
  // The priced-in bracket tag is lifted out of the prose into a warning pill.
  const seenExplanations = new Set<string>();
  const reasoning = (report.company_impacts ?? [])
    .filter((c) => {
      if (!c.explanation || seenExplanations.has(c.explanation)) return false;
      seenExplanations.add(c.explanation);
      return true;
    })
    .slice(0, 6)
    .map((c) => {
      const { clean, pricedIn } = extractPricedIn(c.explanation || '');
      return {
        symbol: c.symbol,
        tag: c.hop_distance > 0 ? `indirect · hop ${c.hop_distance}` : undefined,
        pricedIn,
        text: clean,
      };
    });

  return (
    <div style={{ height: '100%', overflow: 'auto', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      {/* Company header */}
      <div style={{ background: 'var(--bg-elev)', padding: '18px 24px', borderBottom: '1px solid var(--border)' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <span className="mono" style={{ fontSize: 18, fontWeight: 600, color: 'var(--ink)' }}>{imp.symbol}</span>
          <span className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-mute)' }}>NSE · EQ</span>
          {ltp && (liveLtp != null ? (
            <span className="mono" style={{ fontSize: 11.5, color: 'var(--accent)' }}>
              ₹{num(liveLtp, 2)} <span style={{ fontSize: 9, color: 'var(--ink-dim)' }}>LTP</span>
            </span>
          ) : ltp.prev_close != null ? (
            <span className="mono" style={{ fontSize: 11.5, color: 'var(--ink-dim)' }}>
              ₹{num(ltp.prev_close, 2)} <span style={{ fontSize: 9, color: 'var(--ink-mute)' }}>EOD</span>
            </span>
          ) : null)}
          <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 7 }}>
            <span style={{ width: 7, height: 7, borderRadius: '50%', background: SENTIMENT_DOT[imp.sentiment] }} />
            <span className="mono upper" style={{ fontSize: 10, letterSpacing: '0.1em', color: SENTIMENT_DOT[imp.sentiment] }}>
              {SENTIMENT_WORD[imp.sentiment]}
            </span>
          </span>
        </div>
        <div className="mono" style={{ fontSize: 11.5, color: 'var(--ink-dim)', marginTop: 6 }}>
          {dash(imp.company_name || detail?.company_name)} · {dash(detail?.sector)}
        </div>
        <div
          style={{
            marginTop: 14,
            borderLeft: '2px solid var(--accent-line)',
            paddingLeft: 12,
            fontFamily: 'var(--sans)',
            fontSize: 13,
            lineHeight: 1.5,
            color: 'var(--ink-2)',
          }}
        >
          {report.news_item?.headline}
        </div>
        {report.news_item?.summary && (
          <div style={{ marginTop: 12 }}>
            <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.12em', color: 'var(--ink-mute)', marginBottom: 5 }}>
              Filing summary
            </div>
            <div style={{ fontFamily: 'var(--sans)', fontSize: 12.5, lineHeight: 1.55, color: 'var(--ink-dim)' }}>
              <Hi text={report.news_item.summary} />
            </div>
          </div>
        )}
      </div>

      {/* Fundamentals */}
      <SectionHeader style={{ borderTop: 'none' }}>Fundamentals</SectionHeader>
      <div style={{ padding: '14px 24px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
        <KpiCard label="Mcap" value={crore(detail?.mcap_cr)} sub="₹ market cap" />
        <KpiCard label="P/E (TTM)" value={num(detail?.pe_ratio, 1)} sub="price / earnings" />
        <KpiCard label="RoE" value={pct(detail?.roe_pct)} sub="return on equity" subColor="var(--tail)" />
        <KpiCard label="Rev TTM" value={crore(detail?.revenue_ttm_cr)} sub="trailing 12M" />
      </div>

      {/* Companies Affected */}
      <SectionHeader>Companies Affected</SectionHeader>
      <div
        className="mono upper"
        style={{
          display: 'grid',
          gridTemplateColumns: '88px 1fr 110px 64px',
          gap: 10,
          padding: '7px 24px',
          fontSize: 10,
          letterSpacing: '0.08em',
          color: 'var(--ink-dim)',
        }}
      >
        <span>Symbol</span>
        <span>Affected lines</span>
        <span>Direction</span>
        <span style={{ textAlign: 'right' }}>Horizon</span>
      </div>
      {(report.company_impacts ?? []).map((c) => (
        <div
          key={c.symbol}
          style={{
            display: 'grid',
            gridTemplateColumns: '88px 1fr 110px 64px',
            gap: 10,
            padding: '9px 24px',
            borderTop: '1px solid var(--border)',
            alignItems: 'center',
          }}
        >
          <span className="mono" style={{ fontSize: 11.5, color: 'var(--ink)' }}>{c.symbol}</span>
          <span style={{ fontSize: 11.5, color: 'var(--ink-dim)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {(c.affected_line_items ?? []).join(', ') || dash(null)}
          </span>
          <DirectionPill direction={sentimentToDirection(c.sentiment)} />
          <span className="num" style={{ fontSize: 11.5, color: 'var(--ink-dim)', textAlign: 'right' }}>
            {horizonLabel(c.time_horizon)}
          </span>
        </div>
      ))}

      {/* Reasoning */}
      <SectionHeader>Reasoning</SectionHeader>
      <ReasoningList items={reasoning} />

      <div style={{ flex: 1 }} />
    </div>
  );
}
