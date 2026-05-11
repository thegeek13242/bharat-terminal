import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ForceGraph3D from 'react-force-graph-3d';
import type { ImpactGraph3DPayload, ImpactGraph3DNode, ImpactGraph3DLink, TriggerInfo } from '@/types';
import { api } from '@/lib/api';

// ── Colour palette per hop / type ─────────────────────────────────────────

const TRIGGER_COLOR = '#facc15'; // yellow-400
const HOP1_POS = '#22c55e';      // green-500 (positive beta — bullish India)
const HOP1_NEG = '#ef4444';      // red-500   (negative beta — bearish India)
const HOP2_PEER = '#60a5fa';     // blue-400  (sector peer, competitor)
const HOP2_CHAIN = '#a78bfa';    // violet-400 (customer, supplier, subsidiary, jv)

function nodeColor(n: ImpactGraph3DNode): string {
  if (n.type === 'trigger') return TRIGGER_COLOR;
  if (n.hop === 1) {
    const beta = n.beta ?? 0;
    return beta >= 0 ? HOP1_POS : HOP1_NEG;
  }
  return HOP2_PEER;
}

function linkColor(l: ImpactGraph3DLink, opacity = 0.5): string {
  const a = opacity;
  if (l.kind === 'sensitivity') {
    const beta = l.beta ?? 0;
    return beta >= 0
      ? `rgba(34, 197, 94, ${a})`
      : `rgba(239, 68, 68, ${a})`;
  }
  // Inter-company linkages
  const peerish = l.kind === 'competitor' || l.kind === 'sector_peer';
  return peerish ? `rgba(96, 165, 250, ${a})` : `rgba(167, 139, 250, ${a})`;
}

// ── Top control bar ──────────────────────────────────────────────────────

function ControlBar({
  trigger,
  setTrigger,
  triggers,
  maxHops,
  setMaxHops,
  minAbsBeta,
  setMinAbsBeta,
  stats,
}: {
  trigger: string;
  setTrigger: (t: string) => void;
  triggers: TriggerInfo[];
  maxHops: number;
  setMaxHops: (n: number) => void;
  minAbsBeta: number;
  setMinAbsBeta: (v: number) => void;
  stats?: { n_first_degree: number; n_second_degree: number; n_links: number };
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 px-3 py-2 border-b border-gray-800 bg-gray-900/40 text-xs">
      <label className="flex items-center gap-1">
        <span className="text-gray-500 uppercase tracking-wider text-[10px]">Trigger</span>
        <select
          value={trigger}
          onChange={(e) => setTrigger(e.target.value)}
          className="bg-gray-900 text-gray-100 px-2 py-1 rounded border border-gray-700 focus:outline-none focus:border-yellow-600"
        >
          {triggers.map((t) => (
            <option key={t.id} value={t.id}>
              {t.id} ({t.coverage_count})
            </option>
          ))}
        </select>
      </label>

      <label className="flex items-center gap-1">
        <span className="text-gray-500 uppercase tracking-wider text-[10px]">Hops</span>
        {[1, 2, 3].map((n) => (
          <button
            key={n}
            onClick={() => setMaxHops(n)}
            className={`px-2 py-0.5 rounded ${
              maxHops === n
                ? 'bg-yellow-500/20 text-yellow-400 border border-yellow-600/40'
                : 'text-gray-500 hover:text-gray-300'
            }`}
          >
            {n}
          </button>
        ))}
      </label>

      <label className="flex items-center gap-2">
        <span className="text-gray-500 uppercase tracking-wider text-[10px]">|β| min</span>
        <input
          type="range"
          min={0.0}
          max={0.5}
          step={0.05}
          value={minAbsBeta}
          onChange={(e) => setMinAbsBeta(parseFloat(e.target.value))}
          className="w-24 accent-yellow-500"
        />
        <span className="font-mono text-gray-400 w-10">{minAbsBeta.toFixed(2)}</span>
      </label>

      <div className="ml-auto flex items-center gap-3 text-[10px] text-gray-500">
        {stats ? (
          <>
            <span><span className="text-green-400">{stats.n_first_degree}</span> 1st-deg</span>
            <span><span className="text-blue-400">{stats.n_second_degree}</span> 2nd-deg</span>
            <span><span className="text-gray-300">{stats.n_links}</span> edges</span>
          </>
        ) : (
          <span>—</span>
        )}
      </div>
    </div>
  );
}

// ── Legend ───────────────────────────────────────────────────────────────

function Legend() {
  return (
    <div className="absolute bottom-3 left-3 bg-gray-950/90 border border-gray-800 rounded px-3 py-2 text-[10px] space-y-1">
      <div className="text-yellow-400 uppercase tracking-wider font-bold mb-1">Legend</div>
      <div className="flex items-center gap-2"><div className="w-3 h-3 rounded-full" style={{ backgroundColor: TRIGGER_COLOR }} /> trigger</div>
      <div className="flex items-center gap-2"><div className="w-3 h-3 rounded-full" style={{ backgroundColor: HOP1_POS }} /> 1st-deg β &gt; 0 <span className="text-gray-600">(already priced)</span></div>
      <div className="flex items-center gap-2"><div className="w-3 h-3 rounded-full" style={{ backgroundColor: HOP1_NEG }} /> 1st-deg β &lt; 0 <span className="text-gray-600">(already priced)</span></div>
      <div className="flex items-center gap-2"><div className="w-3 h-3 rounded-full" style={{ backgroundColor: HOP2_PEER }} /> 2nd-deg peer/competitor <span className="text-green-300">(actionable)</span></div>
      <div className="flex items-center gap-2"><div className="w-3 h-3 rounded-full" style={{ backgroundColor: HOP2_CHAIN }} /> 2nd-deg supply-chain <span className="text-green-300">(actionable)</span></div>
    </div>
  );
}

// ── Detail panel on selected node ────────────────────────────────────────

function NodePanel({ node, onClose }: { node: ImpactGraph3DNode | null; onClose: () => void }) {
  if (!node) return null;
  return (
    <div className="absolute top-3 right-3 w-72 bg-gray-950/95 border border-yellow-700/40 rounded p-3 text-xs text-gray-200 shadow-xl">
      <div className="flex items-start justify-between">
        <div>
          <div className="text-yellow-400 font-bold tracking-tight">
            {node.type === 'trigger' ? `${node.label} (trigger)` : node.label}
          </div>
          {node.company_name && (
            <div className="text-[10px] text-gray-500">{node.company_name}</div>
          )}
        </div>
        <button onClick={onClose} className="text-gray-500 hover:text-gray-300">×</button>
      </div>
      {node.type === 'company' && (
        <div className="mt-2 space-y-1 text-[11px] font-mono">
          <div>hop <span className="text-gray-400">{node.hop}</span></div>
          {node.beta !== undefined && (
            <div>
              β <span className={node.beta >= 0 ? 'text-green-400' : 'text-red-400'}>
                {node.beta >= 0 ? '+' : ''}{node.beta.toFixed(3)}
              </span>
            </div>
          )}
          {node.channel && (
            <div>channel <span className="text-gray-400">{node.channel}</span></div>
          )}
          {node.lag_days !== undefined && (
            <div>lag <span className="text-gray-400">{node.lag_days}d</span></div>
          )}
          {node.confidence && (
            <div>confidence <span className="text-gray-400">{node.confidence}</span></div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Main 3D graph ────────────────────────────────────────────────────────

export function ImpactGraph3D() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const fgRef = useRef<any>(null);
  const [triggers, setTriggers] = useState<TriggerInfo[]>([]);
  const [trigger, setTrigger] = useState<string>('BRENT');
  const [maxHops, setMaxHops] = useState(2);
  const [minAbsBeta, setMinAbsBeta] = useState(0.1);
  const [data, setData] = useState<ImpactGraph3DPayload | null>(null);
  const [selectedNode, setSelectedNode] = useState<ImpactGraph3DNode | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dims, setDims] = useState({ w: 800, h: 600 });

  // Triggers list
  useEffect(() => {
    api.getDossierTriggers()
      .then((d) => {
        const list = d.items as TriggerInfo[];
        // Default to the trigger with most coverage
        const withCoverage = list.filter((t) => t.coverage_count > 0);
        setTriggers(withCoverage.length > 0 ? withCoverage : list);
        if (withCoverage.length > 0 && !withCoverage.some((t) => t.id === trigger)) {
          setTrigger(withCoverage[0].id);
        }
      })
      .catch(() => setTriggers([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Graph data
  useEffect(() => {
    if (!trigger) return;
    setLoading(true);
    setError(null);
    api.getDossierImpactGraph(trigger, {
      max_hops: maxHops,
      min_abs_beta: minAbsBeta,
      limit_first_degree: 25,
      limit_per_node: 6,
    })
      .then((d) => setData(d as ImpactGraph3DPayload))
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [trigger, maxHops, minAbsBeta]);

  // Track container size for canvas
  useEffect(() => {
    if (!containerRef.current) return;
    const obs = new ResizeObserver(() => {
      if (!containerRef.current) return;
      const r = containerRef.current.getBoundingClientRect();
      setDims({ w: Math.max(400, r.width), h: Math.max(300, r.height) });
    });
    obs.observe(containerRef.current);
    return () => obs.disconnect();
  }, []);

  // Compose graph data for force-graph-3d
  const graphData = useMemo(() => {
    if (!data) return { nodes: [], links: [] };
    return {
      nodes: data.nodes.map((n) => ({
        ...n,
        color: nodeColor(n),
        val: n.type === 'trigger' ? 20 : n.hop === 1 ? 8 + Math.abs(n.beta ?? 0) * 10 : 4,
      })),
      links: data.links.map((l) => ({
        ...l,
        color: linkColor(l),
        // thickness derived from value (beta or strength)
        width: 0.5 + l.value * 4,
      })),
    };
  }, [data]);

  const handleNodeClick = useCallback((n: any) => {
    setSelectedNode(n as ImpactGraph3DNode);
    if (fgRef.current && n.x !== undefined) {
      const distance = 200;
      const distRatio = 1 + distance / Math.hypot(n.x || 0.0001, n.y || 0.0001, n.z || 0.0001);
      fgRef.current.cameraPosition(
        { x: (n.x || 0) * distRatio, y: (n.y || 0) * distRatio, z: (n.z || 0) * distRatio },
        n,
        1000,
      );
    }
  }, []);

  return (
    <div className="h-full flex flex-col bg-gray-950">
      <ControlBar
        trigger={trigger}
        setTrigger={setTrigger}
        triggers={triggers}
        maxHops={maxHops}
        setMaxHops={setMaxHops}
        minAbsBeta={minAbsBeta}
        setMinAbsBeta={setMinAbsBeta}
        stats={data?.stats}
      />
      <div ref={containerRef} className="flex-1 relative bg-black overflow-hidden">
        {error && (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-red-400 z-10">
            {error}
          </div>
        )}
        {loading && (
          <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 text-xs text-yellow-400 bg-gray-900/90 px-3 py-1 rounded border border-yellow-700/40">
            Loading impact graph…
          </div>
        )}
        {!loading && data && data.nodes.length === 0 && (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-gray-500 z-10">
            No dossiers cover this trigger yet. Generate dossiers for NIFTY 50 first.
          </div>
        )}
        <ForceGraph3D
          ref={fgRef}
          width={dims.w}
          height={dims.h}
          backgroundColor="#000000"
          graphData={graphData as any}
          nodeRelSize={5}
          nodeLabel={(n: any) =>
            n.type === 'trigger'
              ? `<div style="font-family:monospace"><b>${n.label}</b><br/>trigger</div>`
              : `<div style="font-family:monospace"><b>${n.label}</b>${n.company_name ? `<br/>${n.company_name}` : ''}${
                  n.beta !== undefined ? `<br/>β = ${n.beta >= 0 ? '+' : ''}${n.beta.toFixed(3)}` : ''
                }${n.channel ? `<br/>${n.channel}` : ''}${n.lag_days !== undefined ? `<br/>lag ${n.lag_days}d` : ''}</div>`
          }
          linkLabel={(l: any) =>
            l.kind === 'sensitivity'
              ? `${l.channel} β=${l.beta >= 0 ? '+' : ''}${Number(l.beta).toFixed(3)} lag=${l.lag_days}d`
              : `${l.kind} strength=${Number(l.strength).toFixed(2)} ${l.direction}`
          }
          linkDirectionalArrowLength={3}
          linkDirectionalArrowRelPos={0.85}
          linkWidth={(l: any) => l.width}
          linkOpacity={0.6}
          linkColor={(l: any) => l.color}
          nodeColor={(n: any) => n.color}
          nodeVal={(n: any) => n.val}
          onNodeClick={handleNodeClick}
          enableNodeDrag={true}
          showNavInfo={false}
        />
        <Legend />
        <NodePanel node={selectedNode} onClose={() => setSelectedNode(null)} />
      </div>
    </div>
  );
}
