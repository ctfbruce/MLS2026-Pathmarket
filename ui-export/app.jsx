// Main app — state container, scenario engine, routing

function App() {
  const [pmOn, setPmOn] = useState(true);
  const [view, setView] = useState('planner');
  const [destinationId, setDestinationId] = useState('1-ff00:0:130');
  const [selectedPathId, setSelectedPathId] = useState('P-A');

  const paths = window.PM_DATA.PATHS[destinationId] || [];

  // SLA map (mutable for scenarios)
  const [slas, setSlas] = useState(() => {
    const m = {};
    window.PM_DATA.SLAS.forEach(s => m[s.id] = { ...s });
    return m;
  });

  // claimedByPath[pathId] = [slaId...] — which of the operator's HELD claims are
  // currently APPLIED to this path. Local routing state, persisted in localStorage.
  const [claimedByPath, setClaimedByPath] = useState(() => {
    try {
      const saved = localStorage.getItem('pm.claimedByPath.v2');
      if (saved) return JSON.parse(saved);
    } catch (e) {}
    const m = {};
    Object.values(window.PM_DATA.PATHS).flat().forEach(p => {
      m[p.id] = Array.isArray(p.initial_claims) ? p.initial_claims.slice() : [];
    });
    return m;
  });
  useEffect(() => {
    try { localStorage.setItem('pm.claimedByPath.v2', JSON.stringify(claimedByPath)); } catch (e) {}
  }, [claimedByPath]);

  // Swap/remove an SLA's application on a path
  const swapApplication = useCallback((pathId, oldSlaId, newSlaId) => {
    setClaimedByPath(prev => {
      const cur = prev[pathId] || [];
      let next = cur.filter(id => id !== oldSlaId);
      if (newSlaId) next = [...next, newSlaId];
      return { ...prev, [pathId]: next };
    });
  }, []);

  // Per-path hop status overrides: { [pathId]: { [hopIndex]: 'warn'|'bad'|'normal' } }
  const [hopStatusByPath, setHopStatusByPath] = useState({});

  // Portfolio
  const [portfolio, setPortfolio] = useState(() => window.PM_DATA.INITIAL_PORTFOLIO.map(c => ({ ...c })));

  // Complaints
  const [complaints, setComplaints] = useState(() => window.PM_DATA.COMPLAINTS.map(c => ({ ...c })));

  // Ticker
  const [tickerItems, setTickerItems] = useState(() => (window.PM_DATA.TICKER_SEEDS || []).slice());
  const addTickerItem = useCallback((item) => {
    setTickerItems(prev => [item, ...prev].slice(0, 40));
  }, []);

  // Newly signed flash
  const [newlySignedSlas, setNewlySignedSlas] = useState(new Set());
  const markSlaNew = (id) => {
    setNewlySignedSlas(prev => { const n = new Set(prev); n.add(id); return n; });
    setTimeout(() => {
      setNewlySignedSlas(prev => { const n = new Set(prev); n.delete(id); return n; });
    }, 1500);
  };

  // Claimed SLA ids across everything
  const claimedSlaIds = useMemo(() => {
    const s = new Set();
    Object.values(claimedByPath).forEach(arr => arr.forEach(id => s.add(id)));
    portfolio.forEach(p => s.add(p.sla));
    return s;
  }, [claimedByPath, portfolio]);

  // Consumption tick
  const [consumptionTick, setConsumptionTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setConsumptionTick(t => t + 1), 3000);
    return () => clearInterval(id);
  }, []);

  // Live refresh from aggregator + user agent. Skipped while a scripted
  // scenario is running so we don't clobber the local-only demo mutations
  // (fake SLAs, reputation drops) the scenario injected.
  useEffect(() => {
    let cancelled = false;
    const pull = async () => {
      // Tour still polls — reshop narrative depends on reputation updates
      // flowing into the UI live during the tour.
      if (scenarioRunning) return;
      try {
        const r = await window.PM_REFRESH();
        if (cancelled) return;
        // Replace SLAs with server state, but preserve any local-only ids
        // that the scenario may have injected (ids not known to the server).
        setSlas(prev => {
          const serverIds = new Set(r.SLAS.map(s => s.id));
          const next = {};
          for (const s of r.SLAS) next[s.id] = s;
          for (const [id, sla] of Object.entries(prev)) {
            if (!serverIds.has(id) && sla.__local) next[id] = sla;
          }
          return next;
        });
        // Replace portfolio + complaints + ticker from server (canonical).
        setPortfolio(r.PORTFOLIO.map(c => ({ ...c })));
        setComplaints(r.COMPLAINTS.map(c => ({ ...c })));
        setTickerItems(prev => {
          // Prepend server events not already present (by ts+kind+text).
          const seen = new Set(prev.map(e => `${e.ts||''}|${e.kind}|${e.text}`));
          const fresh = r.TICKER_SEEDS.filter(e => !seen.has(`${e.ts||''}|${e.kind}|${e.text}`));
          return [...fresh, ...prev].slice(0, 80);
        });
        // Refresh PATHS for the currently selected destination.
        if (r.PATHS && r.PATHS[destinationId]) {
          window.PM_DATA.PATHS = r.PATHS;
        }
        // Refresh AS reputations (for the directory side panel).
        window.PM_DATA.ASES = r.ASES;
      } catch (e) {
        console.warn('refresh failed', e);
      }
    };
    const id = setInterval(pull, 3000);
    return () => { cancelled = true; clearInterval(id); };
  }, [scenarioRunning, destinationId]);

  // Ambient ticker
  useEffect(() => {
    if (!pmOn) return;
    const gen = () => {
      const r = Math.random();
      if (r < 0.34) return { kind: 'sign', text: `SLA-0${Math.floor(Math.random()*100)+13} signed by ${2 + Math.floor(Math.random()*3)}-AS consortium · 0.0${15 + Math.floor(Math.random()*15)} CHF/GB · ${2 + Math.floor(Math.random()*3)} hops` };
      if (r < 0.67) return { kind: 'rep', text: `AS 1-ff00:0:${Math.floor(Math.random()*300)+100} reputation: 0.${80 + Math.floor(Math.random()*15)} → 0.${75 + Math.floor(Math.random()*15)}` };
      return { kind: 'claim', text: `AS 1-ff00:0:${Math.floor(Math.random()*300)+100} claimed SLA-${Math.floor(Math.random()*12)+1} · ${100 + Math.floor(Math.random()*900)} GB` };
    };
    const id = setInterval(() => addTickerItem(gen()), 3500);
    return () => clearInterval(id);
  }, [pmOn, addTickerItem]);

  // Add a claim to a path (only if the SLA's segment fits within that path).
  // POSTs to /local/actions/claim; optimistic local update so the UI reflects
  // the purchase instantly; the 3s refresh loop reconciles with server truth.
  const addClaim = useCallback(async (pathId, slaId) => {
    const sla = slas[slaId];
    if (!sla) return;
    try {
      await window.PM_ACTIONS.postClaim(slaId, 500);
    } catch (e) {
      console.error('claim failed', e);
      addTickerItem({ kind: 'complaint', text: `Claim failed: ${e.message}` });
      return;
    }
    setClaimedByPath(prev => {
      const cur = prev[pathId] || [];
      if (cur.includes(slaId)) return prev;
      return { ...prev, [pathId]: [...cur, slaId] };
    });
    setPortfolio(prev => {
      if (prev.find(p => p.sla === slaId)) return prev;
      return [{
        sla: slaId, path_id: pathId, purchased_gb: 500, consumed_gb: 0,
        price_paid: sla.price * 500,
        claim_time: new Date().toISOString(),
        reputation_at_claim: sla.rep,
      }, ...prev];
    });
    const hops = sla.segment.length - 1;
    addTickerItem({ kind: 'claim', text: `You claimed ${slaId.slice(0,16)}… · 500 GB · ${sla.price.toFixed(3)} CHF/GB · ${hops} hop${hops !== 1 ? 's' : ''}` });
  }, [slas, addTickerItem]);

  // Claim from market: POST, then place into any path it fits.
  const claimFromMarket = useCallback(async (slaId) => {
    const sla = slas[slaId];
    if (!sla) return;
    try {
      await window.PM_ACTIONS.postClaim(slaId, 500);
    } catch (e) {
      console.error('claim failed', e);
      addTickerItem({ kind: 'complaint', text: `Claim failed: ${e.message}` });
      return;
    }
    let placedInto = null;
    for (const p of paths) {
      const placed = window.placeSla(p, sla);
      if (placed) { placedInto = p.id; break; }
    }
    if (placedInto) {
      setClaimedByPath(prev => {
        const cur = prev[placedInto] || [];
        if (cur.includes(slaId)) return prev;
        return { ...prev, [placedInto]: [...cur, slaId] };
      });
    }
    setPortfolio(prev => {
      if (prev.find(p => p.sla === slaId)) return prev;
      return [{
        sla: slaId, path_id: placedInto, purchased_gb: 500, consumed_gb: 0,
        price_paid: sla.price * 500,
        claim_time: new Date().toISOString(),
        reputation_at_claim: sla.rep,
      }, ...prev];
    });
    const hops = sla.segment.length - 1;
    addTickerItem({ kind: 'claim', text: `You claimed ${slaId.slice(0,16)}… · 500 GB · ${sla.price.toFixed(3)} CHF/GB · ${hops} hop${hops !== 1 ? 's' : ''}` });
  }, [slas, paths, addTickerItem]);

  // ============ Scenarios ============
  // Two-button demo: "Network trouble on AS X" degrades every SLA cosigned
  // by X in the real simulator; the reputation board + complaint log react
  // over the next few ticks. "Restored" flips it back so τ-decay recovery
  // is visible. Reset wipes aggregator + simulator state via the same API.
  const [scenarioStatus, setScenarioStatus] = useState(null);
  const [scenarioRunning, setScenarioRunning] = useState(false);
  const [troubledAs, setTroubledAs] = useState('2-ff00:0:220');
  const [troubleActive, setTroubleActive] = useState(false);

  // 90-second guided tour — overrides scenarioRunning gate so refresh polling
  // stays quiescent while the tour is driving view + action state.
  const [tourRunning, setTourRunning] = useState(false);
  const startTour = useCallback(() => setTourRunning(true), []);
  const stopTour = useCallback(() => setTourRunning(false), []);

  const runScenario = useCallback(async (which, isdAs) => {
    if (scenarioRunning) return;
    setScenarioRunning(true);
    const S = window.PM_SCENARIOS || {};
    const targetAs = isdAs || troubledAs;

    if (which === 'reset') {
      setScenarioStatus('Resetting aggregator + simulator…');
      try { if (S.reset) await S.reset(); } catch (_) {}
      setHopStatusByPath({});
      setTroubleActive(false);
      setView('leaderboard');
      addTickerItem({ kind: 'sign', text: `Market reset · aggregator + simulator re-seeded` });
      setScenarioStatus(null);
      setScenarioRunning(false);
      return;
    }

    if (which === 'trouble') {
      setScenarioStatus(`Network trouble on AS ${targetAs} — watch the reputation board`);
      setView('leaderboard');
      document.querySelector('.flash-overlay')?.classList.add('show');
      setTimeout(() => document.querySelector('.flash-overlay')?.classList.remove('show'), 1600);
      addTickerItem({ kind: 'outage', text: `NETWORK TROUBLE · AS ${targetAs} · every SLA it cosigns is now violating` });
      try {
        const res = S.degrade_as ? await S.degrade_as(targetAs) : { ok: false };
        if (res.ok) {
          const n = (res.data && res.data.sla_ids && res.data.sla_ids.length) || 0;
          addTickerItem({ kind: 'rep', text: `${n} SLA${n === 1 ? '' : 's'} cosigned by ${targetAs} flipped to violating · k-corroboration will fire as buyers resample` });
        } else {
          addTickerItem({ kind: 'rep', text: `Simulator unreachable · UI-only mode` });
        }
      } catch (_) {}
      setTroubleActive(true);
      setTroubledAs(targetAs);
      setScenarioRunning(false);
      return;
    }

    if (which === 'recover') {
      setScenarioStatus(`Network restored on AS ${targetAs} — reputation will recover via τ-decay`);
      setView('leaderboard');
      addTickerItem({ kind: 'sign', text: `NETWORK RESTORED · AS ${targetAs} · quality back to nominal` });
      try {
        const res = S.fix_as ? await S.fix_as(targetAs) : { ok: false };
        if (res.ok) {
          const n = (res.data && res.data.sla_ids && res.data.sla_ids.length) || 0;
          addTickerItem({ kind: 'rep', text: `${n} SLA${n === 1 ? '' : 's'} restored · reputation recovering per §6.2 decay τ` });
        }
      } catch (_) {}
      setTroubleActive(false);
      setScenarioRunning(false);
      return;
    }
  }, [scenarioRunning, troubledAs, addTickerItem]);

  const goToRoutePlanner = useCallback(() => { setView('market'); }, []);

  useEffect(() => {
    if (paths.length && !paths.find(p => p.id === selectedPathId)) {
      setSelectedPathId(paths[0].id);
    }
  }, [paths, selectedPathId]);

  const ctx = {
    pmOn, setPmOn,
    view, setView,
    destinationId, setDestinationId,
    paths, selectedPathId, setSelectedPathId,
    slaById: slas,
    claimedByPath, addClaim, claimFromMarket, swapApplication,
    hopStatusByPath,
    portfolio,
    complaints,
    tickerItems, addTickerItem,
    consumptionTick,
    newlySignedSlas,
    claimedSlaIds,
    runScenario, scenarioStatus, scenarioRunning,
    troubledAs, setTroubledAs, troubleActive,
    goToRoutePlanner,
    tourRunning, startTour, stopTour,
  };

  return (
    <PMContext.Provider value={ctx}>
      <div className={`app${pmOn ? '' : ' pm-off'}`} data-screen-label="PathMarket Terminal">
        <TopBar />
        <LeftNav />
        {!pmOn && (
          <div className="raw-scion-banner">
            <div className="rsb-title">Raw SCION · no market layer</div>
            <div className="rsb-sub">
              paths only · no coverage claims · no aggregate reputation · no complaint recourse · no price discovery
            </div>
          </div>
        )}
        <div className="main">
          {view === 'planner'     && <RoutePlanner />}
          {view === 'market'      && <MarketBrowser />}
          {view === 'complaints'  && <ComplaintLog />}
          {view === 'leaderboard' && <Leaderboard />}
          {view === 'coldstart'   && <ColdStartPanel />}
          <ScenarioPanel />
        </div>
        <Portfolio />
        <ActivityTicker />
        <div className="flash-overlay" />
        <TourController />
      </div>
    </PMContext.Provider>
  );
}

// Defer render until PM_BOOT() fetches live state from the user agent +
// aggregator. If bootstrap fails, surface the error to the page instead of
// rendering with empty data and pretending everything's fine.
window.PM_BOOT().then(() => {
  ReactDOM.createRoot(document.getElementById('root')).render(<App />);
}).catch(err => {
  const root = document.getElementById('root');
  root.innerHTML = `<pre style="color:#e35d5d;padding:2em;font-family:monospace">` +
    `PathMarket bootstrap failed:\n${err.message}\n\n` +
    `Check that the aggregator (http://127.0.0.1:8080) is running and the ` +
    `user agent can reach it.</pre>`;
  console.error(err);
});
