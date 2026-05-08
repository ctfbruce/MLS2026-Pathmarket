// Route Planner view

function RoutePlanner() {
  const pm = usePM();
  const {
    destinationId, setDestinationId,
    paths, selectedPathId, setSelectedPathId,
    claimedByPath, addClaim, swapApplication,
    slaById, hopStatusByPath,
    portfolio,
    pmOn,
  } = pm;

  const path = paths.find(p => p.id === selectedPathId) || paths[0];
  const hasPath = !!path;
  const [drawer, setDrawer] = useState(null); // { pathId, hopIndex }
  const [tooltip, setTooltip] = useState(null);
  const [swapPopover, setSwapPopover] = useState(null); // { pathId, placement, x, y }

  const handleHopClick = (pathId, hopIndex, e, slaId) => {
    if (slaId) return;
    setDrawer({ pathId, hopIndex });
    setSelectedPathId(pathId);
  };

  // click on gold arc → swap popover
  const handleArcClick = (pathId, placement, e) => {
    setSelectedPathId(pathId);
    setTooltip(null);
    setSwapPopover({ pathId, placement, x: e.clientX, y: e.clientY });
  };

  // Close popover on outside click
  useEffect(() => {
    if (!swapPopover) return;
    const close = (e) => {
      if (e.target.closest('.swap-popover')) return;
      setSwapPopover(null);
    };
    setTimeout(() => document.addEventListener('click', close), 0);
    return () => document.removeEventListener('click', close);
  }, [swapPopover]);

  const handleHopHover = (pathId, hopIndex, e, slaId) => {
    if (slaId) return; // covered hops handled by arc hover
    const pth = paths.find(p => p.id === pathId);
    const from = pth.hops[hopIndex], to = pth.hops[hopIndex + 1];
    const available = window.PM_DATA.SLAS.filter(s => {
      // matches if sla.segment has a consecutive [from,to] pair
      for (let i = 0; i < s.segment.length - 1; i++) {
        if (s.segment[i] === from && s.segment[i+1] === to) return true;
      }
      return false;
    });
    setTooltip({ x: e.clientX, y: e.clientY, kind: 'uncovered', available, from, to });
  };

  const handleArcHover = (pathId, placement, e) => {
    if (!placement) { setTooltip(null); return; }
    setTooltip({ x: e.clientX, y: e.clientY, kind: 'covered', sla: placement.sla, placement });
  };

  const handleHopLeave = () => setTooltip(null);

  const aggregate = useMemo(() => {
    if (!path) return null;
    return computeAggregate(path, claimedByPath[path.id] || [], slaById, hopStatusByPath[path.id] || {});
  }, [path, claimedByPath, slaById, hopStatusByPath]);

  const drawerContent = useMemo(() => {
    if (!drawer) return null;
    const pth = paths.find(p => p.id === drawer.pathId);
    const from = pth.hops[drawer.hopIndex], to = pth.hops[drawer.hopIndex + 1];
    const available = Object.values(slaById).filter(s => {
      for (let i = 0; i < s.segment.length - 1; i++) {
        if (s.segment[i] === from && s.segment[i+1] === to) return true;
      }
      return false;
    });
    return { pth, from, to, available };
  }, [drawer, paths, slaById]);

  return (
    <div className="view">
      <div className="topo-bg" />

      <div className="view-head">
        <div>
          <div className="view-title">Route Planner</div>
          <div className="view-sub">Compose a path · hover gold arcs for SLA details · click gray hops for coverage</div>
        </div>
        <div className="spacer" />
        <div data-tour="destination" className="dest-selector">
          <span className="lbl">Route to:</span>
          <select value={destinationId} onChange={(e) => setDestinationId(e.target.value)}>
            {Object.keys(window.PM_DATA.PATHS).map(d => (
              <option key={d} value={d}>{d} · {asShortName(d)}</option>
            ))}
          </select>
        </div>
      </div>

      {!hasPath && (
        <div className="path-card" style={{ padding: '2em', textAlign: 'center', color: 'var(--muted)' }}>
          No candidate paths to {destinationId}. Check that the user agent's
          path discovery has a route for this destination (scion-topology/static_paths.yaml),
          and that there are active SLAs on the aggregator that cover those hops.
        </div>
      )}
      {hasPath && (
        <div data-tour="aggregate"><AggregateCard agg={aggregate} path={path} pmOn={pmOn} /></div>
      )}

      <div className="path-stack">
        {paths.map((p, idx) => {
          const agg = computeAggregate(p, claimedByPath[p.id] || [], slaById, hopStatusByPath[p.id] || {});
          const sel = p.id === selectedPathId;
          const repColor = agg.rep == null ? 'muted' : agg.rep >= 0.85 ? 'gold' : agg.rep >= 0.75 ? 'warn' : 'bad';
          const tourAnchor = p.hops.length === 2
            ? 'path-A'
            : (p.hops.length >= 4 ? 'path-B' : null);
          return (
            <div
              key={p.id}
              data-tour={tourAnchor || undefined}
              className={`path-card${sel ? ' selected' : ''}`}
              onClick={() => setSelectedPathId(p.id)}
            >
              <div className="path-head">
                <div className="path-label">{p.label}</div>
                <div className="path-stats">
                  <span className={`pair ${agg.coverage === 'full' ? 'gold' : agg.coverage === 'partial' ? 'warn' : 'bad'}`}>
                    <span className="k">cov</span>
                    <span className="v">{agg.coveredCount}/{agg.totalHops}</span>
                  </span>
                  <span className="pair">
                    <span className="k">lat</span>
                    <span className="v">{agg.latency != null ? fmt.latency(agg.latency) + 'ms' : '—'}</span>
                  </span>
                  <span className="pair">
                    <span className="k">$</span>
                    <span className="v">{agg.price != null ? fmt.price(agg.price) : '—'}</span>
                  </span>
                  <span className={`pair ${repColor}`}>
                    <span className="k">rep</span>
                    <span className="v">{agg.rep != null ? fmt.rep(agg.rep) : '—'}</span>
                  </span>
                </div>
                <button className="select-btn" onClick={(e) => { e.stopPropagation(); setSelectedPathId(p.id); }}>
                  {sel ? '✓ Selected' : 'Select route'}
                </button>
              </div>

              <PathGraph
                path={p}
                claimedSlaIds={claimedByPath[p.id] || []}
                slaById={slaById}
                hopStatus={hopStatusByPath[p.id] || {}}
                onArcClick={(pl, e) => handleArcClick(p.id, pl, e)}
                onArcHover={(pl, e) => handleArcHover(p.id, pl, e)}
                onHopClick={(i, e, sid) => handleHopClick(p.id, i, e, sid)}
                onHopHover={(i, e, sid) => handleHopHover(p.id, i, e, sid)}
                onHopLeave={handleHopLeave}
                flowing={sel && pmOn}
                selected={sel}
              />
            </div>
          );
        })}
      </div>

      {tooltip && !swapPopover && <HopTooltip t={tooltip} />}

      {swapPopover && (
        <SwapPopover
          popover={swapPopover}
          path={paths.find(p => p.id === swapPopover.pathId)}
          portfolio={portfolio}
          slaById={slaById}
          appliedIds={claimedByPath[swapPopover.pathId] || []}
          onApply={(newSlaId) => {
            swapApplication(swapPopover.pathId, swapPopover.placement.sla.id, newSlaId);
            setSwapPopover(null);
          }}
          onRemove={() => {
            swapApplication(swapPopover.pathId, swapPopover.placement.sla.id, null);
            setSwapPopover(null);
          }}
          onClose={() => setSwapPopover(null)}
        />
      )}

      <div className={`drawer${drawer ? ' open' : ''}`}>
        {drawerContent && (
          <>
            <div className="drawer-head">
              <div>
                <div className="drawer-title">Available coverage</div>
                <div className="drawer-sub">{drawerContent.from.slice(-6)} → {drawerContent.to.slice(-6)}</div>
              </div>
              <button className="x" onClick={() => setDrawer(null)} aria-label="Close">✕</button>
            </div>
            <div className="drawer-body">
              {drawerContent.available.length === 0 && (
                <div className="drawer-empty">
                  No SLAs covering this hop.<br/>
                  <span style={{ color: 'var(--faint)' }}>Try the Market Browser.</span>
                </div>
              )}
              {drawerContent.available.sort((a, b) => b.rep - a.rep).map(sla => {
                const claimed = (claimedByPath[drawer.pathId] || []).includes(sla.id);
                return (
                  <div key={sla.id} className="sla-card">
                    <div className="head">
                      <span className="id">{sla.id} <span style={{ color: 'var(--muted)' }}>· {sla.segment.length - 1} hop{sla.segment.length > 2 ? 's' : ''}</span></span>
                      <span className="price">{fmt.price(sla.price)}<span className="unit"> CHF/GB</span></span>
                    </div>
                    <div className="segment-mini"><MiniGraph hops={sla.segment} /></div>
                    <div className="grid">
                      <span className="k">latency ≤</span><span className="v">{fmt.latency(sla.latency)} ms</span>
                      <span className="k">loss ≤</span><span className="v">{fmt.loss(sla.loss)} ppm</span>
                      <span className="k">bandwidth ≥</span><span className="v">{fmt.bw(sla.bw)}</span>
                      <span className="k">reputation</span><span className="v"><RepBar rep={sla.rep} /></span>
                      <span className="k">expires</span><span className="v">in {sla.expiry_days}d</span>
                      <span className="k">cosigners</span><span className="v">{sla.cosigners.length} ✓</span>
                    </div>
                    <div className="actions">
                      <button
                        className={`btn-claim${claimed ? ' claimed' : ''}`}
                        disabled={claimed}
                        onClick={() => {
                          if (claimed) return;
                          addClaim(drawer.pathId, sla.id);
                          setDrawer(null);
                        }}
                      >
                        {claimed ? 'Claimed' : 'Claim'}
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function HopTooltip({ t }) {
  if (t.kind === 'covered') {
    const { sla } = t;
    return (
      <Tooltip x={t.x} y={t.y}>
        <div className="ttitle">
          <span>{sla.id}</span>
          <span style={{ color: 'var(--muted)', fontSize: 10 }}>{sla.segment.length - 1}-hop segment</span>
        </div>
        <div className="trow"><span className="k">price</span><span className="v">{fmt.price(sla.price)} CHF/GB</span></div>
        <div className="trow"><span className="k">latency ≤</span><span className="v">{fmt.latency(sla.latency)} ms</span></div>
        <div className="trow"><span className="k">loss ≤</span><span className="v">{fmt.loss(sla.loss)} ppm</span></div>
        <div className="trow"><span className="k">bandwidth ≥</span><span className="v">{fmt.bw(sla.bw)}</span></div>
        <div className="trow"><span className="k">reputation</span><span className="v">{fmt.rep(sla.rep)}</span></div>
        <div className="trow"><span className="k">expires</span><span className="v">in {sla.expiry_days}d</span></div>
        <div className="cosigners">
          jointly cosigned by:<br/>
          {sla.cosigners.map(c => <div key={c}><span className="sig">✓</span> {c} · <span style={{ color: 'var(--muted)' }}>{asShortName(c)}</span></div>)}
        </div>
      </Tooltip>
    );
  }
  return (
    <Tooltip x={t.x} y={t.y}>
      <div className="ttitle"><span style={{ color: 'var(--muted)' }}>uncovered hop</span></div>
      <div className="trow"><span className="k">{t.from.slice(-6)} → {t.to.slice(-6)}</span><span className="v">{t.available.length} SLA{t.available.length !== 1 ? 's' : ''} offered</span></div>
      <div style={{ marginTop: 6, color: 'var(--cyan)', fontSize: 10 }}>click to view available coverage →</div>
    </Tooltip>
  );
}

function RepBar({ rep, showValue = true }) {
  const cls = repClass(rep);
  return (
    <span className="rep-bar">
      {showValue && <span>{fmt.rep(rep)}</span>}
      <span className="track">
        <span className={`fill ${cls}`} style={{ width: `${Math.round((rep || 0) * 100)}%` }} />
      </span>
    </span>
  );
}

function AggregateCard({ agg, path, pmOn }) {
  if (!pmOn) {
    return (
      <div className="aggregate">
        <div className="aggregate-head">
          <div className="aggregate-title">Aggregate <span className="accent">Ensured</span></div>
          <span className="coverage-pill none">no recourse · PathMarket OFF</span>
        </div>
        <div className="aggregate-grid">
          <div className="rep-hero">
            <div className="k">Aggregate reputation</div>
            <div className="v disabled">—</div>
            <div className="sub">no market · no accountability</div>
          </div>
          <div className="agg-sub-grid">
            {[{k:'Worst-case latency', u:'ms'},{k:'Worst-case loss', u:'ppm'},{k:'Min bandwidth', u:'Gbps'},{k:'Aggregate price', u:'CHF/GB'}].map(m => (
              <div className="metric" key={m.k}>
                <div className="k">{m.k}</div>
                <div className="v disabled">—<span className="unit">{m.u}</span></div>
              </div>
            ))}
          </div>
        </div>
      </div>
    );
  }

  const covPill =
    agg.coverage === 'full' ? <span className="coverage-pill full">fully covered · {agg.coveredCount}/{agg.totalHops} hops</span> :
    agg.coverage === 'partial' ? <span className="coverage-pill partial">partially covered · {agg.coveredCount} of {agg.totalHops} hops</span> :
    <span className="coverage-pill none">uncovered · {agg.totalHops} hops exposed</span>;

  const repCls = agg.rep == null ? 'unknown' : agg.rep >= 0.85 ? '' : agg.rep >= 0.75 ? 'warn' : 'bad';
  const repSub =
    agg.coverage === 'full' ? <>min of {agg.placements.length} SLA{agg.placements.length !== 1 ? 's' : ''} · <span className="accent">most conservative floor</span></> :
    agg.coverage === 'partial' ? <><span className="warn">unknown</span> · uncovered hops have no reputation floor</> :
    <><span className="bad">exposed</span> · no SLAs claimed</>;

  return (
    <div className="aggregate">
      <div className="aggregate-head">
        <div className="aggregate-title">Aggregate <span className="accent">Ensured</span></div>
        {covPill}
        <div className="aggregate-pathlabel">{path.label}</div>
      </div>
      <div className="aggregate-grid">
        <div className="rep-hero">
          <div className="k">Aggregate reputation</div>
          <div className={`v ${repCls}`}>
            {agg.rep == null
              ? <TickingNumber value={null} placeholder="—" />
              : <TickingNumber value={agg.rep} format={v => v.toFixed(2)} />}
          </div>
          <div className="sub">{repSub}</div>
        </div>
        <div className="agg-sub-grid">
          <div className="metric">
            <div className="k">Worst-case latency</div>
            <div className="v"><TickingNumber value={agg.latency} format={v => v.toFixed(1)} unit="ms" /></div>
          </div>
          <div className="metric">
            <div className="k">Worst-case loss</div>
            <div className="v"><TickingNumber value={agg.loss} format={v => Math.round(v).toString()} unit="ppm" /></div>
          </div>
          <div className="metric">
            <div className="k">Min bandwidth</div>
            <div className="v"><TickingNumber value={agg.bw} format={v => (v/1_000_000).toFixed(1)} unit="Gbps" /></div>
          </div>
          <div className="metric">
            <div className="k">Aggregate price</div>
            <div className="v"><TickingNumber value={agg.price} format={v => v.toFixed(3)} unit="CHF/GB" /></div>
          </div>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { RoutePlanner, RepBar, AggregateCard });

// ============================================================================
// SwapPopover — click a gold arc to swap which held claim is applied to this
// segment, or remove coverage entirely. This is a pure routing decision — the
// portfolio is unchanged; only the appliedByPath mapping updates.
// ============================================================================
function SwapPopover({ popover, path, portfolio, slaById, appliedIds, onApply, onRemove, onClose }) {
  if (!path || !popover) return null;
  const applied = popover.placement.sla;
  const { startIdx, endIdx } = popover.placement;

  // Find HELD claims (from portfolio) whose segment EXACTLY matches this segment
  // of the path, and are not already the applied one.
  const segmentKey = path.hops.slice(startIdx, endIdx + 1).join('|');
  const alternatives = portfolio
    .map(p => slaById[p.sla])
    .filter(Boolean)
    .filter(s => s.id !== applied.id)
    .filter(s => !appliedIds.includes(s.id)) // can't double-apply
    .filter(s => s.segment.join('|') === segmentKey);

  // Position: anchor near click, clamp to viewport
  const W = 320;
  const left = Math.min(Math.max(popover.x - W / 2, 12), window.innerWidth - W - 12);
  const top = Math.min(popover.y + 14, window.innerHeight - 280);

  return (
    <div className="swap-popover" style={{ left, top }} onClick={(e) => e.stopPropagation()}>
      <div className="sp-head">
        <div className="sp-applied">
          <span className="sp-label">APPLIED</span>
          <span className="sp-id">{applied.id}</span>
          <span className="sp-meta">{applied.cosigners.length} cosigners · rep {applied.rep.toFixed(2)}</span>
        </div>
        <button className="sp-x" onClick={onClose} aria-label="Close">✕</button>
      </div>

      <div className="sp-seg">
        <div className="sp-seg-label">Segment</div>
        <MiniGraph hops={applied.segment} />
      </div>

      <div className="sp-section-title">
        {alternatives.length > 0
          ? 'Swap for another held claim covering this segment:'
          : 'No other held claims cover this segment.'}
      </div>

      <div className="sp-alts">
        {alternatives.map(s => (
          <div key={s.id} className="sp-alt">
            <div className="sp-alt-main">
              <span className="sp-alt-id">{s.id}</span>
              <span className="sp-alt-meta">
                {s.cosigners.length} cosigners · rep {s.rep.toFixed(2)} · {s.expiry_days}d left
              </span>
            </div>
            <button className="sp-btn apply" onClick={() => onApply(s.id)}>Apply</button>
          </div>
        ))}

        <div className="sp-alt sp-remove">
          <div className="sp-alt-main">
            <span className="sp-alt-id muted">Remove coverage</span>
            <span className="sp-alt-meta">route this segment uncovered</span>
          </div>
          <button className="sp-btn remove" onClick={onRemove}>Remove</button>
        </div>
      </div>

      <div className="sp-foot">
        Local routing choice · claim stays in your portfolio
      </div>
    </div>
  );
}

Object.assign(window, { SwapPopover });