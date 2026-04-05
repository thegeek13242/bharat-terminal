import { useEffect, useState } from 'react';
import { api } from '@/lib/api';

interface WatchlistItem {
  symbol: string;
  price_alert?: number;
  impact_threshold: number;
}

export function Watchlist() {
  const [items, setItems] = useState<WatchlistItem[]>([]);
  const [newSymbol, setNewSymbol] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.getWatchlist().then((data) => setItems(data.items ?? []));
  }, []);

  const addSymbol = async () => {
    const sym = newSymbol.trim().toUpperCase();
    if (!sym || items.some((i) => i.symbol === sym)) return;
    const updated = [...items, { symbol: sym, impact_threshold: 3 }];
    setItems(updated);
    setNewSymbol('');
    setSaving(true);
    await api.saveWatchlist(updated).finally(() => setSaving(false));
  };

  const removeSymbol = async (symbol: string) => {
    const updated = items.filter((i) => i.symbol !== symbol);
    setItems(updated);
    setSaving(true);
    await api.saveWatchlist(updated).finally(() => setSaving(false));
  };

  return (
    <div className="flex flex-col h-full bg-gray-900 border border-gray-700 rounded">
      <div className="px-3 py-2 border-b border-gray-700 flex items-center gap-2">
        <span className="text-xs font-mono text-gray-400 uppercase tracking-widest">Watchlist</span>
        {saving && <span className="text-xs text-yellow-400 ml-auto">Saving...</span>}
      </div>

      <div className="flex gap-2 p-2 border-b border-gray-800">
        <input
          type="text"
          value={newSymbol}
          onChange={(e) => setNewSymbol(e.target.value.toUpperCase())}
          onKeyDown={(e) => e.key === 'Enter' && addSymbol()}
          placeholder="Add symbol (e.g. RELIANCE)"
          className="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200 font-mono placeholder-gray-600 focus:outline-none focus:border-blue-500"
        />
        <button
          onClick={addSymbol}
          className="px-3 py-1 bg-blue-600 hover:bg-blue-500 rounded text-xs font-mono text-white transition-colors"
        >
          Add
        </button>
      </div>

      <div className="flex-1 overflow-y-auto">
        {items.length === 0 && (
          <div className="text-center text-gray-600 text-sm py-8">No symbols watched</div>
        )}
        {items.map((item) => (
          <div
            key={item.symbol}
            className="flex items-center gap-2 px-3 py-2 border-b border-gray-800 hover:bg-gray-800/50"
          >
            <span className="font-mono text-sm text-white flex-1">{item.symbol}</span>
            <span className="text-xs text-gray-500">
              Alert ≥ {item.impact_threshold}
            </span>
            <button
              onClick={() => removeSymbol(item.symbol)}
              className="text-gray-600 hover:text-red-400 text-xs ml-2 transition-colors"
            >
              ✕
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
