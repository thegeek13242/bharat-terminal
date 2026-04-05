import { useEffect, useState } from 'react';
import { GraphNode, GraphEdge } from '@/types';
import { api } from '@/lib/api';

interface Props {
  symbol: string | null;
}

export function ImpactGraph({ symbol }: Props) {
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!symbol) return;
    setLoading(true);
    setError(null);

    api
      .getGraph(symbol)
      .then((data) => {
        setNodes(data.nodes ?? []);
        setEdges(data.edges ?? []);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [symbol]);

  if (!symbol) {
    return (
      <div className="flex items-center justify-center h-full text-gray-600 text-sm">
        Select a company to view its relationship graph
      </div>
    );
  }

  if (loading) {
    return <div className="p-4 text-gray-400 text-sm animate-pulse">Loading graph...</div>;
  }

  if (error) {
    return <div className="p-4 text-red-400 text-sm">Error: {error}</div>;
  }

  return (
    <div className="flex flex-col h-full bg-gray-900 border border-gray-700 rounded">
      <div className="px-3 py-2 border-b border-gray-700">
        <span className="text-xs font-mono text-gray-400 uppercase tracking-widest">
          Impact Graph — {symbol}
        </span>
        <span className="ml-2 text-xs text-gray-500">
          {nodes.length} nodes · {edges.length} edges
        </span>
      </div>
      <div className="flex-1 overflow-auto p-3">
        {/* Simple table-based fallback — replace with Sigma.js renderer if desired */}
        <div className="space-y-1">
          {edges.map((edge, i) => (
            <div key={i} className="flex items-center gap-2 text-xs font-mono">
              <span className="text-blue-400">{edge.source_symbol}</span>
              <span className="text-gray-600">→</span>
              <span className="text-green-400">{edge.target_symbol}</span>
              <span className="text-gray-500 ml-2">{edge.relationship_type}</span>
              <span className="text-gray-600 ml-auto">hop {edge.hop}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
