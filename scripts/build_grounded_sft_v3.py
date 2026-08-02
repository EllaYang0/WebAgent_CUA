#!/usr/bin/env python3
"""Build a grounded SFT probe set from raw WebAgent benchmark runs.

This is deliberately stricter than scripts/build_dataset.py. A final answer is
not enough: the answer must be grounded in a successful tool observation.
"""

import argparse
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path


FATAL_ERROR_TERMS = (
    "ClosedResourceError",
    "TimeoutError",
    "ECONNREFUSED",
    "RefreshError",
    "invalid_grant",
)

TOOL_ERROR_TERMS = (
    "Visit error",
    "Fill error",
    "Click error",
    "Invalid arguments",
    "Invalid input",
    "could not find coordinates",
    "URL did NOT change",
)


def read_jsonl(path: Path):
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def answer_variants(record: dict) -> list[str]:
    variants = []
    for key in ("prediction", "answer", "gt_answer"):
        value = record.get(key)
        if isinstance(value, str) and value.strip() and value.strip() != "[No Prediction]":
            variants.append(value.strip())
    for key in ("valid_answers",):
        values = record.get(key)
        if isinstance(values, list):
            variants.extend(str(v).strip() for v in values if str(v).strip())
    seen = set()
    out = []
    for value in variants:
        norm = normalize_text(value)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(value)
    return out


def has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in (text or "") for term in terms)


def successful_observations(record: dict, trajectory_record: dict | None) -> list[str]:
    observations = []

    for message in record.get("messages") or []:
        if message.get("role") != "user":
            continue
        content = message.get("function_result") or message.get("content") or ""
        if "Evidence in page:" not in content:
            continue
        if has_any(content, FATAL_ERROR_TERMS + TOOL_ERROR_TERMS):
            continue
        observations.append(content)

    for step in (trajectory_record or {}).get("trajectory") or []:
        obs = step.get("observation_snippet") or ""
        if "Evidence in page:" not in obs:
            continue
        if has_any(obs, FATAL_ERROR_TERMS + TOOL_ERROR_TERMS):
            continue
        observations.append(obs)

    return observations


def is_answer_grounded(record: dict, trajectory_record: dict | None) -> tuple[bool, str]:
    variants = answer_variants(record)
    if not variants:
        return False, "no_answer_variant"

    observations = successful_observations(record, trajectory_record)
    if not observations:
        return False, "no_successful_evidence_observation"

    normalized_observations = [normalize_text(obs) for obs in observations]
    for answer in variants:
        norm_answer = normalize_text(answer)
        if not norm_answer:
            continue
        if any(norm_answer in norm_obs for norm_obs in normalized_observations):
            return True, f"answer_span_found:{answer}"

    return False, "answer_not_in_evidence"


def first_success_prefix(messages: list[dict]) -> list[dict]:
    out = []
    for message in messages:
        out.append(message)
        if message.get("role") == "assistant" and "<answer>" in str(message.get("content", "")):
            break
    return out


def record_text(record: dict, trajectory_record: dict | None) -> str:
    return json.dumps(record, ensure_ascii=False) + "\n" + json.dumps(trajectory_record or {}, ensure_ascii=False)


def build_for_run(root: Path, run_id: str, reject_tool_errors: bool) -> tuple[list[dict], list[dict]]:
    result_dir = root / "results" / run_id
    success_rows = read_jsonl(result_dir / "success.jsonl")
    trajectory_by_id = {
        row.get("task_id") or row.get("id"): row
        for row in read_jsonl(result_dir / "trajectory.jsonl")
    }

    accepted = []
    rejected = []
    for row in success_rows:
        task_id = row.get("task_id") or row.get("id")
        tr = trajectory_by_id.get(task_id)
        text = record_text(row, tr)

        reasons = []
        if has_any(text, FATAL_ERROR_TERMS):
            reasons.append("fatal_mcp_error")
        if reject_tool_errors and has_any(text, TOOL_ERROR_TERMS):
            reasons.append("tool_error")

        grounded, grounded_reason = is_answer_grounded(row, tr)
        if not grounded:
            reasons.append(grounded_reason)

        if reasons:
            rejected.append({
                "task_id": task_id,
                "source_batch": run_id,
                "prediction": row.get("prediction"),
                "reasons": reasons,
            })
            continue

        accepted.append({
            "task_id": task_id,
            "source_batch": run_id,
            "answer_type": row.get("answer_type", ""),
            "task": row.get("task"),
            "answer": row.get("prediction"),
            "valid_answers": row.get("valid_answers", []),
            "grounded_reason": grounded_reason,
            "messages": first_success_prefix(row.get("messages") or []),
        })

    return accepted, rejected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--reject-tool-errors",
        action="store_true",
        help="Reject any record containing Visit/Fill/Click/Invalid tool errors.",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    output_dir = args.output_dir
    if not output_dir.is_absolute():
        output_dir = root / output_dir

    all_accepted = []
    all_rejected = []
    for run_id in args.runs:
        accepted, rejected = build_for_run(root, run_id, args.reject_tool_errors)
        all_accepted.extend(accepted)
        all_rejected.extend(rejected)

    write_jsonl(output_dir / "train.jsonl", all_accepted)
    write_jsonl(output_dir / "rejected.jsonl", all_rejected)

    summary = {
        "runs": args.runs,
        "reject_tool_errors": args.reject_tool_errors,
        "accepted": len(all_accepted),
        "rejected": len(all_rejected),
        "accepted_by_source": dict(Counter(row["source_batch"] for row in all_accepted)),
        "rejected_reasons": dict(Counter(reason for row in all_rejected for reason in row["reasons"])),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
