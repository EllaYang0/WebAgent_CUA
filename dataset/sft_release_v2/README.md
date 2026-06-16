# SFT Release v2 — Wikipedia 2-hop Web Agent Trajectories

225 supervised-fine-tuning samples. This release extends `sft_release_v1` (121 records) with 104 selected `wiki_2hop_batch4` records.

Batch4 inclusion uses all clean successes plus a conservative filtered subset of messy successes:
- clean successes: 91
- filtered messy successes: 13

Filtered messy rule: `n_tool_calls >= 3`, `n_repeats <= 5`, `n_turns <= 15`.

## Files

- `train.jsonl`: SFT training records, same schema as v1.
- `metadata.jsonl`: metadata and quality stats.
- `quality_report.json`: merge counts and validation checks.
- `batch4_messy_filter_report.json`: filter rule and rejection counts.

## Source Counts

```json
{
  "wiki_2hop_fix12": 14,
  "wiki_2hop_fix3_v2": 10,
  "wiki_2hop_batch2": 37,
  "wiki_2hop_batch3": 60,
  "wiki_2hop_batch4": 104
}
```

## Answer Type Counts

```json
{
  "doctoral_advisor": 140,
  "birthplace_city": 52,
  "employer": 29,
  "educated_at": 4
}
```
