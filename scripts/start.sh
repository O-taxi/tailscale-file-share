#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime"
PID_FILE="$RUNTIME_DIR/app.pid"
LOG_FILE="$RUNTIME_DIR/app.log"

mkdir -p "$RUNTIME_DIR"

if [[ -f "$PID_FILE" ]] && kill -0 "$(<"$PID_FILE")" 2>/dev/null; then
  echo "App is already running (PID $(<"$PID_FILE"))."
  exit 0
fi

rm -f "$PID_FILE"
nohup python3 "$ROOT_DIR/app.py" >"$LOG_FILE" 2>&1 &
APP_PID=$!
echo "$APP_PID" >"$PID_FILE"

for _ in {1..20}; do
  if curl --fail --silent http://127.0.0.1:8080/healthz >/dev/null; then
    echo "App started: http://127.0.0.1:8080 (PID $APP_PID)"
    exit 0
  fi
  sleep 0.1
done

echo "App did not start. See $LOG_FILE" >&2
if kill -0 "$APP_PID" 2>/dev/null; then
  kill "$APP_PID"
fi
rm -f "$PID_FILE"
exit 1
