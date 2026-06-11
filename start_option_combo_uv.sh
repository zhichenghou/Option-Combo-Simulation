#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: 'uv' is not installed or not on PATH." >&2
    echo "Install it from https://astral.sh/uv (e.g. 'curl -LsSf https://astral.sh/uv/install.sh | sh')." >&2
    exit 1
fi

# Make sure dependencies are installed / up to date. Fast no-op if already synced.
uv sync

RUNTIME_DIR="$SCRIPT_DIR/logs"
mkdir -p "$RUNTIME_DIR"
HTTP_LOG="$RUNTIME_DIR/http_server.log"

# Pick the live data source backend: futu (default, free research mode) | ibkr.
DATA_SOURCE="${DATA_SOURCE:-futu}"
case "$DATA_SOURCE" in
    futu) BACKEND_SCRIPT="futu_server.py"; IB_LOG="$RUNTIME_DIR/futu_server.log" ;;
    ibkr) BACKEND_SCRIPT="ib_server.py";   IB_LOG="$RUNTIME_DIR/ib_server.log" ;;
    *)    echo "ERROR: unknown DATA_SOURCE '$DATA_SOURCE' (expected futu or ibkr)." >&2; exit 1 ;;
esac

uv run python -m http.server 8000 >>"$HTTP_LOG" 2>&1 &
HTTP_PID=$!

uv run python "$BACKEND_SCRIPT" >>"$IB_LOG" 2>&1 &
IB_PID=$!

cleanup() {
    echo
    echo "Stopping services..."
    kill "$HTTP_PID" "$IB_PID" 2>/dev/null || true
    wait "$HTTP_PID" "$IB_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Started (managed by uv):"
echo "  - Frontend: http://localhost:8000/index.html?entry=live&marketDataMode=live&lockMarketDataMode=1"
echo "  - Data bridge ($DATA_SOURCE -> $BACKEND_SCRIPT): ws://localhost:8765"
echo
echo "PIDs:   http=$HTTP_PID  backend=$IB_PID"
echo
echo "Logs:"
echo "  - $HTTP_LOG"
echo "  - $IB_LOG"
echo
echo "Press Ctrl+C to stop both services."

wait "$HTTP_PID" "$IB_PID"
