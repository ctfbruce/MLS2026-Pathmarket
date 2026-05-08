// Reputation Leaderboard view — top- and bottom-ranked ASes, and the
// lowest-reputation SLAs. Driven entirely off live data already fetched into
// window.PM_DATA by data.js; tiebreakers use SLA issuance count (how many
// active SLAs the AS is cosigning).

function Leaderboard() {
  const pm = usePM();
  const { slaById } = pm;

  // Re-render on the 3s refresh tick so scores/SLAs stay fresh.
  const [_, force] = useState(0);
  useEffect(() => {
    const id = setInterval(() => force(t => t + 1), 3000);
    return () => clearInterval(id);
  }, []);

  const ases = window.PM_DATA.ASES || [];
  const slas = Object.values(slaById);

  // Issuance count: active SLAs this AS cosigns.
  const slaCountByAs = useMemo(() => {
    const out = {};
    slas.forEach(s => {
      (s.cosigners || []).forEach(c => { out[c] = (out[c] || 0) + 1; });
    });
    return out;
  }, [slas]);

  const ranked = useMemo(() => {
    return ases
      .map(a => ({ ...a, slaCount: slaCountByAs[a.id] || 0 }))
      .filter(a => a.rep != null); // drop ASes with no score yet
  }, [ases, slaCountByAs]);

  const topByRep = useMemo(() => {
    return [...ranked].sort((a, b) => {
      if (b.rep !== a.rep) return b.rep - a.rep;
      return b.slaCount - a.slaCount; // tiebreaker: more SLAs issued wins
    }).slice(0, 10);
  }, [ranked]);

  const bottomByRep = useMemo(() => {
    return [...ranked].sort((a, b) => {
      if (a.rep !== b.rep) return a.rep - b.rep;
      return a.slaCount - b.slaCount;
    }).slice(0, 10);
  }, [ranked]);

  const lowestRepSlas = useMemo(() => {
    return [...slas]
      .filter(s => s.rep != null)
      .sort((a, b) => a.rep - b.rep)
      .slice(0, 10);
  }, [slas]);

  return (
    <div className="view" data-tour="leaderboard">
      <div className="topo-bg" />
      <div className="view-head">
        <div>
          <div className="view-title">Reputation Leaderboard</div>
          <div className="view-sub">Ranked by k-corroborated score (§6.1). Tiebreaker: active SLAs cosigned.</div>
        </div>
      </div>

      <div className="lb-grid">
        <LbPanel
          title="Most reputable"
          subtitle="Top 10 · higher score + more issuance wins"
          rows={topByRep}
          highlightHigh
        />
        <LbPanel
          title="Least reputable"
          subtitle="Bottom 10 · watch for reshop triggers"
          rows={bottomByRep}
          highlightHigh={false}
        />
      </div>

      <div className="lb-section">
        <div className="view-title" style={{ fontSize: 14 }}>Lowest-reputation SLAs</div>
        <div className="view-sub">Min-cosigner-score across each SLA's consortium</div>
        <div className="lb-sla-grid">
          {lowestRepSlas.length === 0 && (
            <div style={{ color: 'var(--muted)', padding: '2em', textAlign: 'center' }}>
              No SLAs on the aggregator yet.
            </div>
          )}
          {lowestRepSlas.map(s => {
            const short = s.id.length > 18 ? s.id.slice(0, 14) + '…' : s.id;
            return (
              <div key={s.id} className="lb-sla-row">
                <div className="lb-sla-id" title={s.id}>{short}</div>
                <div className="lb-sla-seg">
                  {s.segment.slice(0, 3).map(a => a.split(':').slice(-1)[0]).join(' → ')}
                  {s.segment.length > 3 ? ` … (${s.segment.length - 1}h)` : ''}
                </div>
                <div className={`lb-sla-rep ${repClass(s.rep)}`}>{fmt.rep(s.rep)}</div>
                <div className="lb-sla-price">{fmt.price(s.price)} <span className="unit">CHF/GB</span></div>
                <div className="lb-sla-cosigners">{s.cosigners.length}✓</div>
              </div>
            );
          })}
        </div>
      </div>

      <style>{`
        .lb-grid {
          display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 16px;
        }
        .lb-panel {
          background: var(--panel, #15110a);
          border: 1px solid var(--border, #2a2418);
          border-radius: 6px; padding: 14px;
        }
        .lb-panel-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 10px; }
        .lb-panel-title { font-size: 13px; font-weight: 600; color: var(--gold, #d4a74c); }
        .lb-panel-sub   { font-size: 10px; color: var(--muted, #988767); }
        .lb-row {
          display: grid;
          grid-template-columns: 18px 1fr 62px 50px 40px;
          gap: 8px; align-items: center;
          padding: 6px 4px;
          border-bottom: 1px dashed rgba(212,167,76,0.12);
          font-family: var(--mono);
          font-size: 11px;
        }
        .lb-row:last-child { border-bottom: none; }
        .lb-rank { color: var(--faint, #5a4f3a); text-align: right; }
        .lb-name { color: var(--text, #d6c9a8); }
        .lb-name .id { color: var(--muted); font-size: 10px; display: block; }
        .lb-rep { text-align: right; font-weight: 600; }
        .lb-rep.high { color: #7cc27c; }
        .lb-rep.med  { color: var(--gold, #d4a74c); }
        .lb-rep.low  { color: #e89c4a; }
        .lb-rep.bad  { color: #e35d5d; }
        .lb-rep.unknown { color: var(--muted); }
        .lb-slas { text-align: right; color: var(--cyan, #6ec6c6); }
        .lb-role { color: var(--muted); text-align: right; font-size: 10px; }
        .lb-section { margin-top: 20px; }
        .lb-sla-grid {
          margin-top: 10px;
          background: var(--panel, #15110a);
          border: 1px solid var(--border, #2a2418);
          border-radius: 6px; padding: 10px;
        }
        .lb-sla-row {
          display: grid;
          grid-template-columns: 180px 1fr 60px 100px 40px;
          gap: 10px; align-items: center;
          padding: 6px 4px;
          border-bottom: 1px dashed rgba(212,167,76,0.1);
          font-family: var(--mono);
          font-size: 11px;
        }
        .lb-sla-row:last-child { border-bottom: none; }
        .lb-sla-id { color: var(--text); }
        .lb-sla-seg { color: var(--muted); }
        .lb-sla-rep { text-align: right; font-weight: 600; }
        .lb-sla-rep.high { color: #7cc27c; }
        .lb-sla-rep.med  { color: var(--gold); }
        .lb-sla-rep.low  { color: #e89c4a; }
        .lb-sla-rep.bad  { color: #e35d5d; }
        .lb-sla-price { text-align: right; color: var(--gold); }
        .lb-sla-price .unit { color: var(--muted); font-size: 9px; }
        .lb-sla-cosigners { text-align: right; color: var(--green, #7cc27c); }
      `}</style>
    </div>
  );
}

function LbPanel({ title, subtitle, rows, highlightHigh }) {
  return (
    <div className="lb-panel">
      <div className="lb-panel-head">
        <span className="lb-panel-title">{title}</span>
        <span className="lb-panel-sub">{subtitle}</span>
      </div>
      {rows.length === 0 && (
        <div style={{ color: 'var(--muted)', padding: '1em 0' }}>No scored ASes yet.</div>
      )}
      {rows.map((a, i) => (
        <div key={a.id} className="lb-row">
          <span className="lb-rank">{i + 1}</span>
          <div className="lb-name">
            {a.name || a.id}
            <span className="id">{a.id}</span>
          </div>
          <span className={`lb-rep ${repClass(a.rep)}`}>{fmt.rep(a.rep)}</span>
          <span className="lb-slas">{a.slaCount} SLA{a.slaCount !== 1 ? 's' : ''}</span>
          <span className="lb-role">{a.role || '—'}</span>
        </div>
      ))}
    </div>
  );
}

Object.assign(window, { Leaderboard });
