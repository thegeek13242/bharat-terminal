import { ImpactReport, TradeSignal } from '@/types';

const DIRECTION_STYLE = {
  long: 'text-green-400 border-green-800',
  short: 'text-red-400 border-red-800',
  neutral: 'text-gray-400 border-gray-700',
};

const CONVICTION_BAR = {
  high: 3,
  medium: 2,
  low: 1,
};

interface Props {
  reports: ImpactReport[];
  convictionFilter: 'all' | 'high' | 'medium' | 'low';
}

export function TradeIdeas({ reports, convictionFilter }: Props) {
  const signals: TradeSignal[] = [];

  for (const report of reports) {
    for (const signal of report.trade_signals) {
      if (convictionFilter !== 'all' && signal.conviction !== convictionFilter) continue;
      signals.push(signal);
    }
  }

  return (
    <div className="flex flex-col h-full bg-gray-900 border border-gray-700 rounded">
      <div className="px-3 py-2 border-b border-gray-700">
        <span className="text-xs font-mono text-gray-400 uppercase tracking-widest">Trade Ideas</span>
        <span className="ml-2 text-xs text-gray-500">{signals.length} signals</span>
      </div>

      <div className="flex-1 overflow-y-auto p-2 space-y-2">
        {signals.length === 0 && (
          <div className="text-center text-gray-600 text-sm py-8">No signals yet</div>
        )}

        {signals.map((signal, idx) => (
          <div
            key={`${signal.symbol}-${idx}`}
            className={`p-3 rounded border ${DIRECTION_STYLE[signal.direction]} bg-gray-800/50`}
          >
            <div className="flex items-center gap-2 mb-2">
              <span className="font-mono font-bold text-sm">{signal.symbol}</span>
              <span className={`text-xs font-mono uppercase ${DIRECTION_STYLE[signal.direction]}`}>
                {signal.direction}
              </span>
              <span className="text-xs text-gray-500">{signal.instrument_type}</span>
              <div className="ml-auto flex gap-0.5">
                {Array.from({ length: CONVICTION_BAR[signal.conviction] }).map((_, i) => (
                  <span key={i} className="w-2 h-2 bg-yellow-500 rounded-sm" />
                ))}
                {Array.from({ length: 3 - CONVICTION_BAR[signal.conviction] }).map((_, i) => (
                  <span key={i} className="w-2 h-2 bg-gray-700 rounded-sm" />
                ))}
              </div>
            </div>

            <div className="flex items-center gap-3 mb-2">
              <div className="flex-1">
                <div className="text-xs text-gray-500 mb-0.5">Position Size</div>
                <div className="flex items-center gap-1">
                  <div className="flex-1 h-1.5 bg-gray-700 rounded overflow-hidden">
                    <div
                      className="h-full bg-blue-500 rounded"
                      style={{ width: `${(signal.position_size_pct_of_portfolio / 10) * 100}%` }}
                    />
                  </div>
                  <span className="text-xs font-mono text-blue-400">
                    {signal.position_size_pct_of_portfolio.toFixed(1)}%
                  </span>
                </div>
              </div>
            </div>

            <p className="text-xs text-gray-400 leading-relaxed">{signal.reasoning}</p>

            <div className="mt-1.5 text-xs text-gray-600">
              Stop: {signal.stop_loss_rationale}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
