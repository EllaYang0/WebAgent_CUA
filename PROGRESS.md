# WebAgent_CUA — Progress & File Map

Last updated against commit `5e6fb3c` (branch `main`).
Repo: https://github.com/EllaYang0/WebAgent_CUA

## Goal

Use a strong teacher model (Gemini 3.1 Pro Preview, via Vertex AI) to generate
**high-quality, reasoning-bearing web-agent trajectories** on multi-hop
(`n_hops=2`) Wikipedia question-answering tasks, then distil them into a Qwen
student via SFT and show the distillation is effective.

The agent runs on the **NestBrowse two-layer architecture**: an outer decision
agent that issues tool calls (`search` / `visit` / `click` / `fill`), and an inner
page-summarizer LLM that turns raw page snapshots into an `Evidence in page` +
`Summary` block the outer agent reads.

## Current status

- **Collection pipeline fixed and reasoning capture solved** (see below).
- **101 clean, reasoning-bearing trajectories** collected so far into
  `results/wiki_2hop_n200_reasoned/clean.jsonl` (birthplace-city dominated, plus
  some doctoral-advisor / employer), gated by `scripts/groundedness.py`.
- Collection of the `wiki_2hop_v3_n200` pool is ongoing, run in small
  fresh-session batches to work around an MCP session-collapse issue.

## What was accomplished this cycle

1. **Fixed the research-task prompt.** `SYSTEM_PROMPT_NAVI` (built for the
   flight-search `navi_bench`) assumed the agent already sat on a start page and
   *banned* `search`. Wiki tasks have no `start_url`, so that prompt left the agent
   with no legal opening move — it invented element refs and hand-built URLs. Added
   `SYSTEM_PROMPT_RESEARCH` (search-first, ref-provenance rules) selected when a
   task has no `start_url`. Clean-trajectory yield went from ~3% (historical) to
   ~90% on healthy runs.
2. **Captured the teacher's reasoning.** Gemini's chain-of-thought is billed as
   hidden `reasoning_tokens` and never reaches `message.content`, so every prior
   teacher run had **no reasoning at all**. Enabling Vertex `include_thoughts`
   surfaces the model's actual thought summary as a visible `<think>` block before
   each tool call (~90% turn coverage). This matches the Qwen student's own
   `</think>` format.
3. **Built a groundedness gate.** A trajectory is SFT-usable only if its answer is
   grounded in a real page observation, its refs have provenance, it has no fatal
   MCP error, and (reported) it carries reasoning. Runs inline during collection
   and offline as a CLI.
4. **Hardened collection**: third-person question reframing (keeps reasoning in the
   agent's voice), per-task browser-tab reset, self-fabricated `<tool_response>`
   truncation, and a producing-model stamp on every record.

## File map

### Agent / collection core
- [infer_async_nestbrowse.py](infer_async_nestbrowse.py) — main collection loop.
  Runs the outer agent turn loop, calls tools, resets browser state between tasks
  (`reset_browser_state`), selects `SYSTEM_PROMPT_NAVI` vs `SYSTEM_PROMPT_RESEARCH`
  by `start_url`, reframes research questions to third person, gates each finished
  rollout inline (writing `clean.jsonl` + `gate.jsonl`), stamps `agent_model`, and
  honors `BENCHMARK_NAME` / `RUN_ID` / `EVAL_LIMIT` env overrides.
- [prompts.py](prompts.py) — all prompts. `SYSTEM_PROMPT_NAVI` (navi_bench, has a
  start page, bans search), `SYSTEM_PROMPT_RESEARCH` (this cycle; search-first,
  ref-provenance, `<think>`/`<tool_call>` convention, agent-voice framing), and the
  page-summarizer prompts (`SYSTEM_PROMPT_SUMMARY_OURS`, `SUMMARY_PROMPT`).
- [utils.py](utils.py) — `call_llm` wrapper over the Vertex OpenAI-compatible
  endpoint. This cycle: enables `include_thoughts` for agent-mode calls
  (`AGENT_INCLUDE_THOUGHTS`, default on; `AGENT_THINKING_BUDGET` optional) so the
  teacher emits visible reasoning; keeps `safety_settings`.
- [toolkit/browser_hybrid.py](toolkit/browser_hybrid.py) — hybrid DOM+visual
  executor: the `Visit` / `Click` / `Fill` tool classes, plus vision helpers
  (`find_coordinates`, `verify_action`, `check_dom_focus`). Prior-work file; this
  cycle only switches the vision model to `VISION_MODEL_NAME` (falling back to
  `SUMMARY_MODEL_NAME`) and refactors the DOM-evidence prompt injection.
- [toolkit/tool_explore.py](toolkit/tool_explore.py) — the inner summarizer:
  `process_response` shards a raw page and calls the summary LLM to produce the
  `Evidence in page` + `Summary` block.
- [toolkit/tool_search.py](toolkit/tool_search.py) — the `search` tool (Brave
  Search API).
- [toolkit/mcp_client.py](toolkit/mcp_client.py) — the shared Playwright-MCP SSE
  client used for browser control.

### Data quality / SFT tooling
- [scripts/groundedness.py](scripts/groundedness.py) — **the unified groundedness
  gate** (this cycle). `evaluate()` is imported by the collection loop; the CLI
  (`--report-only` / `--out`) replays it over finished runs and reports
  `clean_grounded_success_rate` and `reasoned_turn_rate`. Supersedes the two scripts
  below.
- [scripts/build_grounded_sft_v3.py](scripts/build_grounded_sft_v3.py) — older
  grounded-SFT builder (answer-in-evidence only); kept for back-compat.
- [scripts/audit_sft_trajectories.py](scripts/audit_sft_trajectories.py) — audits a
  finished SFT file for ref-provenance and MCP-error problems, emits a CSV + summary.
- [scripts/synthesis_agent.py](scripts/synthesis_agent.py) — generates the wiki
  2-hop questions from Wikidata (clue selection, obscurity/answer filters). Prior
  work.

### Launchers
- [scripts/run_probe30.sh](scripts/run_probe30.sh) — launches a collection run;
  self-sources `.env.eval` and exports credentials (so it survives tmux's stale
  server env); writes its logfile path to `logs/<RUN_ID>.current`.
- [scripts/watchdog_collect.sh](scripts/watchdog_collect.sh) — a resume supervisor
  that relaunches a run only on abnormal death. NOTE: prone to respawn races if
  launched multiple times; the current workflow drives fixed-size batches manually
  instead.

### Data & outputs
- [data/recollect_grounded_probe_30.jsonl](data/recollect_grounded_probe_30.jsonl) —
  30 never-before-attempted probe tasks used to validate the fixed pipeline.
- [data/wiki_2hop_v3_n200.jsonl](data/wiki_2hop_v3_n200.jsonl) — the 200-question
  collection pool (all `n_hops=2`).
- `results/<RUN_ID>/` — per-run outputs: `success/failure/trajectory.jsonl`, plus
  `clean.jsonl` (gate-passed SFT candidates) and `gate.jsonl` (per-record verdicts).
  LFS-tracked. The active run is `results/wiki_2hop_n200_reasoned/`.
- `dataset/sft_release_v1|v2/` — previous SFT releases (121 / 225 records) built
  from reasoning-less trajectories; kept as a baseline for comparison.
- [training/qwen_sft/](training/qwen_sft/) — Qwen SFT training: `convert_to_sharegpt.py`
  (JSON messages → LLaMA-Factory ShareGPT), `qwen9b_lora_full.yaml` (Qwen3.5-9B LoRA,
  rank 16, 3 epochs, lr 1e-4, template `qwen`), `run_train.sh`,
  `openai_compat_transformers_server.py` (serves the LoRA for eval).

## Known issues / next steps

- **MCP session collapse.** The single shared Playwright-MCP SSE session degrades on
  long runs (`ClosedResourceError`), after which browser tool calls fail and the run
  produces search-only garbage. Worked around by collecting in fresh-session batches
  (`EVAL_LIMIT=25`); a durable fix would reconnect the session on error.
- **Task difficulty.** Questions are genuine 2-hop but each hop is fairly direct (one
  distinctive clue often pins the entity in a single search). Making them
  BrowseComp-hard (indirect, cross-referenced clues) is a possible synthesis upgrade.
- **Training experiment.** Planned comparison: base Qwen vs SFT-with-reasoning
  (this cycle's data) vs SFT-without-reasoning (`sft_release_v2`), evaluated on
  held-out advisor/employer + real BrowseComp questions to test transfer.
