"""Static path-table backend — reads ``scion-topology/static_paths.yaml``.

Authoritative source: ``DESIGN.md`` §7.8, §10.4. File format::

    paths:
      "1-ff00:0:112 -> 1-ff00:0:130":
        - [{"isd_as": "1-ff00:0:112", "ingress": 0, "egress": 495},
           {"isd_as": "1-ff00:0:111", "ingress": 113, "egress": 104},
           {"isd_as": "1-ff00:0:130", "ingress": 2, "egress": 0}]
        - ...

The key is ``"{src} -> {dst}"`` with exactly one space either side of the
arrow. Values are lists of paths, each of which is a list of hop dicts.
Queries for unknown pairs return an empty list.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from pathmarket.schemas import PathHop

_KEY_SEP = " -> "


class StaticPathTableDiscovery:
    """In-memory static path table, loaded once at construction."""

    def __init__(self, table: dict[tuple[str, str], list[list[PathHop]]]) -> None:
        self._table = table

    @classmethod
    def from_file(cls, path: str | Path) -> "StaticPathTableDiscovery":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        raw_paths = data.get("paths", {}) or {}
        return cls._from_raw(raw_paths)

    @classmethod
    def from_mapping(cls, raw_paths: dict) -> "StaticPathTableDiscovery":
        """Build from an in-memory mapping — useful for unit tests."""

        return cls._from_raw(raw_paths)

    @classmethod
    def _from_raw(cls, raw_paths: dict) -> "StaticPathTableDiscovery":
        table: dict[tuple[str, str], list[list[PathHop]]] = {}
        for key, paths in raw_paths.items():
            if _KEY_SEP not in key:
                raise ValueError(f"static_paths.yaml: bad key {key!r} (missing ' -> ' separator)")
            src, dst = key.split(_KEY_SEP, 1)
            parsed_paths: list[list[PathHop]] = []
            for p in paths or []:
                parsed_paths.append(
                    [
                        PathHop(
                            isd_as=hop["isd_as"],
                            ingress=int(hop["ingress"]),
                            egress=int(hop["egress"]),
                        )
                        for hop in p
                    ]
                )
            table[(src, dst)] = parsed_paths
        return cls(table)

    def paths_from(self, src_isd_as: str, dst_isd_as: str) -> list[list[PathHop]]:
        """Return the configured paths from ``src`` to ``dst`` or ``[]`` if unknown."""

        return [list(p) for p in self._table.get((src_isd_as, dst_isd_as), [])]


__all__ = ["StaticPathTableDiscovery"]
