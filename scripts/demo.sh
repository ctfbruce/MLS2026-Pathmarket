#!/usr/bin/env bash
# PathMarket v2 one-shot demo launcher (§9 pitch ops).
#
# Boots the three long-running processes (aggregator, simulator, user-agent)
# in the foreground, interleaves their output with colored prefixes, and
# tears them all down cleanly on Ctrl-C.
#
# Usage:
#   scripts/demo.sh              # static path discovery (safe default)
#   scripts/demo.sh scion        # real SCION path discovery (needs scion.sh run)
#
# Ports:
#   aggregator  127.0.0.1:8080
#   simulator   127.0.0.1:8081 (scenario API)
#   user-agent  127.0.0.1:8090 (UI + /local)
set -u

MODE="${1:-static}"
SCION_ROOT="${SCION_ROOT:-/home/kali/code/scion}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

mkdir -p runtime

# ANSI colors — makes the multiplexed log readable. Set NO_COLOR=1 to disable.
if [[ -z "${NO_COLOR:-}" && -t 1 ]]; then
  C_AGG=$'\033[38;5;214m'   # gold
  C_SIM=$'\033[38;5;75m'    # cyan
  C_UA=$'\033[38;5;120m'    # green
  C_INFO=$'\033[38;5;244m'  # dim
  C_RST=$'\033[0m'
else
  C_AGG='' C_SIM='' C_UA='' C_INFO='' C_RST=''
fi

PIDS=()
cleanup() {
  echo
  echo "${C_INFO}demo: shutting down…${C_RST}"
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  exit 0
}
trap cleanup SIGINT SIGTERM

# Prefix each line of a command's stdout/stderr with a colored tag.
launch() {
  local tag="$1"; local color="$2"; shift 2
  ( "$@" 2>&1 | while IFS= read -r line; do
      printf '%s[%-10s]%s %s\n' "$color" "$tag" "$C_RST" "$line"
    done ) &
  PIDS+=($!)
}

echo "${C_INFO}demo: mode=$MODE  repo=$REPO_ROOT${C_RST}"
if [[ "$MODE" == "scion" ]]; then
  echo "${C_INFO}demo: SCION_ROOT=$SCION_ROOT (expects 'scion.sh run' already up)${C_RST}"
  if [[ ! -d "$SCION_ROOT/gen" ]]; then
    echo "demo: $SCION_ROOT/gen not found — run 'cd \$SCION_ROOT && ./scion.sh run' first" >&2
    exit 1
  fi
fi

# Aggregator first — simulator and user-agent both depend on it.
launch "aggregator" "$C_AGG" pathmarket-aggregator --config config.toml

# Give the aggregator a moment to bind its port before the others try to
# connect. 1.5s is generous for a local boot; longer waits hurt the demo UX.
sleep 1.5

if [[ "$MODE" == "scion" ]]; then
  launch "simulator" "$C_SIM" env SCION_ROOT="$SCION_ROOT" \
    pathmarket-simulator --config simulator.toml \
    --path-discovery scion --scion-root "$SCION_ROOT"
  launch "user-agent" "$C_UA" env SCION_ROOT="$SCION_ROOT" \
    pathmarket-user-agent --config user_agent.toml \
    --path-discovery scion --scion-root "$SCION_ROOT"
else
  launch "simulator" "$C_SIM" pathmarket-simulator --config simulator.toml \
    --path-discovery static
  launch "user-agent" "$C_UA" pathmarket-user-agent --config user_agent.toml \
    --path-discovery static
fi

sleep 1.0
echo
echo "${C_INFO}demo: open http://127.0.0.1:8090 in a browser.${C_RST}"
echo "${C_INFO}demo: Ctrl-C to stop all three processes.${C_RST}"
echo

# Wait for any one process to exit → kill the others → cleanup.
wait -n 2>/dev/null || true
cleanup
