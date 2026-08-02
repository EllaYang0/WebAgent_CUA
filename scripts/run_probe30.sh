#!/usr/bin/env bash
# Launch the grounded re-collection probe, fully detached.
#
# Why not scripts/launch_in_tmux.sh: a tmux server has been running on this box
# since May, and `tmux new` inherits the SERVER's environment, not the calling
# shell's. GOOGLE_APPLICATION_CREDENTIALS / BRAVE_SEARCH_KEY are therefore absent
# inside the new session, the launcher's `set -e` auth pre-flight fails, and the
# session dies before it writes a log. This script sources the env itself.
#
# Usage: setsid scripts/run_probe30.sh >/dev/null 2>&1 &
set -euo pipefail

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_DIR"

set -a
source .env.eval          # BRAVE_SEARCH_KEY — the `search` tool is dead without it
set +a

# Set explicitly rather than inherited: under `tmux new` the session gets the tmux
# SERVER's environment (running here since May), where this is unset — google.auth
# then finds no credentials and every LLM call dies.
export GOOGLE_APPLICATION_CREDENTIALS=${GOOGLE_APPLICATION_CREDENTIALS:-/scr/rucnyz/.config/gcloud/vertex-express-key.json}
export WINDOWS_MCP_URL=${WINDOWS_MCP_URL:-http://localhost:8015}
export GCP_PROJECT_ID=${GCP_PROJECT_ID:-msagentrt}
export BENCHMARK_NAME=${BENCHMARK_NAME:-recollect_grounded_probe_30}
export RUN_ID=${RUN_ID:-$BENCHMARK_NAME}

LOGFILE="$REPO_DIR/logs/${RUN_ID}_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$REPO_DIR/logs"
echo "$LOGFILE" > "$REPO_DIR/logs/${RUN_ID}.current"

# Direct redirect, no pipe — a tee/pipe here has previously deadlocked on tqdm
# spam and frozen stdout while the process stayed alive (see launch_in_tmux.sh).
# No `exec`: keep the wrapping shell alive after python exits so the tmux pane
# survives a crash/interrupt and its `echo EXIT=$?; sleep` can show the cause,
# instead of the pane vanishing the instant python dies.
python3 -u infer_async_nestbrowse.py > "$LOGFILE" 2>&1 < /dev/null
echo "python exited with $? at $(date)" >> "$LOGFILE"
