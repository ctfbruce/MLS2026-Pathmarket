"""PathDiscovery protocol (``DESIGN.md`` §7.8).

Two implementations ship with v2:

- ``StaticPathTableDiscovery`` — reads ``scion-topology/static_paths.yaml``.
  Used in unit tests, CI, and as an emergency fallback at demo time if real
  SCION is unstable.
- ``ScionShowpathsDiscovery`` — shells out to ``scion showpaths`` against a
  live sciond. Production path once the 32-AS topology is validated.

Both return the same ``list[list[PathHop]]`` shape so they are interchangeable
at configuration time (``--path-discovery {scion,static}``).
"""

from __future__ import annotations

from typing import Protocol

from pathmarket.schemas import PathHop


class PathDiscovery(Protocol):
    """Abstract path-discovery interface (§7.8)."""

    def paths_from(self, src_isd_as: str, dst_isd_as: str) -> list[list[PathHop]]:
        """Return candidate paths from ``src_isd_as`` to ``dst_isd_as``.

        Each path is a list of ``PathHop`` in source-first order, with
        ``ingress == 0`` on the source AS and ``egress == 0`` on the destination.
        Returns an empty list if there are no known paths.
        """
        ...
