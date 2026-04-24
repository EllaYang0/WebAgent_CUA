import os
import json
import json5
import sys
import re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from prompts import *
from utils import count_tokens, call_llm


async def process_response(raw_response, goal, summary_model, tokenizer, sem):
    limit = int(os.getenv("MAX_SUMMARY_SHARD_LEN"))
    record = []
    raw_response_shard = []

    if count_tokens(raw_response, tokenizer) > limit:
        tokens = tokenizer.encode(raw_response)
        for i in range(0, len(tokens), limit):
            chunk_tokens = tokens[i:i+limit]
            chunk_text = tokenizer.decode(chunk_tokens)
            raw_response_shard.append(chunk_text)
    else:
        raw_response_shard.append(raw_response)

    evidence = ""
    summary = ""

    for i, raw_resp in enumerate(raw_response_shard):
        if i == 0:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT_SUMMARY_OURS},
                {"role": "user", "content": SUMMARY_PROMPT.format(raw_response=raw_resp, goal=goal)}
            ]
        else:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT_SUMMARY_OURS},
                {"role": "user", "content": SUMMARY_PROMPT_INCREMENTAL.format(raw_response=raw_resp, goal=goal, existing_evidence=evidence, existing_summary=summary)}
            ]

        response = await call_llm(sem, messages, int(os.getenv("MAX_SINGLE_GEN_TOKENS")), summary_model, mode="summary")
        messages.append({"role": "assistant", "content": response})
        record.append({"messages": messages})

        print(f"[tool_explore] raw response[:300]: {response[:300]}")

        # 尝试多种解析方式
        json_str = None

        # 1. 优先找 <useful_info> 标签
        if '<useful_info>' in response and '</useful_info>' in response:
            json_str = response.split('<useful_info>')[-1].split('</useful_info>')[0].strip()
        # 2. 去掉 </think> 之后再找
        elif '</think>' in response:
            after_think = response.split('</think>')[-1].strip()
            json_str = re.sub(r'^```(?:json)?\s*', '', after_think)
            json_str = re.sub(r'\s*```$', '', json_str).strip()
        # 3. 直接尝试解析整个 response
        else:
            json_str = re.sub(r'^```(?:json)?\s*', '', response.strip())
            json_str = re.sub(r'\s*```$', '', json_str).strip()

        print(f"[tool_explore] json_str[:200]: {json_str[:200] if json_str else 'None'}")

        try:
            processed_response_json = json5.loads(json_str)
        except Exception as e:
            print(f"[tool_explore] json5.loads failed: {e}")
            raise

        evidence = processed_response_json.get("evidence", "")
        summary = processed_response_json.get("summary", "")

    processed_response = "Evidence in page: \n" + str(evidence) + "\n\n" + "Summary: \n" + str(summary)
    processed_response = processed_response.strip()

    return processed_response, record