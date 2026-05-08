"""SCION showpaths path-discovery backend (``DESIGN.md`` §7.8, §10, Phase E2).

Shells out to the ``scion`` CLI against a running ``sciond`` and parses the
JSON-formatted ``showpaths`` output into ``list[list[PathHop]]`` — the same
shape :class:`StaticPathTableDiscovery` returns, so the two implementations
are interchangeable at configuration time.

Construction is usually done via :meth:`ScionShowpathsDiscovery.from_scion_gen`,
which reads a SCION build's ``gen/sciond_addresses.json`` to look up the
loopback address of the local AS's scion-daemon. The caller selects which AS
this discoverer speaks for — you cannot query "paths from X" with a discoverer
bound to Y, because ``scion showpaths`` always queries the sciond it connects
to, which has a fixed local AS.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from pathmarket.schemas import PathHop

_log = logging.getLogger(__name__)

# Default sciond control port on a scion.sh-generated topology.
_DEFAULT_SCIOND_PORT = 30255


class ScionShowpathsDiscovery:
    """Live path discovery via ``scion showpaths`` JSON output."""

    def __init__(
        self,
        local_isd_as: str,
        scion_bin: str | Path,
        sciond_addr: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.local_isd_as = local_isd_as
        self.scion_bin = str(scion_bin)
        self.sciond_addr = sciond_addr
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_scion_gen(
        cls,
        local_isd_as: str,
        scion_root: str | Path,
        *,
        sciond_port: int = _DEFAULT_SCIOND_PORT,
        timeout_seconds: float = 5.0,
    ) -> "ScionShowpathsDiscovery":
        """Build a discoverer that talks to the sciond of ``local_isd_as``.

        Reads ``{scion_root}/gen/sciond_addresses.json`` (produced by
        ``scion.sh topology``) to find the loopback IP for that AS's sciond,
        and points at ``{scion_root}/bin/scion`` as the CLI to shell out to.
        """

        scion_root = Path(scion_root)
        addresses_file = scion_root / "gen" / "sciond_addresses.json"
        if not addresses_file.is_file():
            raise FileNotFoundError(
                f"sciond_addresses.json not found at {addresses_file}; "
                f"is SCION's gen/ populated (scion.sh topology)?"
            )
        mapping = json.loads(addresses_file.read_text())
        ip = mapping.get(local_isd_as)
        if ip is None:
            raise KeyError(
                f"AS {local_isd_as} not found in {addresses_file}; "
                f"known ASes: {sorted(mapping)}"
            )
        scion_bin = scion_root / "bin" / "scion"
        if not scion_bin.is_file():
            raise FileNotFoundError(
                f"scion CLI not found at {scion_bin}; did SCION build succeed?"
            )
        return cls(
            local_isd_as=local_isd_as,
            scion_bin=scion_bin,
            sciond_addr=f"[{ip}]:{sciond_port}",
            timeout_seconds=timeout_seconds,
        )

    # ------------------------------------------------------------------
    # PathDiscovery protocol
    # ------------------------------------------------------------------

    def paths_from(self, src_isd_as: str, dst_isd_as: str) -> list[list[PathHop]]:
        if src_isd_as != self.local_isd_as:
            # sciond queries are always from the local AS; asking for paths
            # from a different source has no meaningful answer.
            return []
        try:
            proc = subprocess.run(
                [
                    self.scion_bin,
                    "showpaths",
                    dst_isd_as,
                    "--sciond",
                    self.sciond_addr,
                    "--format",
                    "json",
                    "--no-probe",
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _log.warning("scion showpaths %s -> %s timed out", src_isd_as, dst_isd_as)
            return []
        if proc.returncode != 0:
            # rc=1 = no alive paths (only when probing); rc=2 = other errors.
            # With --no-probe rc=1 shouldn't happen; anything non-zero means the
            # daemon had no routing info or errored out — treat as "no paths".
            _log.info(
                "scion showpaths %s -> %s failed rc=%s: %s",
                src_isd_as,
                dst_isd_as,
                proc.returncode,
                proc.stderr.strip(),
            )
            return []
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            _log.warning("scion showpaths JSON parse failed: %s", e)
            return []
        return [self._parse_path(p) for p in data.get("paths", [])]

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_path(path_obj: dict) -> list[PathHop]:
        """Parse a single ``paths[i]`` entry into ``list[PathHop]``.

        Prefers the ``sequence`` string (``"ISDAS#ingress,egress ..."``), which
        carries per-AS ingress/egress interface IDs directly. Falls back to
        stitching the ``hops`` array if ``sequence`` is absent.
        """

        sequence = path_obj.get("sequence")
        if sequence:
            return ScionShowpathsDiscovery._parse_sequence(sequence)
        return ScionShowpathsDiscovery._parse_hops_array(path_obj.get("hops") or [])

    @staticmethod
    def _parse_sequence(sequence: str) -> list[PathHop]:
        out: list[PathHop] = []
        for token in sequence.split():
            # Token shape: "1-ff00:0:112#0,495"
            if "#" not in token:
                raise ValueError(f"malformed sequence token: {token!r}")
            isd_as, ifaces = token.rsplit("#", 1)
            ingress_s, _, egress_s = ifaces.partition(",")
            out.append(
                PathHop(
                    isd_as=isd_as,
                    ingress=int(ingress_s),
                    egress=int(egress_s) if egress_s else 0,
                )
            )
        return out

    @staticmethod
    def _parse_hops_array(hops: list[dict]) -> list[PathHop]:
        """Collapse a SCION ``hops[]`` array into per-AS PathHops.

        Transit ASes appear twice (ingress entry, then egress entry); source
        and destination appear once. This is the inverse of the sequence
        format and used only as a fallback when ``sequence`` is missing.
        """

        out: list[PathHop] = []
        i = 0
        n = len(hops)
        while i < n:
            cur = hops[i]
            nxt = hops[i + 1] if i + 1 < n else None
            if nxt is not None and nxt.get("isd_as") == cur.get("isd_as"):
                out.append(
                    PathHop(
                        isd_as=cur["isd_as"],
                        ingress=int(cur["interface"]),
                        egress=int(nxt["interface"]),
                    )
                )
                i += 2
            else:
                if not out:
                    # First entry — source AS, ingress is 0.
                    out.append(
                        PathHop(
                            isd_as=cur["isd_as"],
                            ingress=0,
                            egress=int(cur["interface"]),
                        )
                    )
                else:
                    # Last entry — destination AS, egress is 0.
                    out.append(
                        PathHop(
                            isd_as=cur["isd_as"],
                            ingress=int(cur["interface"]),
                            egress=0,
                        )
                    )
                i += 1
        return out


class MultiScionShowpathsDiscovery:
    """Multi-AS SCION discovery — dispatches ``paths_from(src, ...)`` to a
    per-AS :class:`ScionShowpathsDiscovery`.

    The simulator runs many ASes in-process and each agent asks for paths
    starting at *its own* AS. A single sciond only knows one local AS, so we
    build one sub-discoverer per AS in ``gen/sciond_addresses.json`` and
    route by ``src_isd_as``.
    """

    def __init__(
        self,
        scion_root: str | Path,
        *,
        sciond_port: int = _DEFAULT_SCIOND_PORT,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.scion_root = Path(scion_root)
        self.sciond_port = sciond_port
        self.timeout_seconds = timeout_seconds
        self._subs: dict[str, ScionShowpathsDiscovery] = {}

    def _sub(self, isd_as: str) -> ScionShowpathsDiscovery | None:
        sub = self._subs.get(isd_as)
        if sub is not None:
            return sub
        try:
            sub = ScionShowpathsDiscovery.from_scion_gen(
                isd_as,
                self.scion_root,
                sciond_port=self.sciond_port,
                timeout_seconds=self.timeout_seconds,
            )
        except (FileNotFoundError, KeyError) as e:
            _log.info("no sciond for %s: %s", isd_as, e)
            return None
        self._subs[isd_as] = sub
        return sub

    def paths_from(self, src_isd_as: str, dst_isd_as: str) -> list[list[PathHop]]:
        sub = self._sub(src_isd_as)
        if sub is None:
            return []
        return sub.paths_from(src_isd_as, dst_isd_as)


__all__ = ["ScionShowpathsDiscovery", "MultiScionShowpathsDiscovery"]
