#!/usr/bin/env bash
# Restart the engine if its heartbeat file is older than 120 s, or /health reports "fatal".
set -u
HB=/opt/dhan-quant/data/heartbeat.json
PORT="${HEALTH_PORT:-8787}"
if [ ! -f "$HB" ]; then
  systemctl restart dhan-quant-engine; exit 0
fi
age=$(( $(date +%s) - $(stat -c %Y "$HB") ))
if [ "$age" -gt 120 ]; then
  logger -t dhan-quant "heartbeat stale (${age}s) - restarting"
  systemctl restart dhan-quant-engine; exit 0
fi
status=$(curl -s --max-time 5 "http://127.0.0.1:${PORT}/health" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",""))' 2>/dev/null)
logger -t dhan-quant "health=${status:-unreachable} heartbeat_age=${age}s"
exit 0
