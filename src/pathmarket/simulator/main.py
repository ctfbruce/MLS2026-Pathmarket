"""Simulator entry point — wires Orchestrator + scenario API + tick loop.

Usage (§7.6)::

    pathmarket-simulator \\
        --config simulator.toml \\
        --aggregator http://127.0.0.1:8080 \\
        --tick-interval 3.0 \\
        --seed 42

Two modes (§7.7):

- ``--mode live`` (default) — tick forever at ``--tick-interval`` seconds,
  exposing a scenario-control FastAPI on ``[server].bind_port`` (8081).
- ``--mode record`` — tick as fast as possible, write every signed artifact
  to ``--record-file`` as JSONL, exit after ``--record-duration-hours`` of
  simulated time. The JSONL consumer is the UI's pitch-opener (§9.5).

This module is deliberately thin; the heavy lifting is in
:mod:`pathmarket.simulator.orchestrator` and
:mod:`pathmarket.simulator.scenarios`. We keep main.py small so it's easy to
read end-to-end during a demo-day fire drill.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import httpx
import uvicorn

from pathmarket.client import AggregatorClient, HttpAggregatorClient
from pathmarket.path_discovery.protocol import PathDiscovery
from pathmarket.path_discovery.scion_showpaths import MultiScionShowpathsDiscovery
from pathmarket.path_discovery.static_table import StaticPathTableDiscovery
from pathmarket.schemas import PathHop, Policy, SLABounds
from pathmarket.simulator.orchestrator import (
    AgentSpec,
    Orchestrator,
    SLATemplate,
)
from pathmarket.simulator.scenario_api import create_scenario_app


log = logging.getLogger("pathmarket.simulator")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _policy_from_toml(d: dict[str, Any]) -> Policy:
    rb = d.get("required_bounds") or {}
    return Policy(
        max_price_per_gb=str(d["max_price_per_gb"]),
        min_reputation_floor=float(d["min_reputation_floor"]),
        required_bounds=SLABounds(
            latency_max_ms=rb.get("latency_max_ms"),
            loss_max_ppm=rb.get("loss_max_ppm"),
            bandwidth_min_kbps=rb.get("bandwidth_min_kbps"),
        ),
        alpha=float(d["alpha"]),
        beta=float(d["beta"]),
        uncovered_tolerance=str(d["uncovered_tolerance"]),
        complaint_sensitivity=str(d["complaint_sensitivity"]),
        reshop_on_reputation_drop=float(d["reshop_on_reputation_drop"]),
        portfolio_redundancy=int(d["portfolio_redundancy"]),
    )


def _path_from_toml(entries: list[dict[str, Any]]) -> list[PathHop]:
    return [
        PathHop(
            isd_as=str(h["isd_as"]),
            ingress=int(h["ingress"]),
            egress=int(h["egress"]),
        )
        for h in entries
    ]


def _sla_template_from_toml(d: dict[str, Any]) -> SLATemplate:
    bounds = d.get("bounds") or {}
    return SLATemplate(
        path=_path_from_toml(d["path"]),
        bounds=SLABounds(
            latency_max_ms=bounds.get("latency_max_ms"),
            loss_max_ppm=bounds.get("loss_max_ppm"),
            bandwidth_min_kbps=bounds.get("bandwidth_min_kbps"),
        ),
        price_per_gb=str(d["price_per_gb"]),
        consortium_profile=str(d["consortium_profile"]),  # type: ignore[arg-type]
        validity_hours=int(d.get("validity_hours", 24)),
    )


def load_config(path: Path) -> dict[str, Any]:
    """Load ``simulator.toml`` into a plain dict."""

    return tomllib.loads(path.read_text(encoding="utf-8"))


def build_specs_from_config(cfg: dict[str, Any]) -> list[AgentSpec]:
    """Build the full list of :class:`AgentSpec` from ``[personas.*]`` + ``[[generic_ases]]``."""

    specs: list[AgentSpec] = []

    personas = cfg.get("personas") or {}
    for name, p in personas.items():
        specs.append(
            AgentSpec(
                isd_as=str(p["isd_as"]),
                role=str(p.get("role", "edge-buyer")),
                policy=_policy_from_toml(p["policy"]),
                quality_profile=p.get("quality_profile"),
            )
        )

    for g in cfg.get("generic_ases") or []:
        specs.append(
            AgentSpec(
                isd_as=str(g["isd_as"]),
                role=str(g["role"]),
                policy=_policy_from_toml(g["policy"]),
                quality_profile=g.get("quality_profile"),
            )
        )

    return specs


def build_templates_from_config(cfg: dict[str, Any]) -> list[SLATemplate]:
    return [_sla_template_from_toml(t) for t in (cfg.get("sla_templates") or [])]


def load_private_keys(keys_dir: Path, isd_ases: list[str]) -> dict[str, Ed25519PrivateKey]:
    """Load one Ed25519 private key per AS from ``keys/<isd_as>.private``."""

    out: dict[str, Ed25519PrivateKey] = {}
    for isd_as in isd_ases:
        p = keys_dir / f"{isd_as}.private"
        if not p.exists():
            raise FileNotFoundError(f"missing private key: {p}")
        out[isd_as] = Ed25519PrivateKey.from_private_bytes(p.read_bytes())
    return out


# ---------------------------------------------------------------------------
# Path discovery selection
# ---------------------------------------------------------------------------


def _build_path_discovery(
    kind: str,
    static_path: Path,
    *,
    scion_root: Path | None = None,
) -> PathDiscovery:
    if kind == "static":
        return StaticPathTableDiscovery.from_file(static_path)
    if kind == "scion":
        if scion_root is not None and (scion_root / "gen" / "sciond_addresses.json").is_file():
            log.info("using real SCION discovery rooted at %s", scion_root)
            return MultiScionShowpathsDiscovery(scion_root)
        # SCION unavailable — fall back to static so CI/demo still boots.
        log.warning(
            "scion backend requested but SCION not found at %s; falling back to static",
            scion_root,
        )
        if static_path.exists():
            return StaticPathTableDiscovery.from_file(static_path)
        return StaticPathTableDiscovery.from_mapping({})
    raise ValueError(f"unknown path_discovery: {kind!r}")


# ---------------------------------------------------------------------------
# Orchestrator assembly
# ---------------------------------------------------------------------------


def build_orchestrator(
    *,
    config: dict[str, Any],
    aggregator_client: AggregatorClient,
    path_discovery: PathDiscovery,
    seed: int,
    keys_dir: Path,
) -> Orchestrator:
    specs = build_specs_from_config(config)
    templates = build_templates_from_config(config)
    private_keys = load_private_keys(keys_dir, [s.isd_as for s in specs])
    return Orchestrator(
        specs=specs,
        private_keys=private_keys,
        sla_templates=templates,
        aggregator_client=aggregator_client,
        path_discovery=path_discovery,
        rng=random.Random(seed),
    )


# ---------------------------------------------------------------------------
# Live tick loop
# ---------------------------------------------------------------------------


def live_tick_loop(
    orchestrator: Orchestrator, *, interval_seconds: float, max_ticks: int | None = None
) -> None:
    """Tick until SIGTERM / Ctrl-C, logging a one-line summary per tick."""

    ticks = 0
    try:
        while True:
            t0 = time.monotonic()
            summary = orchestrator.tick()
            log.info(
                "tick t=%s +slas=%d +claims=%d +complaints=%d",
                summary.tick_time.isoformat(),
                len(summary.new_slas),
                len(summary.new_claims),
                len(summary.new_complaints),
            )
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                return
            elapsed = time.monotonic() - t0
            sleep_for = max(0.0, interval_seconds - elapsed)
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        log.info("shutdown: received KeyboardInterrupt after %d ticks", ticks)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pathmarket-simulator",
        description="Run the PathMarket v2 simulator.",
    )
    p.add_argument("--config", type=Path, default=Path("simulator.toml"))
    p.add_argument("--aggregator", type=str, default=None)
    p.add_argument("--tick-interval", type=float, default=None)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--keys-dir", type=Path, default=Path("keys"))
    p.add_argument(
        "--path-discovery",
        choices=["scion", "static"],
        default=None,
        help="override the config's path_discovery backend",
    )
    p.add_argument(
        "--static-paths",
        type=Path,
        default=Path("scion-topology/static_paths.yaml"),
    )
    p.add_argument(
        "--scion-root",
        type=Path,
        default=None,
        help="path to a running SCION checkout (default: $SCION_ROOT or /home/kali/code/scion)",
    )
    p.add_argument("--max-ticks", type=int, default=None, help="exit after N ticks (smoke-test)")
    p.add_argument(
        "--mode",
        choices=["live", "record"],
        default="live",
    )
    p.add_argument("--record-file", type=Path, default=Path("assets/cold_start.jsonl"))
    p.add_argument("--record-duration-hours", type=float, default=3.0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    if not args.config.exists():
        print(f"simulator: config not found: {args.config}", file=sys.stderr)
        return 2

    cfg = load_config(args.config)

    aggregator_url = args.aggregator or cfg.get("aggregator", {}).get("url", "http://127.0.0.1:8080")
    tick_interval = (
        args.tick_interval
        if args.tick_interval is not None
        else float(cfg.get("simulation", {}).get("tick_interval_seconds", 3.0))
    )
    path_kind = args.path_discovery or cfg.get("simulation", {}).get("path_discovery", "scion")
    scion_root = args.scion_root or Path(
        cfg.get("simulation", {}).get("scion_root")
        or os.environ.get("SCION_ROOT", "/home/kali/code/scion")
    )

    client = HttpAggregatorClient(aggregator_url)
    path_disco = _build_path_discovery(path_kind, args.static_paths, scion_root=scion_root)
    orchestrator = build_orchestrator(
        config=cfg,
        aggregator_client=client,
        path_discovery=path_disco,
        seed=args.seed,
        keys_dir=args.keys_dir,
    )

    initial_count = int(cfg.get("simulation", {}).get("initial_sla_count", 5))
    log.info("pre-seeding %d SLAs", initial_count)
    orchestrator.pre_seed(count=initial_count)

    if args.mode == "record":
        from pathmarket.simulator.recorder import run_record_mode
        run_record_mode(
            orchestrator,
            record_file=args.record_file,
            duration_hours=args.record_duration_hours,
            tick_interval_seconds=tick_interval,
        )
        return 0

    # Scenario-control API — presenter buttons in the UI POST here.
    srv_cfg = cfg.get("server") or {}
    bind_host = str(srv_cfg.get("bind_host", "127.0.0.1"))
    bind_port = int(srv_cfg.get("bind_port", 8081))

    def _on_reset() -> None:
        # Wipe aggregator-side state before the orchestrator re-seeds.
        try:
            httpx.post(f"{aggregator_url}/admin/reset", timeout=3.0)
        except httpx.HTTPError as e:
            log.warning("admin/reset failed: %s", e)

    scenario_app = create_scenario_app(
        orchestrator,
        on_reset=_on_reset,
        default_preseed_count=initial_count,
    )
    api_thread = threading.Thread(
        target=lambda: uvicorn.run(
            scenario_app, host=bind_host, port=bind_port, log_level="warning"
        ),
        name="simulator-scenario-api",
        daemon=True,
    )
    api_thread.start()
    log.info("scenario API listening on http://%s:%d", bind_host, bind_port)

    live_tick_loop(orchestrator, interval_seconds=tick_interval, max_ticks=args.max_ticks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
