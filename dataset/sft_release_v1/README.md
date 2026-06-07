# SFT Release v1 — Wikipedia 2-hop Web Agent Trajectories

121 high-quality supervised-fine-tuning samples for training web agents on
multi-hop Wikipedia retrieval tasks. Each sample is a complete, successful
trajectory of an agent identifying an obscure person from clue sets and
extracting a specific attribute (advisor / birthplace / employer / etc.).

## Files

| File | Format | Use |
|---|---|---|
| `train.jsonl` | One record per line, includes `messages` chat history | SFT training input |
| `metadata.jsonl` | One record per line, per-task statistics | Analysis / filtering |
| `README.md` | This file | — |

## Sample counts

```
By answer type:
  doctoral_advisor:  73   (60%)
  birthplace_city:   32   (26%)
  employer:          15   (12%)
  educated_at:        1    (1%)

By source batch (incremental synthesis runs):
  wiki_2hop_fix12:   14   (initial multi-fix validation, advisor prompt)
  wiki_2hop_fix3_v2: 10   (advisor-first answer priority validation)
  wiki_2hop_batch2:  37   (first scaled batch with all fixes)
  wiki_2hop_batch3:  60   (second scaled batch, 13 categories)

Tool-call statistics on cleaned trajectories:
  min=3  max=12  mean=4.1  median=4
```

All trajectories satisfy:
- Agent answered correctly (LLM-judged semantic match against ground truth)
- Used at least 3 web tool calls (`visit` / `click` / `fill`)
- ≤ 2 repeated tool-call signatures (no thrash loops)

## Record schema (`train.jsonl`)

```json
{
  "task_id": "uuid",
  "source_batch": "wiki_2hop_batchN",
  "answer_type": "doctoral_advisor",
  "task": "Identify a person matching all of these clues: works as a mathematician; received the Salem Prize; ...",
  "answer": "Viktor Khavin",
  "valid_answers": ["Viktor Khavin", "V. P. Khavin"],
  "messages": [
    {"role": "system", "content": "You are a web navigation agent..."},
    {"role": "user", "content": "<the task above>"},
    {"role": "assistant", "content": "<tool_call>...</tool_call>"},
    {"role": "user", "content": "<tool_response>...</tool_response>"},
    ...
    {"role": "assistant", "content": "<answer>Viktor Khavin</answer>"}
  ]
}
```

`messages` is in ShareGPT / OpenAI chat format and is the primary training
target. Use a chat template that matches the agent's runtime format —
e.g. `<tool_call>{json}</tool_call>` tags are inlined in assistant content
rather than using separate `function_call` fields.

## Record schema (`metadata.jsonl`)

```json
{
  "task_id": "uuid",
  "source_batch": "wiki_2hop_batchN",
  "answer_type": "doctoral_advisor",
  "gt_answer": "Viktor Khavin",          // canonical Wikidata-first answer
  "valid_answers": [...],                 // all accepted answers (Wikidata P184 multi-valued)
  "agent_prediction": "Viktor Khavin",    // what the agent emitted
  "eval_reasoning": "Matched ...",
  "n_tool_calls": 4,
  "n_repeats": 0,
  "n_turns": 5,
  "n_msgs": 11,
  "source_urls": ["https://en.wikipedia.org/wiki/..."],
  "seed_clues": ["works as a mathematician", "received the Salem Prize", ...]
}
```

## How this was built

End-to-end pipeline in repo (commit-time):
1. **Synthesis** (`scripts/synthesis_agent.py`): walk Wikidata, generate
   clue-based questions where the answer is a specific entity (advisor,
   city, etc.). Three-stage filter:
   - Clue strength check (rejects ambiguous clue sets)
   - Country/region BAN filter (avoids "Russia"-style answers)
   - Dual-prompt LLM screener (drops questions Gemini can answer from memory)
2. **Solving** (`infer_async_nestbrowse.py` + hybrid agent in `toolkit/`):
   Gemini 3.1 Pro drives an Edge browser via Playwright MCP (DOM-first)
   with Windows-MCP visual fallback. Each task gets a trajectory.
3. **Bucketing** (`scripts/build_dataset.py`): trajectories are
   classified — only `A_clean_success` (≥3 tools, ≤2 repeats, answer
   accepted by LLM judge) is included here.

See repo CHANGELOG_session.md for the full fix history that led to this
release (verify-layer fixes, multi-advisor accept, clue shuffle,
advisor-first answer priority).

## Known limitations

- **Answer-type skew**: only 1 educated_at sample, no notable_work / award.
  Synthesis priority favors advisor → employer → educated_at → city in that
  order, and educated_at often falls off because Wikipedia rarely has rich
  enough clue sets for non-canonical alma maters.
- **Geographic skew**: ~70% from Russian / Soviet / Eastern European
  mathematicians; small representation of Asian, African, Latin American
  scientists. Synthesis category list at time of release: see
  CHANGELOG_session.md.
- **Question template uniformity**: all questions begin "Identify a person
  matching all of these clues..." — a model trained on this will not
  generalize to other phrasings without additional data.
- **Multi-valued answer handling**: `valid_answers` is populated for
  records from batch4+. Earlier batches (fix12, fix3_v2, batch2, batch3)
  have valid_answers == [canonical_answer] only.
