import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ForceGraph2D from 'react-force-graph-2d';
import type {
  ImpactGraph3DPayload,
  ImpactGraph3DNode,
  ImpactGraph3DLink,
  TriggerInfo,
} from '@/types';
import { api } from '@/lib/api';

// ── Layout constants ──────────────────────────────────────────────────
// Radial-tree layout: trigger at origin, hop-1 in a ring at HOP1_R,
// hop-2 extending OUTWARD from its hop-1 parent (so children cluster
// near their parent, making the supply chain visually obvious).
const HOP1_R = 220;
const HOP2_R = 420;
const HOP2_SPREAD = 35; // ± degrees of angular spread for hop-2 children around their parent's angle

const COLOURS = {
  trigger: '#facc15',
  hop1_pos: '#22c55e',
  hop1_neg: '#ef4444',
  hop2_peer: '#60a5fa',
  hop2_chain: '#a78bfa',
  dim: '#374151',
  edge_pos: 'rgba(34, 197, 94, 0.85)',
  edge_neg: 'rgba(239, 68, 68, 0.85)',
  edge_peer: 'rgba(96, 165, 250, 0.6)',
  edge_chain: 'rgba(167, 139, 250, 0.6)',
  edge_dim: 'rgba(75, 85, 99, 0.15)',
};

function nodeBaseColor(n: ImpactGraph3DNode, kindHint?: string): string {
  if (n.type === 'trigger') return COLOURS.trigger;
  if (n.hop === 1) return (n.beta ?? 0) >= 0 ? COLOURS.hop1_pos : COLOURS.hop1_neg;
  const peerish = kindHint === 'competitor' || kindHint === 'sector_peer';
  return peerish ? COLOURS.hop2_peer : COLOURS.hop2_chain;
}

function linkBaseColor(l: ImpactGraph3DLink): string {
  if (l.kind === 'sensitivity') {
    return (l.beta ?? 0) >= 0 ? COLOURS.edge_pos : COLOURS.edge_neg;
  }
  const peerish = l.kind === 'competitor' || l.kind === 'sector_peer';
  return peerish ? COLOURS.edge_peer : COLOURS.edge_chain;
}

// ── Top control bar ───────────────────────────────────────────────────

function ControlBar({
  trigger, setTrigger, triggers,
  maxHops, setMaxHops,
  minAbsBeta, setMinAbsBeta,
  stats,
  onResetView,
}: {
  trigger: string;
  setTrigger: (t: string) => void;
  triggers: TriggerInfo[];
  maxHops: number;
  setMaxHops: (n: number) => void;
  minAbsBeta: number;
  setMinAbsBeta: (v: number) => void;
  stats?: { n_first_degree: number; n_second_degree: number; n_links: number };
  onResetView: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 px-3 py-2 border-b border-gray-800 bg-gray-900/40 text-xs">
      <label className="flex items-center gap-1">
        <span className="text-gray-500 uppercase tracking-wider text-[10px]">Trigger</span>
        <select
          value={trigger}
          onChange={(e) => setTrigger(e.target.value)}
          className="bg-gray-900 text-gray-100 px-2 py-1 rounded border border-gray-700 focus:outline-none focus:border-yellow-600 max-w-[280px]"
        >
          {triggers.map((t) => (
            <option key={t.id} value={t.id}>
              {t.id} · {t.label} ({t.coverage_count})
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
          min={0.0} max={0.5} step={0.05}
          value={minAbsBeta}
          onChange={(e) => setMinAbsBeta(parseFloat(e.target.value))}
          className="w-24 accent-yellow-500"
        />
        <span className="font-mono text-gray-400 w-10">{minAbsBeta.toFixed(2)}</span>
      </label>

      <button
        onClick={onResetView}
        className="px-2 py-0.5 rounded text-gray-500 hover:text-gray-300 text-[10px] uppercase tracking-wider border border-gray-700 hover:border-gray-500"
        title="Re-fit graph to viewport"
      >
        ⌂ fit
      </button>

      <div className="ml-auto flex items-center gap-3 text-[10px] text-gray-500">
        {stats ? (
          <>
            <span><span className="text-green-400">{stats.n_first_degree}</span> 1st-deg</span>
            <span><span className="text-blue-400">{stats.n_second_degree}</span> 2nd-deg</span>
            <span><span className="text-gray-300">{stats.n_links}</span> edges</span>
          </>
        ) : <span>—</span>}
      </div>
    </div>
  );
}

function Legend() {
  return (
    <div className="absolute bottom-3 left-3 bg-gray-950/95 border border-gray-800 rounded px-3 py-2 text-[10px] space-y-1 backdrop-blur-sm">
      <div className="text-yellow-400 uppercase tracking-wider font-bold mb-1">
        Radial · trigger at centre
      </div>
      <div className="flex items-center gap-2"><span className="w-3 h-3 rounded-full" style={{ background: COLOURS.trigger }}/> Trigger</div>
      <div className="flex items-center gap-2"><span className="w-3 h-3 rounded-full" style={{ background: COLOURS.hop1_pos }}/> 1st-deg β &gt; 0 <span className="text-gray-600">(already priced)</span></div>
      <div className="flex items-center gap-2"><span className="w-3 h-3 rounded-full" style={{ background: COLOURS.hop1_neg }}/> 1st-deg β &lt; 0 <span className="text-gray-600">(already priced)</span></div>
      <div className="flex items-center gap-2"><span className="w-3 h-3 rounded-full" style={{ background: COLOURS.hop2_peer }}/> 2nd-deg peer / competitor <span className="text-green-300">(actionable)</span></div>
      <div className="flex items-center gap-2"><span className="w-3 h-3 rounded-full" style={{ background: COLOURS.hop2_chain }}/> 2nd-deg supply chain <span className="text-green-300">(actionable)</span></div>
      <div className="text-gray-600 mt-1">Click a node to focus · scroll to zoom · drag to pan</div>
    </div>
  );
}

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
        <button onClick={onClose} className="text-gray-500 hover:text-gray-300 text-base leading-none">×</button>
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
          {node.channel && <div>channel <span className="text-gray-400">{node.channel}</span></div>}
          {node.lag_days !== undefined && <div>lag <span className="text-gray-400">{node.lag_days}d</span></div>}
          {node.confidence && <div>confidence <span className="text-gray-400">{node.confidence}</span></div>}
        </div>
      )}
    </div>
  );
}

// ── Main 2D graph ─────────────────────────────────────────────────────

export function ImpactGraph() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const fgRef = useRef<any>(null);
  const [triggers, setTriggers] = useState<TriggerInfo[]>([]);
  const [trigger, setTrigger] = useState<string>('BRENT');
  const [maxHops, setMaxHops] = useState(2);
  const [minAbsBeta, setMinAbsBeta] = useState(0.1);
  const [data, setData] = useState<ImpactGraph3DPayload | null>(null);
  const [selectedNode, setSelectedNode] = useState<ImpactGraph3DNode | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dims, setDims] = useState({ w: 800, h: 600 });

  // Load triggers
  useEffect(() => {
    api.getDossierTriggers()
      .then((d) => {
        const list = d.items as TriggerInfo[];
        const withCoverage = list.filter((t) => t.coverage_count > 0);
        const ordered = [...withCoverage, ...list.filter((t) => t.coverage_count === 0)];
        setTriggers(ordered);
        if (withCoverage.length > 0 && !withCoverage.some((t) => t.id === trigger)) {
          setTrigger(withCoverage[0].id);
        }
      })
      .catch(() => setTriggers([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load graph data
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
      .then((d) => {
        setData(d as ImpactGraph3DPayload);
        setSelectedNode(null);
        setHoveredId(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [trigger, maxHops, minAbsBeta]);

  // Track container size
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

  // Radial-tree layout: pin trigger at origin, hop-1 in a ring, hop-2 fanned out from its hop-1 parent.
  const graphData = useMemo(() => {
    if (!data) return { nodes: [], links: [] };

    const parentOf = new Map<string, string>();
    for (const l of data.links) {
      if (l.kind !== 'sensitivity') {
        const src = typeof l.source === 'string' ? l.source : (l.source as any).id;
        const tgt = typeof l.target === 'string' ? l.target : (l.target as any).id;
        if (!parentOf.has(tgt)) parentOf.set(tgt, src);
      }
    }

    const hop1 = data.nodes
      .filter((n) => n.hop === 1)
      .sort((a, b) => Math.abs(b.beta ?? 0) - Math.abs(a.beta ?? 0));
    const hop2 = data.nodes.filter((n) => n.hop === 2);

    const hop1Angle = new Map<string, number>();
    const hop1Count = hop1.length || 1;
    hop1.forEach((n, i) => {
      const angle = (i / hop1Count) * 2 * Math.PI - Math.PI / 2;
      hop1Angle.set(n.id, angle);
    });

    const childrenOfParent = new Map<string, ImpactGraph3DNode[]>();
    for (const n of hop2) {
      const p = parentOf.get(n.id);
      if (!p) continue;
      if (!childrenOfParent.has(p)) childrenOfParent.set(p, []);
      childrenOfParent.get(p)!.push(n);
    }

    const hop2Angle = new Map<string, number>();
    childrenOfParent.forEach((children, parentId) => {
      const base = hop1Angle.get(parentId);
      if (base === undefined) return;
      const spread = (HOP2_SPREAD * Math.PI) / 180;
      const n = children.length;
      children.forEach((child, i) => {
        const offset = n === 1 ? 0 : ((i / (n - 1)) - 0.5) * 2 * spread;
        hop2Angle.set(child.id, base + offset);
      });
    });

    const inboundKindByTarget: Record<string, string> = {};
    for (const l of data.links) {
      if (l.kind !== 'sensitivity') {
        const tgt = typeof l.target === 'string' ? l.target : (l.target as any).id;
        if (!(tgt in inboundKindByTarget)) inboundKindByTarget[tgt] = l.kind;
      }
    }

    const positionedNodes = data.nodes.map((n) => {
      let fx = 0, fy = 0;
      if (n.hop === 1) {
        const a = hop1Angle.get(n.id) ?? 0;
        fx = Math.cos(a) * HOP1_R;
        fy = Math.sin(a) * HOP1_R;
      } else if (n.hop === 2) {
        const a = hop2Angle.get(n.id);
        if (a !== undefined) {
          fx = Math.cos(a) * HOP2_R;
          fy = Math.sin(a) * HOP2_R;
        } else {
          const orphans = hop2.filter((x) => !parentOf.has(x.id));
          const idx = orphans.indexOf(n);
          const a2 = (idx / Math.max(1, orphans.length)) * 2 * Math.PI;
          fx = Math.cos(a2) * HOP2_R;
          fy = Math.sin(a2) * HOP2_R;
        }
      }
      const inboundKind = n.hop === 2 ? inboundKindByTarget[n.id] : undefined;
      const color = nodeBaseColor(n, inboundKind);
      const r =
        n.type === 'trigger' ? 18
        : n.hop === 1 ? 8 + Math.abs(n.beta ?? 0) * 12
        : 6;
      return { ...n, fx, fy, x: fx, y: fy, color, _radius: r };
    });

    const links = data.links.map((l) => ({
      ...l,
      _baseColor: linkBaseColor(l),
      _width: 1 + Math.min(l.value, 1) * 3,
    }));

    return { nodes: positionedNodes, links };
  }, [data]);

  const highlight = useMemo(() => {
    if (!hoveredId || !data) return null;
    const focus = new Set<string>([hoveredId]);
    for (const l of data.links) {
      const src = typeof l.source === 'string' ? l.source : (l.source as any).id;
      const tgt = typeof l.target === 'string' ? l.target : (l.target as any).id;
      if (src === hoveredId) focus.add(tgt);
      if (tgt === hoveredId) focus.add(src);
    }
    return focus;
  }, [hoveredId, data]);

  const drawNode = useCallback(
    (node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const dimmed = highlight && !highlight.has(node.id);
      const r = node._radius;
      const x = node.x ?? 0;
      const y = node.y ?? 0;

      ctx.beginPath();
      ctx.arc(x, y, r, 0, 2 * Math.PI);
      ctx.fillStyle = dimmed ? COLOURS.dim : node.color;
      ctx.globalAlpha = dimmed ? 0.35 : 1.0;
      ctx.fill();

      if (node.id === selectedNode?.id) {
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 2 / globalScale;
        ctx.stroke();
      } else if (node.type === 'trigger') {
        ctx.strokeStyle = '#fde047';
        ctx.lineWidth = 2 / globalScale;
        ctx.stroke();
      }
      ctx.globalAlpha = 1.0;

      const fontPx = node.type === 'trigger' ? 14 : node.hop === 1 ? 11 : 9;
      const px = fontPx / globalScale;
      ctx.font = `700 ${px}px ui-monospace, "JetBrains Mono", "Geist Mono", monospace`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      const text = node.label;
      const m = ctx.measureText(text);
      const pad = 3 / globalScale;
      const lblY = y + r + 3 / globalScale;
      ctx.fillStyle = 'rgba(3, 7, 18, 0.85)';
      ctx.fillRect(
        x - m.width / 2 - pad,
        lblY - pad,
        m.width + pad * 2,
        px + pad * 2,
      );
      ctx.fillStyle = dimmed ? '#6b7280' : '#f3f4f6';
      ctx.fillText(text, x, lblY);

      let sub = '';
      let subColor = '#9ca3af';
      if (node.type === 'trigger') {
        sub = 'trigger';
        subColor = '#fde047';
      } else if (node.hop === 1 && node.beta !== undefined) {
        const sign = node.beta >= 0 ? '+' : '';
        const lag = node.lag_days !== undefined ? ` ·${node.lag_days}d` : '';
        sub = `β${sign}${node.beta.toFixed(2)} ${node.channel ?? ''}${lag}`.trim();
        subColor = node.beta >= 0 ? '#86efac' : '#fca5a5';
      } else if (node.company_name) {
        sub = String(node.company_name).slice(0, 26);
      }
      if (sub) {
        const subPx = (fontPx - 3) / globalScale;
        ctx.font = `${subPx}px ui-monospace, monospace`;
        const sm = ctx.measureText(sub);
        const subY = lblY + px + 3 / globalScale;
        ctx.fillStyle = 'rgba(3, 7, 18, 0.85)';
        ctx.fillRect(
          x - sm.width / 2 - pad,
          subY - pad,
          sm.width + pad * 2,
          subPx + pad * 2,
        );
        ctx.fillStyle = dimmed ? '#4b5563' : subColor;
        ctx.fillText(sub, x, subY);
      }
    },
    [highlight, selectedNode],
  );

  const drawNodePointerArea = useCallback(
    (node: any, color: string, ctx: CanvasRenderingContext2D) => {
      const r = node._radius;
      ctx.fillStyle = color;
      ctx.fillRect(
        (node.x ?? 0) - Math.max(40, r),
        (node.y ?? 0) - r,
        Math.max(80, r * 2),
        r * 2 + 40,
      );
    },
    [],
  );

  const linkColorFn = useCallback(
    (l: any) => {
      if (!highlight) return l._baseColor;
      const src = typeof l.source === 'object' ? l.source.id : l.source;
      const tgt = typeof l.target === 'object' ? l.target.id : l.target;
      return highlight.has(src) && highlight.has(tgt) ? l._baseColor : COLOURS.edge_dim;
    },
    [highlight],
  );

  const handleNodeClick = useCallback((n: any) => {
    setSelectedNode(n as ImpactGraph3DNode);
    if (fgRef.current && n.x !== undefined) {
      fgRef.current.centerAt(n.x, n.y, 600);
      fgRef.current.zoom(2.0, 600);
    }
  }, []);

  const resetView = useCallback(() => {
    fgRef.current?.zoomToFit(600, 70);
  }, []);

  useEffect(() => {
    if (!data) return;
    const t = setTimeout(() => {
      try { fgRef.current?.zoomToFit(600, 70); } catch { /* ready later */ }
    }, 200);
    return () => clearTimeout(t);
  }, [data, dims.w, dims.h]);

  return (
    <div className="h-full flex flex-col bg-gray-950">
      <ControlBar
        trigger={trigger} setTrigger={setTrigger} triggers={triggers}
        maxHops={maxHops} setMaxHops={setMaxHops}
        minAbsBeta={minAbsBeta} setMinAbsBeta={setMinAbsBeta}
        stats={data?.stats}
        onResetView={resetView}
      />
      <div ref={containerRef} className="flex-1 relative bg-black overflow-hidden">
        {error && (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-red-400 z-10 px-4 text-center">
            {error}
          </div>
        )}
        {loading && (
          <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 text-xs text-yellow-400 bg-gray-900/90 px-3 py-1 rounded border border-yellow-700/40">
            Loading impact graph…
          </div>
        )}
        {!loading && data && data.nodes.length <= 1 && (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-gray-500 z-10 px-4 text-center">
            No dossiers cover this trigger yet. Run<br />
            <code className="text-gray-400 mt-1">research-runner dossier --tier nifty50</code>
          </div>
        )}
        <ForceGraph2D
          ref={fgRef}
          width={dims.w}
          height={dims.h}
          backgroundColor="#000000"
          graphData={graphData as any}
          nodeCanvasObject={drawNode}
          nodePointerAreaPaint={drawNodePointerArea}
          nodeCanvasObjectMode={() => 'replace'}
          linkColor={linkColorFn}
          linkWidth={(l: any) => l._width}
          linkDirectionalArrowLength={6}
          linkDirectionalArrowRelPos={0.93}
          linkLabel={(l: any) =>
            l.kind === 'sensitivity'
              ? `${l.channel} · β ${l.beta >= 0 ? '+' : ''}${Number(l.beta).toFixed(2)} · lag ${l.lag_days}d`
              : `${l.kind} · strength ${Number(l.strength).toFixed(2)} · ${l.direction}`
          }
          cooldownTicks={30}
          warmupTicks={10}
          d3VelocityDecay={0.6}
          onNodeClick={handleNodeClick}
          onNodeHover={(n: any) => setHoveredId(n?.id ?? null)}
          enableNodeDrag={false}
        />
        <Legend />
        <NodePanel node={selectedNode} onClose={() => setSelectedNode(null)} />
      </div>
    </div>
  );
}
