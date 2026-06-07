#!/usr/bin/env python3
"""One-off: rebuild trajectory.jsonl from success.jsonl + failure.jsonl when
the original trajectory.jsonl is truncated.

Background: solver process can die with its stdout pipe buffer frozen, leaving
trajectory.jsonl with only the first few records that managed to flush before
the freeze. success.jsonl / failure.jsonl keep getting written (separate file
handles, not via stdout) so the per-task outcomes are preserved — but they
lack the `trajectory` step list that build_dataset.py needs.

This script reconstructs minimal step lists by parsing the agent's assistant
messages for inline `<tool_call>` blocks and `<answer>` tags. Produces records
in the same outer schema as the original trajectory.jsonl, with a slimmed
`trajectory` step list containing only the fields build_dataset cares about:
  - action: "tool_call" | "final_answer" | "termination"
  - tool_name, tool_args, ref, text, url   (for tool_call)
  - prediction, eval_result, eval_reasoning (for final_answer)
  - reason                                  (for termination)
  - turn

Usage:
    python scripts/reconstruct_trajectory.py <run_id>

E.g.:
    python scripts/reconstruct_trajectory.py wiki_2hop_v3_scaled

Backs up the original to trajectory.jsonl.partial<N> where N = original line count.
"""
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Regex over assistant content: each tool_call is a JSON block inside
# <tool_call>...</tool_call>. The JSON has "name": "<tool>" and
# "arguments": <object>. arguments may be on multiple lines.
TOOL_CALL_RE = re.compile(
    r'<tool_call>\s*({[\s\S]*?})\s*</tool_call>',
    re.DOTALL,
)
# Fallback: some assistant messages emit bare JSON without the wrapper tags
BARE_TOOL_RE = re.compile(
    r'\{\s*"name"\s*:\s*"(visit|click|fill|search)"\s*,\s*"arguments"\s*:\s*({[\s\S]*?})\s*\}',
    re.DOTALL,
)
ANSWER_RE = re.compile(r'<answer>([\s\S]*?)</answer>', re.DOTALL)


def _parse_tool_calls(content: str) -> list:
    """Return a list of {'name': ..., 'arguments': {...}} dicts found in
    assistant content. Tolerates malformed JSON by also trying a fallback
    regex over bare {"name":..., "arguments":...} blocks."""
    calls = []

    # Primary path: <tool_call>{...}</tool_call>
    for m in TOOL_CALL_RE.finditer(content):
        try:
            obj = json.loads(m.group(1))
            name = obj.get('name')
            args = obj.get('arguments', {})
            # arguments could be a string (rare); try parsing
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if name:
                calls.append({'name': name, 'arguments': args or {}})
        except Exception:
            # Try the bare-block fallback on this match's text
            for m2 in BARE_TOOL_RE.finditer(m.group(0)):
                try:
                    args = json.loads(m2.group(2))
                    calls.append({'name': m2.group(1), 'arguments': args or {}})
                except Exception:
                    pass

    # If primary regex found nothing, last-chance try bare-block over full content
    if not calls:
        for m in BARE_TOOL_RE.finditer(content):
            try:
                args = json.loads(m.group(2))
                calls.append({'name': m.group(1), 'arguments': args or {}})
            except Exception:
                pass

    return calls


def _build_step_list(record: dict) -> list:
    """Build a minimal trajectory step list from a success/failure record."""
    steps = []
    msgs = record.get('messages') or []
    turn = 0

    for m in msgs:
        if m.get('role') != 'assistant':
            continue
        turn += 1
        content = m.get('content', '') or ''
        if not isinstance(content, str):
            # rare: structured content (list of parts)
            content = json.dumps(content)

        # 1) Tool calls
        for call in _parse_tool_calls(content):
            args = call['arguments']
            step = {
                'turn': turn,
                'action': 'tool_call',
                'tool_name': call['name'],
                'tool_args': args,
            }
            # Surface common args at top level (build_dataset.signature reads these)
            if 'ref' in args:
                step['ref'] = args['ref']
            if 'text' in args:
                step['text'] = args['text']
            if 'url' in args:
                step['url'] = args['url']
            if 'queries' in args:
                step['queries'] = args['queries']
            steps.append(step)

        # 2) Final answer (mutually exclusive with tool_call in well-formed turns,
        #    but some agents emit both — we accept both, final_answer last)
        am = ANSWER_RE.search(content)
        if am:
            pred = am.group(1).strip()
            steps.append({
                'turn': turn,
                'action': 'final_answer',
                'prediction': pred,
                'task_type': record.get('task_type'),
                'eval_result': record.get('termination'),
                'eval_reasoning': record.get('eval_reasoning'),
            })

    # 3) Terminal record
    term = record.get('termination')
    if term:
        steps.append({
            'turn': turn,
            'action': 'termination',
            'reason': term,
        })

    return steps


def _build_traj_record(record: dict) -> dict:
    """Outer schema mirrors infer_async_nestbrowse.py's trajectory_record:
    task_id, task, task_type, termination, visited_urls, gt_urls, prediction,
    eval_reasoning, trajectory."""
    return {
        'task_id': record.get('task_id'),
        'task': record.get('task'),
        'task_type': record.get('task_type'),
        'termination': record.get('termination'),
        'visited_urls': record.get('visited_urls', []),
        'gt_urls': record.get('gt_urls', []),
        'prediction': record.get('prediction'),
        'eval_reasoning': record.get('eval_reasoning'),
        'trajectory': _build_step_list(record),
    }


def main():
    if len(sys.argv) < 2:
        print("usage: python scripts/reconstruct_trajectory.py <run_id>")
        sys.exit(1)
    run_id = sys.argv[1]
    res_dir = os.path.join(ROOT, 'results', run_id)
    success_path = os.path.join(res_dir, 'success.jsonl')
    failure_path = os.path.join(res_dir, 'failure.jsonl')
    traj_path = os.path.join(res_dir, 'trajectory.jsonl')

    # Backup existing trajectory.jsonl
    if os.path.exists(traj_path):
        with open(traj_path) as f:
            n_orig = sum(1 for _ in f)
        backup = f'{traj_path}.partial{n_orig}'
        shutil.copy2(traj_path, backup)
        print(f'[backup] {traj_path}  ->  {backup}  (orig had {n_orig} records)')

    # Read success + failure
    records = []
    for p, label in [(success_path, 'success'), (failure_path, 'failure')]:
        if not os.path.exists(p):
            print(f'[skip] {p} does not exist')
            continue
        n = 0
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                    n += 1
                except Exception as e:
                    print(f'[!] {label}: bad json line: {e}')
        print(f'[read] {label}.jsonl: {n} records')

    # Reconstruct and write
    rebuilt = [_build_traj_record(r) for r in records]
    with open(traj_path, 'w', encoding='utf-8') as f:
        for r in rebuilt:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')

    # Per-record sanity
    n_with_tools = sum(1 for r in rebuilt if any(s.get('action') == 'tool_call' for s in r['trajectory']))
    n_with_answer = sum(1 for r in rebuilt if any(s.get('action') == 'final_answer' for s in r['trajectory']))
    avg_steps = sum(len(r['trajectory']) for r in rebuilt) / max(1, len(rebuilt))
    print()
    print(f'[write] {traj_path}: {len(rebuilt)} records')
    print(f'  has >=1 tool_call:    {n_with_tools} / {len(rebuilt)}')
    print(f'  has final_answer:     {n_with_answer} / {len(rebuilt)}')
    print(f'  avg steps per record: {avg_steps:.1f}')

    # Cross-check against original trajectory.jsonl head (5 records)
    backup_path = f'{traj_path}.partial5'
    if os.path.exists(backup_path):
        print()
        print('[crosscheck] comparing reconstructed vs original head 5 records')
        orig_by_id = {}
        with open(backup_path) as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    orig_by_id[r['task_id']] = r
        for new_r in rebuilt:
            tid = new_r['task_id']
            if tid in orig_by_id:
                orig = orig_by_id[tid]
                orig_tool_calls = sum(1 for s in orig.get('trajectory', []) if s.get('action') == 'tool_call')
                new_tool_calls = sum(1 for s in new_r['trajectory'] if s.get('action') == 'tool_call')
                match = '✅' if orig_tool_calls == new_tool_calls else '⚠️'
                print(f'  {match} {tid[:8]}: orig={orig_tool_calls} tool_calls, new={new_tool_calls}')


if __name__ == '__main__':
    main()
