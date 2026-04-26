#!/usr/bin/env python3
"""Build a per-category dataset from a finished benchmark run.

Reads `<run>_success.jsonl` + `<run>_failure.jsonl` + `<run>_trajectory.jsonl`
(produced by `infer_async_nestbrowse.py`), classifies each trajectory by its
shape (success quality, repetition, length), and writes per-purpose dataset
files plus a manifest.

Output (under `dataset/<run>/`):
- sft_positive_clean.jsonl    — A class: clean success (≥3 tools, ≤2 repeats)
- sft_positive_messy.jsonl    — A2 class: success but messy; we keep
                                 the "first-success prefix" (messages up to
                                 and including the turn that emitted <answer>)
- rerank_negative_hard.jsonl  — B class: real attempt that ended wrong
                                 (≥5 tools); to be paired by task_id with a
                                 future success rollout
- rerank_negative_quick.jsonl — B2 class: agent gave up early
- bfs_prefix.jsonl            — C2 + D2 + C class: stuck or mid-run-dead
                                 trajectories; we extract the "non-repeating
                                 prefix" (turns up to where repetition began)
                                 as a BFS starting point
- discarded.jsonl             — D class: 0-tool trajectories (no signal)
- manifest.json               — counts + index

Run:
    python scripts/build_dataset.py browsecomp_first50
"""
import os
import sys
import json
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_jsonl(p):
    if not os.path.exists(p):
        return []
    out = []
    for line in open(p):
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def trajectory_features(tr_record, full_record):
    """Compute features used for categorization."""
    traj = tr_record.get("trajectory") or []
    msgs = (full_record or {}).get("messages") or []
    tool_calls = [s for s in traj if s.get("action") == "tool_call"]
    sigs = []
    for s in tool_calls:
        tn = s.get("tool_name")
        if tn == "visit":
            sigs.append(("visit", s.get("url", "")))
        elif tn == "click":
            sigs.append(("click", s.get("ref", "")))
        elif tn == "fill":
            sigs.append(("fill", s.get("ref", ""), str(s.get("text", ""))[:40]))
        elif tn == "search":
            sigs.append(("search", tuple(s.get("queries", []))[:1]))
    sig_counter = Counter(sigs)
    sig_dups = sum(c - 1 for c in sig_counter.values() if c > 1)
    turns = max([s.get("turn", 0) for s in traj], default=0)
    return {
        "n_tool_calls": len(tool_calls),
        "sig_dups": sig_dups,
        "turns": turns,
        "n_msgs": len(msgs),
        "tool_calls": tool_calls,
        "msgs": msgs,
    }


def categorize(tr_record, feat):
    term = tr_record.get("termination")
    n = feat["n_tool_calls"]
    dups = feat["sig_dups"]
    if term == "answer":
        return "A_clean_success" if (n >= 3 and dups <= 2) else "A2_messy_success"
    if term == "answer_incorrect":
        return "B_real_attempt_wrong" if n >= 5 else "B2_quick_wrong"
    if term == "max_turn_exceeded":
        return "C2_explored_long" if dups < 5 else "C_stuck_loop"
    if term == "llm_response_error":
        return "D2_dead_mid_run" if n >= 1 else "D_dead_at_start"
    return "Z_unknown"


def first_success_prefix(msgs, traj):
    """For a successful trajectory, return messages up to and including the
    turn that emitted <answer>. If no <answer> turn found in trajectory,
    return all messages unchanged."""
    answer_turn = None
    for s in traj:
        if s.get("action") == "final_answer":
            answer_turn = s.get("turn")
            break
    if answer_turn is None:
        return msgs
    # Each turn in messages is roughly: assistant + (optional) tool_response.
    # The "final answer" turn N corresponds to the assistant message that has
    # the <answer> tag. Walk msgs and stop after we see an assistant whose
    # content contains "<answer>".
    out = []
    for m in msgs:
        out.append(m)
        if m.get("role") == "assistant" and "<answer>" in str(m.get("content", "")):
            break
    return out


def non_repeating_prefix(msgs, tool_calls, max_dup_threshold=2):
    """For stuck / mid-run-dead trajectories, return the messages up to the
    point where action repetition started crossing the threshold. This gives
    BFS a sane starting state.

    Heuristic: walk tool_calls in order, accumulating signature counts.
    Cut at the first tool_call whose signature would push any (tool, arg)
    duplicate count over `max_dup_threshold`. Then map back to the
    corresponding message slice.
    """
    if not tool_calls:
        return msgs
    seen = Counter()
    cut_turn = None
    for s in tool_calls:
        tn = s.get("tool_name")
        if tn == "visit":
            sig = ("visit", s.get("url", ""))
        elif tn == "click":
            sig = ("click", s.get("ref", ""))
        elif tn == "fill":
            sig = ("fill", s.get("ref", ""), str(s.get("text", ""))[:40])
        elif tn == "search":
            sig = ("search", tuple(s.get("queries", []))[:1])
        else:
            sig = (tn,)
        seen[sig] += 1
        if seen[sig] > max_dup_threshold:
            cut_turn = s.get("turn")
            break
    if cut_turn is None:
        return msgs
    # Walk msgs, count assistant turns, stop at cut_turn (exclusive — we drop
    # the repeating action and what follows).
    out = []
    asst_seen = 0
    for m in msgs:
        if m.get("role") == "assistant":
            asst_seen += 1
            if asst_seen >= cut_turn:
                break
        out.append(m)
    return out


def build(run_id, model_prefix="google_gemini-3.1-pro-preview", use_truth=False):
    """If use_truth is True, prefer the *_truth.jsonl files produced by
    reeval_with_truth.py (ground-truth-aware judge). Falls back to the original
    files when truth-files are missing."""
    res_dir = os.path.join(ROOT, "results")
    suffix = "_truth" if use_truth else ""
    out_dir = os.path.join(ROOT, "dataset", run_id + ("_truth" if use_truth else ""))
    os.makedirs(out_dir, exist_ok=True)
    prefix = os.path.join(res_dir, f"{model_prefix}_results_{run_id}")
    # Allow falling back if a truth-suffixed file is missing
    def _pick(name):
        cand = prefix + name + suffix + ".jsonl"
        if use_truth and not os.path.exists(cand):
            print(f"[!] {cand} missing, falling back to {prefix + name + '.jsonl'}")
            return prefix + name + ".jsonl"
        return cand
    trajs = load_jsonl(_pick("_trajectory"))
    fulls = {}
    for r in load_jsonl(_pick("_success")):
        fulls[r.get("task_id")] = r
    for r in load_jsonl(_pick("_failure")):
        fulls[r.get("task_id")] = r

    bins = defaultdict(list)
    cat_counts = Counter()

    for tr_rec in trajs:
        tid = tr_rec.get("task_id")
        full = fulls.get(tid)
        feat = trajectory_features(tr_rec, full)
        cat = categorize(tr_rec, feat)
        cat_counts[cat] += 1

        # Extract messages relevant to category's purpose
        if cat in ("A_clean_success", "A2_messy_success"):
            relevant_msgs = first_success_prefix(feat["msgs"], tr_rec.get("trajectory") or [])
        elif cat in ("C_stuck_loop", "C2_explored_long", "D2_dead_mid_run"):
            relevant_msgs = non_repeating_prefix(feat["msgs"], feat["tool_calls"])
        else:
            relevant_msgs = feat["msgs"]

        record = {
            "task_id": tid,
            "task": tr_rec.get("task"),
            "category": cat,
            "termination": tr_rec.get("termination"),
            "prediction": tr_rec.get("prediction"),
            "eval_reasoning": tr_rec.get("eval_reasoning"),
            "n_tool_calls": feat["n_tool_calls"],
            "n_repeats": feat["sig_dups"],
            "n_turns": feat["turns"],
            "n_msgs_full": feat["n_msgs"],
            "n_msgs_kept": len(relevant_msgs),
            "messages": relevant_msgs,
            "trajectory": tr_rec.get("trajectory") or [],
            "visited_urls": tr_rec.get("visited_urls", []),
        }
        bins[cat].append(record)

    file_map = {
        "A_clean_success":      "sft_positive_clean.jsonl",
        "A2_messy_success":     "sft_positive_messy.jsonl",
        "B_real_attempt_wrong": "rerank_negative_hard.jsonl",
        "B2_quick_wrong":       "rerank_negative_quick.jsonl",
        "C_stuck_loop":         "bfs_prefix.jsonl",
        "C2_explored_long":     "bfs_prefix.jsonl",
        "D2_dead_mid_run":      "bfs_prefix.jsonl",
        "D_dead_at_start":      "discarded.jsonl",
        "Z_unknown":            "discarded.jsonl",
    }
    files_open = {}
    for cat, fname in file_map.items():
        if fname not in files_open:
            files_open[fname] = open(os.path.join(out_dir, fname), "w", encoding="utf-8")
    for cat, recs in bins.items():
        f = files_open[file_map[cat]]
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    for f in files_open.values():
        f.close()

    manifest = {
        "run_id": run_id,
        "model_prefix": model_prefix,
        "source_files": [
            prefix + "_trajectory.jsonl",
            prefix + "_success.jsonl",
            prefix + "_failure.jsonl",
        ],
        "category_counts": dict(cat_counts),
        "files": {fname: file_map_inv(file_map, fname, cat_counts) for fname in set(file_map.values())},
    }
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"=== built dataset under {out_dir} ===")
    for fname in sorted(set(file_map.values())):
        path = os.path.join(out_dir, fname)
        n = sum(1 for _ in open(path)) if os.path.exists(path) else 0
        cats_in = [c for c, fn in file_map.items() if fn == fname]
        print(f"  {fname:32s} {n:3d} records  ({'+'.join(cats_in)})")
    print()
    print("category breakdown:")
    for c, n in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {c:30s} {n}")


def file_map_inv(file_map, fname, counts):
    cats = [c for c, fn in file_map.items() if fn == fname]
    return {"categories": cats, "count": sum(counts.get(c, 0) for c in cats)}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python scripts/build_dataset.py <run_id> [--truth]")
        sys.exit(1)
    use_truth = "--truth" in sys.argv[2:]
    build(sys.argv[1], use_truth=use_truth)
