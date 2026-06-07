#!/usr/bin/env bash
# Robust tmux launcher for long-running solver / synth jobs.
#
# Why this exists:
#   Previous launches used `python -u ... 2>&1 | tee LOGFILE`. The pipe
#   between python and tee can deadlock if tee blocks on any I/O hiccup
#   (slow disk, pty churn, full kernel pipe buffer due to tqdm spam).
#   Symptoms: python process still ALIVE but stdout frozen for hours,
#   results files still get written (separate file handles) but log stops.
#   When the process eventually dies, traceback is lost.
#
# Fix: redirect directly to a file (no pipe). Monitor via `tail -f LOGFILE`.
#
# Usage:
#   scripts/launch_in_tmux.sh <session_name> <command...>
#
# Example:
#   scripts/launch_in_tmux.sh solver_v3s python -u infer_async_nestbrowse.py
#
# Env vars consumed (must be exported BEFORE calling, or set in the session):
#   GOOGLE_APPLICATION_CREDENTIALS, MODEL_NAME, GCP_PROJECT_ID, WINDOWS_MCP_URL
#
# Output:
#   logs/<session>_<timestamp>.log    (direct write, no pipe)
#   prints SESSION, PID, LOGFILE to stdout

set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: $0 <session_name> <command...>"
  exit 1
fi

SESSION=$1
shift
CMD=("$@")

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
TS=$(date +%Y%m%d_%H%M%S)
LOGFILE="$REPO_DIR/logs/${SESSION}_${TS}.log"
mkdir -p "$REPO_DIR/logs"

# Kill any prior session of the same name
tmux kill-session -t "$SESSION" 2>/dev/null || true

# Build a command string that:
#   1. cd into repo
#   2. propagate env (only the ones we need for the solver/synth stack)
#   3. quick auth check (so the process doesn't die silently 4s in)
#   4. exec the actual command with stdout+stderr DIRECT-redirected to LOGFILE
#      (no pipe, no tee, no buffering games)
#
# Note: we use `exec` so the python process becomes the tmux pane root and
# `pgrep` / `kill -0` find it cleanly.

CMD_STR=""
for arg in "${CMD[@]}"; do
  # shell-escape each arg
  CMD_STR+=$(printf '%q ' "$arg")
done

# Run inside tmux; exit_pane=on so dead tmux session signals the failure
tmux new -d -s "$SESSION" -c "$REPO_DIR" "
set -e
echo '=== launch at '\$(date)' ==='
echo '=== session: $SESSION'
echo '=== logfile: $LOGFILE'
# Pre-flight auth check (fails fast if SA cred missing)
python3 -c 'import google.auth, google.auth.transport.requests as r; c,_=google.auth.default(scopes=[\"https://www.googleapis.com/auth/cloud-platform\"]); rq=r.Request(); c.refresh(rq); print(\"AUTH OK\", c.token[:25])' \
  || { echo 'AUTH FAIL — aborting'; exit 1; }
# Direct redirect — no tee, no pipe. tail -f to monitor.
exec $CMD_STR > '$LOGFILE' 2>&1
"

# Wait a few seconds for the python process to actually spawn
sleep 4

# Identify the python pid (pgrep may return non-zero if no match — don't let set -e kill us)
# Match on the .py script name (last arg most likely a script path)
SCRIPT_HINT="${CMD[-1]}"
PYTHON_PID=$(pgrep -af -- "$SCRIPT_HINT" 2>/dev/null | grep -vE 'pgrep|launch_in_tmux|/bin/sh' | awk 'NR==1 {print $1}' || true)

echo
echo "SESSION=$SESSION"
echo "PID=${PYTHON_PID:-<not found via pgrep — check 'tmux capture-pane -t $SESSION -p'>}"
echo "LOGFILE=$LOGFILE"
echo
echo "Monitor:"
echo "  tail -f $LOGFILE"
echo "  pgrep -af -- '$SCRIPT_HINT'"
echo "  tmux capture-pane -t $SESSION -p   # see tmux pane (pre-exec output)"
