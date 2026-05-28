# Session changelog — verify-layer fixes + synthesis v2

Date span: late session (2026-05 timeframe)
Files touched: `toolkit/browser_hybrid.py`, `scripts/synthesis_agent.py`,
`infer_async_nestbrowse.py` (benchmark name only)

This document is for picking up context after time away. It lists each change,
why it exists, and which observation triggered it.

---

## Part 1 — Hybrid agent verify-layer fixes (`toolkit/browser_hybrid.py`)

### Background

Hybrid agent design: DOM-first (Playwright MCP via [ref=eXXX]) + visual fallback
(Windows-MCP screenshot + Gemini coord-finding + OS-level click). When DOM
operation fails, the visual fallback fires `visual_click` / `visual_fill`. After
the OS-level action, there's a **verification step** that decides "did the
click/type actually do anything useful?"

### Problem observed

Looking at `results/wiki_2hop_rev/` (the broken pre-fix run) and its log:

- `check_dom_focus` failed 100% of calls with `JSONDecodeError('Expecting
  property name enclosed in double quotes: line 1 column 2 (char 1)')`. 40+
  occurrences in the log.
- Therefore every visual-fallback verification ran "blind" — no DOM ground truth,
  only Gemini's screenshot judgment.
- Worse, `judge_fill_by_dom` was returning `(decided=True, success=False)` for
  the very common case of "focus moved away after typing" (autocomplete dropdown,
  formatted-value rewrite). This single-sidedly declared failure.
- That failure then triggered a **destructive retry**: `ctrl+a` + `delete` to
  clear the field, then re-type. If the original fill was actually successful
  (just looked empty because focus moved), this **wiped a correct answer**.
- The agent then saw `[fill] Fill error: both DOM and visual fill failed` and
  abandoned `fill`, switching to brute-forcing URLs like
  `Special:Search?search=...` to bypass the broken tool. This explains the
  Rozhdestvensky task's 60+ visit calls / 143-message trajectory.

### Fix #1 — `check_dom_focus` parser (lines ~132–226)

**What changed**:
- JS side stopped using `JSON.stringify({...})`; just returns the object
  directly. Playwright MCP 1.60+ already serializes object return values to
  proper JSON inside its `### Result` block; the extra stringify was producing
  double-escaped strings that no JSON parser could read.
- Python side added `_parse_dom_focus_payload(text)` with a 4-stage parser:
  1. anchor on `### Result` if present, then extract the first **balanced**
     `{...}` block (NOT the old greedy `\{.*\}` regex — that gobbled the
     `### Ran Playwright code` block's JS source too, which is what was actually
     causing the second wave of `parse failed` warnings after the first fix)
  2. try `json.loads` directly
  3. fall back to `ast.literal_eval` (handles Python repr / single-quoted dicts)
  4. fall back to bare-key quoting regex → `json.loads`
  5. fall back to unescape + `json.loads`

**Why**: cross-process boundaries with `stringify→render→regex→loads` chains
have too many opportunities to flatten data into "looks like JSON but isn't"
form. Better to have one tolerant parser at the receiving end.

**Validated by**: post-fix v2 fresh run shows `check_dom_focus error = 0` and
`check_dom_focus parse failed = 0`, plus `DOM-decided verification = 7` (the
first time non-zero in any run).

### Fix #2 — `judge_fill_by_dom` only judges success, never failure (lines ~274–300)

**What changed**:
- Removed the `(decided=True, success=False)` branches.
- Now returns `(True, True, ...)` only when the focused element is editable AND
  its value/innerText bidirectionally substring-matches the expected text.
- All other cases — including "value is empty", "value doesn't match", "focus
  moved to a non-editable element", "no DOM info" — return
  `(decided=False, ...)` meaning **uncertain**, deferring the decision to the
  visual judge.

**Why**: DOM evidence is great at proving success (the element actually contains
the text) but bad at proving failure (autocomplete may have consumed the input
and reset the visible field; date/number formatting may have rewritten the
value). One-sided error: only declare confident wins, never confident losses.
Symmetric with how `judge_click_by_dom` was already designed — it never returns
decided failure.

Added bidirectional substring matching (`"SFO" in "San Francisco (SFO)"` and
the reverse) to handle normalization scenarios.

**Validated by**: `fill_both_failed` dropped from 9 (broken baseline) to 0
(first-fix run) to 0 (v2 fresh run).

### Fix #3 — `visual_fill` no longer destructively retries on uncertainty (lines ~492–605)

**What changed**:
- Removed the `ctrl+a` + `delete` block entirely.
- New retry logic:
  - DOM confirmed success → return True
  - DOM uncertain + visual confirmed success → return True
  - DOM uncertain + visual returns a **strong-failure signal** (Gemini explicitly
    says "no input field visible" / "typed text appeared in taskbar" / etc.) →
    re-pick coordinates and re-type WITHOUT clearing the field (the OS-level
    type tool clicks first, so a different coord refocuses elsewhere)
  - DOM uncertain + visual uncertain (vague failure reason) → **return
    best-effort True**; the agent will see the next snapshot and self-correct
- A list of `STRONG_FAIL_KEYWORDS` defines what counts as "strong" visual
  failure (currently: `'no input field', 'input field is not', ...`).

**Why**: destructive operations (clearing a field) should only fire on
**confident** failure evidence. If we don't have confident failure evidence,
the previous fill may have actually succeeded — destroying it is worse than
trusting it. Aligns with "defer to next observation" principle.

**Validated by**: `Clearing previous input` count is 0 in all post-fix runs
(was 3 in broken baseline). Combined with Fix #2, `fill_both_failed` went
9 → 0.

### Side effects worth knowing

- The hybrid agent now produces 39% fewer `visit` calls and 16% fewer assistant
  messages on the same wiki_2hop_rev benchmark, because agent no longer needs
  to brute-force around broken fill/click.
- Some click verifications still go "Gemini found no coordinates" on the
  Wikipedia search button (ref=e25) — that's unrelated to verify layer,
  it's a Gemini coord-finding limitation on certain screenshots.

---

## Part 2 — Synthesis agent improvements (`scripts/synthesis_agent.py`)

### Background

Pre-existing script that walks Wikidata to build "2-hop" Q-A pairs (seed person
→ birthplace → country). Three variants: `basic_2hop` (reveals seed name),
`reverse_union` (hides seed behind clues), and the new `specific` (v2).

### Problem observed (pre-improvements)

Running `build_dataset.py` on the first-fix-run results showed:
- 24/29 success trajectories were classified — but 9 of them used **zero tool
  calls** (the agent answered directly from LLM memory)
- Those 9 all had answers like "Germany", "United States", "France" — common
  country names Gemini has memorized for thousands of famous-ish people
- Conclusion: **single-country-answer questions are not training data**, because
  the model isn't being forced to use web tools

### Change #1 — LLM screener (lines ~50–145)

**What added**: `llm_can_answer_without_tools(question, expected_answer)` that
asks Gemini (no tools, no search) for the answer using the same Vertex AI
endpoint as the main agent. Returns `(is_guessable, raw_response)` where
guessable = bidirectional alpha-numeric substring match between Gemini's reply
and the expected answer.

Added CLI flags:
- `--screen-llm` — drop candidates Gemini can answer
- `--screen-llm-tag-only` — keep but tag `llm_guessable: true/false` for offline
  filtering

**Why**: empirical filter > heuristic. Don't argue about whether a question is
"LLM-uncrackable"; ask the LLM. Catches whatever the current synthesis
templates produce, including future templates we haven't designed yet.

**Validated by**: ran on the 29 existing benchmark questions, flagged ~28% as
guessable — same ballpark as the 31% observed zero-tool success rate, with
modest set overlap (the screener's prompt is more cautious than the agent's
system prompt, so it catches a different but equally-valid slice).

### Change #2 — MediaWiki Category API seed path (lines ~250–360)

**What added**: `fetch_category_seeds(categories, ...)` and supporting helpers
`_wiki_category_members` + `_wikidata_sitelink_count`. Plus a `DEFAULT_CATEGORIES`
list of 8 curated obscure-people categories.

Added CLI flags:
- `--category` — use Category API path
- `--categories` — list of Wikipedia categories to walk
- `--category-max-per-cat` — raw page cap per category before sitelink filtering
- `--category-recurse-depth` — recurse into subcategories N levels

**Why**: Wikidata SPARQL (WDQS) goes down for hours under outages with
"1 req/min" rate limiting. MediaWiki Category API runs on a different
infrastructure and stays up. Also more topically precise — you can target
"20th-century Russian mathematicians" by category name instead of guessing the
right occupation QIDs.

**Validated by**: works while SPARQL is rate-limited; the v0 baseline + v2 runs
both used this path successfully.

### Change #3 — Default sitelink_max 25 → 10

**Why**: post-build_dataset analysis showed Gemini still memorizes ~28% of
sitelink<=25 entities. Tightening to <=10 makes the seed pool more obscure
across the board.

**Caveat**: this alone doesn't fix the problem — see Change #4. Obscurity
helps but isn't sufficient when answer space is small.

### Change #4 — v2 synthesis schema with specific-entity answers (lines ~450–600)

**What added**: `synth_v2_specific(seed_title, batch_answer_counts, max_repeat_per_answer)`
and `--mode specific` CLI option.

**Answer priority chain** (try in order, pick first non-banned hit):
1. P19 (birthplace city) — answer is a city name, not country
2. P184 (doctoral advisor) — answer is a person name
3. P108 (employer / institution)
4. P69 (educated_at)
5. P800 (notable work)
6. P166 (award received)

**BAN filter** `_answer_is_banned(label, qid)`:
- `BANNED_P31_QIDS` — Wikidata instance-of classes that mean country/region/
  state-like entities (Q6256 country, Q15180 USSR, Q5107 continent, etc.)
- `BANNED_ANSWER_LABELS` — case-insensitive string blacklist (Russia, USA,
  Germany, ... ~30 entries) as a safety net beyond P31

**Per-batch frequency cap**: tracks how often each answer label has appeared
in the current batch; reject any candidate whose answer label has already hit
`max_repeat_per_answer` (auto-set to `ceil(N/4)`, so N=10 → cap 3). Forces
diversity.

**Question template**: hides seed behind clues (same as reverse_union), but
asks the specific-entity question ("In which city was this person born?",
"Who was the doctoral advisor of this person?", etc.).

**Why**:
- v0 baseline (10 outputs) hit 100% country-answer leakage: 8× Russia,
  1× Soviet Union, 1× Kazakhstan. Single-country seed pool collapses answer
  space to ~3 unique answers.
- Cities are extremely sparse in LLM memory (Gemini doesn't know "Bykovo,
  Moscow Oblast" from "obscure mathematician + Lomonosov U")
- Advisor names are even sparser (specific person-to-person relationships
  don't appear in pretraining the way country statistics do)
- BAN filter prevents the chain from accidentally landing on country at any
  hop (P19 could resolve to "Russia" if the seed has no specific city; P31
  check catches it and falls through to P184 / P108 / ...)
- Frequency cap prevents one common answer (e.g., everyone studied at
  Moscow State University → "Moscow" everywhere) from dominating

**Validated by**: v2 actual run (10 outputs) produced:
- 7 cities + 3 advisor names (no countries)
- 8 unique answers (vs v0's 3)
- 0% LLM-screened (Gemini couldn't memorize a single one)
- 0% skipped (P19→answer is 1 hop, more reliably available than P19→P17)
- "Moscow" appeared exactly 3 times — frequency cap working as intended

---

## Part 3 — Memory artifacts

These persist across sessions:
- `synthesis_v2_design.md` — design rationale + v0 baseline numbers + v2 actuals
- Updated `MEMORY.md` index to point to the above

---

## Where to pick up next

1. **Bring MCP services back up** (Docker → Edge VM, Playwright MCP :3006,
   Windows-MCP :8015). Solver was about to run on v2 but MCP went down.
2. **Run solver on `data/wiki_2hop_v2.jsonl`** — 10 v2 Q-As, benchmark_name in
   `infer_async_nestbrowse.py` already set to `wiki_2hop_v2`. This is the
   missing validation: confirm hybrid agent can actually solve v2 questions
   via web search/browse (we've only confirmed LLM can't memorize them).
3. **If solver mostly succeeds**: scale synthesis to 50-100 Q-As, then 1000+
   for actual training.
4. **If solver struggles on subset**: examine which v2 templates produced
   unsolvable questions (likely "1910s + mathematician" type with too few
   clues) and add clue-density requirements.
5. **Deferred**: BFS efficiency lower-bound field; cleanup of remaining 6
   `parse_failed` edge cases; Pyright type cleanup.
