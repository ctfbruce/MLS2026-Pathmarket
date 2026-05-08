// Market Browser + Complaint Log views

function MarketBrowser() {
  const pm = usePM();
  const { slaById, pmOn, claimedSlaIds, claimFromMarket, newlySignedSlas } = pm;
  const [sort, setSort] = useState({ col: 'rep', dir: 'desc' });
  const [filters, setFilters] = useState({ src: '', dst: '', maxPrice: '', minRep: '' });
  const [expanded, setExpanded] = useState(null);

  if (!pmOn) {
    return (
      <div className="view">
        <div className="view-head">
          <div className="view-title">Market Browser</div>
          <div className="view-sub">Live SLA marketplace</div>
        </div>
        <div style={{ padding: 80, textAlign: 'center', fontFamily: 'var(--mono)', color: 'var(--faint)' }}>
          N/A — no market data without PathMarket
        </div>
      </div>
    );
  }

  const slas = Object.values(slaById);
  const filtered = slas.filter(s => {
    const src = s.segment[0], dst = s.segment[s.segment.length - 1];
    if (filters.src && !src.includes(filters.src)) return false;
    if (filters.dst && !dst.includes(filters.dst)) return false;
    if (filters.maxPrice && s.price > parseFloat(filters.maxPrice)) return false;
    if (filters.minRep && s.rep < parseFloat(filters.minRep)) return false;
    return true;
  });

  const sorted = [...filtered].sort((a, b) => {
    let av, bv;
    switch (sort.col) {
      case 'id': av = a.id; bv = b.id; break;
      case 'path': av = a.segment.length; bv = b.segment.length; break;
      case 'latency': av = a.latency; bv = b.latency; break;
      case 'loss': av = a.loss; bv = b.loss; break;
      case 'bw': av = a.bw; bv = b.bw; break;
      case 'price': av = a.price; bv = b.price; break;
      case 'rep': av = a.rep; bv = b.rep; break;
      case 'expiry': av = a.expiry_days; bv = b.expiry_days; break;
      default: av = 0; bv = 0;
    }
    if (typeof av === 'string') return sort.dir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
    return sort.dir === 'asc' ? av - bv : bv - av;
  });

  const toggleSort = (col) => {
    setSort(s => s.col === col ? { col, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { col, dir: col === 'rep' ? 'desc' : 'asc' });
  };

  const Th = ({ col, children, right }) => (
    <th
      onClick={() => toggleSort(col)}
      className={`${sort.col === col ? 'sorted' : ''}${right ? ' right' : ''}`}
      style={right ? { textAlign: 'right' } : {}}
    >
      {children}
      <span className="arrow">{sort.col === col ? (sort.dir === 'asc' ? '▲' : '▼') : '↕'}</span>
    </th>
  );

  return (
    <div className="view">
      <div className="view-head">
        <div>
          <div className="view-title">Market Browser</div>
          <div className="view-sub">All active SLAs · sort reputation desc for leaderboard</div>
        </div>
      </div>

      <div className="market-toolbar">
        <div className="filter">
          <span>src AS</span>
          <input placeholder="any" value={filters.src} onChange={e => setFilters(f => ({ ...f, src: e.target.value }))} />
        </div>
        <div className="filter">
          <span>dst AS</span>
          <input placeholder="any" value={filters.dst} onChange={e => setFilters(f => ({ ...f, dst: e.target.value }))} />
        </div>
        <div className="filter">
          <span>max price</span>
          <input placeholder="CHF/GB" value={filters.maxPrice} onChange={e => setFilters(f => ({ ...f, maxPrice: e.target.value }))} />
        </div>
        <div className="filter">
          <span>min rep</span>
          <input placeholder="0.80" value={filters.minRep} onChange={e => setFilters(f => ({ ...f, minRep: e.target.value }))} />
        </div>
        <div className="toolbar-spacer" />
        <div className="toolbar-count">{sorted.length} / {slas.length} SLAs</div>
      </div>

      <table className="tbl">
        <thead>
          <tr>
            <Th col="id">SLA</Th>
            <Th col="path">Segment</Th>
            <th>Cosigners</th>
            <Th col="latency" right>Latency</Th>
            <Th col="loss" right>Loss</Th>
            <Th col="bw" right>BW</Th>
            <Th col="price" right>Price</Th>
            <Th col="rep" right>Reputation</Th>
            <Th col="expiry" right>Expires</Th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {sorted.map(sla => {
            const isExpanded = expanded === sla.id;
            const isNew = newlySignedSlas.has(sla.id);
            const claimed = claimedSlaIds.has(sla.id);
            const hops = sla.segment.length - 1;
            return (
              <Fragment key={sla.id}>
                <tr
                  className={`${isExpanded ? 'expanded' : ''} ${isNew ? 'row-new' : ''}`}
                  onClick={() => setExpanded(isExpanded ? null : sla.id)}
                >
                  <td className="id">{sla.id}</td>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center' }}>
                      <MiniGraph hops={sla.segment} />
                      <span className="hop-count">{hops} hop{hops !== 1 ? 's' : ''}</span>
                    </div>
                    <div style={{ fontSize: 10, color: 'var(--muted)', marginTop: 2 }}>
                      {sla.segment[0].slice(-6)} → {sla.segment[sla.segment.length - 1].slice(-6)}
                    </div>
                  </td>
                  <td>
                    <span style={{ color: 'var(--green)' }}>✓</span> {sla.cosigners.length}
                  </td>
                  <td className="right">≤ {fmt.latency(sla.latency)} ms</td>
                  <td className="right">≤ {fmt.loss(sla.loss)} ppm</td>
                  <td className="right">≥ {fmt.bw(sla.bw)}</td>
                  <td className="right">{fmt.price(sla.price)}</td>
                  <td className="right"><RepBar rep={sla.rep} /></td>
                  <td className="right">in {sla.expiry_days}d</td>
                  <td className="right" onClick={e => e.stopPropagation()}>
                    <button
                      className={`tbl-claim-btn${claimed ? ' claimed' : ''}`}
                      disabled={claimed}
                      onClick={() => !claimed && claimFromMarket(sla.id)}
                    >
                      {claimed ? 'Held' : 'Claim'}
                    </button>
                  </td>
                </tr>
                {isExpanded && (
                  <tr className="expand-row">
                    <td colSpan={10}>
                      <div className="expand-grid">
                        <div className="expand-section">
                          <div className="h">Cosigners & individual reputation</div>
                          {sla.cosigners.map(c => {
                            const as = asByName(c);
                            return (
                              <div key={c} className="cosigner-row">
                                <span className="as"><span className="check">✓</span>{c} <span style={{ color: 'var(--muted)' }}>· {as ? as.name : ''}</span></span>
                                <RepBar rep={as ? as.rep : 0.8} />
                              </div>
                            );
                          })}
                          <div style={{ marginTop: 10 }}>
                            <div className="h">Complaint history</div>
                            <ComplaintMini slaId={sla.id} />
                          </div>
                        </div>
                        <div className="expand-section">
                          <div className="h">Segment visualization</div>
                          <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 4, padding: '12px 16px' }}>
                            <PathGraph
                              path={{ hops: sla.segment, id: sla.id }}
                              claimedSlaIds={[sla.id]}
                              slaById={{ [sla.id]: sla }}
                              hopStatus={{}}
                              compact={false}
                              flowing={false}
                            />
                          </div>
                          <div className="h" style={{ marginTop: 12 }}>Jointly signed by</div>
                          <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-2)', lineHeight: 1.7 }}>
                            {sla.cosigners.length}-AS consortium · threshold signature
                          </div>
                          <div style={{ display: 'flex', gap: 8, marginTop: 14 }}>
                            <button className="btn-claim" style={{ flex: '0 0 160px' }} disabled={claimed} onClick={() => !claimed && claimFromMarket(sla.id)}>
                              {claimed ? 'Already held' : 'Claim this SLA'}
                            </button>
                            <button className="btn-secondary">View consortium</button>
                          </div>
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ComplaintMini({ slaId }) {
  const complaints = window.PM_DATA.COMPLAINTS.filter(c => c.sla === slaId);
  if (complaints.length === 0) {
    return <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--muted)' }}>No complaints on record.</div>;
  }
  return (
    <div>
      {complaints.slice(0, 3).map(c => (
        <div key={c.id} className="cosigner-row">
          <span className="as" style={{ color: 'var(--red)' }}>
            <span style={{ marginRight: 6 }}>⚠</span>
            {c.id} · {c.metric}
          </span>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--muted)' }}>{new Date(c.ts).toLocaleDateString()}</span>
        </div>
      ))}
    </div>
  );
}

// ============ Complaint Log — note-first design ============
function ComplaintLog() {
  const pm = usePM();
  const { complaints, pmOn } = pm;
  const [expanded, setExpanded] = useState(null);
  const [filters, setFilters] = useState({ complainant: '', sla: '', metric: '' });
  const [search, setSearch] = useState('');

  if (!pmOn) {
    return (
      <div className="view">
        <div className="view-head">
          <div className="view-title">Complaint Log</div>
          <div className="view-sub">SLA violation history</div>
        </div>
        <div style={{ padding: 80, textAlign: 'center', fontFamily: 'var(--mono)', color: 'var(--faint)' }}>
          N/A — no complaint mechanism without PathMarket
        </div>
      </div>
    );
  }

  const filtered = complaints.filter(c => {
    if (filters.complainant && !c.complainant.includes(filters.complainant)) return false;
    if (filters.sla && !c.sla.includes(filters.sla)) return false;
    if (filters.metric && c.metric !== filters.metric) return false;
    if (search && !c.note.toLowerCase().includes(search.toLowerCase()) && !c.sla.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  });

  // Severity calc: ratio of measured to bound, inverted for bandwidth
  const severity = (c) => {
    if (c.metric === 'bandwidth') {
      const ratio = c.bound / Math.max(1, c.measured);
      if (ratio > 2.5) return 'bad';
      if (ratio > 1.4) return 'warn';
      return 'info';
    }
    const ratio = c.measured / c.bound;
    if (ratio > 3) return 'bad';
    if (ratio > 1.4) return 'warn';
    return 'info';
  };

  return (
    <div className="view" data-tour="complaint-log">
      <div className="view-head">
        <div>
          <div className="view-title">Complaint Log</div>
          <div className="view-sub">{complaints.length} filed · signed complaints only</div>
        </div>
      </div>

      <div className="market-toolbar">
        <div className="filter">
          <span>complainant</span>
          <input placeholder="AS" value={filters.complainant} onChange={e => setFilters(f => ({ ...f, complainant: e.target.value }))} />
        </div>
        <div className="filter">
          <span>SLA</span>
          <input placeholder="SLA-###" value={filters.sla} onChange={e => setFilters(f => ({ ...f, sla: e.target.value }))} />
        </div>
        <div className="filter">
          <span>metric</span>
          <select value={filters.metric} onChange={e => setFilters(f => ({ ...f, metric: e.target.value }))}>
            <option value="">any</option>
            <option value="latency">latency</option>
            <option value="loss">loss</option>
            <option value="bandwidth">bandwidth</option>
          </select>
        </div>
        <div className="filter" style={{ flex: 1, minWidth: 180 }}>
          <span>search</span>
          <input style={{ flex: 1, width: '100%' }} placeholder="free text over notes" value={search} onChange={e => setSearch(e.target.value)} />
        </div>
        <div className="toolbar-count">{filtered.length} / {complaints.length}</div>
      </div>

      <table className="tbl">
        <thead>
          <tr>
            <th style={{ width: 120 }}>Filed</th>
            <th style={{ width: 80 }}>SLA</th>
            <th style={{ width: 100 }}>Metric</th>
            <th style={{ width: 170 }} className="right">Violation</th>
            <th>Note</th>
            <th style={{ width: 70 }}></th>
          </tr>
        </thead>
        <tbody>
          {filtered.map(c => {
            const isExpanded = expanded === c.id;
            const sev = severity(c);
            const measured = c.metric === 'bandwidth' ? fmt.bw(c.measured) : c.metric === 'loss' ? `${c.measured} ppm` : `${c.measured} ms`;
            const bound = c.metric === 'bandwidth' ? fmt.bw(c.bound) : c.metric === 'loss' ? `${c.bound} ppm` : `${c.bound} ms`;
            return (
              <Fragment key={c.id}>
                <tr className={`complaint-row severity-${sev} ${isExpanded ? 'expanded' : ''}`} onClick={() => setExpanded(isExpanded ? null : c.id)}>
                  <td>
                    <div className="ts">{new Date(c.ts).toISOString().replace('T',' ').slice(0, 16)}</div>
                    <div className="complainant">by {c.complainant.slice(-6)}</div>
                  </td>
                  <td className="id">{c.sla}</td>
                  <td><span className={`metric-tag ${c.metric}`}>{c.metric}</span></td>
                  <td className="right measured">
                    <div><span className="v">{measured}</span></div>
                    <div style={{ fontSize: 10 }}><span className="b">vs bound {bound}</span></div>
                  </td>
                  <td>
                    <div className="note">{c.note}</div>
                  </td>
                  <td className="right attach-icon">
                    {c.attachments.length > 0 ? <>📎 {c.attachments.length}</> : ''}
                  </td>
                </tr>
                {isExpanded && (
                  <tr className="expand-row complaint-expand">
                    <td colSpan={6}>
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
                        <div>
                          <div className="h" style={{ fontFamily: 'var(--mono)', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em', color: 'var(--muted)', marginBottom: 6 }}>Full note</div>
                          <div className="note-full">{c.note}</div>
                          <div style={{ marginTop: 12, fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)' }}>
                            Filed against <span className="id" style={{ color: 'var(--gold)' }}>{c.sla}</span>
                            &nbsp;· complainant <span style={{ color: 'var(--fg-2)' }}>{c.complainant}</span>
                            &nbsp;· complaint id <span style={{ color: 'var(--fg-2)' }}>{c.id}</span>
                          </div>
                        </div>
                        <div>
                          <div className="h" style={{ fontFamily: 'var(--mono)', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em', color: 'var(--muted)', marginBottom: 6 }}>Attachments ({c.attachments.length})</div>
                          {c.attachments.length === 0 && <div style={{ color: 'var(--faint)', fontFamily: 'var(--mono)', fontSize: 11 }}>No attachments.</div>}
                          {c.attachments.map(a => (
                            <div key={a}>
                              <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--cyan)', marginTop: 8 }}>📎 {a}</div>
                              <pre>{renderSample(a, c)}</pre>
                            </div>
                          ))}
                          <div style={{ fontSize: 10, color: 'var(--faint)', marginTop: 8, fontFamily: 'var(--mono)' }}>
                            display only · not cryptographically verified by viewer
                          </div>
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function renderSample(filename, complaint) {
  if (filename.startsWith('traceroute')) {
    return `traceroute to ${complaint.sla}, 30 hops max
 1  1-ff00:0:112 (edge-br-1)         0.412 ms   0.388 ms
 2  1-ff00:0:111 (init7-zh-1)        1.884 ms   1.902 ms
 3  1-ff00:0:122 (de-cix-fra)        6.112 ms  14.871 ms *
 4  1-ff00:0:121 (frankfurt-t1)     ${Number(complaint.measured).toFixed(1)} ms  ${(Number(complaint.measured) + 0.3).toFixed(1)} ms
 5  1-ff00:0:130 (telia-sto)        18.44 ms   17.98 ms`;
  }
  if (filename.startsWith('owamp')) {
    return `{ "window": "90s", "metric": "latency_ms",
  "p50": ${(Number(complaint.measured) - 1.2).toFixed(2)},
  "p95": ${(Number(complaint.measured) + 0.6).toFixed(2)},
  "p99": ${(Number(complaint.measured) + 2.1).toFixed(2)},
  "samples": 1800,
  "bound": ${complaint.bound} }`;
  }
  if (filename.startsWith('loss')) {
    return `ts,loss_ppm\n2026-04-18T07:40:00,412\n2026-04-18T07:40:30,458\n2026-04-18T07:41:00,580\n2026-04-18T07:41:30,532\n2026-04-18T07:42:00,481`;
  }
  if (filename.startsWith('iperf')) {
    return `[  5] 0.00-10.00 sec   2.51 GBytes  2.15 Gbits/sec
[  5] 10.0-20.0 sec   2.48 GBytes  2.13 Gbits/sec
window: below committed 3.5 Gbps floor`;
  }
  return '// ' + filename;
}

Object.assign(window, { MarketBrowser, ComplaintLog });
