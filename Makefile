.PHONY: help install fast full scion lint type clean \
	demo demo-split demo-scion demo-scion-split \
	demo-aggregator demo-simulator demo-user-agent \
	demo-scion-aggregator demo-scion-simulator demo-scion-user-agent

SCION_ROOT ?= $(HOME)/code/scion

help:
	@echo "PathMarket v2 targets:"
	@echo "  install       pip install -e .[dev] (requires active venv)"
	@echo "  fast          pytest -m 'not end_to_end and not requires_scion'"
	@echo "  full          pytest (all tests except requires_scion)"
	@echo "  scion         pytest -m requires_scion"
	@echo "  lint          ruff check"
	@echo "  type          mypy src"
	@echo "  clean         remove caches and build artifacts"
	@echo ""
	@echo "Demo (python simulation, static paths — safe default):"
	@echo "  demo              one-shot: starts all three, Ctrl-C stops all"
	@echo "  demo-split        print three commands for tmux/separate panes"
	@echo "  demo-aggregator   run aggregator on :8080"
	@echo "  demo-simulator    run simulator on :8081 (static discovery)"
	@echo "  demo-user-agent   run user agent + UI on :8090 (static discovery)"
	@echo ""
	@echo "Demo (real SCION — requires scion.sh run at $$SCION_ROOT):"
	@echo "  demo-scion             one-shot: all three, SCION-backed"
	@echo "  demo-scion-split       print three commands for tmux"
	@echo "  demo-scion-aggregator  run aggregator on :8080"
	@echo "  demo-scion-simulator   run simulator with live ScionShowpathsDiscovery"
	@echo "  demo-scion-user-agent  run user agent with live ScionShowpathsDiscovery"

install:
	pip install -e ".[dev]"

fast:
	pytest -m "not end_to_end and not requires_scion"

full:
	pytest -m "not requires_scion"

scion:
	pytest -m requires_scion

lint:
	ruff check src tests scripts

type:
	mypy src

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +

# -------- Demo recipes: python simulation (static path discovery) --------

demo-aggregator:
	pathmarket-aggregator --config config.toml

demo-simulator:
	pathmarket-simulator --config simulator.toml --path-discovery static

demo-user-agent:
	pathmarket-user-agent --config user_agent.toml --path-discovery static

demo:
	@scripts/demo.sh static

demo-split:
	@echo "Run each in its own terminal (or tmux pane):"
	@echo "  make demo-aggregator"
	@echo "  make demo-simulator"
	@echo "  make demo-user-agent"
	@echo "Then open http://127.0.0.1:8090 in a browser."

# -------- Demo recipes: real SCION (live ScionShowpathsDiscovery) --------
# These require 'scion.sh run' to be up at $SCION_ROOT and the simulator/user
# agent's configured ASes to be present in gen/sciond_addresses.json.

demo-scion-aggregator:
	pathmarket-aggregator --config config.toml

demo-scion-simulator:
	SCION_ROOT=$(SCION_ROOT) pathmarket-simulator \
		--config simulator.toml --path-discovery scion --scion-root $(SCION_ROOT)

demo-scion-user-agent:
	SCION_ROOT=$(SCION_ROOT) pathmarket-user-agent \
		--config user_agent.toml --path-discovery scion --scion-root $(SCION_ROOT)

demo-scion:
	@SCION_ROOT=$(SCION_ROOT) scripts/demo.sh scion

demo-scion-split:
	@echo "Requires 'scion.sh run' to be up at $(SCION_ROOT)."
	@echo "Run each in its own terminal (or tmux pane):"
	@echo "  make demo-scion-aggregator"
	@echo "  make demo-scion-simulator"
	@echo "  make demo-scion-user-agent"
	@echo "Then open http://127.0.0.1:8090 in a browser."
