# WebAgent_CUA — Result Summary 中英对照

> Last updated 最后更新: 2026-04-27. Single-page index — open this when you come back. 一页索引，回来打开这个就够。

---

## 1. Goal 项目目标

| EN | 中 |
|---|---|
| Build a hybrid DOM + visual web agent that produces high-quality trajectories on synthesized Q-A pairs, so WebLeaper-style models can be trained and evaluated on GAIA / BrowseComp. | 做一个 DOM 优先 + 视觉兜底的 web agent，在合成 Q-A 上跑出高质量 trajectory，给 WebLeaper 这类模型当训练数据，最终在 GAIA / BrowseComp 上评估。 |

---

## 2. Pipeline status 流水线进度

| # | Stage 阶段 | EN | 中 | Status |
|---|---|---|---|---|
| 1 | Hybrid executor | DOM-first agent with OS-level visual fallback | DOM 优先，DOM 失败切视觉兜底 | ✅ done |
| 2 | Trajectory collection | Run agent on existing benchmark to get raw trajectories | 在现有 benchmark 上跑出真实 trajectory | ✅ browsecomp_first50 跑了两次 |
| 3 | Trajectory cleaning + filter | Robust JSON parser, ground-truth-aware judge, dedup, prefix extraction | 修脆弱 JSON 解析、给 judge 加 ground truth、去重、提取关键 prefix | ✅ done |
| 4 | Trajectory categorization | Per-purpose buckets (SFT / rerank / BFS) | 按用途分桶（SFT 正例、rerank 负例、BFS 起点）| ✅ done |
| 5 | Q-A synthesis (MVP) | Wikipedia 2-hop Q-A grounded in Wikidata | Wikipedia 2-hop Q-A，事实在 Wikidata 验证 | ✅ 8 / 9 verified |
| 6 | Run agent on synth Q-A | Generate "our own" training trajectories | 用我们合成的 Q-A 拿训练用 trajectory | ⬜ 待做 |
| 7 | Scale synthesis | 8 → 100+, add Union / Reverse-Union variants | 从 8 条扩到 100+，加 Union / Reverse-Union 变种 | ⬜ 待做 |
| 8 | ISR / ISE filtering | WebLeaper-style coverage + efficiency thresholds | 按 WebLeaper 的 ISR / ISE 阈值筛选 SFT trajectory | ⬜ 待做 |
| 9 | BFS / rerank | Multiple rollouts → shortest path → preference data | 多次 rollout 找最短成功路径 → DPO 数据 | ⬜ 待做 |
| 10 | SFT + RL training | Hand off to WebLeaper team | 数据交给 WebLeaper team 训 | ⬜ 待做 |
| 11 | Final eval | GAIA / BrowseComp | 训完模型在 GAIA / BrowseComp 上跑 | ⬜ 待做 |

---

## 3. Headline numbers 关键数据

| Metric 指标 | EN explanation | 中文说明 | Value |
|---|---|---|---|
| browsecomp_first50 final success | After ground-truth judging | ground-truth 校正后的真实成功率 | **15 / 50 = 30 %** |
| Pre-truth raw success | LLM judge without ground truth (inflated) | 没传 ground truth 的旧 judge（虚高） | 27 / 50 = 54 % |
| Reclassification flips | success → fail / fail → success | 翻盘数 | 15 down / 3 up |
| Trajectory yield | Usable for downstream pipeline | 可进 pipeline 的 trajectory 数 | **42 / 50 = 84 %** |
| Visual fallback E2E | reCAPTCHA cross-origin iframe checkbox | 跨域 iframe 验证码 checkbox | 2/2 DOM fail → 2/2 visual pass ✅ |
| DOM click success rate | Hybrid Click DOM path | DOM 点击成功率 | 325 / 401 = **81 %** |
| DOM fill success rate | Hybrid Fill DOM path | DOM 填表成功率 | 299 / 311 = **96 %** |
| Visual fallback triggered | Cases the DOM path failed | DOM 失败转视觉的次数 | 95 (79 click + 16 fill) |
| Safety-filter retries that recovered content | Vertex AI null-content cases | 安全过滤导致的空响应被重试救回 | 14 / 24 |
| Synthesized Q-A | Wikipedia 2-hop, live-verified | 合成的 Wikipedia 2-hop Q-A | **8** |

---

## 4. Bugs we fixed (executor robustness) 修过的 bug

| # | EN | 中 | Symptom 现象 | Fix 修法 |
|---|---|---|---|---|
| 1 | Playwright MCP 1.60 changed `browser_navigate` to return only a YAML file path, not inline DOM | Playwright MCP 1.60+ 不再 inline 返回 DOM，只给 YAML 文件引用 | Agent never sees `[ref=]`, loops re-visiting same URL forever | After every navigate / DOM click, force `browser_snapshot` to backfill inline DOM with `[ref=]` |
| 2 | `Visit` tool used as "refresh", but `browser_navigate` reset SPA state | Agent 把 visit 当刷新用，结果 SPA 状态被重置 | Filled-in form is wiped, agent restarts | Same-SPA detection: skip navigate, only call snapshot when current and target URL share an SPA host |
| 3 | Cross-task SPA leakage | 任务间 SPA 状态污染 | task N sees task N-1's filled form | `browser_navigate('about:blank')` at the start of every `agentic_loop` |
| 4 | Click-Search has no feedback signal | Click 完没反馈，agent 反复换 ref 重点 | Endless retry loop on the same Search button | Read `document.location.href` before/after click, surface URL change to agent in observation |
| 5 | Vertex AI safety filter returns `content=null` | Gemini 安全过滤返回空 content | 22/50 browsecomp tasks die at turn 0 with no trajectory | Pass `safety_settings: BLOCK_NONE` for all 5 categories + retry-on-empty with exponential backoff |
| 6 | Evaluator did not see ground truth | LLM-as-judge 没看 ground truth | 15 false positives ("AC Milan" judged equal to "Ireland v Romania") | Add `ground_truth` arg to `evaluate_answer_with_llm`, write `reeval_with_truth.py` to fix old runs offline |
| 7 | Brittle JSON parsing | LLM JSON 输出剥 fence 不干净 | 15 SFT positives + 41 visual verifications dropped | `lenient_json_extract`: handles fences, prose-wrapped, multi-block, truncated, regex fallback |
| 8 | `sem` kwarg is a dict not a Semaphore | `sem` 是字典但代码当成 Semaphore | Visual fallback enters and immediately `TypeError`s | `_pick_sem(sem)` helper picks `sem['llm']` |
| 9 | 50 concurrent SSE on Python 3.13 anyio TaskGroup | 50 并发 SSE 触发 anyio TaskGroup 大规模 cancel | 50/50 ExceptionGroup in 10 s | Move `mcp_client` from `agentic_loop` to `main()` — one shared SSE per run |
| 10 | OS-level click race when MAX_WORKERS > 1 | 多并发下 OS 级鼠标互相抢 | Click lands on wrong tab | Hard constraint MAX_WORKERS=1 for hybrid mode |

---

## 5. Path index 路径索引

### Source code 源代码（已 git commit，未 push）

| Path 路径 | EN | 中 |
|---|---|---|
| `infer_async_nestbrowse.py` | Agent main loop, ground-truth judge, shared mcp_client | agent 主循环、ground-truth judge、共享 mcp_client |
| `utils.py` | `lenient_json_extract`, `GEMINI_SAFETY_SETTINGS`, retry-on-empty | 鲁棒 JSON 解析、安全设置、空响应重试 |
| `toolkit/browser_hybrid.py` | Hybrid Visit / Click / Fill + DOM focus + URL-CHECK | 混合工具类，DOM 焦点校验，URL 变化检测 |
| `toolkit/browser.py` | Pure-DOM baseline (reference) | 纯 DOM 基线（参考） |
| `toolkit/browser_cua.py` | Pure-visual baseline (reference) | 纯视觉基线（参考） |
| `scripts/watchdog.sh` | Periodic status writer for overnight runs | 过夜跑的 5 分钟状态快照 |
| `scripts/reeval_with_truth.py` | Re-judge predictions against ground-truth offline | 离线用 ground truth 重判 |
| `scripts/build_dataset.py` | Trajectory → 6-bucket dataset | trajectory 自动分 6 桶 |
| `scripts/synthesis_agent.py` | Wikipedia 2-hop Q-A synthesis (Wikidata-grounded) | Wikipedia 2-hop Q-A 合成 |

### Benchmark task data 评测任务数据

| Path 路径 | EN | 中 |
|---|---|---|
| `data/browsecomp_first50.jsonl` | Main eval set, 50 obscure-fact Q-A | 主评测集，50 条冷知识题 |
| `data/browsecomp_first10.jsonl` | 10-task subset for fast iteration | 10 条快速迭代用子集 |
| `data/navi_bench_first50_ready.jsonl` | 20 Google Flights tasks (despite name) | 20 条 Google Flights 题（数字名字误导） |
| `data/smoke_test.jsonl` | 3-task smoke (navi + browsecomp + recaptcha) | 3 条 smoke 集（机票 + 冷知识 + 验证码） |
| `data/wiki_2hop.jsonl` | 8 synthesized 2-hop Q-A (used by infer) | 我们合成的 8 条 2-hop（infer 读这个） |
| `data/synth/wiki_2hop.jsonl` | Same — synthesis script's output dir | 同上，synthesis 脚本默认输出位置 |

### Run logs 运行日志

| Path 路径 | EN | 中 |
|---|---|---|
| `logs/run_browsecomp_first50_20260424_120907.log` | First first50 run, 8h, before safety patch | 第一次 first50 跑，8 小时，安全补丁之前 |
| `logs/run_browsecomp_first50_20260426_073654.log` | Second first50 run, 10h32m, with all patches | 第二次 first50 跑，10h32m，所有补丁齐全 |
| `logs/status_20260426_073654.md` | Watchdog snapshots for the second run (readable!) | 第二次跑的 watchdog 快照（**最易读**） |
| `logs/status_20260424_120907.md` | Watchdog snapshots for the first run | 第一次跑的 watchdog 快照 |

### Raw benchmark results 原始结果

| Path 路径 | EN | 中 |
|---|---|---|
| `results/...browsecomp_first50_success.jsonl` | LLM-judge "answer" — inflated, 27 records | 没用 ground truth 的"判对"——虚高 |
| `results/...browsecomp_first50_failure.jsonl` | LLM-judge "incorrect" — 23 records | 同上"判错" |
| `results/...browsecomp_first50_trajectory.jsonl` | All 50 trajectory summaries | 全部 50 条 trajectory 概要 |
| `results/...browsecomp_first50_success_truth.jsonl` | **ground-truth-corrected — 15 records** | **真实判对，15 条** |
| `results/...browsecomp_first50_failure_truth.jsonl` | ground-truth-corrected — 35 records | 真实判错，35 条 |
| `results/...browsecomp_first50_trajectory_truth.jsonl` | trajectories with corrected `termination` | trajectory + 修正后的 termination |
| `results/...browsecomp_first50_reeval_truth_audit.csv` | Per-task old vs new judgement | 每条 task 的 old vs new 判决 |

### Final dataset 最终数据集（**deliverable**）

Path: `dataset/browsecomp_first50_truth/` （已分类好的训练数据）

| File 文件 | Records | EN purpose | 中文用途 |
|---|---|---|---|
| `manifest.json` | – | counts + index | 计数索引 |
| `sft_positive_clean.jsonl` | 3 | SFT chosen — gold (≥3 tools, ≤2 repeats) | SFT 正例首选 |
| `sft_positive_messy.jsonl` | 12 | SFT chosen — first-success prefix already cut | SFT 正例（含杂讯，已截到首次答案前缀） |
| `rerank_negative_hard.jsonl` | 12 | DPO/rerank rejected — real attempt wrong | DPO/rerank 难负例 |
| `rerank_negative_quick.jsonl` | 7 | weak rejected — gave up early | DPO 弱负例 |
| `bfs_prefix.jsonl` | 15 | BFS roots — prefix already cut at repetition onset | BFS 起点（已截到循环开始前） |
| `discarded.jsonl` | 1 | safety-filter killed at turn 0 | 0-tool 直接死，丢 |

Each record 每条记录的字段:
```jsonc
{
  "task_id": ...,
  "task": "...",                  // 原问题 / original question
  "category": "A_clean_success",
  "termination": "answer",
  "prediction": "...",
  "eval_reasoning": "...",
  "n_tool_calls": 3,
  "n_repeats": 0,
  "n_turns": 4,
  "n_msgs_full": 9,
  "n_msgs_kept": 9,               // 已为该用途截断 / truncated for the bucket
  "messages": [...],              // 用于训练的消息序列 / messages chosen for training
  "trajectory": [...],            // 完整动作轨迹 / full action-level trace for analysis
  "visited_urls": [...]
}
```

### Git commits（未 push）

| Commit | EN | 中 |
|---|---|---|
| `aea1c56` | Ground-truth-aware judge + dataset builder + reeval-with-truth script | judge 加 ground truth、dataset builder、离线 reeval 脚本 |
| `9cd3cb0` | Robust JSON parsing + safety_settings + watchdog/reeval scripts | 鲁棒 JSON、安全设置、watchdog/reeval 脚本 |
| `d7b6557` | Stable hybrid DOM+visual executor checkpoint | hybrid 执行层稳定版 checkpoint |

---

## 6. How to reproduce 怎么复现

| Action | Command |
|---|---|
| Re-judge an old run with ground truth 用 ground truth 重判 | `GOOGLE_APPLICATION_CREDENTIALS=/scr/rucnyz/.config/gcloud/vertex-express-key.json AGENT_LLM_BASE_URL='https://aiplatform.googleapis.com/v1beta1/projects/msagentrt/locations/global/endpoints/openapi' MODEL_NAME='google/gemini-3.1-pro-preview' python scripts/reeval_with_truth.py browsecomp_first50` |
| Rebuild dataset buckets 重建数据集分桶 | `python scripts/build_dataset.py browsecomp_first50 --truth` |
| Synthesize more Q-A 合成更多 Q-A | `python scripts/synthesis_agent.py --n 50 --out data/synth/wiki_2hop_50.jsonl` |
| Run benchmark 跑 benchmark | tmux + export `GOOGLE_APPLICATION_CREDENTIALS` + `BRAVE_SEARCH_KEY` + `MODEL_NAME` → set `benchmark_name` in `infer_async_nestbrowse.py` line ~474 → `python infer_async_nestbrowse.py > logs/run_<ts>.log 2>&1 &` → optionally attach `scripts/watchdog.sh` |

---

## 7. Caveats 已知边角问题

| EN | 中 |
|---|---|
| Wikidata P17 returns historical countries first; Ada Lovelace's London resolves to "Roman Empire". Need P582 end-time qualifier filter. | Wikidata P17 会返历史 country；Ada Lovelace 的 London 拿到 "Roman Empire"。要加 P582 时间过滤拿现今 country。 |
| `BRAVE_SEARCH_KEY` not set in run shell → search tool errored, agent fell back to direct visit URLs. Affects browsecomp tasks where the agent can't deduce the URL. | 跑的时候 shell 里没 `BRAVE_SEARCH_KEY`，search 工具报错，agent 只能直接 visit URL。影响那些猜不到 URL 的 browsecomp 题。 |
| navi_bench evaluator literal-substring-matches airport codes; Google Flights URLs base64-encode them. We chose **not** to fix — would over-fit a narrow benchmark. Use navi only as a stress test. | navi_bench 评测器对机场代码做字面子串匹配，但 Google Flights URL 是 base64 编码。**故意不修**——避免过拟合一个窄 benchmark。只把 navi 当压力测试。 |
| Single-browser physical constraint: `MAX_WORKERS=1` for hybrid; concurrent OS-level visual clicks would race the cursor. | 单浏览器物理约束：hybrid 模式必须 `MAX_WORKERS=1`；多并发 OS 级点击会抢光标。 |

---

## 8. Next-step priority 下一步优先级

| EN | 中 | Effort 工作量 |
|---|---|---|
| Run hybrid agent on the 8 wiki_2hop tasks → first batch of "our own" trajectories | 用 hybrid agent 跑 8 条 wiki_2hop，得到第一批"我们自己"的 trajectory | 30 min wall-clock |
| Scale synthesis 8 → 100+; add P582 time-filter for current country | 合成扩到 100+；加 P582 时间过滤现今 country | 1 day |
| Add Union / Reverse-Union variants from WebLeaper paper | 加 WebLeaper paper 的 Union / Reverse-Union 变种 | 2–3 days |
| Implement ISR / ISE calculator + WebLeaper-style filter | 写 ISR / ISE 计算器，按 WebLeaper 阈值过滤 | 1 day |
| BFS multiple-rollout per task → shortest-path preference data | 每条 task 多次 rollout → 最短路径 → DPO 数据 | 3–5 days (compute-heavy) |
| Hand off to WebLeaper team for SFT + RL | 数据交给 WebLeaper team 做 SFT + RL | (their side) |
| Final eval on GAIA + BrowseComp | 训后模型在 GAIA + BrowseComp 上评估 | (their side) |
