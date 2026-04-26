#!/usr/bin/env python3
"""Re-evaluate an existing benchmark run with the patched lenient JSON parser.

For each task in the run:
- If termination is `answer_incorrect` and the original `eval_reasoning` is the
  "JSON parse failed, fallback: ..." string, re-extract the LLM judge verdict
  from the embedded fallback text using lenient_json_extract. If the judge
  actually said "correct": true, reclassify as `answer`.

For visual fallback (verify_action) — the action already happened during the
original run, so re-evaluating won't change task outcomes. We only re-tally
visual pass/fail counts from the log file's `[verify] raw text:` lines.

Usage:
    python scripts/reeval.py <run_id>
where <run_id> is the suffix matching the *_results_<run_id>_{success,failure,trajectory}.jsonl files.

Example:
    python scripts/reeval.py browsecomp_first50

Outputs:
- writes <prefix>_success_reeval.jsonl, <prefix>_failure_reeval.jsonl
- prints corrected stats and visual-fallback re-evaluated stats
"""
import os
import sys
import json
import re
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from utils import lenient_json_extract  # noqa


def reeval(run_id, model_prefix="google_gemini-3.1-pro-preview"):
    res_dir = os.path.join(ROOT, "results")
    log_dir = os.path.join(ROOT, "logs")
    prefix = os.path.join(res_dir, f"{model_prefix}_results_{run_id}")

    succ_in = prefix + "_success.jsonl"
    fail_in = prefix + "_failure.jsonl"
    traj_in = prefix + "_trajectory.jsonl"
    succ_out = prefix + "_success_reeval.jsonl"
    fail_out = prefix + "_failure_reeval.jsonl"

    if not os.path.exists(traj_in):
        print(f"[!] no trajectory file found at {traj_in}")
        return

    # ---- 1. reclassify ----
    succ_records = []
    fail_records = []
    moved_to_succ = 0
    judge_said_false = 0
    unparseable = 0
    non_eval_failures = Counter()

    if os.path.exists(succ_in):
        for line in open(succ_in):
            line = line.strip()
            if line:
                succ_records.append(json.loads(line))

    if os.path.exists(fail_in):
        for line in open(fail_in):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            t = r.get("termination")
            er = r.get("eval_reasoning") or ""
            # Try to recover via lenient parser. The original recorded the LLM
            # judge response in eval_reasoning when its parser failed:
            #   "JSON parse failed, fallback: <raw response>"
            # Newer code records "JSON unrecoverable, raw: ...". Both contain
            # the raw judge text after the colon.
            if t == "answer_incorrect" and (
                er.startswith("JSON parse failed, fallback:")
                or er.startswith("JSON unrecoverable, raw:")
            ):
                # Strip the prefix
                raw_judge = er.split(":", 1)[1].strip() if ":" in er else er
                verdict = lenient_json_extract(raw_judge)
                if verdict and "correct" in verdict:
                    if verdict["correct"]:
                        # Reclassify as success
                        r["termination"] = "answer"
                        r["eval_reasoning"] = (
                            verdict.get("reasoning")
                            or verdict.get("reason")
                            or "[reeval] judge said correct=true"
                        )
                        succ_records.append(r)
                        moved_to_succ += 1
                        continue
                    else:
                        judge_said_false += 1
                else:
                    unparseable += 1
            else:
                if t and t != "answer_incorrect":
                    non_eval_failures[t] += 1
            fail_records.append(r)

    # Write out reclassified files
    with open(succ_out, "w") as f:
        for r in succ_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(fail_out, "w") as f:
        for r in fail_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---- 2. summary ----
    total = sum(1 for _ in open(traj_in))
    real_success = len(succ_records)
    print("=" * 70)
    print(f"RE-EVALUATION: {run_id}")
    print("=" * 70)
    print(f"  total tasks recorded:           {total}")
    print(f"  official success (before):      {len(succ_records) - moved_to_succ}")
    print(f"  reclassified as success:        +{moved_to_succ}")
    print(f"  ----")
    print(f"  REAL SUCCESS (after reeval):    {real_success} ({100*real_success/total:.1f}%)")
    print(f"  judge-said-false (truly wrong): {judge_said_false}")
    print(f"  unparseable judge response:     {unparseable}")
    print()
    print(f"  Non-eval failures (still failed):")
    for term, cnt in sorted(non_eval_failures.items(), key=lambda x: -x[1]):
        print(f"    {term}: {cnt}")

    # ---- 3. visual fallback re-evaluation from log ----
    print()
    print("=" * 70)
    print("VISUAL FALLBACK RE-EVALUATION (from log)")
    print("=" * 70)
    log_candidates = [
        f for f in os.listdir(log_dir)
        if f.startswith(f"run_{run_id}_") and f.endswith(".log")
    ]
    if not log_candidates:
        print(f"  [!] no log file found for run_id={run_id}")
        return
    log_path = os.path.join(log_dir, sorted(log_candidates)[-1])
    print(f"  log: {log_path}")
    raw_blocks = []
    with open(log_path) as f:
        cur = None
        for line in f:
            if line.startswith("[verify] raw text:"):
                if cur:
                    raw_blocks.append("\n".join(cur))
                cur = [line[len("[verify] raw text:"):].rstrip()]
            elif cur is not None:
                # raw text may continue across multiple lines until the next
                # log marker. Use a simple heuristic: stop on lines that look
                # like a new `[xxx] ...` marker.
                if re.match(r'^\[(verify|find_coordinates|visual_|click|fill|visit|agentic_loop|tool_explore|click)\]', line):
                    raw_blocks.append("\n".join(cur))
                    cur = None
                else:
                    cur.append(line.rstrip())
        if cur:
            raw_blocks.append("\n".join(cur))

    pass_old = pass_new = fail_old = fail_new = unparseable_v = 0
    parse_failed_recovered = 0
    for raw in raw_blocks:
        v = lenient_json_extract(raw)
        if v is None:
            unparseable_v += 1
            fail_new += 1  # unparseable still counted as fail
            continue
        # Old parser would have returned False on these too if parse failed
        s = bool(v.get("success", v.get("correct", False)))
        if s:
            pass_new += 1
            # Heuristic: was old parser likely to have failed? Check for fence/prose.
            if raw.lstrip().startswith("```") or raw.strip().startswith("Sure") or "```" in raw:
                parse_failed_recovered += 1
        else:
            fail_new += 1

    print(f"  raw verify blocks parsed:          {len(raw_blocks)}")
    print(f"  unparseable (even with lenient):   {unparseable_v}")
    print(f"  PASS (after reeval):               {pass_new}")
    print(f"  FAIL (after reeval):               {fail_new}")
    if pass_new + fail_new > 0:
        print(f"  pass rate:                         {100*pass_new/(pass_new+fail_new):.1f}%")
    print(f"  recovered-from-parse-fail (likely): {parse_failed_recovered}")

    print()
    print(f"Wrote: {succ_out}")
    print(f"Wrote: {fail_out}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python scripts/reeval.py <run_id>")
        print("       e.g.: python scripts/reeval.py browsecomp_first50")
        sys.exit(1)
    reeval(sys.argv[1])
