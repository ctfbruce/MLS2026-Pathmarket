// Sidebar (Portfolio) + Ticker + Scenario Panel + Top Bar + Left Nav

function Portfolio() {
  const pm = usePM();
  const { portfolio, slaById, pmOn, goToRoutePlanner, consumptionTick } = pm;

  if (!pmOn) {
    return (
      <div className="side">
        <div className="side-head">
          <div className="title">Portfolio</div>
        </div>
        <div className="side-disabled">
          N/A — PathMarket disabled<br />
          <span style={{ color: 'var(--faint)', marginTop: 8, display: 'inline-block' }}>no claims without market layer</span>
        </div>
      </div>
    );
  }

  return (
    <div className="side">
      <div className="side-head">
        <div className="title">Portfolio</div>
        <div className="count">{portfolio.length} claim{portfolio.length !== 1 ? 's' : ''}</div>
      </div>
      <div className="portfolio">
        {portfolio.length === 0 && (
          <div style={{ padding: 30, textAlign: 'center', color: 'var(--faint)', fontFamily: 'var(--mono)', fontSize: 11 }}>
            No active claims.<br /><br />
            Open the Route Planner and claim coverage on a hop to begin.
          </div>
        )}
        {portfolio.map((p, idx) => {
          const sla = slaById[p.sla];
          if (!sla) return null;
          const driftPct = ((sla.rep - p.reputation_at_claim) / p.reputation_at_claim) * 100;
          const driftDown = driftPct < -0.5;
          const driftUp = driftPct > 0.5;
          const warning = sla.rep < 0.78;
          const danger = sla.rep < 0.68;
          const consumed = p.consumed_gb + (consumptionTick * (p.consumed_gb > 0 ? 0.15 : 0));
          const pct = Math.min(100, (consumed / p.purchased_gb) * 100);
          return (
            <div key={p.sla + idx} className={`claim-card slide-in ${danger ? 'danger' : warning ? 'warning' : ''}`}>
              <div className="head">
                <span className="id">{sla.id}</span>
                <span className="meta">{sla.expiry_days}d left</span>
              </div>
              <MiniGraph hops={sla.segment} />
              <div className="claim-meta">
                <span className="k">paid</span><span className="v">{p.price_paid.toFixed(2)} CHF</span>
                <span className="k">purchased</span><span className="v">{p.purchased_gb} GB</span>
                <span className="k">consumed</span><span className="v">{consumed.toFixed(1)} GB</span>
                <span className="k">rate</span><span className="v">{sla.price.toFixed(3)}/GB</span>
              </div>
              <div className="consumption"><div className="bar" style={{ width: `${pct}%` }} /></div>
              <div className="rep-row">
                <span className="k">SLA reputation</span>
                <span>
                  <span className="v">{sla.rep.toFixed(2)}</span>
                  {driftDown && <span className="drift down">▼ {driftPct.toFixed(1)}%</span>}
                  {driftUp && <span className="drift up">▲ +{driftPct.toFixed(1)}%</span>}
                  {!driftDown && !driftUp && <span className="drift" style={{ color: 'var(--muted)' }}>—</span>}
                </span>
              </div>
              {danger && (
                <div className="warning-badge danger">
                  ⚠ reputation collapse — re-shop immediately
                </div>
              )}
              {warning && !danger && (
                <div className="warning-badge">
                  ⚠ reputation warning — consider re-shopping
                </div>
              )}
            </div>
          );
        })}
      </div>
      <div className="side-foot">
        <button className="btn-claim-new" onClick={goToRoutePlanner}>
          + CLAIM NEW SLA
        </button>
      </div>
    </div>
  );
}

function ActivityTicker() {
  const pm = usePM();
  const { tickerItems, pmOn } = pm;

  if (!pmOn) {
    return (
      <div className="ticker disabled">
        <div className="label"><span className="pulse" style={{ background: 'var(--faint)', boxShadow: 'none' }} />Activity</div>
        <div className="stream" />
      </div>
    );
  }

  const stream = [...tickerItems, ...tickerItems];
  return (
    <div className="ticker">
      <div className="label"><span className="pulse" />Live Market</div>
      <div className="stream">
        <div className={`scroll ${tickerItems.length > 12 ? 'fast' : ''}`}>
          {stream.map((item, i) => (
            <div key={i} className="item">
              <span className={`kind ${item.kind}`}>{item.kind}</span>
              <span>{item.text}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function ScenarioPanel() {
  const pm = usePM();
  const { runScenario, scenarioStatus, scenarioRunning, troubledAs, setTroubledAs, troubleActive } = pm;
  const [collapsed, setCollapsed] = useState(false);
  const ases = (window.PM_DATA && window.PM_DATA.ASES) || [];
  return (
    <div className={`scenario-panel${collapsed ? ' collapsed' : ''}`}>
      <div className="scenario-head" onClick={() => setCollapsed(c => !c)}>
        <span className="pulse" />
        <span className="t">Presenter · Scenarios</span>
        <span className="arrow">▾</span>
      </div>
      <div className="scenario-body">
        <div className="scenario-as-row">
          <label className="scenario-as-label">target AS</label>
          <select
            className="scenario-as-select"
            value={troubledAs}
            onChange={(e) => setTroubledAs(e.target.value)}
            disabled={scenarioRunning}
          >
            {ases.map(a => (
              <option key={a.id} value={a.id}>{a.id} · {a.name}</option>
            ))}
          </select>
        </div>
        <button className="scenario-btn" onClick={() => runScenario('trouble', troubledAs)} disabled={scenarioRunning || troubleActive}>
          <span className="num">01</span><span className="t danger">Network trouble on AS</span>
        </button>
        <button className="scenario-btn" onClick={() => runScenario('recover', troubledAs)} disabled={scenarioRunning || !troubleActive}>
          <span className="num">02</span><span className="t info">Network restored on AS</span>
        </button>
        <button className="scenario-btn" onClick={() => runScenario('reset')} disabled={scenarioRunning}>
          <span className="num">03</span><span className="t">Reset clean state</span>
        </button>
        {scenarioStatus && <div className="scenario-status">▸ {scenarioStatus}</div>}
      </div>
    </div>
  );
}

// ============ Top bar — amplified PathMarket toggle ============
function TopBar() {
  const pm = usePM();
  const { pmOn, setPmOn, startTour, tourRunning } = pm;
  return (
    <div className="topbar">
      <div className="brand">
        <div className="logo">
          <svg width="22" height="22" viewBox="0 0 22 22" fill="none">
            <circle cx="4" cy="11" r="2.5" fill="#d4a74c" />
            <circle cx="11" cy="4" r="2" stroke="#d4a74c" strokeWidth="1.5" />
            <circle cx="18" cy="11" r="2" stroke="#5ab0d4" strokeWidth="1.5" />
            <circle cx="11" cy="18" r="2" stroke="#8a95a5" strokeWidth="1.5" />
            <line x1="4" y1="11" x2="11" y2="4" stroke="#d4a74c" strokeWidth="1.2" />
            <line x1="4" y1="11" x2="18" y2="11" stroke="#8a95a5" strokeWidth="1.2" strokeDasharray="2 2" />
            <line x1="11" y1="4" x2="18" y2="11" stroke="#d4a74c" strokeWidth="1.2" />
            <line x1="11" y1="18" x2="18" y2="11" stroke="#8a95a5" strokeWidth="1.2" strokeDasharray="2 2" />
          </svg>
        </div>
        <div>
          <div className="brand-name">PathMarket<span style={{ color: 'var(--muted)', marginLeft: 6, fontWeight: 400 }}>Terminal</span></div>
        </div>
      </div>

      <div className="session">
        <span className="dot" />
        <span className="label">signed in as</span>
        <span className="as">{window.PM_DATA.ME}</span>
        <span className="label">·</span>
        <span className="as">{window.PM_DATA.ME_NAME}</span>
      </div>

      <div className="spacer" />

      <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)' }}>
        {new Date().toISOString().slice(0, 16).replace('T', ' ')} UTC
      </div>

      <button
        className="tour-start-btn"
        onClick={() => startTour && startTour()}
        disabled={tourRunning}
        title="90-second scripted walkthrough"
      >
        <span className="dot" />
        <span>▶ Guided Demo · 90s</span>
      </button>

      <div data-tour="pm-toggle" className={`pm-toggle-wrap${pmOn ? '' : ' off'}`} onClick={() => setPmOn(!pmOn)} role="switch" aria-checked={pmOn} title="Toggle PathMarket layer">
        <div className="labels">
          <div className="top">Market Layer</div>
          <div className="main">{pmOn ? 'PathMarket' : 'Raw SCION'}</div>
        </div>
        <span className="pill"><span className="knob" /></span>
        <span className="state">{pmOn ? 'ON' : 'OFF'}</span>
      </div>
    </div>
  );
}

function LeftNav() {
  const pm = usePM();
  const { view, setView, slaById, complaints } = pm;
  const activeComplaints = complaints.filter(c => {
    const diffDays = (Date.now() - new Date(c.ts).getTime()) / (86400 * 1000);
    return diffDays < 2;
  }).length;
  return (
    <div className="nav">
      <div className="nav-section">Operator</div>
      <button className={`nav-item${view === 'planner' ? ' active' : ''}`} onClick={() => setView('planner')}>
        <NavIcon name="planner" />
        <span>Route Planner</span>
      </button>
      <button className={`nav-item${view === 'market' ? ' active' : ''}`} onClick={() => setView('market')}>
        <NavIcon name="market" />
        <span>Market Browser</span>
        <span className="badge">{Object.keys(slaById).length}</span>
      </button>
      <button className={`nav-item${view === 'complaints' ? ' active' : ''}`} onClick={() => setView('complaints')}>
        <NavIcon name="complaint" />
        <span>Complaint Log</span>
        <span className="badge">{activeComplaints > 0 ? activeComplaints : complaints.length}</span>
      </button>
      <button className={`nav-item${view === 'leaderboard' ? ' active' : ''}`} onClick={() => setView('leaderboard')}>
        <NavIcon name="leaderboard" />
        <span>Reputation Board</span>
      </button>

      <div className="nav-section">Pitch</div>
      <button className={`nav-item${view === 'coldstart' ? ' active' : ''}`} onClick={() => setView('coldstart')}>
        <NavIcon name="doc" />
        <span>Cold-start replay</span>
      </button>

      <div className="nav-section">Reference</div>
      <button className="nav-item" onClick={e => e.preventDefault()} style={{ opacity: 0.6 }}>
        <NavIcon name="doc" />
        <span>Spec · SCION v2</span>
      </button>
      <button className="nav-item" onClick={e => e.preventDefault()} style={{ opacity: 0.6 }}>
        <NavIcon name="settings" />
        <span>Keys & cosigning</span>
      </button>

      <div className="nav-foot">
        <div className="row"><span>ISD</span><span>1 (Europe)</span></div>
        <div className="row"><span>peers</span><span>{window.PM_DATA.ASES.length - 1}</span></div>
        <div className="row"><span>my rep</span><span style={{ color: 'var(--gold)' }}>0.89</span></div>
        <div className="row"><span style={{ color: 'var(--faint)' }}>keys</span><span style={{ color: 'var(--green)' }}>✓ loaded</span></div>
      </div>
    </div>
  );
}

function NavIcon({ name }) {
  const icons = {
    planner: <svg viewBox="0 0 14 14" width="14" height="14" fill="none"><circle cx="2" cy="7" r="1.5" fill="currentColor"/><circle cx="12" cy="7" r="1.5" fill="currentColor"/><circle cx="7" cy="2" r="1.2" stroke="currentColor" strokeWidth="1"/><circle cx="7" cy="12" r="1.2" stroke="currentColor" strokeWidth="1"/><line x1="2" y1="7" x2="12" y2="7" stroke="currentColor" strokeWidth="1"/><line x1="7" y1="2" x2="7" y2="12" stroke="currentColor" strokeWidth="1" strokeDasharray="1.5 1.5"/></svg>,
    market:  <svg viewBox="0 0 14 14" width="14" height="14" fill="none"><rect x="1" y="3" width="12" height="8" stroke="currentColor" strokeWidth="1" rx="1"/><line x1="1" y1="6" x2="13" y2="6" stroke="currentColor" strokeWidth="1"/><line x1="5" y1="6" x2="5" y2="11" stroke="currentColor" strokeWidth="1"/><line x1="9" y1="6" x2="9" y2="11" stroke="currentColor" strokeWidth="1"/></svg>,
    complaint: <svg viewBox="0 0 14 14" width="14" height="14" fill="none"><path d="M7 1.5 L12.5 11 L1.5 11 Z" stroke="currentColor" strokeWidth="1" fill="none"/><line x1="7" y1="5" x2="7" y2="8" stroke="currentColor" strokeWidth="1.2"/><circle cx="7" cy="9.6" r="0.6" fill="currentColor"/></svg>,
    doc: <svg viewBox="0 0 14 14" width="14" height="14" fill="none"><path d="M3 1.5 H9 L11.5 4 V12.5 H3 Z" stroke="currentColor" strokeWidth="1"/><line x1="4.5" y1="6.5" x2="10" y2="6.5" stroke="currentColor" strokeWidth="1"/><line x1="4.5" y1="9" x2="10" y2="9" stroke="currentColor" strokeWidth="1"/></svg>,
    settings: <svg viewBox="0 0 14 14" width="14" height="14" fill="none"><circle cx="7" cy="7" r="2" stroke="currentColor" strokeWidth="1"/><circle cx="7" cy="7" r="5" stroke="currentColor" strokeWidth="1" strokeDasharray="1.5 1.5"/></svg>,
    leaderboard: <svg viewBox="0 0 14 14" width="14" height="14" fill="none"><rect x="1.5" y="6" width="3" height="6.5" stroke="currentColor" strokeWidth="1"/><rect x="5.5" y="3" width="3" height="9.5" stroke="currentColor" strokeWidth="1"/><rect x="9.5" y="8" width="3" height="4.5" stroke="currentColor" strokeWidth="1"/></svg>,
  };
  return <span className="icon" style={{ color: 'currentColor' }}>{icons[name]}</span>;
}

Object.assign(window, { Portfolio, ActivityTicker, ScenarioPanel, TopBar, LeftNav });
