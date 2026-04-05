import { ImpactReport } from '@/types';
import { formatDistanceToNow } from 'date-fns';

const SENTIMENT_COLORS = {
  positive: 'text-green-400 bg-green-900/30',
  negative: 'text-red-400 bg-red-900/30',
  neutral: 'text-gray-400 bg-gray-800/30',
};

const CONVICTION_COLORS = {
  high: 'bg-yellow-500',
  medium: 'bg-blue-500',
  low: 'bg-gray-500',
};

interface Props {
  reports: ImpactReport[];
  onSelectReport: (report: ImpactReport) => void;
  selectedId?: string;
}

export function NewsFeed({ reports, onSelectReport, selectedId }: Props) {
  return (
    <div className="flex flex-col h-full bg-gray-900 border border-gray-700 rounded">
      <div className="px-3 py-2 border-b border-gray-700 flex items-center gap-2">
        <span className="text-xs font-mono text-gray-400 uppercase tracking-widest">Live Feed</span>
        <span className="w-2 h-2 bg-green-500 rounded-full animate-pulse" />
        <span className="text-xs text-gray-500 ml-auto">{reports.length} items</span>
      </div>

      <div className="flex-1 overflow-y-auto">
        {reports.length === 0 && (
          <div className="flex items-center justify-center h-32 text-gray-600 text-sm">
            Awaiting news...
          </div>
        )}

        {reports.map((report) => (
          <div
            key={report.id}
            onClick={() => onSelectReport(report)}
            className={`p-3 border-b border-gray-800 cursor-pointer hover:bg-gray-800/50 transition-colors ${
              selectedId === report.id ? 'bg-gray-800' : ''
            }`}
          >
            <div className="flex items-start gap-2">
              <span className="text-xs text-gray-500 font-mono whitespace-nowrap mt-0.5">
                {report.news_item.source.replace('_', ' ')}
              </span>
              <div className="flex-1 min-w-0">
                <p className="text-sm text-gray-200 leading-snug line-clamp-2">
                  {report.news_item.headline}
                </p>

                {report.company_impacts.slice(0, 3).length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1.5">
                    {report.company_impacts.slice(0, 3).map((impact) => (
                      <span
                        key={impact.symbol}
                        className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs font-mono ${
                          SENTIMENT_COLORS[impact.sentiment]
                        }`}
                      >
                        {impact.symbol}
                        <span className="opacity-75">{'█'.repeat(impact.magnitude)}</span>
                      </span>
                    ))}
                    {report.company_impacts.length > 3 && (
                      <span className="text-xs text-gray-600">
                        +{report.company_impacts.length - 3}
                      </span>
                    )}
                  </div>
                )}

                <div className="flex items-center gap-2 mt-1">
                  <span className="text-xs text-gray-600">
                    {formatDistanceToNow(new Date(report.created_at), { addSuffix: true })}
                  </span>
                  {report.affected_sectors.slice(0, 2).map((s) => (
                    <span key={s} className="text-xs text-blue-400/70 font-mono">
                      {s}
                    </span>
                  ))}
                  {report.trade_signals.length > 0 && (
                    <span
                      className={`ml-auto w-2 h-2 rounded-full ${
                        CONVICTION_COLORS[report.trade_signals[0].conviction]
                      }`}
                      title={`${report.trade_signals.length} signal(s)`}
                    />
                  )}
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
