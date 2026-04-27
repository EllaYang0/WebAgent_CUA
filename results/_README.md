# `results/` layout 目录布局

每个 benchmark 一个文件夹。Each benchmark gets its own folder.

```
results/
├── _README.md                       本文件 / this file
├── browsecomp_first50/              当前主 run (Apr 26) / current main run
│   ├── success.jsonl                LLM judge 不看 ground truth — 虚高
│   │                                LLM judge without ground-truth — INFLATED
│   ├── failure.jsonl                同上 / same
│   ├── trajectory.jsonl             所有 50 条 trajectory 摘要
│   │                                trajectory summary for all 50 tasks
│   ├── success_truth.jsonl          ✅ ground-truth-aware judge 真实成绩
│   │                                trustworthy with ground-truth comparison
│   ├── failure_truth.jsonl          同上 / same
│   ├── trajectory_truth.jsonl       trajectory + 修正后的 termination
│   │                                trajectory with corrected termination
│   └── reeval_truth_audit.csv       每条 task 旧判 vs 新判
│                                    per-task old vs new verdict
├── smoke_test/                      3-task smoke (navi + browsecomp + recaptcha)
│   ├── success.jsonl
│   ├── failure.jsonl
│   └── trajectory.jsonl
├── wiki_2hop/                       我们合成的 8 条 Q-A 跑出的结果
│                                    Hybrid agent run on 8 synthesized 2-hop Q-A
│   ├── success.jsonl
│   ├── failure.jsonl
│   └── trajectory.jsonl
└── archive/                         不再使用的历史结果
                                     historical / unused results
    ├── browsecomp_first10_apr24/    早期调试 / early debugging
    ├── wrong_model_name_apr24/      gemini-3-pro-preview 跑的（型号名错）
    │                                ran with wrong model name (gemini-3-pro-preview)
    ├── pre_our_work_mar30/          上一位同学留下的 / previous student's runs
    └── misc/                        散落的孤立文件 / orphan files
```

## `_truth` vs no-suffix 区别 / difference

| 文件 | 判定方式 / how judged | 可信度 / trust |
|---|---|---|
| `success.jsonl` / `failure.jsonl` | LLM judge **不看** ground truth, 只看 task + prediction | ❌ 虚高 — 15 false positives in browsecomp_first50 |
| `success_truth.jsonl` / `failure_truth.jsonl` | LLM judge **看** dataset 的 gold answer 严格比对 | ✅ 真实成绩 |

For training data / dataset construction, **always use `_truth.jsonl` files**. 训练数据用 `_truth` 系列。

## How to add a new run / 加新 run

1. Set `benchmark_name = "<your_bench>"` in [infer_async_nestbrowse.py](../infer_async_nestbrowse.py).
2. Run agent → outputs to `results/<your_bench>/{success,failure,trajectory}.jsonl` automatically.
3. Optionally re-judge with ground truth: `python scripts/reeval_with_truth.py <your_bench>`.
4. Build dataset: `python scripts/build_dataset.py <your_bench> --truth`.

设置 `benchmark_name` → 自动写入 `results/<benchmark_name>/`。
