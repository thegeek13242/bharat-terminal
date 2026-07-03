import { useEffect, useRef } from 'react';

/**
 * Official TradingView Advanced Chart embed (client-side widget).
 *
 * LICENSING: this is TradingView's sanctioned embed path — the widget loads
 * from TradingView's servers in the user's browser with their attribution
 * link (required by their terms; do not remove it). Never replace this with
 * server-side calls to TradingView's unofficial APIs — that would violate
 * their ToS and recreate the exchange-data exposure this product is
 * deliberately avoiding. Quote freshness (live LTP in market hours, close
 * price off-hours) is handled by TradingView itself.
 */

/** NSE symbol → TradingView symbol code (M&M → M_M, BAJAJ-AUTO → BAJAJ_AUTO). */
function tvSymbol(nseSymbol: string): string {
  return `NSE:${nseSymbol.toUpperCase().replace(/[&-]/g, '_')}`;
}

export function TradingViewChart({ symbol, height = 340 }: { symbol: string; height?: number }) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    container.innerHTML = '';

    const widget = document.createElement('div');
    widget.className = 'tradingview-widget-container__widget';
    widget.style.height = `${height - 24}px`;
    container.appendChild(widget);

    // Required attribution per TradingView widget terms.
    const copyright = document.createElement('div');
    copyright.className = 'tradingview-widget-copyright';
    copyright.innerHTML =
      '<a href="https://www.tradingview.com/" rel="noopener nofollow" target="_blank">' +
      '<span style="color: var(--ink-mute); font-size: 10px;">Chart by TradingView</span></a>';
    container.appendChild(copyright);

    const script = document.createElement('script');
    script.type = 'text/javascript';
    script.src = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js';
    script.async = true;
    script.innerHTML = JSON.stringify({
      symbol: tvSymbol(symbol),
      interval: '15',
      timezone: 'Asia/Kolkata',
      theme: 'dark',
      style: '1',
      locale: 'en',
      backgroundColor: '#10141A',
      gridColor: 'rgba(36, 43, 54, 0.6)',
      hide_top_toolbar: false,
      hide_legend: false,
      allow_symbol_change: false,
      save_image: false,
      withdateranges: true,
      autosize: true,
    });
    container.appendChild(script);

    return () => {
      container.innerHTML = '';
    };
  }, [symbol, height]);

  return (
    <div
      ref={containerRef}
      className="tradingview-widget-container"
      style={{ height, width: '100%' }}
    />
  );
}
