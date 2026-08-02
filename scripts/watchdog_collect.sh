#!/usr/bin/env bash
# Overnight supervisor for the n200 reasoned collection.
#
# Relaunches the collection ONLY when it died abnormally (crash / SIGINT / kill),
# and stops for good once a run exits normally (code 0 = every task processed one
# pass). This avoids the infinite-retry trap: failed tasks never enter
# success.jsonl, so a naive "relaunch on exit" would reprocess the ~25% failures
# forever. run_probe30.sh appends "python exited with <code>" on a clean exit; its
# absence in the newest log means the process was killed, which is the only case we
# resume.
set -u

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_DIR"

export BENCHMARK_NAME=${BENCHMARK_NAME:-wiki_2hop_v3_n200}
export RUN_ID=${RUN_ID:-wiki_2hop_n200_reasoned}

MAX_RESTARTS=12
restarts=0
WLOG="$REPO_DIR/logs/${RUN_ID}_watchdog.log"
echo "watchdog start $(date) — supervising RUN_ID=$RUN_ID" >> "$WLOG"

alive() { pgrep -f "python3 -u infer_async_nestbrowse[.]py" >/dev/null 2>&1; }

while true; do
  if alive; then
    sleep 60
    continue
  fi

  # Process is not running. Decide: normal completion vs abnormal death.
  cur=$(cat "$REPO_DIR/logs/${RUN_ID}.current" 2>/dev/null)
  if [ -n "$cur" ] && grep -q "python exited with 0" "$cur" 2>/dev/null; then
    echo "watchdog: normal exit detected in $cur — done $(date)" >> "$WLOG"
    break
  fi

  if [ "$restarts" -ge "$MAX_RESTARTS" ]; then
    echo "watchdog: hit MAX_RESTARTS=$MAX_RESTARTS, giving up $(date)" >> "$WLOG"
    break
  fi

  restarts=$((restarts + 1))
  echo "watchdog: abnormal death, resuming (restart #$restarts) $(date)" >> "$WLOG"
  # run_probe30.sh runs python in the foreground and self-sources creds; background
  # it so the watchdog keeps supervising, then poll until it comes up.
  setsid scripts/run_probe30.sh >/dev/null 2>&1 &
  sleep 20
done
echo "watchdog stop $(date)" >> "$WLOG"
