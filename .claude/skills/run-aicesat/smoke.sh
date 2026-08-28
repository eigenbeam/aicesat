#!/usr/bin/env bash
# Start/stop the aicesat widget HTTP server for local/agent viewing.
# Usage: smoke.sh start | smoke.sh stop
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PORT="${AICESAT_PORT:-8765}"
LOG="/tmp/aicesat_serve.log"

start() {
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found on PATH - installing it into the user site" >&2
    python3 -m pip install --user uv
  fi

  if lsof -ti:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "something is already listening on :$PORT - leaving it alone" >&2
    exit 1
  fi

  cd "$REPO_ROOT"
  uv sync

  # aicesat-server (the pyproject.toml entry point) speaks MCP over
  # stdio: it starts the HTTP widget server on a background thread,
  # then blocks reading stdin. Launched with `&` (no live stdin), that
  # read hits EOF immediately and the whole process exits — the HTTP
  # thread dies with it even though its startup log line already
  # printed. scripts/serve.py is the widget-server-only entry point
  # meant for exactly this: it starts the same HTTP server, then just
  # sleeps, so it survives being backgrounded.
  nohup uv run scripts/serve.py > "$LOG" 2>&1 &
  disown

  echo "waiting for http://127.0.0.1:$PORT/ ..." >&2
  for i in $(seq 1 30); do
    curl -sf "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break
    sleep 1
  done

  if ! curl -sf "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
    echo "server did not come up - see $LOG" >&2
    cat "$LOG" >&2
    exit 1
  fi

  TITLE=$(curl -s "http://127.0.0.1:$PORT/" | grep -o '<title>[^<]*</title>')
  echo "up: http://127.0.0.1:$PORT/  ($TITLE)"
  echo "log: $LOG"
}

stop() {
  local killed=0
  local pids
  pids="$(lsof -ti:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    echo "$pids" | xargs kill
    killed=1
  fi

  # index.py / planner.py fan work out to a ProcessPoolExecutor; if a
  # background job (index build, scene planning) is in flight when the
  # server above is killed, its worker processes are reparented to
  # init rather than exiting, and linger as
  # `multiprocessing.spawn ... --multiprocessing-fork` under the same
  # venv. Sweep those explicitly.
  local orphans
  orphans="$(pgrep -f "$REPO_ROOT/.venv/bin/python3.*multiprocessing" 2>/dev/null || true)"
  if [ -n "$orphans" ]; then
    echo "$orphans" | xargs kill
    killed=1
  fi

  if [ "$killed" = 1 ]; then
    echo "stopped"
  else
    echo "nothing was listening on :$PORT"
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac
