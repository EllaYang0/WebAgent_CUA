#!/bin/bash
# Benchmark watchdog — appends a metrics snapshot every $INTERVAL seconds to
# $STATUS file. Exits when the python benchmark process dies, writes a final
# DONE summary.
#
# Env:
#   PID=<pid of python>          (required) — so watchdog exits when benchmark exits
#   LOG=<path to python log>     (required) — log to scan for metrics
#   STATUS=<path to status .md>  (required) — output file
#   RESULT_PREFIX=<path prefix>  (required) — prefix for *_success.jsonl / *_failure.jsonl / *_trajectory.jsonl
#   INTERVAL=<seconds>           (default 300)

set -u
INTERVAL=${INTERVAL:-300}
: "${PID:?PID required}"
: "${LOG:?LOG required}"
: "${STATUS:?STATUS required}"
: "${RESULT_PREFIX:?RESULT_PREFIX required}"

mkdir -p "$(dirname "$STATUS")"
{
    echo "# Watchdog log — started $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo ""
    echo "- python pid: $PID"
    echo "- benchmark log: $LOG"
    echo "- result prefix: $RESULT_PREFIX"
    echo "- interval: ${INTERVAL}s"
    echo ""
    echo "---"
    echo ""
} > "$STATUS"

snapshot() {
    local label="$1"
    local ts elapsed
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    if kill -0 "$PID" 2>/dev/null; then
        elapsed=$(ps -p "$PID" -o etime= 2>/dev/null | tr -d ' ')
    else
        elapsed="(process exited)"
    fi

    local log_lines progress last_calls
    log_lines=$(wc -l < "$LOG" 2>/dev/null || echo 0)
    progress=$(tr '\r' '\n' < "$LOG" 2>/dev/null | grep -oE "[0-9]+/[0-9]+ \[[^]]+\]" | tail -1)
    last_calls=$(grep -E "Call tool " "$LOG" 2>/dev/null | tail -3 | cut -c1-180)

    local succ_n fail_n traj_n terms
    succ_n=$(wc -l < "${RESULT_PREFIX}_success.jsonl" 2>/dev/null || echo 0)
    fail_n=$(wc -l < "${RESULT_PREFIX}_failure.jsonl" 2>/dev/null || echo 0)
    traj_n=$(wc -l < "${RESULT_PREFIX}_trajectory.jsonl" 2>/dev/null || echo 0)
    terms=$(grep -oE '"termination": "[^"]+"' "${RESULT_PREFIX}_trajectory.jsonl" 2>/dev/null \
            | sort | uniq -c | awk '{printf "%s=%d ", $3, $1}')
    terms=${terms:-(none yet)}

    {
        echo "## [$label] $ts"
        echo ""
        echo "- elapsed: $elapsed"
        echo "- log lines: $log_lines"
        [ -n "$progress" ] && echo "- tqdm: \`$progress\`"
        echo ""
        echo "### Result counts"
        echo "- success.jsonl: $succ_n"
        echo "- failure.jsonl: $fail_n"
        echo "- trajectory.jsonl: $traj_n"
        echo "- terminations: $terms"
        echo ""
        echo "### Execution-layer metrics"
        echo "| metric | count |"
        echo "|---|---|"
        echo "| about:blank reset | $(grep -c 'reset browser to about:blank' "$LOG" 2>/dev/null) |"
        echo "| Same-SPA refresh | $(grep -c 'Same-SPA refresh' "$LOG" 2>/dev/null) |"
        echo "| Post-navigate snapshot | $(grep -c 'Post-navigate snapshot' "$LOG" 2>/dev/null) |"
        echo "| Backfilled inline snapshot | $(grep -c 'Backfilled inline snapshot' "$LOG" 2>/dev/null) |"
        echo "| DOM click ok | $(grep -c 'DOM click succeeded' "$LOG" 2>/dev/null) |"
        echo "| DOM click fail | $(grep -c 'DOM click failed' "$LOG" 2>/dev/null) |"
        echo "| DOM fill ok | $(grep -c 'DOM fill succeeded' "$LOG" 2>/dev/null) |"
        echo "| DOM fill fail | $(grep -c 'DOM fill failed' "$LOG" 2>/dev/null) |"
        echo "| Visual trigger (click) | $(grep -c 'Falling back to visual click' "$LOG" 2>/dev/null) |"
        echo "| Visual trigger (fill) | $(grep -c 'Falling back to visual fill' "$LOG" 2>/dev/null) |"
        echo "| Visual Verification passed | $(grep -c 'Verification passed' "$LOG" 2>/dev/null) |"
        echo "| Visual Verification failed | $(grep -c 'Verification failed' "$LOG" 2>/dev/null) |"
        echo "| URL CHECK changed | $(grep -c 'URL CHECK\] URL changed' "$LOG" 2>/dev/null) |"
        echo "| URL CHECK did NOT change | $(grep -c 'did NOT change' "$LOG" 2>/dev/null) |"
        echo ""
        if [ -n "$last_calls" ]; then
            echo "### Last 3 tool calls"
            echo '```'
            echo "$last_calls"
            echo '```'
        fi
        echo ""
        echo "---"
        echo ""
    } >> "$STATUS"
}

snapshot "STARTUP"

while kill -0 "$PID" 2>/dev/null; do
    sleep "$INTERVAL"
    snapshot "TICK"
done

# Final summary
snapshot "DONE"

{
    echo "# Final answers breakdown"
    echo ""
    echo '```'
    python3 -c "
import json
import os
traj='${RESULT_PREFIX}_trajectory.jsonl'
if os.path.exists(traj):
    terms={}
    ans=0
    ai=0
    for line in open(traj):
        d=json.loads(line)
        t=d.get('termination','(none)')
        terms[t]=terms.get(t,0)+1
    total=sum(terms.values())
    print(f'total tasks recorded: {total}')
    for k,v in sorted(terms.items(), key=lambda x:-x[1]):
        pct=(100*v/total) if total else 0
        print(f'  {k}: {v} ({pct:.1f}%)')
"
    echo '```'
} >> "$STATUS"
