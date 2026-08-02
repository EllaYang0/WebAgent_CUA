#!/usr/bin/env python3
"""Unified groundedness gate for WebAgent trajectories.

A trajectory is only an SFT candidate when ALL of these hold:

  1. answer_correct          — termination == 'answer' and a real prediction exists
  2. no_fatal_error          — no ClosedResourceError / TimeoutError / ECONNREFUSED / ...
  3. has_successful_tool_obs — at least one tool observation with no error marker
  4. answer_grounded_in_page — the answer string appears in a successful page Evidence
  5. refs_grounded           — every `ref` used in an action came from an earlier observation
  6. (strict, opt-in) no_tool_error — no Visit/Click/Fill error anywhere in the trajectory

Criterion 7 from the design ("clear browser state between tasks") is a property of
the collection loop, not of a single record; it lives in infer_async_nestbrowse.py.

Two entry points:

  * `evaluate(result, trajectory_record)` — importable, called at collection time so
    each rollout is gated the moment it is written.
  * CLI — replays the gate over finished runs:

        python scripts/groundedness.py --runs wiki_2hop_v3_scaled --report-only
        python scripts/groundedness.py --runs batch2 batch3 --out dataset/foo
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

GATE_VERSION = "g1"

# Errors that mean the environment died under the agent. Any occurrence poisons
# the whole trajectory — the model would be trained to imitate recovery from a
# broken harness.
FATAL_ERROR_TERMS = (
    "ClosedResourceError",
    "TimeoutError",
    "ECONNREFUSED",
    "RefreshError",
    "invalid_grant",
    "Ping task was cancelled",
    "ExceptionGroup",
    "server-side error",
)

# Per-action failures. Not fatal to the session, but they teach the model to
# flail. Rejected only under --strict; always counted.
TOOL_ERROR_TERMS = (
    "Visit error",
    "Fill error",
    "Click error",
    "Invalid arguments",
    "Invalid input",
    "could not find coordinates",
    "URL did NOT change",
    "[search] Error",
    "Error: Tool ",
)

EVIDENCE_MARKER = "Evidence in page:"

# Playwright snapshot refs appear as `[ref=f2e18]` in the raw Evidence block and
# get echoed as `ref=f2e18` in the LLM-written Summary. Both are legitimate
# provenance for a later action.
REF_IN_OBS_RE = re.compile(r"\[ref=([^\]\s]+)\]|(?<![\w\[])ref\s*[:=]\s*[\"']?([A-Za-z0-9_.-]+)")
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


# ---------------------------------------------------------------- text helpers

def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def has_any(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if term in (text or "")]


def refs_in_observation(text: str) -> set[str]:
    out = set()
    for bracketed, bare in REF_IN_OBS_RE.findall(text or ""):
        if bracketed:
            out.add(bracketed)
        elif bare:
            out.add(bare)
    return out


def parse_tool_calls(content: str) -> list[dict]:
    calls = []
    for raw in TOOL_CALL_RE.findall(content or ""):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            calls.append(obj)
    return calls


def observation_text(message: dict) -> str:
    return message.get("function_result") or message.get("content") or ""


# ------------------------------------------------------------------- the gate

def answer_variants(result: dict) -> list[str]:
    """Answer strings worth searching for in the evidence.

    `prediction` is what the model actually said; ground truth is included too
    because a gt match in the page also proves the page carried the answer (the
    model may have just phrased it differently).
    """
    variants = []
    for key in ("prediction", "answer", "gt_answer", "ground_truth"):
        value = result.get(key)
        if isinstance(value, str) and value.strip() and value.strip() != "[No Prediction]":
            variants.append(value.strip())
    values = result.get("valid_answers")
    if isinstance(values, list):
        variants.extend(str(v).strip() for v in values if str(v).strip())

    seen, out = set(), []
    for value in variants:
        norm = normalize_text(value)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(value)
    return out


def evaluate(result: dict, trajectory_record: dict | None = None,
             *, strict_tool_errors: bool = False, require_reasoning: bool = False) -> dict:
    """Gate one rollout. Returns a verdict dict; never raises on odd input."""
    messages = result.get("messages") or []
    task_id = result.get("task_id") or result.get("id") or ""

    # --- walk the conversation once, in order -------------------------------
    available_refs: set[str] = set()
    page_evidence: list[str] = []      # successful browser observations
    search_evidence: list[str] = []    # successful search observations
    fatal_terms: set[str] = set()
    tool_error_terms: set[str] = set()
    n_tool_calls = n_ref_actions = n_missing_ref = n_error_obs = 0
    n_assistant_turns = n_reasoned_turns = n_fabricated_obs = 0
    missing_ref_examples: list[str] = []
    first_action = ""
    first_action_missing_ref = False
    saw_error_before_answer = False
    answered_after_error = False

    for message in messages:
        role = message.get("role")

        if role == "assistant":
            content = message.get("content", "") or ""
            n_assistant_turns += 1
            # Reasoning is the point of teacher data. Gemini's own thoughts never
            # reach message.content (they are billed as Vertex reasoning_tokens and
            # dropped), so the prompt asks for an in-band <think> block; count how
            # often that actually happened.
            if "</think>" in content:
                n_reasoned_turns += 1
            if "<tool_response>" in content:
                # The model role-playing the system: fabricated observations.
                n_fabricated_obs += 1
            if "<answer>" in content and saw_error_before_answer:
                answered_after_error = True
            for call in parse_tool_calls(content):
                n_tool_calls += 1
                name = str(call.get("name", ""))
                if not first_action:
                    first_action = name
                args = call.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if not isinstance(args, dict):
                    continue
                ref = args.get("ref")
                if ref is None or str(ref).strip() == "":
                    continue
                n_ref_actions += 1
                ref = str(ref).strip()
                if ref not in available_refs:
                    n_missing_ref += 1
                    if n_tool_calls == 1:
                        first_action_missing_ref = True
                    if len(missing_ref_examples) < 3:
                        missing_ref_examples.append(ref)
            continue

        if role != "user":
            continue

        # --- observation -----------------------------------------------------
        text = observation_text(message)
        if not text:
            continue
        fatal_hits = has_any(text, FATAL_ERROR_TERMS)
        tool_hits = has_any(text, TOOL_ERROR_TERMS)
        fatal_terms.update(fatal_hits)
        tool_error_terms.update(tool_hits)
        if fatal_hits or tool_hits:
            n_error_obs += 1
            saw_error_before_answer = True
        else:
            if EVIDENCE_MARKER in text:
                page_evidence.append(text)
            elif message.get("tool_name") == "search" or text.lstrip().startswith("<tool_response>\n[search]"):
                search_evidence.append(text)

        # Refs only become usable AFTER the observation that exposed them.
        available_refs.update(refs_in_observation(text))

    # Trajectory file carries observation snippets for runs whose messages were
    # trimmed; fold in any extra evidence it has.
    for step in (trajectory_record or {}).get("trajectory") or []:
        snippet = step.get("observation_snippet") or ""
        hits = has_any(snippet, FATAL_ERROR_TERMS)
        fatal_terms.update(hits)
        if not hits and not has_any(snippet, TOOL_ERROR_TERMS) and EVIDENCE_MARKER in snippet:
            page_evidence.append(snippet)

    # --- grounding ----------------------------------------------------------
    variants = answer_variants(result)
    norm_page = [normalize_text(t) for t in page_evidence]
    norm_search = [normalize_text(t) for t in search_evidence]

    grounding = "none"
    matched_variant = None
    for value in variants:
        norm = normalize_text(value)
        if not norm:
            continue
        if any(norm in obs for obs in norm_page):
            grounding, matched_variant = "page", value
            break
        if grounding == "none" and any(norm in obs for obs in norm_search):
            grounding, matched_variant = "search_only", value

    # --- checks -------------------------------------------------------------
    prediction = (result.get("prediction") or "").strip()
    checks = {
        "answer_correct": result.get("termination") == "answer"
                          and bool(prediction) and prediction != "[No Prediction]",
        "no_fatal_error": not fatal_terms,
        "has_successful_tool_obs": bool(page_evidence or search_evidence),
        "answer_grounded_in_page": grounding == "page",
        "refs_grounded": n_missing_ref == 0,
        "no_tool_error": not tool_error_terms,
        # Not part of `clean`: reasoning coverage is reported so a run can be judged
        # on it, but gating on it would have silently rejected every trajectory ever
        # collected. Filter with --require-reasoning once collection reliably emits it.
        "fully_reasoned": n_assistant_turns > 0 and n_reasoned_turns == n_assistant_turns,
        "no_fabricated_obs": n_fabricated_obs == 0,
    }

    optional_checks = {"no_tool_error": strict_tool_errors,
                       "fully_reasoned": require_reasoning,
                       "no_fabricated_obs": True}
    reasons = [name for name, ok in checks.items()
               if not ok and optional_checks.get(name, True)]
    if not checks["answer_grounded_in_page"]:
        # Distinguish "read it off a search snippet" from "made it up" — the two
        # call for very different fixes.
        reasons = [r for r in reasons if r != "answer_grounded_in_page"]
        reasons.append("answer_grounded_search_only" if grounding == "search_only"
                       else "answer_not_in_any_observation")
    if first_action_missing_ref:
        reasons.append("first_action_ref_without_observation")

    return {
        "gate_version": GATE_VERSION,
        "task_id": task_id,
        "agent_model": result.get("agent_model", ""),
        "clean": not reasons,
        "strict": strict_tool_errors,
        "checks": checks,
        "reasons": reasons,
        "grounding": grounding,
        "matched_variant": matched_variant,
        "stats": {
            "termination": result.get("termination"),
            "n_assistant_turns": n_assistant_turns,
            "n_reasoned_turns": n_reasoned_turns,
            "n_fabricated_obs": n_fabricated_obs,
            "first_action": first_action,
            "n_tool_calls": n_tool_calls,
            "n_ref_actions": n_ref_actions,
            "n_missing_ref": n_missing_ref,
            "missing_ref_examples": missing_ref_examples,
            "n_page_evidence": len(page_evidence),
            "n_search_evidence": len(search_evidence),
            "n_error_obs": n_error_obs,
            "answered_after_error": answered_after_error,
            "fatal_terms": sorted(fatal_terms),
            "tool_error_terms": sorted(tool_error_terms),
        },
    }


def first_answer_prefix(messages: list[dict]) -> list[dict]:
    """Trim trailing turns after the model emitted its answer."""
    out = []
    for message in messages:
        out.append(message)
        if message.get("role") == "assistant" and "<answer>" in str(message.get("content", "")):
            break
    return out


def to_sft_record(result: dict, verdict: dict, source_run: str) -> dict:
    return {
        "task_id": verdict["task_id"],
        "source_batch": source_run,
        "agent_model": result.get("agent_model", ""),
        "answer_type": result.get("answer_type", ""),
        "task": result.get("task"),
        "answer": result.get("prediction"),
        "valid_answers": result.get("valid_answers", []),
        "gate_version": GATE_VERSION,
        "grounding": verdict["grounding"],
        "matched_variant": verdict["matched_variant"],
        "messages": first_answer_prefix(result.get("messages") or []),
    }


# ------------------------------------------------------------------------ CLI

def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def gate_run(root: Path, run_id: str, strict: bool, require_reasoning: bool = False) -> tuple[list[dict], list[dict], dict]:
    result_dir = root / "results" / run_id
    success_rows = read_jsonl(result_dir / "success.jsonl")
    failure_rows = read_jsonl(result_dir / "failure.jsonl")
    trajectory_rows = read_jsonl(result_dir / "trajectory.jsonl")
    trajectory_by_id = {row.get("task_id") or row.get("id"): row for row in trajectory_rows}

    accepted, verdicts = [], []
    for row in success_rows:
        verdict = evaluate(row, trajectory_by_id.get(row.get("task_id") or row.get("id")),
                           strict_tool_errors=strict, require_reasoning=require_reasoning)
        verdict["source_batch"] = run_id
        verdicts.append(verdict)
        if verdict["clean"]:
            accepted.append(to_sft_record(row, verdict, run_id))

    attempted = len(trajectory_rows) or (len(success_rows) + len(failure_rows))
    stats = {
        "run": run_id,
        "attempted_tasks": attempted,
        "raw_success": len(success_rows),
        "clean_success": len(accepted),
        "raw_success_rate": round(len(success_rows) / attempted, 4) if attempted else None,
        "clean_grounded_success_rate": round(len(accepted) / attempted, 4) if attempted else None,
        "clean_share_of_success": round(len(accepted) / len(success_rows), 4) if success_rows else None,
        "reject_reasons": dict(Counter(r for v in verdicts if not v["clean"] for r in v["reasons"])),
        "reasoned_turn_rate": (round(sum(v["stats"]["n_reasoned_turns"] for v in verdicts)
                                     / max(1, sum(v["stats"]["n_assistant_turns"] for v in verdicts)), 4)
                               if verdicts else None),
        "grounding_of_success": dict(Counter(v["grounding"] for v in verdicts)),
        "first_action_of_success": dict(Counter(v["stats"]["first_action"] or "(none)" for v in verdicts)),
    }
    return accepted, verdicts, stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--out", type=Path, help="Output dir for train/verdicts/summary")
    parser.add_argument("--strict", action="store_true",
                        help="Also reject any trajectory containing per-action tool errors")
    parser.add_argument("--require-reasoning", action="store_true",
                        help="Also reject trajectories where any assistant turn lacks a <think> block")
    parser.add_argument("--report-only", action="store_true", help="Print stats, write nothing")
    args = parser.parse_args()

    root = args.root.resolve()
    all_accepted, all_verdicts, per_run = [], [], []
    for run_id in args.runs:
        accepted, verdicts, stats = gate_run(root, run_id, args.strict, args.require_reasoning)
        all_accepted.extend(accepted)
        all_verdicts.extend(verdicts)
        per_run.append(stats)

    attempted = sum(s["attempted_tasks"] for s in per_run)
    raw_success = sum(s["raw_success"] for s in per_run)
    summary = {
        "gate_version": GATE_VERSION,
        "strict": args.strict,
        "runs": args.runs,
        "attempted_tasks": attempted,
        "raw_success": raw_success,
        "clean_success": len(all_accepted),
        "raw_success_rate": round(raw_success / attempted, 4) if attempted else None,
        "clean_grounded_success_rate": round(len(all_accepted) / attempted, 4) if attempted else None,
        "clean_share_of_success": round(len(all_accepted) / raw_success, 4) if raw_success else None,
        "reject_reasons": dict(Counter(r for v in all_verdicts if not v["clean"] for r in v["reasons"])),
        "grounding_of_success": dict(Counter(v["grounding"] for v in all_verdicts)),
        "per_run": per_run,
    }

    if not args.report_only:
        if not args.out:
            parser.error("--out is required unless --report-only")
        out = args.out if args.out.is_absolute() else root / args.out
        write_jsonl(out / "train.jsonl", all_accepted)
        write_jsonl(out / "verdicts.jsonl", all_verdicts)
        (out / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
