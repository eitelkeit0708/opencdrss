#!/bin/sh
set -eu
cd "$(dirname "$0")"
LOG_FILE="${LOG_FILE:-opencd_free_rss.log}"
MAX_BYTES="${LOG_MAX_BYTES:-2097152}"
if [ -s opencd_free_rss.pid ] && kill -0 "$(cat opencd_free_rss.pid)" 2>/dev/null && ps -p "$(cat opencd_free_rss.pid)" -o args= | grep -q opencd_free_rss.py; then
  echo "already running pid=$(cat opencd_free_rss.pid)"
  exit 0
fi
if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt "$MAX_BYTES" ]; then
  tail -c $((MAX_BYTES / 2)) "$LOG_FILE" > "$LOG_FILE.tmp"
  mv "$LOG_FILE.tmp" "$LOG_FILE"
fi
nohup python3 opencd_free_rss.py >> "$LOG_FILE" 2>&1 &
echo $! > opencd_free_rss.pid
echo "started pid=$(cat opencd_free_rss.pid)"
