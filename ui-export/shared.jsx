// Helpers + shared components
const { useState, useEffect, useRef, useMemo, useCallback, createContext, useContext, Fragment } = React;

const fmt = {
  latency: (v) => v == null ? '—' : `${v.toFixed(1)}`,
  loss: (v) => v == null ? '—' : `${Math.round(v)}`,
  bw: (kbps) => {
    if (kbps == null) return '—';
    if (kbps >= 1_000_000) return (kbps / 1_000_000).toFixed(1) + ' Gbps';
    if (kbps >= 1_000) return (kbps / 1_000).toFixed(1) + ' Mbps';
    return kbps + ' kbps';
  },
  price: (v) => v == null ? '—' : v.toFixed(3),
  rep: (v) => v == null ? '—' : v.toFixed(2),
  gb: (v) => v == null ? '—' : v.toFixed(0),
  days: (v) => v == null ? '—' : `${v}d`,
};

function repClass(r) {
  if (r == null) return 'unknown';
  if (r >= 0.88) return 'high';
  if (r >= 0.80) return 'med';
  if (r >= 0.72) return 'low';
  return 'bad';
}

function asByName(id) { return window.PM_DATA.ASES.find(a => a.id === id); }
function asShortName(id) { const a = asByName(id); return a ? a.name : id; }

const PMContext = createContext(null);
const usePM = () => useContext(PMContext);

// Given a path and claimed SLA ids, figure out which AS indices each SLA spans in the path,
// and which hop indices are covered.
function placeSla(path, sla) {
  // find contiguous run of path.hops matching sla.segment exactly
  const n = path.hops.length, m = sla.segment.length;
  for (let i = 0; i + m <= n; i++) {
    let ok = true;
    for (let j = 0; j < m; j++) if (path.hops[i+j] !== sla.segment[j]) { ok = false; break; }
    if (ok) return { startIdx: i, endIdx: i + m - 1 }; // indices in path.hops
  }
  return null;
}

function computeAggregate(path, claimedSlaIds, slaById, hopStatus) {
  const totalHops = path.hops.length - 1;
  const placements = [];
  const hopCoveredBy = {}; // hopIndex -> slaId
  for (const id of claimedSlaIds) {
    const sla = slaById[id];
    if (!sla) continue;
    const p = placeSla(path, sla);
    if (!p) continue;
    placements.push({ sla, ...p });
    for (let h = p.startIdx; h < p.endIdx; h++) hopCoveredBy[h] = id;
  }

  const coveredHopCount = Object.keys(hopCoveredBy).length;
  const allCovered = coveredHopCount === totalHops;
  const noneCovered = coveredHopCount === 0;

  if (noneCovered) {
    return { placements, hopCoveredBy, latency: null, loss: null, bw: null, price: null, rep: null, coverage: 'none', coveredCount: 0, totalHops };
  }

  // Cover set used: unique SLAs that actually cover the path
  const usedSlas = placements; // all claimed ones that placed
  const covered = usedSlas;
  let latency = 0, price = 0;
  let survive = 1;
  let bw = Infinity, rep = Infinity;
  for (const { sla } of covered) {
    latency += sla.latency;
    price += sla.price;
    survive *= (1 - sla.loss / 1_000_000);
    bw = Math.min(bw, sla.bw);
    rep = Math.min(rep, sla.rep);
  }
  const warnPenalty = Object.values(hopStatus || {}).filter(s => s === 'warn').length * 0.08;
  rep = Math.max(0, rep - warnPenalty);

  const loss_ppm = (1 - survive) * 1_000_000;

  return {
    placements, hopCoveredBy,
    latency, loss: loss_ppm, bw, price,
    rep: allCovered ? rep : null, // partial coverage => reputation is unknown
    coverage: allCovered ? 'full' : 'partial',
    coveredCount: coveredHopCount,
    totalHops,
  };
}

// Smoothly interpolates when value changes
function TickingNumber({ value, format = (v) => v?.toFixed(2), duration = 500, className = '', unit = null, placeholder = '—' }) {
  const [display, setDisplay] = useState(value);
  const lastRef = useRef(value);
  useEffect(() => {
    if (value == null) { setDisplay(null); lastRef.current = null; return; }
    const from = lastRef.current ?? value;
    const to = value;
    if (from === to) { setDisplay(to); return; }
    const start = performance.now();
    let raf;
    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      const v = from + (to - from) * eased;
      setDisplay(v);
      if (t < 1) raf = requestAnimationFrame(step);
      else lastRef.current = to;
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value, duration]);

  if (display == null) return <span className={className}>{placeholder}{unit && <span className="unit"> {unit}</span>}</span>;
  return <span className={className}>{format(display)}{unit && <span className="unit"> {unit}</span>}</span>;
}

// ======== PathGraph — renders SLA arcs as continuous spans with midpoint label ========
// Layout: node-hop-node-hop-…-node using flex. Arcs are absolutely positioned overlays
// running from startIdx node center to endIdx node center.
function PathGraph({ path, claimedSlaIds, slaById, hopStatus, onArcClick, onArcHover, onHopClick, onHopHover, onHopLeave, flowing = true, compact = false, selected = false }) {
  const containerRef = useRef(null);
  const nodeRefs = useRef([]);
  const [positions, setPositions] = useState([]);

  const placements = useMemo(() => {
    const out = [];
    for (const id of claimedSlaIds) {
      const sla = slaById[id];
      if (!sla) continue;
      const p = placeSla(path, sla);
      if (p) out.push({ sla, ...p });
    }
    // assign a vertical slot to each placement so overlaps stack
    // slot 0 = baseline (on the line), slot 1 = above, slot -1 = below
    const byStart = [...out].sort((a, b) => a.startIdx - b.startIdx);
    const slots = [];
    for (const pl of byStart) {
      let slot = 0;
      const taken = new Set();
      for (const existing of slots) {
        const overlap = !(pl.endIdx <= existing.startIdx || pl.startIdx >= existing.endIdx);
        if (overlap) taken.add(existing.slot);
      }
      // pick lowest slot in order: 0, 1, -1, 2, -2
      const order = [0, 1, -1, 2, -2];
      slot = order.find(s => !taken.has(s)) ?? 0;
      slots.push({ ...pl, slot });
    }
    return slots;
  }, [claimedSlaIds, slaById, path]);

  const hopCoveredBy = {};
  for (const p of placements) {
    for (let h = p.startIdx; h < p.endIdx; h++) hopCoveredBy[h] = p.sla.id;
  }

  useEffect(() => {
    const upd = () => {
      if (!containerRef.current) return;
      const crect = containerRef.current.getBoundingClientRect();
      const pos = nodeRefs.current.map(el => {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { x: r.left - crect.left + r.width / 2, y: r.top - crect.top + r.height / 2 };
      });
      setPositions(pos);
    };
    upd();
    const ro = new ResizeObserver(upd);
    if (containerRef.current) ro.observe(containerRef.current);
    window.addEventListener('resize', upd);
    return () => { ro.disconnect(); window.removeEventListener('resize', upd); };
  }, [path, compact]);

  return (
    <div className={`path-graph${compact ? ' compact' : ''}`} ref={containerRef}>
      {/* SVG arc layer */}
      <svg className="arc-layer" width="100%" height="100%" preserveAspectRatio="none">
        {placements.map((pl, i) => {
          const a = positions[pl.startIdx];
          const b = positions[pl.endIdx];
          if (!a || !b) return null;
          const slotOffset = pl.slot * 10; // px vertical offset per slot
          const baseY = a.y + slotOffset;
          // status: does any covered hop have bad/warn?
          let status = 'normal';
          for (let h = pl.startIdx; h < pl.endIdx; h++) {
            if (hopStatus && hopStatus[h] === 'bad') { status = 'bad'; break; }
            if (hopStatus && hopStatus[h] === 'warn') status = 'warn';
          }
          const midX = (a.x + b.x) / 2;
          const midY = baseY;
          const shade = pl.slot >= 0 ? 1 : 0.88; // subtle shade variation
          const color = status === 'bad' ? '#e35d5d' : status === 'warn' ? '#e89c4a' : (pl.slot > 0 ? '#e8b95a' : '#d4a74c');
          return (
            <g key={pl.sla.id + '-' + i}
               className={`arc-group${status !== 'normal' ? ' status-' + status : ''}`}
               onClick={(e) => { e.stopPropagation(); onArcClick && onArcClick(pl, e); }}
               onMouseEnter={(e) => onArcHover && onArcHover(pl, e)}
               onMouseLeave={(e) => onArcHover && onArcHover(null, e)}
               style={{ cursor: onArcClick ? 'pointer' : 'default' }}>
              {/* line */}
              <line x1={a.x} y1={baseY} x2={b.x} y2={baseY}
                    stroke={color} strokeWidth={status !== 'normal' ? 3.5 : 3}
                    strokeLinecap="round" opacity={shade}
                    style={{ filter: `drop-shadow(0 0 6px ${color}66)` }} />
              {/* end caps as small notches anchored to nodes */}
              {pl.slot !== 0 && (
                <>
                  <line x1={a.x} y1={a.y} x2={a.x} y2={baseY} stroke={color} strokeWidth="2" opacity={shade * 0.7} />
                  <line x1={b.x} y1={b.y} x2={b.x} y2={baseY} stroke={color} strokeWidth="2" opacity={shade * 0.7} />
                </>
              )}
              {/* flow pulse for selected path */}
              {flowing && status === 'normal' && (
                <circle r="3.5" fill={color}>
                  <animate attributeName="cx" from={a.x} to={b.x} dur="2s" repeatCount="indefinite" />
                  <animate attributeName="cy" from={baseY} to={baseY} dur="2s" repeatCount="indefinite" />
                  <animate attributeName="opacity" values="0;1;1;0" dur="2s" repeatCount="indefinite" />
                </circle>
              )}
              {/* midpoint label */}
              {!compact && (
                <g transform={`translate(${midX}, ${midY - 14})`}>
                  <rect x={-36} y={-10} width={72} height={20} rx={4}
                        fill="#15110a" stroke={color} strokeOpacity="0.5" />
                  <text x={0} y={4} textAnchor="middle"
                        fontFamily="var(--mono)" fontSize="10"
                        fill={color} fontWeight="600">
                    {pl.sla.id} · {pl.sla.cosigners.length}✓
                  </text>
                </g>
              )}
            </g>
          );
        })}
      </svg>

      {/* Nodes + hop hit areas */}
      <div className="path-row">
        {path.hops.map((asId, i) => {
          const as = asByName(asId);
          const isSelf = asId === window.PM_DATA.ME;
          const isDest = i === path.hops.length - 1;
          return (
            <Fragment key={i}>
              <div ref={el => nodeRefs.current[i] = el} className={`node${isSelf ? ' self' : ''}${isDest ? ' dest' : ''}`}>
                <div className="dot" />
                {!compact && (
                  <div className="label">
                    <div><span className="name">{as ? as.name : asId}</span></div>
                    <div>{asId}</div>
                  </div>
                )}
              </div>
              {i < path.hops.length - 1 && (
                <div
                  className={`hop${hopCoveredBy[i] ? ' covered' : ' uncovered'}`}
                  onClick={(e) => onHopClick && onHopClick(i, e, hopCoveredBy[i])}
                  onMouseEnter={(e) => onHopHover && onHopHover(i, e, hopCoveredBy[i])}
                  onMouseLeave={(e) => onHopLeave && onHopLeave(i, e)}
                >
                  {!hopCoveredBy[i] && (
                    <>
                      <div className="gray-line" />
                      {!compact && <div className="badge">uncovered</div>}
                    </>
                  )}
                  {hopStatus && hopStatus[i] === 'bad' && !compact && (
                    <div className="badge bad">OUTAGE</div>
                  )}
                </div>
              )}
            </Fragment>
          );
        })}
      </div>
    </div>
  );
}

function MiniGraph({ hops, nodeCount }) {
  const safeHops = Array.isArray(hops) ? hops : [];
  const count = nodeCount || safeHops.length || 2;
  const nodes = [];
  for (let i = 0; i < count; i++) {
    const cls = i === 0 ? 'mini-node self' : i === count - 1 ? 'mini-node dest' : 'mini-node';
    nodes.push(<div key={'n' + i} className={cls} />);
    if (i < count - 1) nodes.push(<div key={'h' + i} className="mini-hop gold" />);
  }
  return <div className="mini-graph tbl-mini-graph">{nodes}</div>;
}

function Tooltip({ x, y, children }) {
  if (x == null) return null;
  const style = { left: Math.min(x, window.innerWidth - 300), top: Math.max(40, y - 10) };
  return <div className="tooltip" style={style}>{children}</div>;
}

Object.assign(window, { fmt, repClass, asByName, asShortName, PMContext, usePM, computeAggregate, placeSla, TickingNumber, PathGraph, MiniGraph, Tooltip });
