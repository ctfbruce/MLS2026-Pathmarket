"""Step C5 — simulator TOML config loading + CLI assembly tests.

Covers the pure-function helpers in :mod:`pathmarket.simulator.main`: TOML
parsers, persona/generic-AS spec builders, SLA template builders, and raw
Ed25519 key loading from a directory.

The live tick loop itself is smoke-tested via ``--max-ticks`` in the
end-to-end smoke tests; here we stay in-process.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pathmarket.simulator.main import (
    _path_from_toml,
    _policy_from_toml,
    _sla_template_from_toml,
    build_specs_from_config,
    build_templates_from_config,
    load_config,
    load_private_keys,
)


SAMPLE_TOML = """
[aggregator]
url = "http://127.0.0.1:8080"

[simulation]
tick_interval_seconds = 2.5
initial_sla_count = 3
path_discovery = "static"

[personas.hospital]
isd_as = "1-ff00:0:140"
role = "edge-buyer"
quality_profile = "transit-premium"

[personas.hospital.policy]
max_price_per_gb = "0.20"
min_reputation_floor = 0.90
alpha = 3.0
beta = 1.0
uncovered_tolerance = "never"
complaint_sensitivity = "strict"
reshop_on_reputation_drop = 0.05
portfolio_redundancy = 2

[personas.hospital.policy.required_bounds]
latency_max_ms = 8
loss_max_ppm = 100
bandwidth_min_kbps = 2_000_000

[[generic_ases]]
isd_as = "1-ff00:0:150"
role = "transit-good"
quality_profile = "transit-good"

[generic_ases.policy]
max_price_per_gb = "1.00"
min_reputation_floor = 0.0
alpha = 1.0
beta = 1.0
uncovered_tolerance = "anywhere"
complaint_sensitivity = "moderate"
reshop_on_reputation_drop = 1.0
portfolio_redundancy = 0

[[sla_templates]]
price_per_gb = "0.05"
consortium_profile = "transit-good"
validity_hours = 12

[[sla_templates.path]]
isd_as = "1-ff00:0:150"
ingress = 0
egress = 201

[[sla_templates.path]]
isd_as = "1-ff00:0:140"
ingress = 101
egress = 0

[sla_templates.bounds]
latency_max_ms = 10
loss_max_ppm = 500
"""


class TestPolicyFromToml:
    def test_parses_required_bounds_and_scalars(self) -> None:
        cfg = {
            "max_price_per_gb": "0.20",
            "min_reputation_floor": 0.90,
            "alpha": 3.0,
            "beta": 1.0,
            "uncovered_tolerance": "never",
            "complaint_sensitivity": "strict",
            "reshop_on_reputation_drop": 0.05,
            "portfolio_redundancy": 2,
            "required_bounds": {
                "latency_max_ms": 8,
                "loss_max_ppm": 100,
                "bandwidth_min_kbps": 2_000_000,
            },
        }
        p = _policy_from_toml(cfg)
        assert p.max_price_per_gb == "0.20"
        assert p.required_bounds.latency_max_ms == 8
        assert p.required_bounds.bandwidth_min_kbps == 2_000_000
        assert p.complaint_sensitivity == "strict"

    def test_missing_required_bounds_treated_as_dont_care(self) -> None:
        cfg = {
            "max_price_per_gb": "0.10",
            "min_reputation_floor": 0.0,
            "alpha": 1.0,
            "beta": 1.0,
            "uncovered_tolerance": "anywhere",
            "complaint_sensitivity": "moderate",
            "reshop_on_reputation_drop": 1.0,
            "portfolio_redundancy": 0,
        }
        p = _policy_from_toml(cfg)
        assert p.required_bounds.latency_max_ms is None
        assert p.required_bounds.loss_max_ppm is None
        assert p.required_bounds.bandwidth_min_kbps is None


class TestPathAndTemplateFromToml:
    def test_path_shape(self) -> None:
        hops = _path_from_toml(
            [
                {"isd_as": "1-a:0:1", "ingress": 0, "egress": 201},
                {"isd_as": "1-a:0:2", "ingress": 101, "egress": 0},
            ]
        )
        assert len(hops) == 2
        assert hops[0].isd_as == "1-a:0:1"
        assert hops[1].egress == 0

    def test_sla_template_validity_hours_default(self) -> None:
        t = _sla_template_from_toml(
            {
                "price_per_gb": "0.05",
                "consortium_profile": "transit-good",
                "path": [{"isd_as": "1-a:0:1", "ingress": 0, "egress": 0}],
                "bounds": {"latency_max_ms": 10},
            }
        )
        assert t.validity_hours == 24  # default
        assert t.bounds.loss_max_ppm is None


class TestBuildFromConfig:
    def test_specs_include_personas_and_generics(self, tmp_path: Path) -> None:
        toml_path = tmp_path / "simulator.toml"
        toml_path.write_text(SAMPLE_TOML)
        cfg = load_config(toml_path)

        specs = build_specs_from_config(cfg)
        ids = [s.isd_as for s in specs]
        assert "1-ff00:0:140" in ids
        assert "1-ff00:0:150" in ids
        hospital = next(s for s in specs if s.isd_as == "1-ff00:0:140")
        assert hospital.role == "edge-buyer"
        assert hospital.quality_profile == "transit-premium"

    def test_templates_parse_path_and_bounds(self, tmp_path: Path) -> None:
        toml_path = tmp_path / "simulator.toml"
        toml_path.write_text(SAMPLE_TOML)
        cfg = load_config(toml_path)
        templates = build_templates_from_config(cfg)
        assert len(templates) == 1
        tmpl = templates[0]
        assert tmpl.price_per_gb == "0.05"
        assert len(tmpl.path) == 2
        assert tmpl.bounds.latency_max_ms == 10
        assert tmpl.validity_hours == 12

    def test_real_simulator_toml_has_source_cosigner_templates(self) -> None:
        # Guards that the demo's Path A and Path B full-cover templates
        # remain present — the claim flow in the UI depends on them.
        repo_root = Path(__file__).resolve().parents[1]
        cfg = load_config(repo_root / "simulator.toml")
        templates = build_templates_from_config(cfg)
        cosigner_sequences = [[h.isd_as for h in t.path] for t in templates]
        assert ["1-ff00:0:112", "1-ff00:0:130"] in cosigner_sequences, (
            "Path A direct cover [112, 130] missing from simulator.toml"
        )
        assert [
            "1-ff00:0:112", "1-ff00:0:111", "1-ff00:0:120", "1-ff00:0:130"
        ] in cosigner_sequences, (
            "Path B full cover [112, 111, 120, 130] missing from simulator.toml"
        )


class TestLoadPrivateKeys:
    def test_loads_all_keys(self, tmp_path: Path) -> None:
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        ases = ["1-a:0:1", "1-a:0:2"]
        for a in ases:
            key = Ed25519PrivateKey.generate()
            raw = key.private_bytes_raw()
            (keys_dir / f"{a}.private").write_bytes(raw)

        loaded = load_private_keys(keys_dir, ases)
        assert set(loaded.keys()) == set(ases)
        for k in loaded.values():
            assert isinstance(k, Ed25519PrivateKey)

    def test_missing_key_raises(self, tmp_path: Path) -> None:
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        with pytest.raises(FileNotFoundError, match="missing private key"):
            load_private_keys(keys_dir, ["1-a:0:1"])
