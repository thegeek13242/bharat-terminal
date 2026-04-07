import { useState } from 'react';
import { useImpactFeed } from '@/hooks/useImpactFeed';
import { NewsFeed } from '@/components/NewsFeed';
import { CompanyDrilldown } from '@/components/CompanyDrilldown';
import { TradeIdeas } from '@/components/TradeIdeas';
import { ImpactGraph } from '@/components/ImpactGraph';
import { Watchlist } from '@/components/Watchlist';
import { ImpactReport } from '@/types';

type Tab = 'feed' | 'graph' | 'trade' | 'watchlist';
type ConvictionFilter = 'all' | 'high' | 'medium' | 'low';

export default function App() {
  const { reports, wsStatus } = useImpactFeed();
  const [selectedReport, setSelectedReport] = useState<ImpactReport | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>('feed');
  const [convictionFilter, setConvictionFilter] = useState<ConvictionFilter>('all');

  const statusDot: Record<string, string> = {
    connected: 'bg-green-500',
    connecting: 'bg-yellow-500 animate-pulse',
    disconnected: 'bg-red-500',
    error: 'bg-red-500',
  };

  const primarySymbol =
    selectedReport?.company_impacts.find((i) => i.hop_distance === 0)?.symbol ?? null;

  const signalCount = reports.reduce((n, r) => n + r.trade_signals.length, 0);

  return (
    <div className="h-screen bg-gray-950 text-gray-100 flex flex-col font-mono text-sm overflow-hidden">
      {/* ── Header ── */}
      <header className="flex items-center gap-4 px-4 py-2 border-b border-gray-800 bg-gray-900 flex-shrink-0">
        <span className="text-yellow-400 font-bold tracking-tight">₿ BHARAT TERMINAL</span>
        <span className="text-gray-600 text-xs hidden sm:block">Indian Equity Intelligence</span>
        <div className="ml-auto flex items-center gap-3 text-xs">
          <span className={`w-2 h-2 rounded-full ${statusDot[wsStatus] ?? 'bg-gray-600'}`} />
          <span className="text-gray-500 uppercase">{wsStatus}</span>
          <span className="text-gray-600">{reports.length} events</span>
        </div>
      </header>

      {/* ── Tab Bar ── */}
      <nav className="flex border-b border-gray-800 bg-gray-900 flex-shrink-0 overflow-x-auto">
        {(
          [
            ['feed', 'Live Feed'],
            ['graph', 'Impact Graph'],
            ['trade', `Signals${signalCount > 0 ? ` (${signalCount})` : ''}`],
            ['watchlist', 'Watchlist'],
          ] as const
        ).map(([tab, label]) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-4 py-2 text-xs uppercase tracking-wider border-b-2 whitespace-nowrap transition-colors ${
              activeTab === tab
                ? 'border-yellow-400 text-yellow-400'
                : 'border-transparent text-gray-500 hover:text-gray-300'
            }`}
          >
            {label}
          </button>
        ))}

        {activeTab === 'trade' && (
          <div className="ml-auto flex items-center gap-1 px-3">
            {(['all', 'high', 'medium', 'low'] as const).map((f) => (
              <button
                key={f}
                onClick={() => setConvictionFilter(f)}
                className={`px-2 py-0.5 rounded text-xs capitalize ${
                  convictionFilter === f
                    ? 'bg-yellow-500/20 text-yellow-400 border border-yellow-600/40'
                    : 'text-gray-500 hover:text-gray-300'
                }`}
              >
                {f}
              </button>
            ))}
          </div>
        )}
      </nav>

      {/* ── Main Content ── */}
      <main className="flex-1 overflow-hidden p-2 sm:p-3">
        {/* Feed + Drilldown */}
        {activeTab === 'feed' && (
          <div className="h-full grid grid-cols-5 gap-3">
            <div className="col-span-3 min-h-0">
              <NewsFeed
                reports={reports}
                onSelectReport={(r) => setSelectedReport(r)}
                selectedId={selectedReport?.id}
              />
            </div>
            <div className="col-span-2 min-h-0">
              <CompanyDrilldown selectedReport={selectedReport} />
            </div>
          </div>
        )}

        {/* Impact Graph */}
        {activeTab === 'graph' && (
          <div className="h-full grid grid-cols-5 gap-3">
            <div className="col-span-2 min-h-0">
              <NewsFeed
                reports={reports}
                onSelectReport={(r) => setSelectedReport(r)}
                selectedId={selectedReport?.id}
              />
            </div>
            <div className="col-span-3 min-h-0">
              <ImpactGraph symbol={primarySymbol} />
            </div>
          </div>
        )}

        {/* Trade Signals */}
        {activeTab === 'trade' && (
          <div className="h-full grid grid-cols-5 gap-3">
            <div className="col-span-2 min-h-0">
              <NewsFeed
                reports={reports}
                onSelectReport={(r) => setSelectedReport(r)}
                selectedId={selectedReport?.id}
              />
            </div>
            <div className="col-span-3 min-h-0">
              <TradeIdeas reports={reports} convictionFilter={convictionFilter} />
            </div>
          </div>
        )}

        {/* Watchlist */}
        {activeTab === 'watchlist' && (
          <div className="h-full grid grid-cols-5 gap-3">
            <div className="col-span-2 min-h-0">
              <NewsFeed
                reports={reports}
                onSelectReport={(r) => setSelectedReport(r)}
                selectedId={selectedReport?.id}
              />
            </div>
            <div className="col-span-3 min-h-0">
              <Watchlist />
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
