// PathMarket data bootstrap — fetches live state from the user-agent and
// aggregator at page load, assembles window.PM_DATA in the shape the rest of
// the UI (app.jsx, *.jsx) already expects. Rendering is deferred in app.jsx
// until window.PM_BOOT() resolves.

(() => {
  const AGGREGATOR_URL = (window.PM_CONFIG && window.PM_CONFIG.aggregatorUrl) || "http://127.0.0.1:8080";
  const SIMULATOR_URL = (window.PM_CONFIG && window.PM_CONFIG.simulatorUrl) || "http://127.0.0.1:8081";
  const LOCAL_BASE = ""; // same-origin — user agent serves both UI and /local/*

  // Cosmetic AS metadata (names, roles, ISD). Live scores/reputation override
  // any value here. Unknown ASes fall back to the ISD-AS id as name.
  const AS_META = [
    { id: "1-ff00:0:110", name: "Swisscom-CH",           isd: 1, role: "Tier-1"  },
    { id: "1-ff00:0:111", name: "Init7-Zurich",          isd: 1, role: "Transit" },
    { id: "1-ff00:0:112", name: "Zurich-Financial-Edge", isd: 1, role: "Edge"    },
    { id: "1-ff00:0:120", name: "Deutsche-Telekom",      isd: 1, role: "Tier-1"  },
    { id: "1-ff00:0:121", name: "Frankfurt-Transit",     isd: 1, role: "Transit" },
    { id: "1-ff00:0:122", name: "DE-CIX-Edge",           isd: 1, role: "IXP"     },
    { id: "1-ff00:0:130", name: "Telia-Nordic",          isd: 1, role: "Tier-1"  },
    { id: "1-ff00:0:131", name: "Stockholm-Carrier",     isd: 1, role: "Transit" },
    { id: "1-ff00:0:132", name: "Oslo-Edge",             isd: 1, role: "Edge"    },
    { id: "1-ff00:0:133", name: "Helsinki-Financial",    isd: 1, role: "Tier-1"  },
    { id: "2-ff00:0:210", name: "London-IX",             isd: 2, role: "Tier-1"  },
    { id: "2-ff00:0:211", name: "Milan-Backbone",        isd: 2, role: "Transit" },
    { id: "2-ff00:0:220", name: "Cogent-EU",             isd: 2, role: "Tier-1"  },
    { id: "2-ff00:0:221", name: "Amsterdam-Hub",         isd: 2, role: "Transit" },
  ];

  // Destinations the UI shows by default. Both correspond to real SCION ASes
  // in the default 16-AS topology, so SCION and static modes see the same set.
  const DEFAULT_DESTINATIONS = ["1-ff00:0:130", "1-ff00:0:133"];

  // -------- HTTP helpers --------
  async function jget(url) {
    const r = await fetch(url, { headers: { accept: "application/json" }});
    if (!r.ok) throw new Error(`GET ${url} → ${r.status}`);
    return r.json();
  }
  async function jpost(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text().catch(() => "");
      throw new Error(`POST ${url} → ${r.status} ${t}`);
    }
    return r.json();
  }
  async function jput(url, body) {
    const r = await fetch(url, {
      method: "PUT",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text().catch(() => "");
      throw new Error(`PUT ${url} → ${r.status} ${t}`);
    }
    return r.json();
  }

  // -------- shape transforms --------

  function daysUntil(iso) {
    if (!iso) return null;
    const ms = (new Date(iso)).getTime() - Date.now();
    return Math.max(0, Math.round(ms / 86400000));
  }

  // Aggregator SLA wire → UI SLA shape. `scoreByAs` is a {isd_as: score} map.
  function toUiSla(wire, scoreByAs) {
    const p = wire.payload;
    const segment = p.path.map(h => h.isd_as);
    const cosignerScores = p.cosigners.map(a => scoreByAs[a]).filter(s => s != null);
    const rep = cosignerScores.length ? Math.min(...cosignerScores) : null;
    return {
      id: p.sla_id,
      segment,
      cosigners: p.cosigners.slice(),
      latency: p.bounds.latency_max_ms,
      loss:    p.bounds.loss_max_ppm,
      bw:      p.bounds.bandwidth_min_kbps,
      price:   parseFloat(p.price_per_gb),
      rep,
      expiry_days: daysUntil(p.valid_until),
      valid_from:  p.valid_from,
      valid_until: p.valid_until,
    };
  }

  // /local/portfolio claim wire → UI portfolio entry
  function toUiPortfolioEntry(entry, slaById) {
    const p = entry.claim.payload;
    const sla = slaById[p.sla_id];
    return {
      sla: p.sla_id,
      path_id: null, // set by routing decision (applied_claims mapping); not tracked here
      purchased_gb: p.gb_purchased,
      consumed_gb: entry.consumed_gb || 0,
      price_paid: parseFloat(p.price_paid_chf),
      claim_time: p.claimed_at,
      reputation_at_claim: sla ? sla.rep : null,
    };
  }

  // Aggregator complaint wire → UI complaint shape
  function toUiComplaint(wire, slaById) {
    const p = wire.payload;
    const sla = slaById[p.sla_id];
    let bound = null;
    if (sla) {
      if (p.metric === "latency")   bound = sla.latency;
      else if (p.metric === "loss") bound = sla.loss;
      else if (p.metric === "bandwidth") bound = sla.bw;
    }
    return {
      id: p.complaint_id,
      ts: p.observed_at,
      complainant: p.complainant,
      sla: p.sla_id,
      metric: p.metric,
      measured: p.measured_value,
      bound,
      note: p.note || "",
      attachments: (p.attachments || []).map(a => a.filename),
    };
  }

  // user-agent candidate_routes → PATHS[dst] list of {id,label,hops,initial_claims}
  function toUiPaths(candidateRoutesBody) {
    const dst = candidateRoutesBody.dst_isd_as;
    const out = [];
    (candidateRoutesBody.candidates || []).forEach((c, idx) => {
      const hops = c.path.map(h => h.isd_as);
      // pick the first full-coverage option's claim ids for "initial_claims"
      const opts = c.coverage_options || [];
      const full = opts.find(o => o.aggregate && o.aggregate.covered_hops === o.aggregate.total_hops);
      const initial_claims = (full ? full.claim_ids : []) || [];
      out.push({
        id: `P-${dst}-${idx}`,
        label: `Path ${String.fromCharCode(65 + idx)} · ${hops.length - 1} hops`,
        hops,
        initial_claims,
      });
    });
    return out;
  }

  // -------- bootstrap --------

  let _slaByIdCache = {};
  let _scoreByAsCache = {};

  async function fetchAllAses() {
    try {
      const { scores } = await jget(`${AGGREGATOR_URL}/scores`);
      const byAs = {};
      scores.forEach(s => { byAs[s.isd_as] = s.score; });
      _scoreByAsCache = byAs;
      // Merge meta + scores; include any AS referenced by a score that isn't in meta.
      const known = new Set(AS_META.map(a => a.id));
      const list = AS_META.map(a => ({ ...a, rep: byAs[a.id] ?? null, self: a.id === window.PM_DATA.ME }));
      scores.forEach(s => {
        if (!known.has(s.isd_as)) {
          list.push({ id: s.isd_as, name: s.isd_as, isd: 1, role: "unknown", rep: s.score });
        }
      });
      return list;
    } catch (e) {
      console.warn("fetchAllAses failed", e);
      return AS_META.map(a => ({ ...a, rep: null }));
    }
  }

  async function fetchAllSlas() {
    try {
      const { slas } = await jget(`${AGGREGATOR_URL}/slas?active_only=true`);
      const uiSlas = slas.map(s => toUiSla(s, _scoreByAsCache));
      const byId = {};
      uiSlas.forEach(s => { byId[s.id] = s; });
      _slaByIdCache = byId;
      return uiSlas;
    } catch (e) {
      console.warn("fetchAllSlas failed", e);
      _slaByIdCache = {};
      return [];
    }
  }

  async function fetchPortfolio() {
    try {
      const { claims } = await jget(`${LOCAL_BASE}/local/portfolio`);
      return claims.map(c => toUiPortfolioEntry(c, _slaByIdCache));
    } catch (e) {
      console.warn("fetchPortfolio failed", e);
      return [];
    }
  }

  async function fetchComplaints() {
    try {
      const { complaints } = await jget(`${AGGREGATOR_URL}/complaints`);
      return complaints.map(c => toUiComplaint(c, _slaByIdCache)).reverse(); // newest first
    } catch (e) {
      console.warn("fetchComplaints failed", e);
      return [];
    }
  }

  async function fetchTicker() {
    try {
      const { events } = await jget(`${AGGREGATOR_URL}/ticker?limit=40`);
      return events.map(e => ({ kind: e.type, text: e.detail, ts: e.timestamp }));
    } catch (e) {
      console.warn("fetchTicker failed", e);
      return [];
    }
  }

  async function fetchPathsForDestinations(dsts) {
    const paths = {};
    for (const dst of dsts) {
      try {
        const body = await jget(`${LOCAL_BASE}/local/candidate_routes?dst=${encodeURIComponent(dst)}`);
        paths[dst] = toUiPaths(body);
      } catch (e) {
        console.warn(`fetchPaths(${dst}) failed`, e);
        paths[dst] = [];
      }
    }
    return paths;
  }

  async function fetchLocalState() {
    try {
      return await jget(`${LOCAL_BASE}/local/state`);
    } catch (e) {
      console.warn("fetchLocalState failed", e);
      return { isd_as: "1-ff00:0:112", display_name: "Zurich-Financial-Edge", policy: null };
    }
  }

  // Seed an empty PM_DATA so helpers that touch window.PM_DATA.ME during boot
  // don't explode.
  window.PM_DATA = {
    ME: "1-ff00:0:112",
    ME_NAME: "Zurich-Financial-Edge",
    POLICY: null,
    ASES: [], SLAS: [], PATHS: {}, COMPLAINTS: [], INITIAL_PORTFOLIO: [], TICKER_SEEDS: [],
  };

  async function boot() {
    const localState = await fetchLocalState();
    window.PM_DATA.ME = localState.isd_as;
    window.PM_DATA.ME_NAME = localState.display_name;
    window.PM_DATA.POLICY = localState.policy;

    // Scores first (SLA rep depends on them).
    const ASES = await fetchAllAses();
    const SLAS = await fetchAllSlas();
    const [PATHS, COMPLAINTS, TICKER_SEEDS, INITIAL_PORTFOLIO] = await Promise.all([
      fetchPathsForDestinations(DEFAULT_DESTINATIONS),
      fetchComplaints(),
      fetchTicker(),
      fetchPortfolio(),
    ]);

    window.PM_DATA = {
      ...window.PM_DATA,
      ASES, SLAS, PATHS, COMPLAINTS, INITIAL_PORTFOLIO, TICKER_SEEDS,
      DEFAULT_DESTINATIONS,
    };
  }

  // Light refresh — recomputes everything except state. Callers merge into
  // React state (preserving local-only demo mutations).
  async function refresh() {
    const ASES = await fetchAllAses();
    const SLAS = await fetchAllSlas();
    const [PATHS, COMPLAINTS, TICKER_SEEDS, INITIAL_PORTFOLIO] = await Promise.all([
      fetchPathsForDestinations(window.PM_DATA.DEFAULT_DESTINATIONS || DEFAULT_DESTINATIONS),
      fetchComplaints(),
      fetchTicker(),
      fetchPortfolio(),
    ]);
    return { ASES, SLAS, PATHS, COMPLAINTS, TICKER_SEEDS, PORTFOLIO: INITIAL_PORTFOLIO };
  }

  // -------- action wrappers (used by app.jsx handlers) --------

  async function postClaim(slaId, gb) {
    return jpost(`${LOCAL_BASE}/local/actions/claim`, { sla_id: slaId, gb_purchased: gb });
  }
  async function postComplaint({ sla_id, metric, measured_value, note, attachments }) {
    return jpost(`${LOCAL_BASE}/local/actions/complaint`, {
      sla_id, metric, measured_value, note: note || "",
      attachments: attachments || null,
    });
  }
  async function postUploadSla(body) {
    return jpost(`${LOCAL_BASE}/local/actions/upload_sla`, body);
  }
  async function putPolicy(policy) {
    return jput(`${LOCAL_BASE}/local/policy`, policy);
  }
  async function putRouting(dst, decision) {
    return jput(`${LOCAL_BASE}/local/routing/${encodeURIComponent(dst)}`, decision);
  }

  // -------- simulator scenario API (best-effort) --------
  // Scenarios also work if the simulator is offline — the UI falls back to
  // pure client-side theatre. This keeps the demo resilient when pitching
  // without a running simulator backend.
  async function callSimulator(path, body) {
    try {
      const r = await fetch(`${SIMULATOR_URL}${path}`, {
        method: "POST",
        headers: { "content-type": "application/json", accept: "application/json" },
        body: JSON.stringify(body || {}),
      });
      if (!r.ok) {
        const t = await r.text().catch(() => "");
        console.warn(`simulator ${path} -> ${r.status} ${t}`);
        return { ok: false, status: r.status };
      }
      return { ok: true, data: await r.json() };
    } catch (e) {
      console.warn(`simulator ${path} unreachable`, e);
      return { ok: false, offline: true };
    }
  }
  const SCENARIOS = {
    reset: (preseedCount) => callSimulator("/scenarios/reset", { preseed_count: preseedCount }),
    hospital_reshops: (slaId) => callSimulator("/scenarios/hospital_reshops", { target_sla_id: slaId || null }),
    cloud_bargain: (price) => callSimulator("/scenarios/cloud_bargain", { price_per_gb: price || "0.008" }),
    bad_actor_cascade: (isdAs) => callSimulator("/scenarios/bad_actor_cascade", { isd_as: isdAs }),
    degrade_as: (isdAs) => callSimulator("/scenarios/degrade_as", { isd_as: isdAs }),
    fix_as: (isdAs) => callSimulator("/scenarios/fix_as", { isd_as: isdAs }),
  };

  window.PM_BOOT = boot;
  window.PM_REFRESH = refresh;
  window.PM_ACTIONS = { postClaim, postComplaint, postUploadSla, putPolicy, putRouting };
  window.PM_SCENARIOS = SCENARIOS;
})();
