"""User-AS agent entry point (``DESIGN.md`` §8.2).

Wires together:

- ``UserAgentService`` + persistent state store (§8.3).
- Local FastAPI app (``/local/*``) for the UI (§8.4).
- Static-file serving of ``ui-export/`` at ``/`` (§8.2 #3).
- Policy tick loop on a background thread (§8.2 #1).
- Clean shutdown that persists state (§8.2 #4).

The process binds to ``127.0.0.1:8090`` by default.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.staticfiles import StaticFiles

from pathmarket.client import AggregatorClient, HttpAggregatorClient
from pathmarket.path_discovery.protocol import PathDiscovery
from pathmarket.path_discovery.scion_showpaths import ScionShowpathsDiscovery
from pathmarket.path_discovery.static_table import StaticPathTableDiscovery
from pathmarket.schemas import Policy, SLABounds
from pathmarket.user_agent.app import UserAgentService, create_app
from pathmarket.user_agent.state_store import (
    UserAgentState,
    load_or_init_state,
    save_state,
)


log = logging.getLogger("pathmarket.user_agent")


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


def load_config(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _build_path_discovery(
    kind: str,
    static_path: Path,
    *,
    scion_root: Path | None = None,
    local_isd_as: str | None = None,
) -> PathDiscovery:
    if kind == "static":
        return StaticPathTableDiscovery.from_file(static_path)
    if kind == "scion":
        if (
            scion_root is not None
            and local_isd_as is not None
            and (scion_root / "gen" / "sciond_addresses.json").is_file()
        ):
            try:
                return ScionShowpathsDiscovery.from_scion_gen(local_isd_as, scion_root)
            except (FileNotFoundError, KeyError) as e:
                log.warning("scion backend unavailable (%s); falling back to static", e)
        else:
            log.warning(
                "scion backend requested but SCION gen/ not populated at %s; "
                "falling back to static",
                scion_root,
            )
        if static_path.exists():
            return StaticPathTableDiscovery.from_file(static_path)
        return StaticPathTableDiscovery.from_mapping({})
    raise ValueError(f"unknown path_discovery: {kind!r}")


def _load_user_private_key(keys_dir: Path, isd_as: str) -> Ed25519PrivateKey:
    p = keys_dir / f"{isd_as}.private"
    if not p.exists():
        raise FileNotFoundError(f"missing user-AS private key: {p}")
    return Ed25519PrivateKey.from_private_bytes(p.read_bytes())


# ---------------------------------------------------------------------------
# Tick loop — policy-driven route refresh + periodic state persist (§7.2 #4)
# ---------------------------------------------------------------------------


def _policy_tick(service: UserAgentService) -> None:
    """One pass: for every destination with a routing decision, re-pick a
    best available SLA if the cached one is no longer acceptable.

    Kept intentionally minimal for Phase D — the 'hot destinations' heuristic
    in §7.2 #4 is not needed for the demo where destinations come from the
    UI's explicit selection. We DO still persist state at tick end so
    simulated consumed-GB walks are not the only in-memory data surviving
    a crash.
    """

    # v1 behavior: no-op here beyond persist. The user AS never auto-files
    # complaints (§8.6), and claim/routing actions are UI-driven. A future
    # expansion can call service.agent.choose_route per destination.
    service._persist()  # intentional: private; tick owns persistence cadence


def _run_tick_loop(
    service: UserAgentService, *, interval_seconds: float, stop_event: threading.Event
) -> None:
    while not stop_event.is_set():
        try:
            _policy_tick(service)
        except Exception:  # defensive: one bad tick must not kill the process
            log.exception("tick failed; continuing")
        stop_event.wait(interval_seconds)


# ---------------------------------------------------------------------------
# Factory: app with static files mounted at "/"
# ---------------------------------------------------------------------------


def build_full_app(
    service: UserAgentService,
    *,
    ui_export_dir: Path,
    assets_dir: Path | None = None,
) -> Any:
    app = create_app(service)
    # /assets/* first so the root mount doesn't shadow it. Used by the cold-
    # start replay panel (§9.14) to fetch assets/cold_start.jsonl.
    if assets_dir is not None and assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")
    if ui_export_dir.exists():
        app.mount("/", StaticFiles(directory=str(ui_export_dir), html=True), name="ui")
    else:
        log.warning("ui-export dir %s not found — UI will 404", ui_export_dir)
    return app


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pathmarket-user-agent",
        description="Run the PathMarket v2 user-AS agent and serve the terminal UI.",
    )
    p.add_argument("--config", type=Path, default=Path("user_agent.toml"))
    p.add_argument("--aggregator", type=str, default=None)
    p.add_argument("--bind", type=str, default="127.0.0.1:8090")
    p.add_argument("--state-file", type=Path, default=None)
    p.add_argument("--tick-interval", type=float, default=None)
    p.add_argument("--keys-dir", type=Path, default=Path("keys"))
    p.add_argument(
        "--path-discovery",
        choices=["scion", "static"],
        default=None,
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
    p.add_argument("--ui-export-dir", type=Path, default=Path("ui-export"))
    p.add_argument("--assets-dir", type=Path, default=Path("assets"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    if not args.config.exists():
        print(f"user-agent: config not found: {args.config}", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    user_as_cfg = cfg.get("user_as") or {}
    isd_as = str(user_as_cfg["isd_as"])
    display_name = str(user_as_cfg.get("display_name", isd_as))
    state_file = args.state_file or Path(user_as_cfg.get("state_file", "runtime/user_as_state.json"))
    tick_interval = float(
        args.tick_interval
        if args.tick_interval is not None
        else user_as_cfg.get("tick_interval_seconds", 5.0)
    )
    path_kind = args.path_discovery or user_as_cfg.get("path_discovery", "scion")
    aggregator_url = (
        args.aggregator
        or (cfg.get("aggregator") or {}).get("url", "http://127.0.0.1:8080")
    )

    default_policy = _policy_from_toml(user_as_cfg["default_policy"])
    state = load_or_init_state(
        state_file,
        isd_as=isd_as,
        display_name=display_name,
        default_policy=default_policy,
    )
    private_key = _load_user_private_key(args.keys_dir, isd_as)

    scion_root = args.scion_root or Path(
        user_as_cfg.get("scion_root")
        or os.environ.get("SCION_ROOT", "/home/kali/code/scion")
    )

    client: AggregatorClient = HttpAggregatorClient(aggregator_url)
    path_disco = _build_path_discovery(
        path_kind,
        args.static_paths,
        scion_root=scion_root,
        local_isd_as=isd_as,
    )

    service = UserAgentService(
        state=state,
        state_file=state_file,
        aggregator_client=client,
        private_key=private_key,
        path_discovery=path_disco,
    )

    app = build_full_app(
        service,
        ui_export_dir=args.ui_export_dir,
        assets_dir=args.assets_dir,
    )

    host, port_s = args.bind.split(":")
    port = int(port_s)

    stop_event = threading.Event()
    tick_thread = threading.Thread(
        target=_run_tick_loop,
        kwargs={"service": service, "interval_seconds": tick_interval, "stop_event": stop_event},
        name="user-agent-tick",
        daemon=True,
    )
    tick_thread.start()

    def _on_term(*_: object) -> None:
        log.info("shutdown: persisting state and stopping tick loop")
        stop_event.set()
        save_state(state_file, service.state)

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    log.info("user-agent listening on http://%s:%d (ui from %s)", host, port, args.ui_export_dir)
    uvicorn.run(app, host=host, port=port, log_level="info")
    stop_event.set()
    save_state(state_file, service.state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
