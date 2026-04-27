#!/usr/bin/env python3
"""Re-judge existing benchmark predictions against the dataset's ground-truth
answers. Original judge prompt did NOT include ground-truth, so it produced
many false positives (mark "AC Milan and Club Olimpia" correct when truth was
"Ireland v Romania", etc.). This script does NOT rerun the agent — just
re-evaluates the saved predictions with a ground-truth-aware judge.

Reads (per-benchmark layout):
  data/<bench>.jsonl                       — task definitions w/ answer
  results/<bench>/success.jsonl            — currently-marked successes
  results/<bench>/failure.jsonl            — currently-marked failures
  results/<bench>/trajectory.jsonl         — termination overview

Writes:
  results/<bench>/success_truth.jsonl
  results/<bench>/failure_truth.jsonl
  results/<bench>/trajectory_truth.jsonl
  results/<bench>/reeval_truth_audit.csv

Usage:
    python scripts/reeval_with_truth.py browsecomp_first50
"""
import os
import sys
import json
import asyncio
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils import call_llm, lenient_json_extract  # noqa
from infer_async_nestbrowse import evaluate_answer_with_llm  # noqa


async def reeval(run_id, model_prefix="google_gemini-3.1-pro-preview"):
    data_path = ROOT / "data" / f"{run_id}.jsonl"
    if not data_path.exists():
        print(f"[!] no data file at {data_path}; cannot get ground truth")
        return
    # Build {task_id_or_id: ground_truth} map
    truths = {}
    tasks = {}
    for line in open(data_path):
        d = json.loads(line)
        tid = d.get("task_id", d.get("id"))
        truths[tid] = d.get("answer") or d.get("ground_truth") or ""
        tasks[tid] = d.get("task") or d.get("question") or ""

    res_dir = ROOT / "results" / run_id
    res_dir.mkdir(parents=True, exist_ok=True)
    succ_in = list(open(str(res_dir / "success.jsonl"))) if (res_dir / "success.jsonl").exists() else []
    fail_in = list(open(str(res_dir / "failure.jsonl"))) if (res_dir / "failure.jsonl").exists() else []
    traj_in = list(open(str(res_dir / "trajectory.jsonl"))) if (res_dir / "trajectory.jsonl").exists() else []

    succ_recs = [json.loads(l) for l in succ_in]
    fail_recs = [json.loads(l) for l in fail_in]
    traj_recs = [json.loads(l) for l in traj_in]

    all_recs = succ_recs + fail_recs

    # Skip records that the agent never produced a real answer for: they aren't
    # eligible for ground-truth judging.
    eligible = []
    skipped = []
    for r in all_recs:
        pred = (r.get("prediction") or "").strip()
        term = r.get("termination")
        if term in ("llm_response_error", "max_turn_exceeded", "server_side_error"):
            skipped.append(r)
            continue
        if not pred or pred == "[No Prediction]":
            skipped.append(r)
            continue
        eligible.append(r)

    print(f"[reeval] {len(eligible)} records eligible for ground-truth re-judging,"
          f" {len(skipped)} skipped (no answer / non-eval termination)")

    # Set up env vars and a fake sem dict (call_llm needs sem['llm']).
    import asyncio as _aio
    sem = {"llm": _aio.Semaphore(int(os.getenv("REEVAL_PARALLEL", "4")))}

    audit_rows = []
    new_term = {}  # task_id -> new termination
    new_reason = {}

    async def judge_one(r):
        tid = r.get("task_id")
        gt = truths.get(tid, "")
        task_text = tasks.get(tid, r.get("task", ""))
        pred = r.get("prediction") or ""
        if not gt:
            new_term[tid] = r.get("termination")
            new_reason[tid] = "[reeval] no ground-truth in data file"
            return
        is_correct, reasoning = await evaluate_answer_with_llm(
            sem, task_text, pred, ground_truth=gt
        )
        new_term[tid] = "answer" if is_correct else "answer_incorrect"
        new_reason[tid] = reasoning
        audit_rows.append({
            "task_id": tid,
            "task": task_text[:120],
            "prediction": pred[:120],
            "ground_truth": gt[:120],
            "old_termination": r.get("termination"),
            "old_reasoning": (r.get("eval_reasoning") or "")[:120],
            "new_termination": new_term[tid],
            "new_reasoning": reasoning[:200],
        })

    await _aio.gather(*[judge_one(r) for r in eligible])

    # Apply new verdicts and split records into corrected success/failure
    new_succ, new_fail, new_traj = [], [], []
    for r in all_recs:
        tid = r.get("task_id")
        if tid in new_term:
            r2 = dict(r)
            r2["termination"] = new_term[tid]
            r2["eval_reasoning"] = new_reason.get(tid) or r.get("eval_reasoning")
        else:
            r2 = r
        if r2.get("termination") == "answer":
            new_succ.append(r2)
        else:
            new_fail.append(r2)

    for r in traj_recs:
        tid = r.get("task_id")
        r2 = dict(r)
        if tid in new_term:
            r2["termination"] = new_term[tid]
            r2["eval_reasoning"] = new_reason.get(tid) or r.get("eval_reasoning")
        new_traj.append(r2)

    # Write outputs into the same per-benchmark directory.
    with open(str(res_dir / "success_truth.jsonl"), "w") as f:
        for r in new_succ:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(str(res_dir / "failure_truth.jsonl"), "w") as f:
        for r in new_fail:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(str(res_dir / "trajectory_truth.jsonl"), "w") as f:
        for r in new_traj:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(str(res_dir / "reeval_truth_audit.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "task_id", "task", "prediction", "ground_truth",
            "old_termination", "old_reasoning",
            "new_termination", "new_reasoning",
        ])
        w.writeheader()
        for row in audit_rows:
            w.writerow(row)

    # Summary
    n_total = len(all_recs)
    old_succ_n = len(succ_recs)
    new_succ_n = len(new_succ)
    flipped_to_fail = sum(
        1 for r in audit_rows
        if r["old_termination"] == "answer" and r["new_termination"] == "answer_incorrect"
    )
    flipped_to_succ = sum(
        1 for r in audit_rows
        if r["old_termination"] == "answer_incorrect" and r["new_termination"] == "answer"
    )
    print()
    print("=" * 78)
    print(f"GROUND-TRUTH RE-EVALUATION: {run_id}")
    print("=" * 78)
    print(f"  total records:                       {n_total}")
    print(f"  old success (without ground truth):  {old_succ_n}  ({100*old_succ_n/n_total:.1f}%)")
    print(f"  new success (with ground truth):     {new_succ_n}  ({100*new_succ_n/n_total:.1f}%)")
    print(f"  flipped success -> incorrect:        {flipped_to_fail}")
    print(f"  flipped incorrect -> success:        {flipped_to_succ}")
    print(f"  records skipped (no pred/non-eval):  {len(skipped)}")
    print()
    print(f"  audit CSV:    {res_dir / 'reeval_truth_audit.csv'}")
    print(f"  new success:  {res_dir / 'success_truth.jsonl'}")
    print(f"  new failure:  {res_dir / 'failure_truth.jsonl'}")
    print(f"  new traj:     {res_dir / 'trajectory_truth.jsonl'}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python scripts/reeval_with_truth.py <run_id>")
        sys.exit(1)
    asyncio.run(reeval(sys.argv[1]))
