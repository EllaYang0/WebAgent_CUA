#!/usr/bin/env python3
"""Audit WebAgent SFT records for trajectory-to-SFT alignment problems.

The script is intentionally conservative: it does not modify data. It reads a
JSONL SFT file, scores each record, and writes a CSV plus a JSON summary.
"""

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


MCP_ERROR_TERMS = (
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

REF_RE = re.compile(r"\[ref=([^\]]+)\]")
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            if line.strip():
                yield line_no, json.loads(line)


def refs_from_observation(text: str) -> set[str]:
    return set(REF_RE.findall(text or ""))


def parse_tool_call(text: str):
    match = TOOL_CALL_RE.search(text or "")
    if not match:
        return None, None
    try:
        obj = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        return None, f"json_parse_error:{exc.msg}"
    return obj, None


def classify_record(record: dict) -> dict:
    messages = record.get("messages") or []
    available_refs: set[str] = set()
    first_action = ""
    n_tool_calls = 0
    n_ref_actions = 0
    n_missing_ref_actions = 0
    n_tool_error_responses = 0
    has_mcp_error = False
    has_tool_error = False
    has_parse_error = False
    answer_after_error = False
    seen_any_error = False
    missing_ref_examples = []
    error_terms_seen = set()

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "assistant":
            if "<answer>" in content and seen_any_error:
                answer_after_error = True

            tool_call, parse_error = parse_tool_call(content)
            if parse_error:
                has_parse_error = True
                continue
            if not tool_call:
                continue

            n_tool_calls += 1
            name = tool_call.get("name", "")
            args = tool_call.get("arguments") or {}
            if not first_action:
                first_action = name

            ref = args.get("ref") if isinstance(args, dict) else None
            if ref is not None:
                n_ref_actions += 1
                ref = str(ref)
                if ref not in available_refs:
                    n_missing_ref_actions += 1
                    if len(missing_ref_examples) < 3:
                        missing_ref_examples.append(ref)

        else:
            for term in MCP_ERROR_TERMS:
                if term in content:
                    has_mcp_error = True
                    seen_any_error = True
                    error_terms_seen.add(term)
            for term in TOOL_ERROR_TERMS:
                if term in content:
                    has_tool_error = True
                    seen_any_error = True
                    error_terms_seen.add(term)
            if any(term in content for term in MCP_ERROR_TERMS + TOOL_ERROR_TERMS):
                n_tool_error_responses += 1
            available_refs.update(refs_from_observation(content))

    reasons = []
    if has_mcp_error:
        reasons.append("mcp_error")
    if has_parse_error:
        reasons.append("tool_call_parse_error")
    if first_action in {"fill", "click", "select"} and n_missing_ref_actions:
        reasons.append("starts_with_ref_action_without_observation")
    if n_missing_ref_actions:
        reasons.append("missing_ref")
    if answer_after_error:
        reasons.append("answer_after_error")
    if has_tool_error:
        reasons.append("tool_error_response")

    if has_mcp_error or has_parse_error:
        label = "bad"
    elif n_missing_ref_actions:
        label = "bad"
    elif has_tool_error or answer_after_error:
        label = "maybe_salvage"
    else:
        label = "clean"

    return {
        "task_id": record.get("task_id") or record.get("id", ""),
        "source_batch": record.get("source_batch", ""),
        "answer_type": record.get("answer_type", ""),
        "first_action": first_action,
        "n_messages": len(messages),
        "n_tool_calls": n_tool_calls,
        "n_ref_actions": n_ref_actions,
        "n_missing_ref_actions": n_missing_ref_actions,
        "missing_ref_examples": ";".join(missing_ref_examples),
        "n_tool_error_responses": n_tool_error_responses,
        "has_mcp_error": has_mcp_error,
        "has_tool_error": has_tool_error,
        "answer_after_error": answer_after_error,
        "label": label,
        "reason": ";".join(reasons) if reasons else "ok",
        "error_terms": ";".join(sorted(error_terms_seen)),
    }


def write_outputs(rows: list[dict], csv_path: Path, summary_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "task_id",
        "source_batch",
        "answer_type",
        "first_action",
        "n_messages",
        "n_tool_calls",
        "n_ref_actions",
        "n_missing_ref_actions",
        "missing_ref_examples",
        "n_tool_error_responses",
        "has_mcp_error",
        "has_tool_error",
        "answer_after_error",
        "label",
        "reason",
        "error_terms",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "total_records": len(rows),
        "labels": dict(Counter(row["label"] for row in rows)),
        "first_actions": dict(Counter(row["first_action"] for row in rows)),
        "by_source_batch": {},
        "totals": {
            "n_tool_calls": sum(int(row["n_tool_calls"]) for row in rows),
            "n_ref_actions": sum(int(row["n_ref_actions"]) for row in rows),
            "n_missing_ref_actions": sum(int(row["n_missing_ref_actions"]) for row in rows),
            "records_with_mcp_error": sum(row["has_mcp_error"] for row in rows),
            "records_with_tool_error": sum(row["has_tool_error"] for row in rows),
            "records_with_answer_after_error": sum(row["answer_after_error"] for row in rows),
        },
    }

    grouped = defaultdict(list)
    for row in rows:
        grouped[row["source_batch"]].append(row)
    for source, source_rows in grouped.items():
        summary["by_source_batch"][source] = {
            "total_records": len(source_rows),
            "labels": dict(Counter(row["label"] for row in source_rows)),
            "first_actions": dict(Counter(row["first_action"] for row in source_rows)),
            "n_ref_actions": sum(int(row["n_ref_actions"]) for row in source_rows),
            "n_missing_ref_actions": sum(int(row["n_missing_ref_actions"]) for row in source_rows),
            "records_with_mcp_error": sum(row["has_mcp_error"] for row in source_rows),
        }

    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Input SFT JSONL path")
    parser.add_argument("--csv", required=True, type=Path, help="Output per-record CSV path")
    parser.add_argument("--summary", required=True, type=Path, help="Output summary JSON path")
    args = parser.parse_args()

    rows = [classify_record(record) for _, record in read_jsonl(args.input)]
    write_outputs(rows, args.csv, args.summary)
    print(f"Wrote {len(rows)} audit rows to {args.csv}")
    print(f"Wrote summary to {args.summary}")


if __name__ == "__main__":
    main()
