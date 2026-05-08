"""Tests for StaticPathTableDiscovery (§7.8, §10.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pathmarket.path_discovery import StaticPathTableDiscovery
from pathmarket.schemas import PathHop


FIXTURE = Path(__file__).parent / "fixtures" / "static_paths_sample.yaml"


class TestFromFile:
    def test_loads_and_returns_paths(self) -> None:
        disc = StaticPathTableDiscovery.from_file(FIXTURE)
        paths = disc.paths_from("1-ff00:0:112", "1-ff00:0:130")
        assert len(paths) == 2
        # source-first order; ingress == 0 at source, egress == 0 at dest.
        assert paths[0][0] == PathHop(isd_as="1-ff00:0:112", ingress=0, egress=495)
        assert paths[0][-1] == PathHop(isd_as="1-ff00:0:130", ingress=2, egress=0)

    def test_unknown_pair_returns_empty_list(self) -> None:
        disc = StaticPathTableDiscovery.from_file(FIXTURE)
        assert disc.paths_from("1-ff00:0:999", "1-ff00:0:888") == []
        # Known src, unknown dst.
        assert disc.paths_from("1-ff00:0:112", "1-ff00:0:999") == []

    def test_hop_order_is_preserved(self) -> None:
        """The list of hops in a path must be returned in the order given."""
        disc = StaticPathTableDiscovery.from_file(FIXTURE)
        paths = disc.paths_from("1-ff00:0:112", "2-ff00:0:301")
        assert [h.isd_as for h in paths[0]] == [
            "1-ff00:0:112",
            "1-ff00:0:100",
            "2-ff00:0:100",
            "2-ff00:0:301",
        ]

    def test_returned_paths_are_independent_copies(self) -> None:
        """Mutating a returned path must not corrupt the backing table."""
        disc = StaticPathTableDiscovery.from_file(FIXTURE)
        paths_a = disc.paths_from("1-ff00:0:112", "1-ff00:0:130")
        paths_a.pop(0)
        paths_b = disc.paths_from("1-ff00:0:112", "1-ff00:0:130")
        assert len(paths_b) == 2


class TestFromMapping:
    def test_accepts_in_memory_dict(self) -> None:
        raw = {
            "1-ff00:0:112 -> 1-ff00:0:130": [
                [
                    {"isd_as": "1-ff00:0:112", "ingress": 0, "egress": 1},
                    {"isd_as": "1-ff00:0:130", "ingress": 1, "egress": 0},
                ]
            ]
        }
        disc = StaticPathTableDiscovery.from_mapping(raw)
        paths = disc.paths_from("1-ff00:0:112", "1-ff00:0:130")
        assert len(paths) == 1
        assert len(paths[0]) == 2

    def test_malformed_key_raises(self) -> None:
        with pytest.raises(ValueError, match="missing ' -> ' separator"):
            StaticPathTableDiscovery.from_mapping({"1-ff00:0:112 1-ff00:0:130": []})

    def test_empty_paths_yields_empty(self) -> None:
        disc = StaticPathTableDiscovery.from_mapping({})
        assert disc.paths_from("1-ff00:0:a", "1-ff00:0:b") == []
