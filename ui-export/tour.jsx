// 90-second guided demo tour.
//
// Drives the viewer through the pitch narrative by spotlighting UI elements
// and triggering real actions (claim, degrade_as, fix_as). Renders a dimming
// overlay with a cutout around the current target plus a callout box that
// carries the voice-over text.
//
// Steps are declarative. `target` is either a CSS selector (pointing at a
// `data-tour="..."` anchor elsewhere in the UI) or `null` for a centered
// modal. `onEnter` fires once when the step becomes active and can drive
// view transitions or POST actions via the pm context.

const TOUR_STEPS = [
  {
    id: 'intro',
    target: null,
    title: 'PathMarket in 90 seconds',
    body: 'A trust and price layer that rides on top of SCION path selection. Keep an eye on the top-right — the market layer toggle — throughout.',
    durationMs: 5000,
  },
  {
    id: 'pm-toggle',
    target: '[data-tour="pm-toggle"]',
    title: 'Market layer is opt-in',
    body: 'Flip off → raw SCION paths, no quality guarantees, no complaint recourse. Flip on → the rest of this demo.',
    durationMs: 9000,
    onEnter: (pm) => {
      // Blink the toggle off then back on so the viewer sees both states.
      pm.setPmOn(false);
      setTimeout(() => pm.setPmOn(true), 2600);
    },
  },
  {
    id: 'destination',
    target: '[data-tour="destination"]',
    title: 'Two candidate paths',
    body: 'Zurich-Financial-Edge → Telia. One direct hop, or through DE-CIX for redundancy. SCION shows them both; PathMarket tells you which to trust.',
    durationMs: 10000,
    onEnter: (pm) => {
      pm.setView('planner');
      pm.setDestinationId('1-ff00:0:130');
    },
  },
  {
    id: 'aggregate',
    target: '[data-tour="aggregate"]',
    title: 'Every path aggregates, live',
    body: 'Coverage, latency, loss, price, and reputation compose from the SLAs applied to each hop. Hover a gold arc to see which consortium signed it.',
    durationMs: 12000,
  },
  {
    id: 'claim',
    target: '[data-tour="path-B"]',
    title: 'One click signs a purchase',
    body: 'The 4-AS cover over Path B. Source AS 112 cosigns its own outbound hop; portfolio, ticker, and aggregate update the moment the claim lands.',
    durationMs: 13000,
    onEnter: async (pm) => {
      const pathB = pm.paths.find(p => p.hops.length >= 4);
      if (!pathB) return;
      const appliedHere = pm.claimedByPath[pathB.id] || [];
      // Prefer the full-cover SLA (longest matching segment) so rep comparisons
      // in the reshop step have a single SLA to track per path.
      const fits = Object.values(pm.slaById)
        .filter(s => !appliedHere.includes(s.id) && window.placeSla && window.placeSla(pathB, s))
        .sort((a, b) => (b.segment?.length || 0) - (a.segment?.length || 0));
      if (fits[0]) {
        try { await pm.addClaim(pathB.id, fits[0].id); } catch (_) {}
      }
    },
  },
  {
    id: 'trouble',
    target: '[data-tour="leaderboard"]',
    title: 'Trouble on the backhaul',
    body: 'AS 1-ff00:0:120 starts violating every SLA it cosigns. Buyers resample, complaints accumulate, k-corroboration fires. Reputation falls — live.',
    durationMs: 14000,
    onEnter: async (pm) => {
      pm.setView('leaderboard');
      const S = window.PM_SCENARIOS || {};
      if (S.degrade_as) {
        try { await S.degrade_as('1-ff00:0:120'); } catch (_) {}
      }
    },
  },
  {
    id: 'complaints',
    target: '[data-tour="complaint-log"]',
    title: 'k-corroboration, not he-said-she-said',
    body: '≥3 distinct complainants on the same metric within 5 minutes → decay. A single attacker cannot forge this; that is what makes the score earn its keep.',
    durationMs: 11000,
    onEnter: (pm) => pm.setView('complaints'),
  },
  {
    id: 'reshop',
    target: '[data-tour="path-A"]',
    title: 'The score just changed the route',
    body: "Path B's cover included AS 120 — its rep just tanked. Path A's direct SLA was untouched. One click switches you onto the path the market now endorses.",
    durationMs: 13000,
    onEnter: async (pm) => {
      pm.setView('planner');
      const pathA = pm.paths.find(p => p.hops.length === 2);
      if (!pathA) return;
      const appliedHere = pm.claimedByPath[pathA.id] || [];
      const fits = Object.values(pm.slaById)
        .filter(s => !appliedHere.includes(s.id) && window.placeSla && window.placeSla(pathA, s))
        .sort((a, b) => (b.segment?.length || 0) - (a.segment?.length || 0));
      if (fits[0]) {
        try { await pm.addClaim(pathA.id, fits[0].id); } catch (_) {}
      }
      pm.setSelectedPathId && pm.setSelectedPathId(pathA.id);
    },
  },
  {
    id: 'outro',
    target: null,
    title: "That's PathMarket",
    body: 'Offer → claim → complaint → score → better choice. Every number you just saw came from signed artifacts, not from a trusted oracle.',
    durationMs: 4000,
  },
];

// -----------------------------------------------------------------------------
// Overlay — dimming layer + spotlight cutout + callout
// -----------------------------------------------------------------------------

function TourOverlay({ step, stepIndex, total, progress, onSkip, onPause, paused }) {
  const [rect, setRect] = useState(null);

  useEffect(() => {
    if (!step) return;
    let raf;
    const update = () => {
      if (step.target) {
        const el = document.querySelector(step.target);
        if (el) {
          const r = el.getBoundingClientRect();
          setRect({ x: r.left, y: r.top, w: r.width, h: r.height });
        } else {
          setRect(null);
        }
      } else {
        setRect(null);
      }
      raf = requestAnimationFrame(update);
    };
    update();
    return () => cancelAnimationFrame(raf);
  }, [step]);

  if (!step) return null;

  // Decide where to place the callout relative to the target.
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const pad = 14;
  const CW = 480;  // callout width
  const CH = 220;  // callout height (approx; height is auto)
  let cx, cy;
  if (!rect) {
    cx = (vw - CW) / 2;
    cy = (vh - CH) / 2;
  } else {
    // Prefer below, else above, else right, else left.
    const below = rect.y + rect.h + pad + CH <= vh;
    const above = rect.y - pad - CH >= 0;
    if (below) {
      cy = rect.y + rect.h + pad;
      cx = Math.max(16, Math.min(vw - CW - 16, rect.x + rect.w / 2 - CW / 2));
    } else if (above) {
      cy = rect.y - pad - CH;
      cx = Math.max(16, Math.min(vw - CW - 16, rect.x + rect.w / 2 - CW / 2));
    } else {
      cy = Math.max(16, Math.min(vh - CH - 16, rect.y + rect.h / 2 - CH / 2));
      cx = rect.x + rect.w + pad;
      if (cx + CW > vw - 16) cx = Math.max(16, rect.x - pad - CW);
    }
  }

  const spot = rect ? {
    left: rect.x - 6, top: rect.y - 6,
    width: rect.w + 12, height: rect.h + 12,
  } : null;

  return (
    <div className="tour-root">
      {/* Four panels forming a frame around the spotlight so the target
          stays visually un-dimmed without relying on clip-path. */}
      {spot ? (
        <>
          <div className="tour-mask" style={{ left: 0, top: 0, width: '100%', height: spot.top }} />
          <div className="tour-mask" style={{ left: 0, top: spot.top + spot.height, width: '100%', height: `calc(100% - ${spot.top + spot.height}px)` }} />
          <div className="tour-mask" style={{ left: 0, top: spot.top, width: spot.left, height: spot.height }} />
          <div className="tour-mask" style={{ left: spot.left + spot.width, top: spot.top, width: `calc(100% - ${spot.left + spot.width}px)`, height: spot.height }} />
          <div className="tour-spot-ring" style={spot} />
        </>
      ) : (
        <div className="tour-mask" style={{ left: 0, top: 0, width: '100%', height: '100%' }} />
      )}

      <div className="tour-callout" style={{ left: cx, top: cy, width: CW }}>
        <div className="tour-callout-head">
          <span className="tour-step">{stepIndex + 1} / {total}</span>
          <span className="tour-title">{step.title}</span>
        </div>
        <div className="tour-body">{step.body}</div>
        <div className="tour-progress"><div className="tour-progress-fill" style={{ width: `${Math.min(100, progress * 100)}%` }} /></div>
        <div className="tour-controls">
          <button className="tour-btn" onClick={onPause}>{paused ? '▶ Resume' : '⏸ Pause'}</button>
          <button className="tour-btn ghost" onClick={onSkip}>Skip tour ✕</button>
        </div>
      </div>
    </div>
  );
}

// -----------------------------------------------------------------------------
// Controller — owns step index, timers, and action orchestration
// -----------------------------------------------------------------------------

function TourController() {
  const pm = usePM();
  const { tourRunning, stopTour } = pm;
  const [stepIndex, setStepIndex] = useState(0);
  const [paused, setPaused] = useState(false);
  const [progress, setProgress] = useState(0);
  const timeoutsRef = useRef([]);
  const rafRef = useRef(null);
  const stepStartRef = useRef(0);
  const pausedAtRef = useRef(null);

  // Clear all timers — called on skip, unmount, or step advance.
  const clearTimers = useCallback(() => {
    timeoutsRef.current.forEach(t => clearTimeout(t));
    timeoutsRef.current = [];
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
  }, []);

  // Run the current step: fire onEnter, start the duration timer, animate
  // the progress bar.
  useEffect(() => {
    if (!tourRunning) return;
    if (stepIndex >= TOUR_STEPS.length) { stopTour(); return; }
    const step = TOUR_STEPS[stepIndex];
    try { step.onEnter && step.onEnter(pm); } catch (e) { console.warn('tour onEnter', e); }

    stepStartRef.current = performance.now();
    setProgress(0);
    const animate = () => {
      if (paused) return;
      const elapsed = performance.now() - stepStartRef.current;
      setProgress(elapsed / step.durationMs);
      if (elapsed < step.durationMs) {
        rafRef.current = requestAnimationFrame(animate);
      }
    };
    rafRef.current = requestAnimationFrame(animate);

    const t = setTimeout(() => {
      setStepIndex(i => i + 1);
    }, step.durationMs);
    timeoutsRef.current.push(t);

    return clearTimers;
  }, [tourRunning, stepIndex]);

  // Pause support: track how long we were paused and push the end forward.
  const onPause = useCallback(() => {
    setPaused(p => {
      const next = !p;
      if (next) {
        pausedAtRef.current = performance.now();
        clearTimers();
      } else {
        const pausedDuration = performance.now() - (pausedAtRef.current || performance.now());
        stepStartRef.current += pausedDuration;
        const step = TOUR_STEPS[stepIndex];
        const remaining = Math.max(0, step.durationMs - (performance.now() - stepStartRef.current));
        const t = setTimeout(() => setStepIndex(i => i + 1), remaining);
        timeoutsRef.current.push(t);
        const animate = () => {
          if (paused) return;
          const elapsed = performance.now() - stepStartRef.current;
          setProgress(elapsed / step.durationMs);
          if (elapsed < step.durationMs) rafRef.current = requestAnimationFrame(animate);
        };
        rafRef.current = requestAnimationFrame(animate);
      }
      return next;
    });
  }, [stepIndex, paused, clearTimers]);

  // Reset on stop so the next start begins fresh at step 0.
  useEffect(() => {
    if (!tourRunning) {
      clearTimers();
      setStepIndex(0);
      setPaused(false);
      setProgress(0);
    }
  }, [tourRunning, clearTimers]);

  if (!tourRunning) return null;
  const step = TOUR_STEPS[stepIndex];
  if (!step) return null;

  return (
    <TourOverlay
      step={step}
      stepIndex={stepIndex}
      total={TOUR_STEPS.length}
      progress={progress}
      paused={paused}
      onPause={onPause}
      onSkip={stopTour}
    />
  );
}

Object.assign(window, { TourController, TOUR_STEPS });
