# PathMarket — Implementation Walkthrough

## 1. What PathMarket is — and isn't

PathMarket is a **market-and-reputation overlay for SCION transit** that operates entirely above the dataplane. Three local Python processes turn configured SCION-style AS identities into economic actors that can:

1. Publish signed transit guarantees (SLAs) over a path segment.
2. Sign claims against those guarantees as a buyer.
3. File signed complaints when an SLA is allegedly violated. In the simulator this is driven by agents' claimed SLAs; the aggregator itself only requires that the complainant is not a cosigner.
4. Have its reputation rise or fall based on k-corroborated complaints.
5. Browse, compose, and select routes annotated with the resulting market data.

It is deliberately **not**:

- A SCION dataplane modification. Zero lines of Go; no PRs to `scionproto/scion`.
- A traffic-accounting or measurement system. Quality observations are sender-attested, and during the demo they are sampled from a probabilistic quality model — not measured from real packets.
- A consortium-formation service. SLAs arrive at the aggregator already fully signed by all cosigners; how cosigners agreed offline is out of scope.
- A TRC-rooted trust verifier. Trust in signing keys is stubbed against a static keyring.
- A persistent system. Aggregator and simulator state is in-memory; only the user-AS persists state to a single JSON file.

The demo's claim is narrow: **the market visibly stratifies under k-corroborated complaints, and the operator UI exposes that stratification as route-selection signal.** Closing the autonomous-routing loop is out of scope (see §10).

---

## 2. Three-process architecture

| Process | Configured bind | Source root | Persists state |
| --- | --- | --- | --- |
| Aggregator | `0.0.0.0:8080` (opened locally as `127.0.0.1:8080`) | [src/pathmarket/aggregator/](src/pathmarket/aggregator/) | No (in-memory) |
| Simulator | `0.0.0.0:8081` (scenario API only) | [src/pathmarket/simulator/](src/pathmarket/simulator/) | No |
| User agent + UI | `0.0.0.0:8090` (opened locally as `127.0.0.1:8090`) | [src/pathmarket/user_agent/](src/pathmarket/user_agent/) | Yes ([runtime/user_as_state.json](runtime/)) |

Shared library surface (used by all three):

| Module | Purpose |
| --- | --- |
| [schemas.py](src/pathmarket/schemas.py) | All frozen dataclasses (SLA / Claim / Complaint payloads + signed envelopes, Score, ViolationEvent, Policy, RoutingDecision) |
| [canonical.py](src/pathmarket/canonical.py) | `canonical_json()` and the three `compute_*_id()` content-hash functions |
| [verifier/static.py](src/pathmarket/verifier/static.py) | `StaticKeyVerifier` — Ed25519 verify against keyring.json |
| [scorer/scorer.py](src/pathmarket/scorer/scorer.py) | `compute_violation_events`, `compute_score` |
| [agent/agent.py](src/pathmarket/agent/agent.py) | The shared `Agent` class — 13 instances in the simulator + 1 in the user agent |
| [path_discovery/](src/pathmarket/path_discovery/) | Pluggable `PathDiscovery` protocol; `StaticPathTableDiscovery` and `ScionShowpathsDiscovery` implementations |
| [client.py](src/pathmarket/client.py) | `httpx`-backed `AggregatorClient` |

The aggregator is the only HTTP server the simulator talks to. The UI talks to both the user agent (`/local/*`) and the aggregator directly (`/scores`, `/slas`, `/complaints`, `/ticker`); simulator scenarios are called directly on `:8081` from the presenter panel.

---

## 3. Data model: signed artifacts

All payloads are `@dataclass(frozen=True)`. Canonical JSON serialization ([canonical.py](src/pathmarket/canonical.py)) uses sorted keys, compact separators, ASCII escaping, and UTF-8 bytes. Artifact IDs are computed over a copy of the payload with the `*_id` field zeroed out, then SHA-256'd and re-inserted. Signatures are then computed over the canonical bytes of the fully populated payload, including the content-hash ID. This makes IDs collision-resistant and pins the payload's exact byte sequence as the signing target.

### 3.1 SignedSLA

```python
SLAPayload(
    sla_id, path: list[PathHop], cosigners: list[str], bounds: SLABounds,
    price_per_gb, valid_from, valid_until, nonce, schema_version=2
)
SignedSLA(payload, signatures: list[Signature])
```

Each `Signature` carries `meta.isd_as` + `meta.key_id` + raw 64-byte Ed25519 sig. `cosigners == [hop.isd_as for hop in path]` exactly — the cosigner list mirrors the path. There is exactly one signature per cosigner.

### 3.2 SignedClaim

```python
ClaimPayload(claim_id, sla_id, claimant, gb_purchased, claimed_at, price_paid_chf, schema_version=2)
SignedClaim(payload, signature: Signature)
```

`price_paid_chf == Decimal(sla.price_per_gb) * Decimal(gb_purchased)` is enforced by the aggregator with **value equality on `Decimal`**, not string equality. One signature, by the claimant.

### 3.3 SignedComplaint

```python
ComplaintPayload(
    complaint_id, sla_id, complainant, path_used: list[PathHop],
    metric: "latency_ms"|"loss_ppm"|"bandwidth_kbps",
    measured_value: int, observed_at,
    note: str, attachments: list[Attachment], schema_version=2
)
SignedComplaint(payload, signature: Signature)
```

`note` and `attachments` are part of the signed payload, so they are tamper-evident. Attachments are inline base64 with hard limits (≤3 attachments, ≤64 KiB each, kind ∈ {`text`, `json`, `log`}).

### 3.4 Score, ViolationEvent, Policy, RoutingDecision

`ViolationEvent` is what the scorer attributes per `(sla_id, metric)` window crossing; `attributed_to` is the cosigner list of the SLA. `Policy` is the per-AS declarative configuration (price/reputation/required-bounds gates plus utility weights `alpha`, `beta`). `RoutingDecision` is the user-AS's persisted route choice for one destination.

Full definitions: [schemas.py](src/pathmarket/schemas.py).

---

## 4. The aggregator: validation chains

The aggregator is a single FastAPI app in [src/pathmarket/aggregator/app.py](src/pathmarket/aggregator/app.py) with 13 PathMarket routes when admin reset is enabled (`/health`, 3 write routes, 8 read/query routes, and `/admin/reset`). Storage is in-process Python dicts ([storage.py](src/pathmarket/aggregator/storage.py)). The interesting code is the validation chains in [validation.py](src/pathmarket/aggregator/validation.py).

### 4.1 `POST /sla` — 9-step verification chain

[validation.py:73-241](src/pathmarket/aggregator/validation.py#L73-L241). Short-circuits on the first failing step; the response body is the **full step-by-step report** (a list of `{step, ok, detail}` records). The backend and UI action wrapper for upload exist, but the current visible UI does not include a full Submit SLA checklist view.

| # | Step | What it checks |
| --- | --- | --- |
| 1 | `parse_json` | Pydantic-decodes into `SignedSLAModel` |
| 2 | `schema_version` | `payload.schema_version == 2` |
| 3 | `content_hash` | `compute_sla_id(payload) == payload.sla_id` ([validation.py:110](src/pathmarket/aggregator/validation.py#L110)) |
| 4 | `validity_window` | Parseable, `valid_until > valid_from` |
| 5 | `bounds_nonempty` | At least one of `latency_max_ms` / `loss_max_ppm` / `bandwidth_min_kbps` is set |
| 6 | `cosigners_match_path` | `cosigners == [hop.isd_as for hop in path]` and `len(cosigners) ≥ 2` |
| 7 | `signature_count` | One signature per cosigner, no duplicates, no missing |
| 8 | `signature_verify` | **Ed25519 verify each cosigner's signature individually** against the canonical bytes ([validation.py:219-231](src/pathmarket/aggregator/validation.py#L219-L231)) |
| 9 | `no_duplicate` | `sla_id` not already in storage |

A submission with one bad signature out of N produces a verification report with the first 7 steps green, step 8 showing exactly which cosigner failed, and the SLA rejected. This is the response shape that motivates the "live verification" UX.

### 4.2 `POST /claim` — 9 short-circuiting steps

[validation.py:269-362](src/pathmarket/aggregator/validation.py#L269-L362). Notable specifics:

- **Self-dealing exception for source AS** ([validation.py:317-327](src/pathmarket/aggregator/validation.py#L317-L327)): claimant must not be a cosigner, **except** when they are the first cosigner — the path's source AS is the natural buyer of SLAs covering its outbound egress.
- **Decimal-value price arithmetic** ([validation.py:336-346](src/pathmarket/aggregator/validation.py#L336-L346)): `Decimal(price_paid_chf) == Decimal(price_per_gb) * Decimal(gb_purchased)` — value equality, so `"1.0"` and `"1.00"` are accepted equivalently.
- **Signature meta-binding** ([validation.py:348-353](src/pathmarket/aggregator/validation.py#L348-L353)): `signature.meta.isd_as == payload.claimant`. You cannot submit a payload claiming to be from AS X with a (valid) signature from AS Y.
- **SLA must exist and be inside its validity window** ([validation.py:302-315](src/pathmarket/aggregator/validation.py#L302-L315)).

### 4.3 `POST /complaint` — 13 short-circuiting steps

[validation.py:371-522](src/pathmarket/aggregator/validation.py#L371-L522). The authorization-relevant ones:

- **Step 6, complainant ∉ cosigners** ([validation.py:425-430](src/pathmarket/aggregator/validation.py#L425-L430)) — a cosigner cannot complain about an SLA it signed. This is the only "is this complainant allowed to file?" gate. There is no requirement that the complainant own a `SignedClaim` on the SLA — buyers and downstream observers may both have grounds. This is a deliberate v2 choice.
- **Step 5, `path_used` exactly matches `sla.path`** ([validation.py:419-423](src/pathmarket/aggregator/validation.py#L419-L423)).
- **Step 7-8, metric whitelist + bound presence** ([validation.py:432-443](src/pathmarket/aggregator/validation.py#L432-L443)) — you cannot complain about latency on an SLA that promised only bandwidth.
- **Step 11, signature meta-binding** as for claims.
- **Step 12, Ed25519 signature verify**.
- **Step 13, dedup window** ([validation.py:499-520](src/pathmarket/aggregator/validation.py#L499-L520)) — `(complainant, sla_id, metric)` within a 5-minute default window collapses to one. This stops one AS from spamming the same complaint to inflate its weight inside the scorer's distinct-complainant count.
- Attachment validation: kind ∈ {text, json, log}, ≤128-char filename with no path separators, valid base64 with decoded size ≤64 KiB, ≤3 attachments per complaint ([validation.py:445-482](src/pathmarket/aggregator/validation.py#L445-L482)).

### 4.4 What the aggregator does *not* check

- **Truth of the measurement.** A complainant can fabricate `measured_value`. The only mitigation is k-corroboration in the scorer.
- **Whether the complainant actually used the path.** No delivery receipts, no traffic proof. Sender-attested.
- **Collusion of k+ ASes.** If 3 keyring ASes coordinate, they can drive any non-cosigner-controlled SLA's reputation toward zero.
- **Sybil attacks via fresh AS identities.** The static keyring makes this explicit: trust is the keyring; widening it is a trust decision outside the mechanism.

### 4.5 Query endpoints

[aggregator/app.py:201-364](src/pathmarket/aggregator/app.py#L201-L364) exposes `GET /slas`, `/slas/{id}`, `/claims`, `/complaints` (with `search`), `/score/{isd_as}`, `/scores`, `/market/summary`, `/ticker`. The ticker is a unified stream of recent events (SLAs/claims/complaints + synthesised `reputation_change` events). It is returned **newest-first**; the UI dedupes client-side.

`/admin/reset` is gated behind `[admin] enable_admin_endpoints` in [config.toml](config.toml) — used by the simulator's "Reset" scenario.

---

## 5. Reputation: k-corroboration + EWMA

[scorer/scorer.py](src/pathmarket/scorer/scorer.py). Stateless, two functions.

### 5.1 `compute_violation_events`

[scorer.py:77-152](src/pathmarket/scorer/scorer.py#L77-L152). Per `(sla_id, metric)`:

1. Filter complaints to those that are *actual violations* of the SLA's bound (latency/loss: `measured > bound`; bandwidth: `measured < bound`).
2. Sort by `observed_at`; slide a `window_minutes` window forward.
3. When the window contains ≥ `k` *distinct* complainants, emit one `ViolationEvent(window_end=t_i, complainants=..., attributed_to=sla.cosigners)`.
4. Suppress further events from the same window until the triggering cohort has fully aged out — re-firing requires a fresh set of k complainants ([scorer.py:122-150](src/pathmarket/scorer/scorer.py#L122-L150)).

### 5.2 `compute_score`

[scorer.py:160-202](src/pathmarket/scorer/scorer.py#L160-L202). Every AS starts at `1.0`. For each `ViolationEvent` whose `attributed_to` includes the AS:

```
penalty_i  = violation_weight * exp(-Δt / τ)
score      = clamp(1.0 - Σ penalty_i, 0.0, 1.0)
```

Defaults: `k=3`, `window_minutes=5`, `violation_weight=0.1`, `τ=100h`. The repo overrides τ to **0.025h** in [config.toml](config.toml) so reputation visibly *recovers* during a multi-minute demo. Note: the nearby config comment still mentions `0.1h`; the actual value the aggregator loads is `0.025`.

### 5.3 `attributed_to` design choice

The penalty applies to *every* cosigner of the offending SLA, not to whichever hop "actually" caused the violation. This is deliberate — v2 makes no claim of full blame attribution; convergence is statistical, not deterministic. Cosigners who knowingly join consortia with bad-actor partners share the rep hit, which is the intended incentive.

---

## 6. The agents and the simulator

### 6.1 The shared `Agent` class

[agent/agent.py](src/pathmarket/agent/agent.py). One instance per AS; the user agent and every simulated AS use the same class. Four methods:

- [`publish_sla`](src/pathmarket/agent/agent.py#L91): build `SLAPayload` → compute `sla_id` → canonicalize → each cosigner signs the same canonical bytes (the orchestrator passes in non-self cosigner keys) → `POST /sla` as `SignedSLA` with all signatures attached.
- [`claim_sla`](src/pathmarket/agent/agent.py#L147): build `ClaimPayload` → compute `claim_id` → sign with claimant's own key → `POST /claim`. Self-dealing rejected here too, mirroring the aggregator (so we fail fast).
- [`file_complaint`](src/pathmarket/agent/agent.py#L193): build → sign → `POST /complaint`.
- [`choose_route`](src/pathmarket/agent/agent.py#L236): enumerate paths via `path_discovery`, find SLAs whose `payload.path` equals each candidate, run `policy_accepts_sla` filter, rank by `sla_utility = α * mean_rep − β * price`, return the winning `RoutingDecision`. **The simulator does not call this method** — see §6.3.

### 6.2 Policy evaluation

[agent/policy.py](src/pathmarket/agent/policy.py). Pure functions; `Agent` is the stateful glue.

- [`policy_accepts_sla`](src/pathmarket/agent/policy.py#L104): three gates — price ≤ `max_price_per_gb`, **min_cosigner_score ≥ `min_reputation_floor`** (this is where reputation enters buying decisions), and bounds meet `required_bounds`.
- [`sla_utility`](src/pathmarket/agent/policy.py#L142): `α * mean_cosigner_score − β * price`. Mean for ranking, min for the floor — using the min for the floor reflects the chain-reputation-is-min composition rule.
- [`should_file_complaint`](src/pathmarket/agent/policy.py#L53): `complaint_sensitivity ∈ {strict, moderate, tolerant}` maps to multipliers 1.10 / 1.25 / 1.75; complain only if measurement exceeds bound by more than the multiplier.
- [`compose_bounds`](src/pathmarket/agent/policy.py#L200): latency sums, loss sums (additive approximation), bandwidth = min, any `None` propagates as `None` (unknown) — this is what the user-agent's candidate-route enumerator uses to surface aggregate guarantees.

### 6.3 Orchestrator tick: what actually fires per tick

[simulator/orchestrator.py:197-292](src/pathmarket/simulator/orchestrator.py#L197-L292):

1. **Maybe publish.** With probability `publish_prob` (default 0.25), pick a random SLA template from [simulator.toml](simulator.toml), `_publish_template` builds and POSTs the SLA. The orchestrator holds every cosigner's private key, so multi-AS SLAs are signed atomically — consortium formation is collapsed to a single operation.
2. **Claims.** Shuffle the `edge-buyer` set; for each, fetch the live market (`GET /slas`), filter with `policy_accepts_sla(... min_cosigner_score(s, scores_by_as))`, pick a random acceptable SLA, claim 100/250/500 GB (uniform), POST. **This is the one place reputation drives autonomous behaviour: drop an AS's score below `min_reputation_floor` and edge-buyers stop claiming SLAs it cosigns.**
3. **Quality sample → complaint.** For every claim in every agent's portfolio, sample a `(latency_ms, loss_ppm, bandwidth_kbps)` triple from the SLA's `QualityModel` (one of `transit-good`, `transit-bad`, `transit-premium`). If the sample violates a metric *and* the complainant's `should_file_complaint` threshold, file a complaint — at most one per (agent, claim, tick) to avoid spam ([orchestrator.py:281-290](src/pathmarket/simulator/orchestrator.py#L281-L290)).

### 6.4 What the simulator does *not* do

- **No traffic.** The "use the SLA" step is replaced by drawing samples from a probability distribution ([agent/simulated_quality.py](src/pathmarket/agent/simulated_quality.py)). No `scion ping`, no UDP, no packets.
- **No autonomous routing.** [`Agent.choose_route`](src/pathmarket/agent/agent.py#L236) is never invoked from the orchestrator's tick. Buyers shop the market for any acceptable SLA, but never decide "given these reputations, route my data through path X instead of path Y."

The user-agent has the same gap by design: its background tick loop is intentionally a no-op beyond persistence, with the comment ([user_agent/main.py:130-132](src/pathmarket/user_agent/main.py#L130-L132)):

```
# v1 behavior: no-op here beyond persist. The user AS never auto-files
# complaints (§8.6), and claim/routing actions are UI-driven. A future
# expansion can call service.agent.choose_route per destination.
```

So to be precise about what the demo demonstrates: **bad-actor reputation falls; edge-buyers stop *buying* their SLAs; their unsold SLAs expire; the leaderboard reorders.** The economic-stratification story is real. **No AS in the simulation steers traffic away from a low-reputation path** — that link exists only as a UI affordance the human operator uses on the user-AS.

### 6.5 Scenarios

[simulator/scenario_api.py](src/pathmarket/simulator/scenario_api.py) exposes six FastAPI endpoints on port 8081 that mutate orchestrator state in-place ([simulator/scenarios.py](src/pathmarket/simulator/scenarios.py)):

- `POST /scenarios/reset` — calls aggregator `/admin/reset`, clears simulator-side quality/portfolio/routing state, and pre-publishes the configured seed SLAs. It does **not** rewind the orchestrator's RNG, so a fresh process with the same `--seed` is deterministic, but repeated reset button presses inside one process are not byte-identical to process startup.
- `POST /scenarios/hospital_reshops` — degrades one target SLA's quality model to "always violating"; it does not currently mutate a distinct live Hospital persona.
- `POST /scenarios/cloud_bargain` — publishes a cheap commodity SLA.
- `POST /scenarios/bad_actor_cascade` — back-compat alias for `degrade_as`.
- `POST /scenarios/degrade_as` / `POST /scenarios/fix_as` — toggle individual ASes for the live ticker demo.

Determinism is end-to-end seeded ([orchestrator.py:117](src/pathmarket/simulator/orchestrator.py#L117)): `--seed` (default 42) drives nonces, claim sampling, quality sampling, and policy random choices.

### 6.6 Cold-start replay

[simulator/recorder.py](src/pathmarket/simulator/recorder.py) runs the orchestrator as fast as the host allows and JSONL-streams each artifact to disk. The bundled [assets/cold_start.jsonl](assets/cold_start.jsonl) is ~4.3 MB with 6,252 artifacts (638 SLAs, 4,700 claims, 914 complaints) spanning `2026-04-19T12:00:01Z` to `2026-04-19T12:40:47Z`. The UI replay ([ui-export/cold_start.jsx](ui-export/cold_start.jsx)) compresses that ~40-minute artifact to ~30s — the "empty market → stratified market" opener for the pitch.

---

## 7. The user agent + UI

[user_agent/main.py](src/pathmarket/user_agent/main.py) wires:

- An `Agent` instance for `1-ff00:0:112` (the operator's AS).
- An atomic-write JSON state store ([state_store.py](src/pathmarket/user_agent/state_store.py)) with `schema_version=2` guard and corruption-fallback-to-fresh.
- A FastAPI app exposing 8 local endpoints under `/local/*`.
- Static-files mount of [ui-export/](ui-export/) at `/`.
- A daemon background tick (no-op beyond state persist, see §6.4).
- Clean SIGTERM/SIGINT shutdown that persists state.

### 7.1 Local API

[user_agent/app.py:227-310](src/pathmarket/user_agent/app.py#L227-L310):

- `GET /local/state`, `GET /local/portfolio`, `PUT /local/policy`
- `POST /local/actions/{claim, complaint, upload_sla}` — sign + POST to aggregator + atomic-persist on success
- `GET /local/candidate_routes?dst=...` — the routing primitive
- `PUT /local/routing/{dst_isd_as}` — persist a chosen route

### 7.2 Candidate-route enumeration

[user_agent/routing.py](src/pathmarket/user_agent/routing.py) is the most non-trivial piece in the user-agent. Given a destination, it:

1. Fetches candidate paths from `path_discovery`.
2. For each path, finds *all non-overlapping* placements of the user's portfolio claims along it (sliding cosigner sequences over path AS sequences).
3. Aggregates per placement: latency/loss/price summed over covered claims, bandwidth = min over covered claims, reputation = min over claim cosigners only when coverage is full. For partial coverage, `reputation` is `None`; the other aggregate fields still describe the covered portion, not a complete route guarantee.

This is what feeds the Route Planner view. The current UI's "Select route" button only selects the visible card client-side; it does not call `PUT /local/routing/{dst}` and does not apply the design's server-side "highest aggregate reputation, lowest price" tiebreak.

### 7.3 Consumed-GB simulation

[user_agent/consumed_walk.py](src/pathmarket/user_agent/consumed_walk.py): per claim, a wall-clock random walk advances `consumed_gb` by `Uniform(0.1%, 0.6%) × gb_purchased` per minute, capped at the purchased amount. This is simulated backend data — there's no real traffic accounting. The current portfolio card labels the field as `consumed`, not explicitly as `simulated`.

### 7.4 UI

[ui-export/](ui-export/) is the operator terminal. The currently visible navigation has five working views: Route Planner, Market Browser, Complaint Log, Reputation Board, and Cold-start replay. It also has the persistent Portfolio sidebar, Activity Ticker, presenter Scenario panel, guided demo tour, and "Market Layer" toggle. `data.js` has action wrappers for claim, complaint, SLA upload, policy save, and routing persistence, but only claim/scenario flows are wired into visible controls today; complaint submission, Submit SLA, Policy editing, and persisted routing are backend/API-ready rather than full UI surfaces.

The terminal fetches `/local/state` on boot. Every 3s it refreshes `/local/portfolio`, `/local/candidate_routes`, and the aggregator's `/scores`, `/slas`, `/complaints`, and `/ticker`.

The UI is an exported design that has been wired to real data; the wire-up lives mostly in [ui-export/data.js](ui-export/data.js).

---

## 8. SCION integration

PathMarket touches SCION through exactly two narrow interfaces:

1. **`scion showpaths --format json` subprocess** — [path_discovery/scion_showpaths.py](src/pathmarket/path_discovery/scion_showpaths.py). The simulator and user-agent shell out to per-AS sciond endpoints to enumerate path candidates. Used for: SLA template authoring (cosigner sequences match real SCION hops) and the user-agent's route enumerator.
2. **AS-key material on disk in [keys/](keys/)** — entirely separate from SCION's TRC. The `(isd_as, public_key)` mapping in [keyring.json](keyring.json) is PathMarket's trust root.

There is no SCION control-plane modification, no Go code, no SCION dataplane involvement. PathMarket's economics ride on top of whatever paths SCION publishes.

### 8.1 What demo runs

Two modes:

- `make demo` — `path_discovery = "static"` ([simulator.toml:11](simulator.toml#L11)), reads paths from [scion-topology/static_paths.yaml](scion-topology/static_paths.yaml). No live SCION daemon needed. **This is the default and currently-active demo path.**
- `make demo-scion` — flips to `ScionShowpathsDiscovery`. Requires `$SCION_ROOT` pointing to a built SCION checkout with the topology booted. Falls back to static if `gen/sciond_addresses.json` is missing.

The static path table mirrors a real `scion showpaths` output, so switching between the two backends produces matching candidate sets ([static_paths.yaml:6-9](scion-topology/static_paths.yaml#L6-L9)).

### 8.2 Topology

The demo runs on SCION's stock 16-AS [`default.topo`](https://github.com/scionproto/scion/blob/master/topology/default.topo). PathMarket's AS IDs were chosen to match the AS IDs that topology generates.

### 8.3 Active AS set

[simulator.toml](simulator.toml) wires **13 simulator-side AS agents**:

- 7 transits: 5× `transit-good`, 1× `transit-premium`, 1× `transit-bad`
- 4 edge-buyers
- 2 cosigner-only/signing-only participants, one of which is `1-ff00:0:112`
- the user-agent process also drives `1-ff00:0:112` separately as the operator's AS

[keyring.json](keyring.json) holds 19 keys; the extra ~4 are signable but unused. Any AS in SCION's underlying topology that isn't in [simulator.toml](simulator.toml) and the keyring is invisible to the market.

The honest framing: **PathMarket runs a 13-agent simulator-side reputation overlay plus one user-agent process on top of a real (or static-mirrored) 16-AS SCION topology.** Not every AS in the SCION topology participates in the reputation layer; only those with explicit Agent configuration + key material do.

---

## 9. End-to-end demo flow (one tick)

Putting §3-§8 together:

```
[SCION 16-AS default.topo, supervisord-managed]   ← live in demo-scion mode
              │
              │  scion showpaths --format json   (or static_paths.yaml)
              ▼
┌─────────────────────────┐         ┌──────────────────────────┐
│ Simulator               │         │ User-agent process        │
│ 13 Agents, tick every 3s│         │ 1 Agent for 1-ff00:0:112  │
│                         │         │ + UI on :8090             │
│ each tick:              │         │                           │
│  1. maybe publish SLA   │         │ tick: persist state only  │
│  2. edge-buyers claim   │         │ visible UI: claim +       │
│  3. quality sample →    │         │  client route select      │
│     maybe complain      │         │ local API: more actions   │
└──────────┬──────────────┘         └──────────────┬────────────┘
           │   POST signed artifacts (HTTP)        │
           └────────────────┬──────────────────────┘
                            ▼
              ┌─────────────────────────────────┐
              │ Aggregator (FastAPI :8080)      │
              │  • verifies all signatures       │
              │  • runs validation chains        │
              │  • stores in-memory              │
              │  • computes scores               │
              │  • serves /slas /claims /...     │
              │  • emits ticker events           │
              └─────────────────────────────────┘
```

In one sentence: **publish → sign → log → claim → simulated-quality → complain → score → (optionally, in the UI) inspect/select a route**, all driven by per-AS policies on top of SCION's path-discovery output.

---

## 10. Limits of the implementation

Technical caveats a reader of the code needs to keep in mind:

- **No traffic forwarding.** The "use the path" step is sampled from a `QualityModel`. No packets flow between simulated ASes.
- **No autonomous reputation-gated routing.** The simulator never calls `choose_route`; the user-agent's tick is a no-op. The reputation→route link exists only as a UI affordance for one human operator.
- **No defence against fabricated measurements.** Complaints are sender-attested. The only mitigation is k-corroboration in the scorer.
- **No defence against k-AS collusion.** k=3 distinct complainants is enough to fire a violation event regardless of motive.
- **No TRC-rooted trust.** Trust in signing keys is the static [keyring.json](keyring.json); widening it is a trust decision outside the mechanism.
- **No persistence across restarts** for aggregator and simulator state. Only the user-AS persists state, to a single JSON file.
- **No defence against strategic gaming** — an AS meeting bounds *just* often enough to maintain reputation while extracting rent.

---

## 11. Tests

24 test files in [tests/](tests/), pytest-driven. Markers: `end_to_end` (registered but not currently applied to any test) and `requires_scion` (used by the live-SCION path-discovery test, which should be run only when SCION is up). Fast feedback command: `pytest -m 'not end_to_end and not requires_scion'`.

Coverage:

- `schemas` / `canonical` — hash invariants, sensitivity, determinism.
- `verifier` — verify/reject matrix incl. tampered bytes, unknown AS, key-id mismatch.
- `scorer` — k-corroboration window semantics, decay over time, distinct-complainant dedup.
- `path_discovery` — both backends; static reads YAML fixtures, SCION reads real `scion showpaths` JSON shapes.
- `agent` — Hospital vs Cloud diverge on the same market; quality distributions hit their target violation-rate bands.
- `aggregator` — happy path + one test per validation step per endpoint (the `test_aggregator_*.py` suite).
- `simulator` — orchestrator determinism, scenario triggers, recorder roundtrip.
- `user_agent` — state persistence + corruption recovery, all local API endpoints, candidate-route enumeration (single/composed/partial/overlapping/no-coverage).

---

## 12. Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python scripts/generate_keys.py 1-ff00:0:111 1-ff00:0:112 1-ff00:0:120 \
  1-ff00:0:121 1-ff00:0:122 1-ff00:0:130 1-ff00:0:131 1-ff00:0:132 \
  1-ff00:0:133 2-ff00:0:210 2-ff00:0:211 2-ff00:0:220 2-ff00:0:221
python scripts/generate_keyring.py
make fast        # full non-SCION unit + TestClient suite
make demo        # static-path-table demo on http://127.0.0.1:8090
make demo-scion  # same demo with live `scion showpaths` (needs $SCION_ROOT)
```

Three processes spawn: aggregator (`:8080`), simulator (scenario API on `:8081`), user-agent + UI (`:8090`). The terminal at `http://127.0.0.1:8090` is the one human-facing surface.

---

## 13. Pointers for review

Suggested reading order, ~30 minutes:

1. [schemas.py](src/pathmarket/schemas.py) — the data model.
2. [canonical.py](src/pathmarket/canonical.py) — what "signed" actually signs.
3. [aggregator/validation.py](src/pathmarket/aggregator/validation.py) — the most security-relevant code; three short-circuit chains, all in one file.
4. [scorer/scorer.py](src/pathmarket/scorer/scorer.py) — 200 lines, the entire reputation mechanism.
5. [agent/agent.py](src/pathmarket/agent/agent.py) — the four action methods every AS uses.
6. [simulator/orchestrator.py:197-292](src/pathmarket/simulator/orchestrator.py#L197-L292) — the tick loop; this is what "the demo runs" means.
7. §10 of *this* document — the limits of the implementation.

Open questions:

- **Strategic-gaming defence** — an AS meeting bounds *just* often enough to maintain reputation while extracting rent. No answer in the current implementation.
- **Tying complaints to claims** — any non-cosigner can currently complain. Should complainants need to own a `SignedClaim` first? Tradeoffs around free-rider observers vs. spam resistance.
- **The reputation→routing link** — should edge-buyers in the simulator autonomously call `choose_route`? See §10.
- **k-corroboration vs. collusion** — k=3 is a small number. What's the right floor on a real network?
- **Beyond a static keyring** — what would TRC-rooted trust look like for SLAs?
