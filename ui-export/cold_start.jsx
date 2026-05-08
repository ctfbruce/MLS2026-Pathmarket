// Cold-start replay panel (§9.14). Fetches assets/cold_start.jsonl, buckets
// events by simulated time, and animates sparklines over ~30 wall seconds
// compressing ~40 minutes of simulated market stratification. Pure frontend —
// no aggregator calls. Serves as the pitch opener.

const COLD_START_URL = "/assets/cold_start.jsonl";
const REPLAY_WALLCLOCK_SECONDS = 30;
const BUCKETS = 180;

async function fetchColdStart() {
  const r = await fetch(COLD_START_URL);
  if (!r.ok) throw new Error(`cold-start fetch ${r.status}`);
  const txt = await r.text();
  const rows = [];
  for (const line of txt.split("\n")) {
    if (!line) continue;
    try { rows.push(JSON.parse(line)); } catch (_) {}
  }
  return rows;
}

// Bucket ~6000 rows into ~180 uniform sim-time buckets, computing per-bucket
// sla count (new), claim count (new), complaint count (new), mean sla price.
function bucketize(rows) {
  if (!rows.length) return { buckets: [], t0: 0, t1: 0 };
  const times = rows.map(r => Date.parse(r.sim_time));
  const t0 = times[0];
  const t1 = times[times.length - 1];
  const span = Math.max(1, t1 - t0);
  const buckets = Array.from({ length: BUCKETS }, () => ({
    sla: 0, claim: 0, complaint: 0, price_sum: 0, price_n: 0,
  }));
  rows.forEach((r, i) => {
    const bIdx = Math.min(BUCKETS - 1, Math.floor(((times[i] - t0) / span) * BUCKETS));
    const b = buckets[bIdx];
    const t = r.artifact.type;
    if (t === "sla") {
      b.sla++;
      const p = parseFloat(r.artifact.payload.payload.price_per_gb);
      if (!isNaN(p)) { b.price_sum += p; b.price_n++; }
    } else if (t === "claim") {
      b.claim++;
    } else if (t === "complaint") {
      b.complaint++;
    }
  });
  // Carry-forward mean price (so the sparkline has no gaps in buckets with no SLA mints).
  let lastPrice = null;
  buckets.forEach(b => {
    if (b.price_n) { b.price_mean = b.price_sum / b.price_n; lastPrice = b.price_mean; }
    else { b.price_mean = lastPrice; }
  });
  return { buckets, t0, t1 };
}

function sparklinePath(values, w, h, yMin, yMax, upto) {
  if (!values.length) return "";
  const span = Math.max(1e-9, yMax - yMin);
  const n = upto != null ? upto : values.length;
  const pts = [];
  for (let i = 0; i < n; i++) {
    const v = values[i];
    if (v == null) continue;
    const x = (i / (values.length - 1)) * w;
    const y = h - ((v - yMin) / span) * h;
    pts.push(`${x.toFixed(1)},${y.toFixed(1)}`);
  }
  return pts.length ? "M" + pts.join(" L") : "";
}

function ColdStartPanel() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [data, setData] = useState(null);
  const [progress, setProgress] = useState(0); // 0..1 through replay
  const [running, setRunning] = useState(false);
  const rafRef = useRef(null);
  const startRef = useRef(null);
  const resumeAtRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    fetchColdStart()
      .then(rows => { if (!cancelled) { setData(bucketize(rows)); setLoading(false); } })
      .catch(e => { if (!cancelled) { setError(String(e)); setLoading(false); } });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!running) return;
    startRef.current = performance.now() - resumeAtRef.current * REPLAY_WALLCLOCK_SECONDS * 1000;
    const step = (now) => {
      const elapsed = (now - startRef.current) / 1000;
      const p = Math.min(1, elapsed / REPLAY_WALLCLOCK_SECONDS);
      setProgress(p);
      if (p >= 1) { setRunning(false); return; }
      rafRef.current = requestAnimationFrame(step);
    };
    rafRef.current = requestAnimationFrame(step);
    return () => { if (rafRef.current) cancelAnimationFrame(rafRef.current); };
  }, [running]);

  const onPlay = () => {
    if (progress >= 1) { resumeAtRef.current = 0; setProgress(0); }
    else { resumeAtRef.current = progress; }
    setRunning(true);
  };
  const onPause = () => { setRunning(false); };
  const onRestart = () => { setRunning(false); setProgress(0); resumeAtRef.current = 0; };

  if (loading) {
    return <div className="cold-start">
      <div className="cold-head"><span className="title">Cold-start replay</span></div>
      <div className="cold-loading">fetching {COLD_START_URL} …</div>
    </div>;
  }
  if (error) {
    return <div className="cold-start">
      <div className="cold-head"><span className="title">Cold-start replay</span></div>
      <div className="cold-err">
        {error}<br />
        <span className="cold-hint">the user-agent must serve /assets/cold_start.jsonl — restart with --assets-dir=assets</span>
      </div>
    </div>;
  }

  const buckets = data.buckets;
  const upto = Math.max(1, Math.floor(progress * buckets.length));
  const priceSeries = buckets.map(b => b.price_mean);
  const slaCum = []; let sSum = 0; buckets.forEach(b => { sSum += b.sla; slaCum.push(sSum); });
  const clmCum = []; let cSum = 0; buckets.forEach(b => { cSum += b.claim; clmCum.push(cSum); });
  const cplCum = []; let xSum = 0; buckets.forEach(b => { xSum += b.complaint; cplCum.push(xSum); });

  const pricesNotNull = priceSeries.filter(v => v != null);
  const priceMin = pricesNotNull.length ? Math.min(...pricesNotNull) : 0;
  const priceMax = pricesNotNull.length ? Math.max(...pricesNotNull) : 1;
  const W = 620, H = 110;

  const simNow = new Date(data.t0 + progress * (data.t1 - data.t0));
  const simHmm = simNow.toISOString().slice(11, 19);
  const slasSoFar = slaCum[upto - 1] || 0;
  const claimsSoFar = clmCum[upto - 1] || 0;
  const complaintsSoFar = cplCum[upto - 1] || 0;
  const currentPrice = priceSeries[upto - 1];

  return (
    <div className="cold-start">
      <div className="cold-head">
        <span className="title">Cold-start replay</span>
        <span className="meta">§9.14 · pitch opener</span>
      </div>
      <div className="cold-sub">
        ~40 minutes of simulated time, compressed to {REPLAY_WALLCLOCK_SECONDS}s. Watch SLAs arrive, claims
        fill the market, complaints rise as quality diverges, and price-per-GB stratify as reputation separates the honest from the cheap.
      </div>
      <div className="cold-controls">
        {running
          ? <button className="cold-btn" onClick={onPause}>⏸ Pause</button>
          : <button className="cold-btn" onClick={onPlay}>{progress > 0 && progress < 1 ? "▶ Resume" : "▶ Play"}</button>}
        <button className="cold-btn" onClick={onRestart}>↺ Restart</button>
        <div className="cold-progress"><div className="bar" style={{ width: `${(progress * 100).toFixed(1)}%` }} /></div>
        <span className="cold-clock">sim&nbsp;{simHmm}</span>
      </div>

      <div className="cold-kpis">
        <div className="kpi"><span className="k">SLAs published</span><span className="v">{slasSoFar}</span></div>
        <div className="kpi"><span className="k">claims filled</span><span className="v">{claimsSoFar}</span></div>
        <div className="kpi"><span className="k">complaints filed</span><span className="v">{complaintsSoFar}</span></div>
        <div className="kpi"><span className="k">avg price / GB</span><span className="v">{currentPrice != null ? currentPrice.toFixed(4) : "—"}</span></div>
      </div>

      <div className="cold-chart">
        <div className="cold-lbl">price per GB (CHF) · SLA-weighted mean</div>
        <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none">
          <defs>
            <linearGradient id="pgrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--gold)" stopOpacity="0.45"/>
              <stop offset="100%" stopColor="var(--gold)" stopOpacity="0"/>
            </linearGradient>
          </defs>
          <path d={sparklinePath(priceSeries, W, H, priceMin, priceMax, upto) + ` L${W},${H} L0,${H} Z`} fill="url(#pgrad)" />
          <path d={sparklinePath(priceSeries, W, H, priceMin, priceMax, upto)} stroke="var(--gold)" strokeWidth="1.6" fill="none" />
        </svg>
      </div>

      <div className="cold-chart">
        <div className="cold-lbl">cumulative activity · SLAs · claims · complaints</div>
        <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none">
          <path d={sparklinePath(clmCum, W, H, 0, clmCum[clmCum.length-1] || 1, upto)} stroke="var(--cyan)" strokeWidth="1.4" fill="none" />
          <path d={sparklinePath(slaCum, W, H, 0, clmCum[clmCum.length-1] || 1, upto)} stroke="var(--green)" strokeWidth="1.4" fill="none" />
          <path d={sparklinePath(cplCum, W, H, 0, clmCum[clmCum.length-1] || 1, upto)} stroke="var(--red)" strokeWidth="1.4" fill="none" />
        </svg>
        <div className="cold-legend">
          <span className="lg"><i style={{background:'var(--cyan)'}}/>claims</span>
          <span className="lg"><i style={{background:'var(--green)'}}/>SLAs</span>
          <span className="lg"><i style={{background:'var(--red)'}}/>complaints</span>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { ColdStartPanel });
