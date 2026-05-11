import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ForceGraph3D from 'react-force-graph-3d';
import * as THREE from 'three';
import SpriteText from 'three-spritetext';
import type {
  ImpactGraph3DPayload,
  ImpactGraph3DNode,
  ImpactGraph3DLink,
  TriggerInfo,
} from '@/types';
import { api } from '@/lib/api';

// ── Visual constants ──────────────────────────────────────────────────
// Stratified layout: trigger high, hop-1 mid, hop-2 low. Inside each
// tier nodes are arrayed in a circle so they don't overlap.

const HOP_Y = { 0: 220, 1: 0, 2: -220 } as const;
const HOP_RADIUS = { 0: 0, 1: 180, 2: 360 } as const;

const COLOURS = {
  trigger: '#facc15',          // yellow-400
  hop1_pos: '#22c55e',         // green-500
  hop1_neg: '#ef4444',         // red-500
  hop2_peer: '#60a5fa',        // blue-400  (competitor / sector_peer)
  hop2_chain: '#a78bfa',       // violet-400 (customer / supplier / subsidiary / jv)
  dim: '#374151',              // gray-700 — for non-neighbours when something is hovered
};

function nodeBaseColor(n: ImpactGraph3DNode, kindHint?: string): string {
  if (n.type === 'trigger') return COLOURS.trigger;
  if (n.hop === 1) return (n.beta ?? 0) >= 0 ? COLOURS.hop1_pos : COLOURS.hop1_neg;
  // Hop 2 — colour by the inbound edge kind if we know it
  const peerish = kindHint === 'competitor' || kindHint === 'sector_peer';
  return peerish ? COLOURS.hop2_peer : COLOURS.hop2_chain;
}

function linkBaseColor(l: ImpactGraph3DLink, opacity = 0.75): string {
  if (l.kind === 'sensitivity') {
    const beta = l.beta ?? 0;
    return beta >= 0
      ? `rgba(34, 197, 94, ${opacity})`
      : `rgba(239, 68, 68, ${opacity})`;
  }
  const peerish = l.kind === 'competitor' || l.kind === 'sector_peer';
  return peerish
    ? `rgba(96, 165, 250, ${opacity})`
    : `rgba(167, 139, 250, ${opacity})`;
}

// ── Top control bar ──────────────────────────────────────────────────

function ControlBar({
  trigger, setTrigger, triggers,
  maxHops, setMaxHops,
  minAbsBeta, setMinAbsBeta,
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
          className="bg-gray-900 text-gray-100 px-2 py-1 rounded border border-gray-700 focus:outline-none focus:border-yellow-600 max-w-[260px]"
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

function Legend() {
  return (
    <div className="absolute bottom-3 left-3 bg-gray-950/95 border border-gray-800 rounded px-3 py-2 text-[10px] space-y-1 backdrop-blur-sm">
      <div className="text-yellow-400 uppercase tracking-wider font-bold mb-1">Legend · stratified by hop</div>
      <div className="flex items-center gap-2"><div className="w-3 h-3 rounded-full" style={{ backgroundColor: COLOURS.trigger }} /> Trigger <span className="text-gray-600">(top)</span></div>
      <div className="flex items-center gap-2"><div className="w-3 h-3 rounded-full" style={{ backgroundColor: COLOURS.hop1_pos }} /> 1st-deg β &gt; 0 <span className="text-gray-600">(already priced)</span></div>
      <div className="flex items-center gap-2"><div className="w-3 h-3 rounded-full" style={{ backgroundColor: COLOURS.hop1_neg }} /> 1st-deg β &lt; 0 <span className="text-gray-600">(already priced)</span></div>
      <div className="flex items-center gap-2"><div className="w-3 h-3 rounded-full" style={{ backgroundColor: COLOURS.hop2_peer }} /> 2nd-deg peer/competitor <span className="text-green-300">(actionable)</span></div>
      <div className="flex items-center gap-2"><div className="w-3 h-3 rounded-full" style={{ backgroundColor: COLOURS.hop2_chain }} /> 2nd-deg supply chain <span className="text-green-300">(actionable)</span></div>
      <div className="text-gray-600 mt-1">Click a node to focus &amp; show details. Drag to rotate.</div>
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

// ── Main 3D graph ────────────────────────────────────────────────────

export function ImpactGraph3D() {
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

  // Load graph payload
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

  // ── Pre-position nodes by hop in stratified rings ──────────────────
  // We pin fx/fy/fz so the force simulation only handles micro-jiggle.
  // Trigger sits at the top, hop-1 in a horizontal ring, hop-2 in an
  // outer larger ring below. This makes hop-depth visually unambiguous.
  const graphData = useMemo(() => {
    if (!data) return { nodes: [], links: [] };

    // Group nodes by hop so we can lay them out in a circle within each tier
    const byHop: Record<number, ImpactGraph3DNode[]> = { 0: [], 1: [], 2: [] };
    for (const n of data.nodes) {
      byHop[n.hop as 0 | 1 | 2] = byHop[n.hop as 0 | 1 | 2] ?? [];
      byHop[n.hop as 0 | 1 | 2].push(n);
    }

    // For hop-2 nodes, pick the inbound edge kind so we can color them right
    const inboundKindByTarget: Record<string, string> = {};
    for (const l of data.links) {
      if (l.kind !== 'sensitivity') {
        // Source is hop-1, target is hop-2
        if (!(l.target in inboundKindByTarget)) {
          inboundKindByTarget[l.target as string] = l.kind;
        }
      }
    }

    // Sort hop-1 nodes by |beta| descending so big movers are spaced first
    byHop[1].sort((a, b) => (Math.abs(b.beta ?? 0) - Math.abs(a.beta ?? 0)));

    const positionedNodes = data.nodes.map((n) => {
      const hop = n.hop as 0 | 1 | 2;
      const sibs = byHop[hop];
      const idx = sibs.indexOf(n);
      const count = sibs.length || 1;
      const angle = (idx / count) * 2 * Math.PI;
      const radius = HOP_RADIUS[hop];
      const y = HOP_Y[hop];

      // Build the rendering attrs
      const inboundKind = hop === 2 ? inboundKindByTarget[n.id] : undefined;
      const color = nodeBaseColor(n, inboundKind);
      const val =
        n.type === 'trigger' ? 28
        : hop === 1 ? 10 + Math.abs(n.beta ?? 0) * 14
        : 6;

      return {
        ...n,
        fx: Math.cos(angle) * radius,
        fy: y,
        fz: Math.sin(angle) * radius,
        color,
        baseColor: color,
        val,
      };
    });

    const links = data.links.map((l) => ({
      ...l,
      color: linkBaseColor(l),
      baseColor: linkBaseColor(l),
      width: 1 + Math.min(l.value, 1) * 4,
    }));

    return { nodes: positionedNodes, links };
  }, [data]);

  // ── Highlight set for hover (focus mode) ───────────────────────────
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

  // ── Build the THREE.js group per node: sphere + persistent labels ──
  const buildNodeObject = useCallback(
    (n: any) => {
      const group = new THREE.Group();
      const dim = highlight && !highlight.has(n.id);

      // Sphere
      const radius = n.val ?? 8;
      const sphere = new THREE.Mesh(
        new THREE.SphereGeometry(radius, 16, 16),
        new THREE.MeshLambertMaterial({
          color: dim ? COLOURS.dim : n.color,
          transparent: true,
          opacity: dim ? 0.25 : 1.0,
        }),
      );
      group.add(sphere);

      // Trigger gets a faint glow ring
      if (n.type === 'trigger') {
        const ring = new THREE.Mesh(
          new THREE.RingGeometry(radius + 4, radius + 6, 32),
          new THREE.MeshBasicMaterial({
            color: COLOURS.trigger, transparent: true, opacity: 0.5, side: THREE.DoubleSide,
          }),
        );
        ring.rotation.x = Math.PI / 2;
        group.add(ring);
      }

      // Primary label: symbol / trigger id
      const primary = new SpriteText(n.label);
      primary.color = dim ? '#6b7280' : '#f3f4f6';
      primary.textHeight = n.type === 'trigger' ? 14 : (n.hop === 1 ? 10 : 7);
      primary.fontWeight = '700';
      primary.position.set(0, radius + (n.type === 'trigger' ? 18 : 12), 0);
      primary.material.depthTest = false;
      primary.material.depthWrite = false;
      primary.renderOrder = 999;
      group.add(primary);

      // Secondary label: company name OR β/channel/lag for hop-1
      let subLine = '';
      if (n.type === 'trigger') {
        subLine = 'trigger';
      } else if (n.hop === 1 && n.beta !== undefined) {
        const sign = n.beta >= 0 ? '+' : '';
        const lag = n.lag_days !== undefined ? `·${n.lag_days}d` : '';
        subLine = `β${sign}${n.beta.toFixed(2)} ${n.channel ?? ''} ${lag}`.trim();
      } else if (n.company_name) {
        subLine = String(n.company_name).slice(0, 28);
      }
      if (subLine) {
        const sub = new SpriteText(subLine);
        sub.color = dim ? '#4b5563' : (
          n.type === 'trigger' ? '#fde047' :
          n.hop === 1 ? (n.beta >= 0 ? '#86efac' : '#fca5a5') :
          '#9ca3af'
        );
        sub.textHeight = n.type === 'trigger' ? 7 : 5;
        sub.position.set(0, radius + (n.type === 'trigger' ? 8 : 6), 0);
        sub.material.depthTest = false;
        sub.material.depthWrite = false;
        sub.renderOrder = 998;
        group.add(sub);
      }

      return group;
    },
    [highlight],
  );

  const handleNodeClick = useCallback(
    (n: any) => {
      setSelectedNode(n as ImpactGraph3DNode);
      if (fgRef.current && n.x !== undefined) {
        const distance = 160;
        const distRatio = 1 + distance / Math.max(50, Math.hypot(n.x || 0, n.y || 0, n.z || 0));
        fgRef.current.cameraPosition(
          { x: (n.x || 0) * distRatio, y: (n.y || 0) * distRatio, z: (n.z || 0) * distRatio },
          n,
          900,
        );
      }
    },
    [],
  );

  // After layout, fit the camera so all nodes are visible
  useEffect(() => {
    if (!fgRef.current || !data) return;
    const t = setTimeout(() => {
      try {
        fgRef.current?.zoomToFit(800, 80);
      } catch { /* fg may not be ready */ }
    }, 300);
    return () => clearTimeout(t);
  }, [data]);

  const linkColorFn = useCallback(
    (l: any) => {
      if (!highlight) return l.baseColor;
      const src = typeof l.source === 'object' ? l.source.id : l.source;
      const tgt = typeof l.target === 'object' ? l.target.id : l.target;
      const inFocus = highlight.has(src) && highlight.has(tgt);
      return inFocus ? l.baseColor : 'rgba(75, 85, 99, 0.15)';
    },
    [highlight],
  );

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
        <ForceGraph3D
          ref={fgRef}
          width={dims.w}
          height={dims.h}
          backgroundColor="#000000"
          graphData={graphData as any}
          nodeRelSize={5}
          // Replace default sphere with a labeled group
          nodeThreeObject={buildNodeObject}
          nodeThreeObjectExtend={false}
          linkDirectionalArrowLength={4}
          linkDirectionalArrowRelPos={0.92}
          linkDirectionalArrowResolution={6}
          linkWidth={(l: any) => l.width}
          linkOpacity={0.7}
          linkColor={linkColorFn}
          linkLabel={(l: any) =>
            l.kind === 'sensitivity'
              ? `${l.channel} · β ${l.beta >= 0 ? '+' : ''}${Number(l.beta).toFixed(2)} · lag ${l.lag_days}d`
              : `${l.kind} · strength ${Number(l.strength).toFixed(2)} · ${l.direction}`
          }
          // Cool down quickly — we pinned positions so force only handles micro-jitter
          cooldownTicks={50}
          warmupTicks={20}
          onNodeClick={handleNodeClick}
          onNodeHover={(n: any) => setHoveredId(n?.id ?? null)}
          enableNodeDrag={false}
          showNavInfo={false}
        />
        <Legend />
        <NodePanel node={selectedNode} onClose={() => setSelectedNode(null)} />
      </div>
    </div>
  );
}
