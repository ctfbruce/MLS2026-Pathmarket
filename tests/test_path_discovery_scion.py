"""Tests for :mod:`pathmarket.path_discovery.scion_showpaths`.

Unit tests exercise the parser against canned ``scion showpaths --format json``
outputs (no SCION required). The real-SCION smoke test is gated by the
``requires_scion`` pytest marker and shells out to the bundled SCION checkout
at ``/home/kali/code/scion``; it auto-skips when the topology isn't running.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pathmarket.path_discovery.scion_showpaths import ScionShowpathsDiscovery


# ----------------------------------------------------------------------
# Parser unit tests
# ----------------------------------------------------------------------


def test_parse_sequence_direct_two_ases():
    seq = "1-ff00:0:112#0,495 1-ff00:0:130#113,0"
    hops = ScionShowpathsDiscovery._parse_sequence(seq)
    assert [(h.isd_as, h.ingress, h.egress) for h in hops] == [
        ("1-ff00:0:112", 0, 495),
        ("1-ff00:0:130", 113, 0),
    ]


def test_parse_sequence_four_hop_transit_chain():
    seq = (
        "1-ff00:0:112#0,494 1-ff00:0:111#103,104 "
        "1-ff00:0:120#5,1 1-ff00:0:130#105,0"
    )
    hops = ScionShowpathsDiscovery._parse_sequence(seq)
    assert [(h.isd_as, h.ingress, h.egress) for h in hops] == [
        ("1-ff00:0:112", 0, 494),
        ("1-ff00:0:111", 103, 104),
        ("1-ff00:0:120", 5, 1),
        ("1-ff00:0:130", 105, 0),
    ]


def test_parse_hops_array_matches_sequence_output():
    """The fallback ``hops[]`` parser should produce the same shape the
    sequence parser does — they are two serializations of the same path."""

    hops_array = [
        {"interface": 494, "isd_as": "1-ff00:0:112"},
        {"interface": 103, "isd_as": "1-ff00:0:111"},
        {"interface": 104, "isd_as": "1-ff00:0:111"},
        {"interface": 5, "isd_as": "1-ff00:0:120"},
        {"interface": 1, "isd_as": "1-ff00:0:120"},
        {"interface": 105, "isd_as": "1-ff00:0:130"},
    ]
    hops = ScionShowpathsDiscovery._parse_hops_array(hops_array)
    assert [(h.isd_as, h.ingress, h.egress) for h in hops] == [
        ("1-ff00:0:112", 0, 494),
        ("1-ff00:0:111", 103, 104),
        ("1-ff00:0:120", 5, 1),
        ("1-ff00:0:130", 105, 0),
    ]


def test_parse_path_prefers_sequence_over_hops():
    """If both ``sequence`` and ``hops`` are present, ``sequence`` wins."""

    path = {
        "sequence": "1-ff00:0:112#0,495 1-ff00:0:130#113,0",
        "hops": [
            # Deliberately wrong so we can tell which parser was used.
            {"interface": 999, "isd_as": "1-ff00:0:999"},
        ],
    }
    hops = ScionShowpathsDiscovery._parse_path(path)
    assert [(h.isd_as, h.ingress, h.egress) for h in hops] == [
        ("1-ff00:0:112", 0, 495),
        ("1-ff00:0:130", 113, 0),
    ]


def test_parse_sequence_rejects_malformed_token():
    with pytest.raises(ValueError, match="malformed sequence token"):
        ScionShowpathsDiscovery._parse_sequence("1-ff00:0:112-495-0")


# ----------------------------------------------------------------------
# from_scion_gen factory
# ----------------------------------------------------------------------


def test_from_scion_gen_loads_sciond_address(tmp_path: Path):
    (tmp_path / "gen").mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "gen" / "sciond_addresses.json").write_text(
        json.dumps({"1-ff00:0:112": "127.0.0.76"})
    )
    (tmp_path / "bin" / "scion").write_bytes(b"#!/bin/false\n")
    (tmp_path / "bin" / "scion").chmod(0o755)

    disco = ScionShowpathsDiscovery.from_scion_gen("1-ff00:0:112", tmp_path)
    assert disco.sciond_addr == "[127.0.0.76]:30255"
    assert disco.scion_bin == str(tmp_path / "bin" / "scion")


def test_from_scion_gen_rejects_unknown_as(tmp_path: Path):
    (tmp_path / "gen").mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "gen" / "sciond_addresses.json").write_text(
        json.dumps({"1-ff00:0:112": "127.0.0.76"})
    )
    (tmp_path / "bin" / "scion").write_bytes(b"#!/bin/false\n")
    (tmp_path / "bin" / "scion").chmod(0o755)

    with pytest.raises(KeyError, match="not found"):
        ScionShowpathsDiscovery.from_scion_gen("1-ff00:0:999", tmp_path)


def test_paths_from_returns_empty_when_src_is_not_local_as():
    disco = ScionShowpathsDiscovery(
        local_isd_as="1-ff00:0:112",
        scion_bin="/nonexistent/scion",
        sciond_addr="127.0.0.1:30255",
    )
    # Different source than local — the sciond can't answer; discoverer
    # short-circuits without even shelling out.
    assert disco.paths_from("1-ff00:0:120", "1-ff00:0:130") == []


# ----------------------------------------------------------------------
# Real-SCION smoke test
# ----------------------------------------------------------------------


def _scion_up() -> bool:
    """Is there a running SCION topology under ``$SCION_ROOT``?

    Checks that the gen/ directory exists AND that the local AS 112's sciond
    port is accepting TCP connections. Much faster than shelling out to
    ``scion.sh status``.
    """

    import socket

    root = Path(os.environ.get("SCION_ROOT", "/home/kali/code/scion"))
    addrs_file = root / "gen" / "sciond_addresses.json"
    if not addrs_file.is_file():
        return False
    if not (root / "bin" / "scion").is_file():
        return False
    try:
        mapping = json.loads(addrs_file.read_text())
        ip = mapping.get("1-ff00:0:112")
        if ip is None:
            return False
        with socket.create_connection((ip, 30255), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.mark.requires_scion
def test_showpaths_against_real_scion():
    """Smoke: pull live paths from 1-ff00:0:112 to 1-ff00:0:130 via real SCION.

    Expects at least one alive path. This validates the full pipeline: CLI
    exec, JSON parsing, sequence decoding.
    """

    if not _scion_up():
        pytest.skip("SCION topology is not running; bring it up with scion.sh run")

    root = os.environ.get("SCION_ROOT", "/home/kali/code/scion")
    disco = ScionShowpathsDiscovery.from_scion_gen("1-ff00:0:112", root)
    paths = disco.paths_from("1-ff00:0:112", "1-ff00:0:130")
    assert paths, "expected at least one path 1-ff00:0:112 -> 1-ff00:0:130"
    # Source and destination of every returned path must match the query.
    for p in paths:
        assert p[0].isd_as == "1-ff00:0:112"
        assert p[0].ingress == 0
        assert p[-1].isd_as == "1-ff00:0:130"
        assert p[-1].egress == 0
