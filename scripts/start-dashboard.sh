#!/usr/bin/env bash
# start-dashboard.sh — ensure Monitor UI is running on port 8093
cd "$(dirname "$0")/.." || exit 1
PORT=8093
LOG="dashboard.log"

if lsof -nP -i :"$PORT" >/dev/null 2>&1; then
  echo "[$(date '+%F %T')] Monitor UI already running on port $PORT" >> "$LOG"
  exit 0
fi

echo "[$(date '+%F %T')] Starting Monitor UI on port $PORT" >> "$LOG"
nohup python server.py >> "$LOG" 2>&1 &
echo $! >> "$LOG"
