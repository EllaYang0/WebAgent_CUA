from datetime import datetime
import re
import os
import json
import copy
import time
import asyncio
import traceback
from tqdm import tqdm
from collections import Counter
import tiktoken

from prompts import *
from toolkit.tool_search import Search
from toolkit.mcp_client import mcp_client
from toolkit.browser_hybrid import Visit, Click, Fill
from utils import read_jsonl, count_tokens, call_llm, lenient_json_extract


def is_url_navigation_task(data: dict) -> bool:
    return data.get('task_type') == 'url_navigation'


def parse_navi_bench_item(data):
    task = data['task']
    start_url = data['start_url']
    gt_urls = data.get('gt_urls', [])
    return task, start_url, gt_urls


def parse_task_item(data):
    """兼容 navi_bench 和 browsecomp 两种 schema。
    browsecomp: {id, question, answer}  → 没有 start_url / gt_urls
    navi_bench: {task_id, task, start_url, gt_urls, task_type, ...}
    """
    if 'question' in data and 'task' not in data:
        return data['question'], None, []
    return parse_navi_bench_item(data)


def url_matches(visited_urls, gt_urls):
    for visited in visited_urls:
        for gt in gt_urls:
            if visited.rstrip('/') == gt.rstrip('/'):
                return True
    return False


def evaluate_gt_info(visited_urls: list, gt_info: list) -> tuple[bool, str]:
    if not gt_info:
        return False, "no gt_info"

    gt = gt_info[0]
    segments = gt.get('segments', [])

    flight_urls = [u for u in visited_urls if 'google.com/travel/flights' in u and len(u) > 40]
    if not flight_urls:
        return False, "no flights URL visited"

    last_url = flight_urls[-1]

    for seg in segments:
        if seg['from'] not in last_url or seg['to'] not in last_url:
            return False, f"segment {seg['from']}->{seg['to']} not found in URL"

    for seg in segments:
        date_str = seg.get('date', '')
        if date_str:
            try:
                dt = datetime.strptime(date_str, '%B %d, %Y')
                url_date = dt.strftime('%Y-%m-%d')
                if url_date not in last_url:
                    return False, f"date {url_date} not found in URL"
            except Exception:
                pass

    return True, f"matched: {last_url}"


async def evaluate_answer_with_llm(sem, task: str, prediction: str, ground_truth: str = None) -> tuple[bool, str]:
    # Without ground truth the judge can only check whether the answer "sounds
    # plausible" for the question — it has no way to know if it's actually right
    # — so it tends to mark unrelated-but-confident answers as correct. ALWAYS
    # supply ground_truth here for benchmarks that have one.
    if ground_truth is not None and ground_truth != '':
        eval_prompt = f"""You are an evaluator for a web agent benchmark. Compare the agent's answer to the ground-truth answer and decide if the agent answered correctly.

Task: {task}

Ground truth answer: {ground_truth}

Agent's answer: {prediction}

The agent is correct ONLY if its answer matches the ground truth in substance. Allow for surface differences:
- different formatting of the same value (e.g. "1988-1996" vs "1988-96", "Auxerre" vs "AJ Auxerre")
- synonyms / abbreviations (e.g. "USA" vs "United States")
- extra phrasing if the core fact matches

Mark INCORRECT if:
- the agent named a different person/place/thing
- the agent gave a related but different fact
- the agent's answer is missing a required component (e.g. only first name when both are needed)

Respond with a JSON object:
{{"correct": true/false, "reasoning": "brief explanation citing both values"}}

Output only the JSON, nothing else."""
    else:
        eval_prompt = f"""You are an evaluator for a web agent benchmark. The agent was given a task and produced an answer.
Your job is to determine if the agent's answer correctly and completely addresses the task.

Task: {task}

Agent's Answer: {prediction}

Evaluate whether the answer:
1. Directly addresses what was asked
2. Contains the specific information requested (prices, flight numbers, yes/no, etc.)
3. Is in the correct format requested by the task

Respond with a JSON object:
{{"correct": true/false, "reasoning": "brief explanation"}}

Output only the JSON, nothing else."""

    messages = [{"role": "user", "content": eval_prompt}]
    response = await call_llm(sem, messages, 512, os.getenv("MODEL_NAME"))

    result = lenient_json_extract(response)
    if result is None:
        return False, f"JSON unrecoverable, raw: {(response or '')[:150]}"
    return bool(result.get('correct', False)), result.get('reasoning') or result.get('reason') or ''


async def call_tool(sem, tool_name: str, tool_args: dict, client, lock):
    global tokenizer
    async with sem['tool']:
        if tool_name == "search":
            return await search.call(tool_args)
        elif tool_name == "visit":
            return await visit.call(tool_args, client=client, lock=lock, tokenizer=tokenizer, sem=sem)
        elif tool_name == "click":
            return await click.call(tool_args, client=client, lock=lock, tokenizer=tokenizer, sem=sem)
        elif tool_name == "fill":
            return await fill.call(tool_args, client=client, lock=lock, tokenizer=tokenizer, sem=sem)
        else:
            await asyncio.sleep(1)
            return f'Tool {tool_name} does not exist.'


async def agentic_loop(sem, data, messages, client, lock):
    global tokenizer

    task, start_url, gt_urls = parse_task_item(data)
    task_id = data.get('task_id', data.get('id', ''))
    is_nav_task = is_url_navigation_task(data)
    gt_info = data.get('gt_info', [])

    record = copy.deepcopy(messages)
    summary_record = []
    trajectory = []
    visited_urls = []

    termination = 'max_turn_exceeded'
    prediction = '[No Prediction]'
    eval_reasoning = ''

    # mcp_client/SSE opened ONCE in main() and shared across all agentic_loop coroutines.
    # Opening it per-task caused 50 parallel anyio TaskGroups → Python 3.13 cancelled
    # every call_llm mid-await with "Ping task was cancelled" + ExceptionGroup.
    # Shared client + sem['session'] + per-MCP-call lock is enough serialization.
    async with sem['session']:
            try:
                # 跨任务隔离：上一 task 跑完后，Edge 窗口 URL 可能停在
                # google.com/travel/flights?tfs=...(上一 task 的 SPA 状态)。
                # 新 task 如果第一次 visit 到 google.com/travel/flights，Visit.call 的
                # Same-SPA 分支会跳过 browser_navigate，旧表单状态会污染新 task。
                # 这里强制 hard-navigate 到 about:blank 清空浏览器状态，再让 init_visit
                # 真正跳转到目标 start_url。
                try:
                    async with lock:
                        await client.call_tool('browser_navigate', {'url': 'about:blank'})
                    print(f"[agentic_loop] reset browser to about:blank for task_id={task_id}")
                except Exception as reset_err:
                    print(f"[agentic_loop] about:blank reset failed (non-fatal): {repr(reset_err)}")

                # Only do an initial visit if the task has a concrete start_url (navi_bench).
                # browsecomp tasks have start_url=None — calling visit(None) triggers
                # browser_navigate(url=None), which kills the Playwright SSE session and
                # cascades into an anyio ExceptionGroup on mcp_client teardown. For those
                # tasks the agent is expected to call `search` / `visit` on its own anyway.
                if start_url:
                    init_result = await call_tool(sem, 'visit', {'url': start_url, 'goal': task}, client, lock)
                    if isinstance(init_result, tuple):
                        init_obs, _ = init_result
                    elif isinstance(init_result, str):
                        init_obs = init_result
                    else:
                        init_obs = str(init_result)

                    visited_urls.append(start_url)
                    trajectory.append({
                        'turn': 0,
                        'action': 'init_visit',
                        'url': start_url,
                        'observation_snippet': init_obs[:200],
                        'timestamp': time.time(),
                    })
                    record.append({"role": "user", "content": f"<tool_response>\n{init_obs}\n</tool_response>",
                                    "tool_name": "visit", "tool_args": {"url": start_url}})
                else:
                    print(f"[agentic_loop] no start_url for task_id={task_id}; skipping init visit")
            except Exception as e:
                print(f"Init visit failed: {e}")

            print(f"[agentic_loop] task_id={task_id} entering main turn loop with {len(record)} record messages", flush=True)
            for turn in range(MAX_AGENT_TURN):
                print(f"[agentic_loop] task_id={task_id} turn={turn}", flush=True)
                if count_tokens(record, tokenizer) > MAX_AGENT_LEN:
                    termination = 'max_length_exceeded'
                    break

                response = await call_llm(sem, record, int(os.getenv("MAX_SINGLE_GEN_TOKENS")), os.getenv("MODEL_NAME"))
                print(f"[agentic_loop] task_id={task_id} turn={turn} response_len={len(response) if response else 'None/empty'}", flush=True)

                if not response:
                    return {
                        'task_id': task_id,
                        'task': task,
                        'task_type': 'url_navigation' if is_nav_task else 'info_extraction',
                        'start_url': start_url,
                        'gt_urls': gt_urls,
                        'visited_urls': visited_urls,
                        'prediction': prediction,
                        'eval_reasoning': eval_reasoning,
                        'messages': record,
                        'summary_record': summary_record,
                        'trajectory': trajectory,
                        'termination': 'llm_response_error'
                    }

                record.append({"role": "assistant", "content": response})

                if "<tool_call>" in response and "</tool_call>" in response:
                    cur_summary_record = None
                    # Only execute the first tool call; ignore any additional ones
                    all_tool_calls = re.findall(r'<tool_call>(.*?)</tool_call>', response, re.DOTALL)
                    if len(all_tool_calls) > 1:
                        print(f"[WARN] LLM returned {len(all_tool_calls)} tool calls, only executing the first one")
                    tool_call = all_tool_calls[0].strip()

                    try:
                        tool_call = json.loads(tool_call)
                        tool_name = tool_call['name']
                        tool_args = tool_call['arguments']

                        if isinstance(tool_args, str):
                            tool_args = json.loads(tool_args)

                        print("========================================================")
                        print(f"Call tool {tool_name}, args: {tool_args}")

                        step = {
                            'turn': turn + 1,
                            'action': 'tool_call',
                            'tool_name': tool_name,
                            'tool_args': tool_args,
                            'timestamp': time.time(),
                            'token_count': count_tokens(record, tokenizer),
                        }

                        t_start = time.time()
                        result = await call_tool(sem, tool_name, tool_args, client, lock)
                        step['duration'] = round(time.time() - t_start, 2)

                        if result is None:
                            observation = f"Error: Tool {tool_name} returned None."
                            cur_summary_record = None
                        elif isinstance(result, tuple):
                            observation, cur_summary_record = result
                        elif isinstance(result, str):
                            observation = result
                            cur_summary_record = None
                        else:
                            observation = f"Error: Tool {tool_name} returned unexpected type: {type(result)}"
                            cur_summary_record = None

                        step['observation_length'] = len(observation)
                        step['observation_snippet'] = observation[:200]
                        step['success'] = 'Error' not in observation[:100]

                        if tool_name == 'search':
                            queries = tool_args.get('query', [])
                            if isinstance(queries, str):
                                queries = [queries]
                            step['queries'] = queries
                            step['num_queries'] = len(queries)
                        elif tool_name == 'visit':
                            url = tool_args.get('url', '')
                            step['url'] = url
                            step['goal'] = tool_args.get('goal', '')
                            if url:
                                visited_urls.append(url)
                            if is_nav_task and gt_urls and url_matches(visited_urls, gt_urls):
                                prediction = url
                                termination = 'answer'
                                trajectory.append(step)
                                trajectory.append({
                                    'turn': turn + 1,
                                    'action': 'url_matched',
                                    'matched_url': url,
                                    'gt_urls': gt_urls,
                                    'timestamp': time.time(),
                                })
                                break
                        elif tool_name == 'click':
                            step['ref'] = tool_args.get('ref', '')
                            step['goal'] = tool_args.get('goal', '')
                        elif tool_name == 'fill':
                            step['ref'] = tool_args.get('ref', '')
                            text = tool_args.get('text', '')
                            step['text'] = text[:50] + '...' if len(text) > 50 else text

                        trajectory.append(step)

                        if cur_summary_record:
                            summary_record.extend(cur_summary_record)

                        print(f"Tool call {tool_name} success, length {len(observation)}")
                        print(observation)

                    except Exception as e:
                        observation = f'Error: {str(e)}'
                        print(f"Tool call error {str(e)}")
                        traceback.print_exc()

                        step['success'] = False
                        step['error'] = str(e)
                        step['duration'] = round(time.time() - step['timestamp'], 2)
                        trajectory.append(step)

                    tool_response = f"<tool_response>\n{observation}\n</tool_response>"

                    if "server-side error" in observation:
                        return {
                            'task_id': task_id,
                            'task': task,
                            'task_type': 'url_navigation' if is_nav_task else 'info_extraction',
                            'start_url': start_url,
                            'gt_urls': gt_urls,
                            'visited_urls': visited_urls,
                            'prediction': prediction,
                            'eval_reasoning': eval_reasoning,
                            'messages': record,
                            'summary_record': summary_record,
                            'trajectory': trajectory,
                            'termination': 'server_side_error'
                        }

                    record.append({"role": "user", "content": tool_response,
                                   "tool_name": tool_name, "tool_args": tool_args,
                                   "function_result": observation})

                else:
                    if "<answer>" in response and "</answer>" in response:
                        prediction = response.split('<answer>')[-1].split('</answer>')[0].strip()

                        if is_nav_task:
                            if gt_urls and url_matches([prediction], gt_urls):
                                termination = 'answer'
                            elif gt_info:
                                is_correct, eval_reasoning = evaluate_gt_info(visited_urls, gt_info)
                                termination = 'answer' if is_correct else 'answer_incorrect'
                            else:
                                termination = 'answer'
                        else:
                            # Pass ground-truth answer when the dataset provides one
                            # (browsecomp has `answer`; agent-eval without it lets the
                            # judge LLM hallucinate matches).
                            ground_truth = data.get('answer') or data.get('ground_truth') or None
                            print(f"[eval] Running semantic evaluation for task: {task_id} "
                                  f"(gt={'yes' if ground_truth else 'no'})")
                            is_correct, eval_reasoning = await evaluate_answer_with_llm(
                                sem, task, prediction, ground_truth=ground_truth
                            )
                            termination = 'answer' if is_correct else 'answer_incorrect'
                            print(f"[eval] Result: {termination}, reasoning: {eval_reasoning}")

                        trajectory.append({
                            'turn': turn + 1,
                            'action': 'final_answer',
                            'prediction': prediction,
                            'task_type': 'url_navigation' if is_nav_task else 'info_extraction',
                            'eval_result': termination,
                            'eval_reasoning': eval_reasoning,
                            'timestamp': time.time(),
                            'token_count': count_tokens(record, tokenizer),
                        })
                    else:
                        termination = 'llm_response_error'

                    break

            trajectory.append({
                'turn': turn + 1,
                'action': 'termination',
                'reason': termination,
                'timestamp': time.time(),
                'token_count': count_tokens(record, tokenizer),
            })

    return {
        'task_id': task_id,
        'task': task,
        'task_type': 'url_navigation' if is_nav_task else 'info_extraction',
        'start_url': start_url,
        'gt_urls': gt_urls,
        'visited_urls': visited_urls,
        'prediction': prediction,
        'eval_reasoning': eval_reasoning,
        'messages': record,
        'summary_record': summary_record,
        'trajectory': trajectory,
        'termination': termination
    }


async def main(sem, rollout_count, input_path, output_path):
    global tokenizer
    dataset = read_jsonl(input_path)

    # Sibling paths inside the same per-benchmark directory:
    #   results/<benchmark_name>/{success,failure,trajectory}.jsonl
    out_dir = os.path.dirname(output_path) or "."
    os.makedirs(out_dir, exist_ok=True)
    success_path    = os.path.join(out_dir, "success.jsonl")
    failure_path    = os.path.join(out_dir, "failure.jsonl")
    trajectory_path = os.path.join(out_dir, "trajectory.jsonl")

    visited_counter = Counter()
    if os.path.exists(success_path):
        for visited_data in read_jsonl(success_path):
            visited_counter[visited_data['task_id']] += 1

    # Collect task input tuples first (don't instantiate coroutines yet — those need
    # the shared client/lock which only exists inside the mcp_client context below).
    task_inputs = []
    pending_counter = Counter()
    for data in dataset:
        task_id = data.get('task_id', data.get('id'))
        total_count = visited_counter[task_id] + pending_counter[task_id]
        need_to_submit = rollout_count - total_count if rollout_count - total_count > 0 else 0
        for _ in range(need_to_submit):
            task, start_url, gt_urls = parse_task_item(data)
            is_nav = is_url_navigation_task(data)
            instruction = ANSWER_INSTRUCTION_URL if is_nav else ANSWER_INSTRUCTION_INFO
            system_prompt = SYSTEM_PROMPT_NAVI.format(answer_instruction=instruction)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task}
            ]
            task_inputs.append((data, messages))
            pending_counter[task_id] += 1

    print(f"Total number of tasks: {len(task_inputs)}")

    # ONE shared mcp_client for the whole run (see agentic_loop comment).
    async with mcp_client(server_url=BROWSER_SERVER_URL) as (client, lock):
      with open(success_path, "a") as f_success, \
         open(failure_path, "a") as f_failure, \
         open(trajectory_path, "a") as f_traj:

        for (data, messages) in tqdm(task_inputs, total=len(task_inputs), desc="Navi-Bench Rollout ..."):
            try:
                result = await agentic_loop(sem, data, messages, client, lock)

                trajectory_record = {
                    'task_id': result['task_id'],
                    'task': result['task'],
                    'task_type': result.get('task_type'),
                    'termination': result['termination'],
                    'visited_urls': result['visited_urls'],
                    'gt_urls': result['gt_urls'],
                    'prediction': result.get('prediction'),
                    'eval_reasoning': result.get('eval_reasoning'),
                    'trajectory': result.pop('trajectory', [])
                }
                f_traj.write(json.dumps(trajectory_record, ensure_ascii=False) + "\n")
                f_traj.flush()

                if result['termination'] == 'answer':
                    f_success.write(json.dumps(result, ensure_ascii=False) + "\n")
                    f_success.flush()
                    os.fsync(f_success.fileno())
                else:
                    f_failure.write(json.dumps(result, ensure_ascii=False) + "\n")
                    f_failure.flush()
                    os.fsync(f_failure.fileno())

            except Exception as e:
                exception_type = type(e).__name__
                exception_message = str(e)
                traceback_info = ''.join(traceback.format_tb(e.__traceback__))
                print(f"[ERROR]: {exception_type}: {exception_message}\nTraceback:\n{traceback_info}")


if __name__ == '__main__':
    BROWSER_SERVER_URL = "http://localhost:3006/sse"

    GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "msagentrt")
    AGENT_LLM_BASE_URL = f"https://aiplatform.googleapis.com/v1beta1/projects/{GCP_PROJECT_ID}/locations/global/endpoints/openapi"
    tokenizer = tiktoken.get_encoding("cl100k_base")

    # ========================================
    rollout_count = 1
    MAX_AGENT_TURN = 100
    MAX_AGENT_LEN = 128 * 1024
    MAX_SINGLE_GEN_TOKENS = 8192
    MAX_SUMMARY_SHARD_LEN = 64 * 1024
    benchmark_name = "wiki_2hop_v3_scaled"
    MODEL_NAME = os.getenv("MODEL_NAME", "google/gemini-3.1-pro-preview")
    MAX_WORKERS = 1
    sem = {
        'session': asyncio.Semaphore(MAX_WORKERS),
        'llm': asyncio.Semaphore(MAX_WORKERS),
        'tool': asyncio.Semaphore(MAX_WORKERS),
    }
    # ========================================

    os.environ["AGENT_LLM_BASE_URL"] = AGENT_LLM_BASE_URL
    os.environ["MAX_SINGLE_GEN_TOKENS"] = str(MAX_SINGLE_GEN_TOKENS)
    os.environ["MAX_SUMMARY_SHARD_LEN"] = str(MAX_SUMMARY_SHARD_LEN)
    os.environ["MODEL_NAME"] = MODEL_NAME

    input_path = f"./data/{benchmark_name}.jsonl"
    # Each benchmark gets its own results subfolder so files don't accumulate flat:
    #   results/<benchmark_name>/{success,failure,trajectory}.jsonl
    # main() derives success_path / failure_path / trajectory_path from output_path
    # by replacing ".jsonl" with "_<kind>.jsonl", so we point output_path at
    # results/<benchmark_name>/results.jsonl which yields the right names.
    out_dir = f"./results/{benchmark_name}"
    os.makedirs(out_dir, exist_ok=True)
    output_path = f"{out_dir}/results.jsonl"

    search = Search()
    visit = Visit()
    click = Click()
    fill = Fill()

    TOOLS_SCHEMA = [search.tool_schema, visit.tool_schema, click.tool_schema, fill.tool_schema]

    asyncio.run(main(sem, rollout_count, input_path, output_path))