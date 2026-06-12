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

# Run mode: live (real-time market data + execution) | historical (SQLite replay only).
# Pick via a positional arg (`./start_option_combo_uv.sh historical`) or the MODE env var.
MODE="${1:-${MODE:-live}}"

case "$MODE" in
    live)
        # Live data source backend: ibkr (default, live trading) | futu (free research mode).
        # Override with DATA_SOURCE, e.g. `DATA_SOURCE=futu ./start_option_combo_uv.sh`.
        DATA_SOURCE="${DATA_SOURCE:-ibkr}"
        case "$DATA_SOURCE" in
            futu) BACKEND_SCRIPT="futu_server.py"; BACKEND_LOG="$RUNTIME_DIR/futu_server.log" ;;
            ibkr) BACKEND_SCRIPT="ib_server.py";   BACKEND_LOG="$RUNTIME_DIR/ib_server.log" ;;
            *)    echo "ERROR: unknown DATA_SOURCE '$DATA_SOURCE' (expected futu or ibkr)." >&2; exit 1 ;;
        esac
        BACKEND_LABEL="$DATA_SOURCE -> $BACKEND_SCRIPT"
        FRONTEND_QUERY="entry=live&marketDataMode=live&lockMarketDataMode=1"
        ;;
    historical)
        # Replay-only backend reading sqlite_spy/spy_options.db. DATA_SOURCE is ignored here.
        BACKEND_SCRIPT="historical_server.py"
        BACKEND_LOG="$RUNTIME_DIR/historical_server.log"
        BACKEND_LABEL="historical replay (SQLite) -> $BACKEND_SCRIPT"
        FRONTEND_QUERY="entry=historical&marketDataMode=historical&lockMarketDataMode=1"
        ;;
    *)
        echo "ERROR: unknown MODE '$MODE' (expected live or historical)." >&2
        echo "Usage: $0 [live|historical]   (or set MODE / DATA_SOURCE env vars)" >&2
        exit 1
        ;;
esac

uv run python -m http.server 8000 >>"$HTTP_LOG" 2>&1 &
HTTP_PID=$!

uv run python "$BACKEND_SCRIPT" >>"$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

cleanup() {
    echo
    echo "Stopping services..."
    kill "$HTTP_PID" "$BACKEND_PID" 2>/dev/null || true
    wait "$HTTP_PID" "$BACKEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Started (mode=$MODE, managed by uv):"
echo "  - Frontend: http://localhost:8000/index.html?$FRONTEND_QUERY"
echo "  - Backend ($BACKEND_LABEL): ws://localhost:8765"
echo
echo "PIDs:   http=$HTTP_PID  backend=$BACKEND_PID"
echo
echo "Logs:"
echo "  - $HTTP_LOG"
echo "  - $BACKEND_LOG"
echo
echo "Press Ctrl+C to stop both services."

wait "$HTTP_PID" "$BACKEND_PID"
